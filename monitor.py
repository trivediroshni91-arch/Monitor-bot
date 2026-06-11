import asyncio
import re
import random
import logging
import html as html_parser
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List
from io import BytesIO

logging.getLogger("discord").setLevel(logging.ERROR)

import aiohttp
import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

# ─── CONFIG ──────────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")

# 10 monitors × ~60s per check cycle = stagger by 6s each
# CHECK_EVERY = base interval between checks per monitor (seconds)
CHECK_EVERY      = 60    # check each account every 60s
STAGGER_DELAY    = 6     # seconds between starting each monitor (so 10 monitors spread over 60s)
REQUEST_TIMEOUT  = 20    # per-request timeout

# ─── PROXIES ─────────────────────────────────────────────────────────────────
_RAW_PROXIES = [
    "q29gnqhjguio:IUa7Y0BtjnrMhZKo@budget.circleproxy.com:1337",
] * 29  # same creds, rotating by index

PROXIES = [f"http://{p}" for p in _RAW_PROXIES]

import threading as _threading

class ProxyManager:
    def __init__(self, proxies):
        self._proxies = proxies
        self._idx     = 0
        self._lock    = _threading.Lock()

    def get(self) -> str:
        with self._lock:
            p = self._proxies[self._idx % len(self._proxies)]
            self._idx += 1
            return p

proxy_manager = ProxyManager(PROXIES)

JSON_URL = "https://www.instagram.com/{username}/?__a=1&__d=dis"
HTML_URL = "https://www.instagram.com/{username}/"
HEADERS  = {
    "User-Agent"      : "Mozilla/5.0 (Linux; Android 11; Mobile)",
    "Accept-Language" : "en-US,en;q=0.9",
    "X-IG-App-ID"     : "936619743392459",
}

import os as _os

def _find_font(bold=False):
    """Auto-detect font path - works on Termux + Ubuntu VPS"""
    candidates = []
    if bold:
        candidates = [
            "/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        ]
    else:
        candidates = [
            "/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        ]
    for p in candidates:
        if _os.path.exists(p):
            return p
    return None

FONT_BOLD_PATH = _find_font(bold=True)
FONT_REG_PATH  = _find_font(bold=False)

def _load_font(size, bold=False):
    path = FONT_BOLD_PATH if bold else FONT_REG_PATH
    try:
        if path:
            return ImageFont.truetype(path, size)
    except:
        pass
    return ImageFont.load_default()

# ─── SESSION ─────────────────────────────────────────────────────────────────
_session: Optional[aiohttp.ClientSession] = None

async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session

async def proxy_get(session: aiohttp.ClientSession, url: str, **kwargs):
    """Make a GET request through a rotating proxy."""
    proxy = proxy_manager.get()
    return session.get(url, proxy=proxy, **kwargs)

# ─── UTILITIES ───────────────────────────────────────────────────────────────
def extract_username(raw: str) -> str:
    raw = raw.strip().rstrip("/")
    if "instagram.com/" in raw:
        raw = raw.split("instagram.com/")[-1].split("?")[0].split("/")[0]
    return raw.lower().lstrip("@")

def fmt(n) -> str:
    try:
        n = int(n)
        if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
        if n >= 1_000:     return f"{n/1_000:.1f}K"
        return f"{n:,}"
    except: return str(n)

def parse_human_number(txt: str) -> Optional[int]:
    t = txt.lower().replace(",", "").strip()
    mult = 1
    if t.endswith("k"):   mult, t = 1_000,         t[:-1]
    elif t.endswith("m"): mult, t = 1_000_000,     t[:-1]
    elif t.endswith("b"): mult, t = 1_000_000_000, t[:-1]
    try:    return int(float(t) * mult)
    except: return None

# ─── STATS MANAGER ───────────────────────────────────────────────────────────
class StatsManager:
    def __init__(self):
        self.total_unbans = 0
        self.total_bans = 0
        self.unban_history: List[datetime] = []
        self.ban_history: List[datetime] = []

    def add_unban(self):
        self.total_unbans += 1
        self.unban_history.append(datetime.now(timezone.utc))

    def add_ban(self):
        self.total_bans += 1
        self.ban_history.append(datetime.now(timezone.utc))

    def count_last_days(self, days: int, history: List[datetime]) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return sum(1 for t in history if t > cutoff)

    def get_stats(self):
        return {
            "total_unbans": self.total_unbans,
            "total_bans": self.total_bans,
            "unban_7d": self.count_last_days(7, self.unban_history),
            "ban_7d": self.count_last_days(7, self.ban_history),
            "unban_30d": self.count_last_days(30, self.unban_history),
            "ban_30d": self.count_last_days(30, self.ban_history),
        }

stats = StatsManager()

# ─── INSTAGRAM CHECKER ───────────────────────────────────────────────────────
async def check_account(session: aiohttp.ClientSession, username: str) -> tuple[bool, Optional[int]]:
    try:
        async with await proxy_get(
            session,
            JSON_URL.format(username=username),
            headers=HEADERS, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        ) as r:
            if r.status == 404:
                return False, None
            if r.status < 400 and r.content_type == "application/json":
                data = await r.json()
                user = data.get("graphql", {}).get("user")
                if user and user.get("username"):
                    return True, user.get("edge_followed_by", {}).get("count")
    except Exception as e:
        print(f"⚠️ JSON check {username}: {e}")

    try:
        async with await proxy_get(
            session,
            HTML_URL.format(username=username),
            headers=HEADERS, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        ) as r:
            html = await r.text()
            if "Sorry, this page isn't available" in html:
                return False, None
            m = re.search(r'"edge_followed_by":\{"count":(\d+)', html)
            if m:
                return True, int(m.group(1))
            m2 = re.search(r'property="og:description" content="([^"]+)"', html)
            if m2:
                return True, parse_human_number(m2.group(1).split(" ", 1)[0])
    except Exception as e:
        print(f"⚠️ HTML check {username}: {e}")

    return False, None

# ─── PROFILE FETCHER ─────────────────────────────────────────────────────────
async def fetch_profile(session: aiohttp.ClientSession, username: str) -> dict:
    data = {
        "username"      : username,
        "followers_str" : "0",
        "following_str" : "0",
        "posts_str"     : "0",
        "pic_url"       : None,
        "bio"           : "",
        "is_verified"   : False,
    }
    try:
        async with await proxy_get(
            session,
            f"https://www.instagram.com/{username}/",
            headers=HEADERS, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        ) as r:
            raw = await r.text()

        m = re.search(r'property="og:description"\s+content="([^"]+)"', raw)
        if m:
            desc = html_parser.unescape(m.group(1))
            fm = re.search(r'([\d,\.]+[KMBkmb]?)\s+Followers', desc, re.IGNORECASE)
            fw = re.search(r'([\d,\.]+[KMBkmb]?)\s+Following', desc, re.IGNORECASE)
            fp = re.search(r'([\d,\.]+[KMBkmb]?)\s+Posts',     desc, re.IGNORECASE)
            if fm: data["followers_str"] = fm.group(1).strip()
            if fw: data["following_str"] = fw.group(1).strip()
            if fp: data["posts_str"]     = fp.group(1).strip()

        if data["followers_str"] == "0":
            mf = re.search(r'"edge_followed_by":\{"count":(\d+)', raw)
            if mf: data["followers_str"] = fmt(int(mf.group(1)))
        if data["following_str"] == "0":
            mw = re.search(r'"edge_follow":\{"count":(\d+)', raw)
            if mw: data["following_str"] = str(mw.group(1))
        if data["posts_str"] == "0":
            mp = re.search(r'"edge_owner_to_timeline_media":\{"count":(\d+)', raw)
            if mp: data["posts_str"] = str(mp.group(1))

        mi = re.search(r'property="og:image"\s+content="([^"]+)"', raw)
        if mi: data["pic_url"] = html_parser.unescape(mi.group(1))

        # Bio from og:title or meta description fallback
        mb = re.search(r'"biography":"([^"]*)"', raw)
        if mb:
            data["bio"] = html_parser.unescape(mb.group(1))[:80]
        else:
            mt = re.search(r'property="og:title"\s+content="([^"]+)"', raw)
            if mt:
                title = html_parser.unescape(mt.group(1))
                # og:title is usually "Name (@username) • Instagram"
                bio_part = title.split("•")[0].strip()
                if bio_part and bio_part.lower() != username.lower():
                    data["bio"] = bio_part[:80]

        # Verified badge
        if '"is_verified":true' in raw:
            data["is_verified"] = True

        print(f"✅ @{username}: {data['followers_str']} followers | verified={data['is_verified']} | pic={bool(data['pic_url'])}")
    except Exception as e:
        print(f"⚠️ fetch_profile: {e}")
    return data

# ─── UNBAN CARD ──────────────────────────────────────────────────────────────
async def generate_unban_card(
    session  : aiohttp.ClientSession,
    username : str,
    elapsed_h: int,
    elapsed_m: int,
    elapsed_s: int,
) -> Optional[BytesIO]:
    try:
        data = await fetch_profile(session, username)

        S = 4
        W = 1035 * S
        H = 495  * S

        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(img).rounded_rectangle([0, 0, W-1, H-1], radius=22*S, fill=(0, 0, 0, 255))
        draw = ImageDraw.Draw(img)

        f_name  = _load_font(28*S)
        f_statv = _load_font(26*S)
        f_statl = _load_font(26*S)
        f_btn   = _load_font(22*S, bold=True)
        f_dots  = _load_font(28*S, bold=True)
        f_bio   = _load_font(20*S)

        content_h = 44*S + 55*S + 26*S
        start_y   = (H - content_h) // 2

        pic_size = 220*S
        pic_x    = 35*S
        pic_y    = (H - pic_size) // 2

        # Profile pic placeholder
        draw.ellipse([pic_x, pic_y, pic_x+pic_size, pic_y+pic_size], fill=(25, 25, 25))
        cx = pic_x + pic_size // 2
        cy = pic_y + pic_size // 2
        draw.ellipse([cx-32*S, cy-44*S, cx+32*S, cy+10*S], fill=(80, 80, 80))
        draw.ellipse([cx-48*S, cy+8*S,  cx+48*S, cy+70*S], fill=(80, 80, 80))

        # Try to load profile pic directly (no proxy - CDN doesn't need it)
        if data.get("pic_url"):
            try:
                async with session.get(data["pic_url"], headers={
                    "User-Agent": HEADERS["User-Agent"],
                    "Accept"    : "image/webp,image/apng,image/*,*/*;q=0.8",
                    "Referer"   : "https://www.instagram.com/",
                }, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 200:
                        raw_pic = await r.read()
                        pic  = Image.open(BytesIO(raw_pic)).convert("RGBA").resize((pic_size, pic_size), Image.LANCZOS)
                        mask = Image.new("L", (pic_size, pic_size), 0)
                        ImageDraw.Draw(mask).ellipse([0, 0, pic_size, pic_size], fill=255)
                        bg   = Image.new("RGBA", (pic_size, pic_size), (0, 0, 0, 255))
                        bg.paste(pic, (0, 0), mask)
                        img.paste(bg, (pic_x, pic_y), mask)
                        print("✅ Profile pic loaded!")
                    else:
                        print(f"⚠️ Pic failed: {r.status}")
            except Exception as e:
                print(f"⚠️ Pic error: {e}")

        tx = pic_x + pic_size + 50*S
        ty = start_y

        # Username
        draw.text((tx, ty), username, fill=(255, 255, 255), font=f_name)
        uw     = int(draw.textlength(username, font=f_name))
        next_x = tx + uw + 10*S

        # Blue verified tick (if verified)
        if data.get("is_verified"):
            tick_r = 18*S
            tick_cx = next_x + tick_r
            tick_cy = ty + 22*S
            draw.ellipse([tick_cx-tick_r, tick_cy-tick_r, tick_cx+tick_r, tick_cy+tick_r], fill=(0, 149, 246))
            # draw checkmark
            pts = [
                (tick_cx - 10*S, tick_cy),
                (tick_cx - 3*S,  tick_cy + 8*S),
                (tick_cx + 11*S, tick_cy - 8*S),
            ]
            draw.line([pts[0], pts[1]], fill=(255,255,255), width=5*S)
            draw.line([pts[1], pts[2]], fill=(255,255,255), width=5*S)
            next_x += tick_r * 2 + 14*S
        else:
            next_x += 6*S

        # Follow button
        bw, bh = 150*S, 40*S
        draw.rounded_rectangle([next_x, ty+2*S, next_x+bw, ty+2*S+bh], radius=10*S, fill=(0, 149, 246))
        tw = int(draw.textlength("Follow", font=f_btn))
        draw.text((next_x+(bw-tw)//2, ty+10*S), "Follow", fill=(255, 255, 255), font=f_btn)
        draw.text((next_x+bw+14*S, ty+6*S), "•••", fill=(130, 130, 130), font=f_dots)

        # Stats row
        sy = ty + 55*S
        sx = tx
        for val, label in [
            (data["posts_str"],     " posts   "),
            (data["followers_str"], " followers   "),
            (data["following_str"], " following"),
        ]:
            vw = int(draw.textlength(val,   font=f_statv))
            lw = int(draw.textlength(label, font=f_statl))
            draw.text((sx, sy), val,   fill=(255, 255, 255), font=f_statv)
            draw.text((sx+vw, sy), label, fill=(150, 150, 150), font=f_statl)
            sx += vw + lw

        # Bio
        if data.get("bio"):
            draw.text((tx, sy + 40*S), data["bio"], fill=(200, 200, 200), font=f_bio)

        final = img.resize((1035, 495), Image.LANCZOS)
        buf   = BytesIO()
        final.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return buf

    except Exception as e:
        print(f"⚠️ generate_unban_card: {e}")
        return None

# ─── BAN CARD (same style but UserNotFound) ──────────────────────────────────
async def generate_ban_card(session: aiohttp.ClientSession, username: str) -> Optional[BytesIO]:
    try:
        S = 4
        W = 1035 * S
        H = 495  * S

        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(img).rounded_rectangle([0, 0, W-1, H-1], radius=22*S, fill=(0, 0, 0, 255))
        draw = ImageDraw.Draw(img)

        f_name  = _load_font(28*S)
        f_statv = _load_font(26*S)
        f_statl = _load_font(26*S)
        f_btn   = _load_font(22*S, bold=True)
        f_dots  = _load_font(28*S, bold=True)

        content_h = 44*S + 55*S + 26*S
        start_y   = (H - content_h) // 2

        pic_size = 220*S
        pic_x    = 35*S
        pic_y    = (H - pic_size) // 2

        # Gray circle placeholder
        draw.ellipse([pic_x, pic_y, pic_x+pic_size, pic_y+pic_size], fill=(200, 200, 200))
        cx = pic_x + pic_size // 2
        cy = pic_y + pic_size // 2
        draw.ellipse([cx-28*S, cy-42*S, cx+28*S, cy+8*S],  fill=(130, 130, 130))
        draw.ellipse([cx-46*S, cy+6*S,  cx+46*S, cy+70*S], fill=(130, 130, 130))

        # Red X over pic
        pad = 30*S
        draw.line([pic_x+pad, pic_y+pad, pic_x+pic_size-pad, pic_y+pic_size-pad], fill=(220, 30, 30), width=12*S)
        draw.line([pic_x+pic_size-pad, pic_y+pad, pic_x+pad, pic_y+pic_size-pad], fill=(220, 30, 30), width=12*S)

        tx = pic_x + pic_size + 50*S
        ty = start_y

        # "UserNotFound" in white
        draw.text((tx, ty), "UserNotFound", fill=(255, 255, 255), font=f_name)
        uw     = int(draw.textlength("UserNotFound", font=f_name))
        next_x = tx + uw + 16*S

        bw, bh = 150*S, 40*S
        draw.rounded_rectangle([next_x, ty+2*S, next_x+bw, ty+2*S+bh], radius=10*S, fill=(0, 149, 246))
        tw = int(draw.textlength("Follow", font=f_btn))
        draw.text((next_x+(bw-tw)//2, ty+10*S), "Follow", fill=(255, 255, 255), font=f_btn)
        draw.text((next_x+bw+14*S, ty+6*S), "•••", fill=(130, 130, 130), font=f_dots)

        # 0 posts 0 followers 0 following
        sy = ty + 55*S
        sx = tx
        for val, label in [("0", " posts   "), ("0", " followers   "), ("0", " following")]:
            vw = int(draw.textlength(val,   font=f_statv))
            lw = int(draw.textlength(label, font=f_statl))
            draw.text((sx, sy), val,   fill=(255, 255, 255), font=f_statv)
            draw.text((sx+vw, sy), label, fill=(150, 150, 150), font=f_statl)
            sx += vw + lw

        draw.text((tx, sy + 44*S), "UserNotFound", fill=(120, 120, 120), font=_load_font(20*S))

        final = img.resize((1035, 495), Image.LANCZOS)
        buf   = BytesIO()
        final.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return buf

    except Exception as e:
        print(f"⚠️ generate_ban_card: {e}")
        return None

# ─── BOT SETUP ───────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

active_unban_monitors: Dict[str, "UnbanMonitor"] = {}
active_ban_monitors: Dict[str, "BanMonitor"] = {}

# ─── UNBAN MONITOR ───────────────────────────────────────────────────────────
class UnbanMonitor:
    def __init__(self, username: str, channel_id: int, requester_id: int):
        self.username     = username.lower().lstrip("@")
        self.channel_id   = channel_id
        self.requester_id = requester_id
        self.started      = datetime.now(timezone.utc)
        self._done        = asyncio.Event()
        self.task         = None

    def elapsed(self) -> str:
        d = datetime.now(timezone.utc) - self.started
        h, rem = divmod(int(d.total_seconds()), 3600)
        m, s   = divmod(rem, 60)
        return f"{h} hours, {m} minutes, {s} seconds"

    async def _notify(self, followers: Optional[int]):
        channel    = bot.get_channel(self.channel_id) or await bot.fetch_channel(self.channel_id)
        d          = datetime.now(timezone.utc) - self.started
        total_secs = int(d.total_seconds())
        h          = total_secs // 3600
        m          = (total_secs % 3600) // 60
        s          = total_secs % 60

        session  = await get_session()
        # fetch_profile is called inside generate_unban_card, reuse result
        prof = await fetch_profile(session, self.username)
        followers_str = prof.get("followers_str", "N/A")
        following_str = prof.get("following_str", "N/A")
        posts_str     = prof.get("posts_str", "N/A")

        card_buf = await generate_unban_card(session, self.username, h, m, s)

        text = (
            f"[Account Recovered | @{self.username}](https://www.instagram.com/{self.username}/) ✅🏆\n"
            f"Followers: {followers_str} | Following: {following_str} | Posts: {posts_str}\n"
            f"⏱️ Time taken: {h}h {m}m {s}s"
        )
        embed = discord.Embed(description=text, colour=0x00ff00)
        if card_buf:
            embed.set_image(url=f"attachment://{self.username}_unban.png")
            await channel.send(embed=embed, file=discord.File(card_buf, filename=f"{self.username}_unban.png"))
        else:
            await channel.send(embed=embed)

        stats.add_unban()
        print(f"✅ Notified @{self.username}")
        self._done.set()

    async def start(self, session: aiohttp.ClientSession):
        print(f"🛰️ Monitoring unban @{self.username}")
        while not self._done.is_set():
            active, followers = await check_account(session, self.username)
            print(f"  [{self.username}] {'✔️ active' if active else '❌ banned'}")
            if active:
                await self._notify(followers)
                break
            await asyncio.sleep(CHECK_EVERY)

    def cancel(self):
        self._done.set()
        if self.task:
            self.task.cancel()
        print(f"🛑 Cancelled unban @{self.username}")

# ─── BAN MONITOR ─────────────────────────────────────────────────────────────
class BanMonitor:
    def __init__(self, username: str, channel_id: int, requester_id: int):
        self.username     = username.lower().lstrip("@")
        self.channel_id   = channel_id
        self.requester_id = requester_id
        self.started      = datetime.now(timezone.utc)
        self._done        = asyncio.Event()
        self.task         = None

    def elapsed(self) -> str:
        d = datetime.now(timezone.utc) - self.started
        h, rem = divmod(int(d.total_seconds()), 3600)
        m, s   = divmod(rem, 60)
        return f"{h} hours, {m} minutes, {s} seconds"

    async def _notify(self):
        channel    = bot.get_channel(self.channel_id) or await bot.fetch_channel(self.channel_id)
        d          = datetime.now(timezone.utc) - self.started
        total_secs = int(d.total_seconds())
        h          = total_secs // 3600
        m          = (total_secs % 3600) // 60
        s          = total_secs % 60

        session  = await get_session()
        card_buf = await generate_ban_card(session, self.username)

        text = (
            f"[@{self.username} is now banned](https://www.instagram.com/{self.username}/)\n"
            f"⏱️ Time taken : {h} hour, {m} minutes, {s} second"
        )
        embed = discord.Embed(description=text, colour=0xff0000)
        if card_buf:
            embed.set_image(url=f"attachment://{self.username}_ban.png")
            await channel.send(embed=embed, file=discord.File(card_buf, filename=f"{self.username}_ban.png"))
        else:
            await channel.send(embed=embed)

        stats.add_ban()
        print(f"💀 Ban notified @{self.username}")
        self._done.set()

    async def start(self, session: aiohttp.ClientSession):
        print(f"👁️ Ban monitoring @{self.username}")
        while not self._done.is_set():
            active, _ = await check_account(session, self.username)
            print(f"  [ban/{self.username}] {'✔️ active' if active else '💀 banned'}")
            if not active:
                await self._notify()
                break
            await asyncio.sleep(CHECK_EVERY)

    def cancel(self):
        self._done.set()
        if self.task:
            self.task.cancel()
        print(f"🛑 Cancelled ban @{self.username}")

# ─── COMMANDS ────────────────────────────────────────────────────────────────

@bot.command(name="unban")
async def unban_cmd(ctx, *args):
    if not args:
        await ctx.send("❌ Usage: `!unban <username/link> [user2] [user3]...`")
        return

    session   = await get_session()
    usernames = [extract_username(a) for a in args if extract_username(a)]
    started = []

    for i, uname in enumerate(usernames):
        if uname in active_unban_monitors and not active_unban_monitors[uname]._done.is_set():
            continue
        if uname in active_ban_monitors:
            active_ban_monitors[uname].cancel()
            del active_ban_monitors[uname]
        monitor = UnbanMonitor(uname, ctx.channel.id, ctx.author.id)
        active_unban_monitors[uname] = monitor
        # stagger start so 10 monitors don't all hit IG at the same second
        delay = i * STAGGER_DELAY
        async def _start_unban(m=monitor, d=delay):
            if d: await asyncio.sleep(d)
            await m.start(session)
        monitor.task = bot.loop.create_task(_start_unban())
        started.append(uname)

    if started:
        await ctx.send(f"🛰️ Unban Monitoring Started for: **{', '.join('@'+u for u in started)}**")

@bot.command(name="ban")
async def ban_cmd(ctx, *args):
    if not args:
        await ctx.send("❌ Usage: `!ban <username/link> [user2] [user3]...`")
        return

    session   = await get_session()
    usernames = [extract_username(a) for a in args if extract_username(a)]
    started = []

    for i, uname in enumerate(usernames):
        if uname in active_ban_monitors and not active_ban_monitors[uname]._done.is_set():
            continue
        if uname in active_unban_monitors:
            active_unban_monitors[uname].cancel()
            del active_unban_monitors[uname]
        monitor = BanMonitor(uname, ctx.channel.id, ctx.author.id)
        active_ban_monitors[uname] = monitor
        delay = i * STAGGER_DELAY
        async def _start_ban(m=monitor, d=delay):
            if d: await asyncio.sleep(d)
            await m.start(session)
        monitor.task = bot.loop.create_task(_start_ban())
        started.append(uname)

    if started:
        await ctx.send(f"👁️ Ban Monitoring Started for: **{', '.join('@'+u for u in started)}**")

@bot.command(name="fakeunban")
async def fakeunban_cmd(ctx, username: str):
    uname = extract_username(username)
    if not uname:
        await ctx.send("❌ Invalid username.")
        return

    fake_h = random.randint(0, 23)
    fake_m = random.randint(0, 59)
    fake_s = random.randint(0, 59)

    session = await get_session()
    profile = await fetch_profile(session, uname)
    followers_str = profile.get("followers_str", "N/A")
    following_str = profile.get("following_str", "N/A")
    posts_str     = profile.get("posts_str", "N/A")
    
    text = f"[Account Recovered | @{uname}](https://www.instagram.com/{uname}/) ✅🏆\nFollowers: {followers_str} | Following: {following_str} | Posts: {posts_str}\n⏱️ Time taken: {fake_h}h {fake_m}m {fake_s}s"
    embed = discord.Embed(description=text, colour=0x00ff00)
    
    card_buf = await generate_unban_card(session, uname, fake_h, fake_m, fake_s)
    if card_buf:
        embed.set_image(url=f"attachment://{uname}_fakeunban.png")
        await ctx.send(embed=embed, file=discord.File(card_buf, filename=f"{uname}_fakeunban.png"))
    else:
        await ctx.send(embed=embed)

@bot.command(name="fakeban")
async def fakeban_cmd(ctx, username: str):
    uname = extract_username(username)
    if not uname:
        await ctx.send("❌ Invalid username.")
        return

    fake_h = random.randint(0, 23)
    fake_m = random.randint(0, 59)
    fake_s = random.randint(0, 59)

    text = f"[@{uname} is now banned](https://www.instagram.com/{uname}/)\n⏱️ Time taken : {fake_h} hour, {fake_m} minutes, {fake_s} second"
    embed = discord.Embed(description=text, colour=0xff0000)
    
    session = await get_session()
    card_buf = await generate_ban_card(session, uname)
    if card_buf:
        embed.set_image(url=f"attachment://{uname}_fakeban.png")
        await ctx.send(embed=embed, file=discord.File(card_buf, filename=f"{uname}_fakeban.png"))
    else:
        await ctx.send(embed=embed)

@bot.command(name="listunban")
async def listunban_cmd(ctx):
    active = [f"• @{u} — {m.elapsed()}" for u, m in active_unban_monitors.items() if not m._done.is_set()]
    if active:
        await ctx.send("**📡 Unban Monitors:**\n" + "\n".join(active))
    else:
        await ctx.send("ℹ️ No unban monitors running.")

@bot.command(name="listban")
async def listban_cmd(ctx):
    active = [f"• @{u} — {m.elapsed()}" for u, m in active_ban_monitors.items() if not m._done.is_set()]
    if active:
        await ctx.send("**📡 Ban Monitors:**\n" + "\n".join(active))
    else:
        await ctx.send("ℹ️ No ban monitors running.")

@bot.command(name="clear")
async def clear_cmd(ctx, username: str = None):
    if not username:
        for m in active_unban_monitors.values(): m.cancel()
        for m in active_ban_monitors.values(): m.cancel()
        active_unban_monitors.clear()
        active_ban_monitors.clear()
        await ctx.send("🛑 Cleared all monitors.")
        return
    
    uname = extract_username(username)
    removed = False
    if uname in active_unban_monitors:
        active_unban_monitors[uname].cancel()
        del active_unban_monitors[uname]
        removed = True
    if uname in active_ban_monitors:
        active_ban_monitors[uname].cancel()
        del active_ban_monitors[uname]
        removed = True
    
    if removed:
        await ctx.send(f"✅ Removed `@{uname}` from all monitors.")
    else:
        await ctx.send(f"❌ `@{uname}` not found in any monitor.")

@bot.command(name="stats")
async def stats_cmd(ctx):
    s = stats.get_stats()
    embed = discord.Embed(title="📊 Instagram Monitor Stats", colour=0x1e1e2f)
    embed.add_field(name="Total Unbans", value=s["total_unbans"], inline=True)
    embed.add_field(name="Total Bans", value=s["total_bans"], inline=True)
    embed.add_field(name="Last 7d Unbans", value=s["unban_7d"], inline=True)
    embed.add_field(name="Last 7d Bans", value=s["ban_7d"], inline=True)
    embed.add_field(name="Last 30d Unbans", value=s["unban_30d"], inline=True)
    embed.add_field(name="Last 30d Bans", value=s["ban_30d"], inline=True)
    await ctx.send(embed=embed)

@bot.command(name="ping")
async def ping_cmd(ctx):
    await ctx.send(f"🏓 Pong! `{round(bot.latency * 1000)}ms`")

@bot.command(name="cmds")
async def cmds_cmd(ctx):
    await ctx.send(
        "**📋 Commands:**\n"
        "`!unban <username>` — Monitor unban (with card)\n"
        "`!ban <username>` — Monitor ban (with card)\n"
        "`!fakeunban <username>` — Fake unban card\n"
        "`!fakeban <username>` — Fake ban card\n"
        "`!listunban` — Show unban monitors\n"
        "`!listban` — Show ban monitors\n"
        "`!clear <username>` — Remove monitor\n"
        "`!clear` — Clear all\n"
        "`!stats` — Total bans/unbans stats\n"
        "`!ping` — Check latency\n"
        "`!cmds` — This help"
    )

@bot.event
async def on_ready():
    print(f"✅ Bot ready: {bot.user} (ID: {bot.user.id})")
    print("Commands: !unban, !ban, !fakeunban, !fakeban, !listunban, !listban, !clear, !stats, !ping, !cmds")

if __name__ == "__main__":
    try:
        bot.run(DISCORD_TOKEN)
    finally:
        if _session and not _session.closed:
            asyncio.run(_session.close())
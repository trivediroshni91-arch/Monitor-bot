import os
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


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHECK_EVERY = 500

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 11; Mobile)",
    "Accept-Language": "en-US,en;q=0.9",
}

FONT_BOLD = "/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"
FONT_REG = "/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans.ttf"

_session = None

async def get_session():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session

def extract_username(raw: str) -> str:
    raw = raw.strip().rstrip("/")
    if "instagram.com/" in raw:
        raw = raw.split("instagram.com/")[-1].split("?")[0].split("/")[0]
    return raw.lower().lstrip("@")

def fmt(n):
    try:
        n = int(n)
        if n >= 1000000:
            return f"{n/1000000:.1f}M"
        if n >= 1000:
            return f"{n/1000:.1f}K"
        return f"{n:,}"
    except:
        return str(n)

def parse_human_number(txt):
    t = txt.lower().replace(",", "").strip()
    mult = 1
    if t.endswith("k"):
        mult, t = 1000, t[:-1]
    elif t.endswith("m"):
        mult, t = 1000000, t[:-1]
    elif t.endswith("b"):
        mult, t = 1000000000, t[:-1]
    try:
        return int(float(t) * mult)
    except:
        return None

class StatsManager:
    def __init__(self):
        self.total_unbans = 0
        self.total_bans = 0
        self.unban_history = []
        self.ban_history = []

    def add_unban(self):
        self.total_unbans += 1
        self.unban_history.append(datetime.now(timezone.utc))

    def add_ban(self):
        self.total_bans += 1
        self.ban_history.append(datetime.now(timezone.utc))

    def count_last_days(self, days, history):
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

async def check_account(session, username):
    try:
        async with session.get(f"https://www.instagram.com/{username}/?__a=1&__d=dis", headers=HEADERS, timeout=60) as r:
            if r.status == 404:
                return False, None
            if r.status < 400:
                data = await r.json()
                user = data.get("graphql", {}).get("user")
                if user and user.get("username"):
                    return True, user.get("edge_followed_by", {}).get("count")
    except:
        pass
    try:
        async with session.get(f"https://www.instagram.com/{username}/", headers=HEADERS, timeout=60) as r:
            html = await r.text()
            if "Sorry, this page isn't available" in html:
                return False, None
            m = re.search(r'"edge_followed_by":\{"count":(\d+)', html)
            if m:
                return True, int(m.group(1))
    except:
        pass
    return False, None

async def fetch_profile(session, username):
    data = {"username": username, "followers_str": "0", "following_str": "0", "posts_str": "0", "pic_url": None}
    try:
        async with session.get(f"https://www.instagram.com/{username}/", headers=HEADERS, timeout=60) as r:
            raw = await r.text()
        m = re.search(r'property="og:description" content="([^"]+)"', raw)
        if m:
            desc = html_parser.unescape(m.group(1))
            fm = re.search(r'([\d,\.]+[KMBkmb]?)\s+Followers', desc, re.IGNORECASE)
            fw = re.search(r'([\d,\.]+[KMBkmb]?)\s+Following', desc, re.IGNORECASE)
            fp = re.search(r'([\d,\.]+[KMBkmb]?)\s+Posts', desc, re.IGNORECASE)
            if fm:
                data["followers_str"] = fm.group(1).strip()
            if fw:
                data["following_str"] = fw.group(1).strip()
            if fp:
                data["posts_str"] = fp.group(1).strip()
        if data["followers_str"] == "0":
            mf = re.search(r'"edge_followed_by":\{"count":(\d+)', raw)
            if mf:
                data["followers_str"] = fmt(int(mf.group(1)))
        if data["following_str"] == "0":
            mw = re.search(r'"edge_follow":\{"count":(\d+)', raw)
            if mw:
                data["following_str"] = str(mw.group(1))
        if data["posts_str"] == "0":
            mp = re.search(r'"edge_owner_to_timeline_media":\{"count":(\d+)', raw)
            if mp:
                data["posts_str"] = str(mp.group(1))
        mi = re.search(r'property="og:image" content="([^"]+)"', raw)
        if mi:
            data["pic_url"] = html_parser.unescape(mi.group(1))
    except Exception as e:
        print(f"Error: {e}")
    return data

async def generate_unban_card(session, username, h, m, s):
    try:
        data = await fetch_profile(session, username)
        S = 4
        W, H = 1035 * S, 495 * S
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(img).rounded_rectangle([0, 0, W-1, H-1], radius=22*S, fill=(0, 0, 0, 255))
        draw = ImageDraw.Draw(img)
        try:
            f_name = ImageFont.truetype(FONT_REG, 28*S)
            f_statv = ImageFont.truetype(FONT_REG, 26*S)
            f_statl = ImageFont.truetype(FONT_REG, 26*S)
            f_btn = ImageFont.truetype(FONT_BOLD, 22*S)
        except:
            f_name = f_statv = f_statl = f_btn = ImageFont.load_default()
        start_y = (H - (44*S + 55*S + 26*S)) // 2
        pic_size = 220*S
        pic_x, pic_y = 35*S, (H - pic_size) // 2
        draw.ellipse([pic_x, pic_y, pic_x+pic_size, pic_y+pic_size], fill=(25, 25, 25))
        cx, cy = pic_x + pic_size//2, pic_y + pic_size//2
        draw.ellipse([cx-32*S, cy-44*S, cx+32*S, cy+10*S], fill=(80, 80, 80))
        draw.ellipse([cx-48*S, cy+8*S, cx+48*S, cy+70*S], fill=(80, 80, 80))
        if data.get("pic_url"):
            try:
                async with session.get(data["pic_url"], headers=HEADERS, timeout=60) as r:
                    if r.status == 200:
                        pic_data = await r.read()
                        pic = Image.open(BytesIO(pic_data)).convert("RGBA").resize((pic_size, pic_size))
                        mask = Image.new("L", (pic_size, pic_size), 0)
                        ImageDraw.Draw(mask).ellipse([0, 0, pic_size, pic_size], fill=255)
                        bg = Image.new("RGBA", (pic_size, pic_size), (0, 0, 0, 255))
                        bg.paste(pic, (0, 0), mask)
                        img.paste(bg, (pic_x, pic_y), mask)
            except:
                pass
        tx, ty = pic_x + pic_size + 50*S, start_y
        draw.text((tx, ty), username, fill=(255,255,255), font=f_name)
        uw = int(draw.textlength(username, font=f_name))
        next_x = tx + uw + 16*S
        bw, bh = 150*S, 40*S
        draw.rounded_rectangle([next_x, ty+2*S, next_x+bw, ty+2*S+bh], radius=10*S, fill=(0,149,246))
        tw = int(draw.textlength("Follow", font=f_btn))
        draw.text((next_x+(bw-tw)//2, ty+10*S), "Follow", fill=(255,255,255), font=f_btn)
        sy, sx = ty + 55*S, tx
        for val, label in [(data["posts_str"], " posts   "), (data["followers_str"], " followers   "), (data["following_str"], " following")]:
            vw = int(draw.textlength(val, font=f_statv))
            lw = int(draw.textlength(label, font=f_statl))
            draw.text((sx, sy), val, fill=(255,255,255), font=f_statv)
            draw.text((sx+vw, sy), label, fill=(150,150,150), font=f_statl)
            sx += vw + lw
        final = img.resize((1035, 495), Image.LANCZOS)
        buf = BytesIO()
        final.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return buf
    except Exception as e:
        print(f"Card error: {e}")
        return None

async def generate_ban_card(session, username):
    try:
        S = 4
        W, H = 1035 * S, 495 * S
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(img).rounded_rectangle([0, 0, W-1, H-1], radius=22*S, fill=(0, 0, 0, 255))
        draw = ImageDraw.Draw(img)
        try:
            f_name = ImageFont.truetype(FONT_REG, 28*S)
            f_statv = ImageFont.truetype(FONT_REG, 26*S)
            f_statl = ImageFont.truetype(FONT_REG, 26*S)
            f_btn = ImageFont.truetype(FONT_BOLD, 22*S)
        except:
            f_name = f_statv = f_statl = f_btn = ImageFont.load_default()
        start_y = (H - (44*S + 55*S + 26*S)) // 2
        pic_size = 220*S
        pic_x, pic_y = 35*S, (H - pic_size) // 2
        draw.ellipse([pic_x, pic_y, pic_x+pic_size, pic_y+pic_size], fill=(200,200,200))
        cx, cy = pic_x + pic_size//2, pic_y + pic_size//2
        draw.ellipse([cx-28*S, cy-42*S, cx+28*S, cy+8*S], fill=(130,130,130))
        draw.ellipse([cx-46*S, cy+6*S, cx+46*S, cy+70*S], fill=(130,130,130))
        pad = 30*S
        draw.line([pic_x+pad, pic_y+pad, pic_x+pic_size-pad, pic_y+pic_size-pad], fill=(220,30,30), width=12*S)
        draw.line([pic_x+pic_size-pad, pic_y+pad, pic_x+pad, pic_y+pic_size-pad], fill=(220,30,30), width=12*S)
        tx, ty = pic_x + pic_size + 50*S, start_y
        draw.text((tx, ty), "UserNotFound", fill=(255,255,255), font=f_name)
        uw = int(draw.textlength("UserNotFound", font=f_name))
        next_x = tx + uw + 16*S
        bw, bh = 150*S, 40*S
        draw.rounded_rectangle([next_x, ty+2*S, next_x+bw, ty+2*S+bh], radius=10*S, fill=(0,149,246))
        tw = int(draw.textlength("Follow", font=f_btn))
        draw.text((next_x+(bw-tw)//2, ty+10*S), "Follow", fill=(255,255,255), font=f_btn)
        sy, sx = ty + 55*S, tx
        for val, label in [("0", " posts   "), ("0", " followers   "), ("0", " following")]:
            vw = int(draw.textlength(val, font=f_statv))
            lw = int(draw.textlength(label, font=f_statl))
            draw.text((sx, sy), val, fill=(255,255,255), font=f_statv)
            draw.text((sx+vw, sy), label, fill=(150,150,150), font=f_statl)
            sx += vw + lw
        try:
            f_bio = ImageFont.truetype(FONT_REG, 20*S)
        except:
            f_bio = ImageFont.load_default()
        draw.text((tx, sy + 44*S), "UserNotFound", fill=(120,120,120), font=f_bio)
        final = img.resize((1035, 495), Image.LANCZOS)
        buf = BytesIO()
        final.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return buf
    except Exception as e:
        print(f"Ban card error: {e}")
        return None

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

active_unban_monitors = {}
active_ban_monitors = {}

class UnbanMonitor:
    def __init__(self, username, channel_id, requester_id):
        self.username = username.lower().lstrip("@")
        self.channel_id = channel_id
        self.requester_id = requester_id
        self.started = datetime.now(timezone.utc)
        self._done = asyncio.Event()
        self.task = None

    def elapsed(self):
        d = datetime.now(timezone.utc) - self.started
        h, rem = divmod(int(d.total_seconds()), 3600)
        m, s = divmod(rem, 60)
        return f"{h} hours, {m} minutes, {s} seconds"

    async def _notify(self, followers):
        channel = bot.get_channel(self.channel_id) or await bot.fetch_channel(self.channel_id)
        d = datetime.now(timezone.utc) - self.started
        h = int(d.total_seconds()) // 3600
        m = (int(d.total_seconds()) % 3600) // 60
        s = int(d.total_seconds()) % 60
        followers_str = f"{followers:,}" if followers else "N/A"
        session = await get_session()
        profile = await fetch_profile(session, self.username)
        following_str = profile.get("following_str", "N/A")
        text = f"Account Recovered | @{self.username} 🏆✅\nFollowers: {followers_str} | Following: {following_str}\n⏱️ Time taken: {h} hours, {m} minutes, {s} seconds"
        embed = discord.Embed(description=text, colour=0x00ff00)
        card = await generate_unban_card(session, self.username, h, m, s)
        if card:
            embed.set_image(url=f"attachment://{self.username}_unban.png")
            await channel.send(embed=embed, file=discord.File(card, filename=f"{self.username}_unban.png"))
        else:
            await channel.send(embed=embed)
        stats.add_unban()
        self._done.set()

    async def start(self, session):
        while not self._done.is_set():
            active, followers = await check_account(session, self.username)
            if active:
                await self._notify(followers)
                break
            await asyncio.sleep(CHECK_EVERY)

    def cancel(self):
        self._done.set()
        if self.task:
            self.task.cancel()

class BanMonitor:
    def __init__(self, username, channel_id, requester_id):
        self.username = username.lower().lstrip("@")
        self.channel_id = channel_id
        self.requester_id = requester_id
        self.started = datetime.now(timezone.utc)
        self._done = asyncio.Event()
        self.task = None

    def elapsed(self):
        d = datetime.now(timezone.utc) - self.started
        h, rem = divmod(int(d.total_seconds()), 3600)
        m, s = divmod(rem, 60)
        return f"{h} hours, {m} minutes, {s} seconds"

    async def _notify(self):
        channel = bot.get_channel(self.channel_id) or await bot.fetch_channel(self.channel_id)
        d = datetime.now(timezone.utc) - self.started
        h = int(d.total_seconds()) // 3600
        m = (int(d.total_seconds()) % 3600) // 60
        s = int(d.total_seconds()) % 60
        text = f"@{self.username} is banned now 🚨\nTime taken : {h} hours, {m} minutes, {s} seconds"
        embed = discord.Embed(description=text, colour=0xff0000)
        session = await get_session()
        card = await generate_ban_card(session, self.username)
        if card:
            embed.set_image(url=f"attachment://{self.username}_ban.png")
            await channel.send(embed=embed, file=discord.File(card, filename=f"{self.username}_ban.png"))
        else:
            await channel.send(embed=embed)
        stats.add_ban()
        self._done.set()

    async def start(self, session):
        while not self._done.is_set():
            active, _ = await check_account(session, self.username)
            if not active:
                await self._notify()
                break
            await asyncio.sleep(CHECK_EVERY)

    def cancel(self):
        self._done.set()
        if self.task:
            self.task.cancel()

@bot.command(name="unban")
async def unban_cmd(ctx, *args):
    if not args:
        await ctx.send("❌ Usage: `!unban <username>`")
        return
    session = await get_session()
    usernames = [extract_username(a) for a in args if extract_username(a)]
    started = []
    for uname in usernames:
        if uname in active_unban_monitors and not active_unban_monitors[uname]._done.is_set():
            continue
        if uname in active_ban_monitors:
            active_ban_monitors[uname].cancel()
            del active_ban_monitors[uname]
        monitor = UnbanMonitor(uname, ctx.channel.id, ctx.author.id)
        active_unban_monitors[uname] = monitor
        monitor.task = bot.loop.create_task(monitor.start(session))
        started.append(uname)
    if started:
        await ctx.send(f"🛰️ Unban Monitoring Started for: **{', '.join('@'+u for u in started)}**")

@bot.command(name="ban")
async def ban_cmd(ctx, *args):
    if not args:
        await ctx.send("❌ Usage: `!ban <username>`")
        return
    session = await get_session()
    usernames = [extract_username(a) for a in args if extract_username(a)]
    started = []
    for uname in usernames:
        if uname in active_ban_monitors and not active_ban_monitors[uname]._done.is_set():
            continue
        if uname in active_unban_monitors:
            active_unban_monitors[uname].cancel()
            del active_unban_monitors[uname]
        monitor = BanMonitor(uname, ctx.channel.id, ctx.author.id)
        active_ban_monitors[uname] = monitor
        monitor.task = bot.loop.create_task(monitor.start(session))
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
    text = f"Account Recovered | @{uname} 🏆✅\nFollowers: {profile.get('followers_str', 'N/A')} | Following: {profile.get('following_str', 'N/A')}\n⏱️ Time taken: {fake_h} hours, {fake_m} minutes, {fake_s} seconds"
    embed = discord.Embed(description=text, colour=0x00ff00)
    card = await generate_unban_card(session, uname, fake_h, fake_m, fake_s)
    if card:
        embed.set_image(url=f"attachment://{uname}_fakeunban.png")
        await ctx.send(embed=embed, file=discord.File(card, filename=f"{uname}_fakeunban.png"))
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
    text = f"@{uname} is banned now 🚨\nTime taken : {fake_h} hours, {fake_m} minutes, {fake_s} seconds"
    embed = discord.Embed(description=text, colour=0xff0000)
    session = await get_session()
    card = await generate_ban_card(session, uname)
    if card:
        embed.set_image(url=f"attachment://{uname}_fakeban.png")
        await ctx.send(embed=embed, file=discord.File(card, filename=f"{uname}_fakeban.png"))
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
        for m in active_unban_monitors.values():
            m.cancel()
        for m in active_ban_monitors.values():
            m.cancel()
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

async def main():
    async with bot:
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
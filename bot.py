import os
import asyncio
import aiohttp
import logging
import re
import itertools
import time
from contextlib import suppress
from monitoring import InstagramChecker, check_due_accounts
from datetime import datetime
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

load_dotenv()

# --- CONFIGURATION ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
IG_SESSIONID = os.getenv("IG_SESSIONID", "").strip()

# Low-bandwidth defaults. Checks use one request, with no HTML fallback.
CHECK_INTERVAL_SECONDS = max(10, int(os.getenv("CHECK_INTERVAL_SECONDS", "60")))
MAX_BACKOFF_SECONDS = max(CHECK_INTERVAL_SECONDS, int(os.getenv("MAX_BACKOFF_SECONDS", "3600")))
MAX_CONCURRENT_CHECKS = max(1, int(os.getenv("MAX_CONCURRENT_CHECKS", "3")))
MAX_RESPONSE_BYTES = max(4096, int(os.getenv("MAX_RESPONSE_BYTES", "131072")))

def parse_proxy_entry(entry: str) -> str:
    entry = entry.strip()
    if not entry:
        return ""
    if entry.startswith("http://") or entry.startswith("https://") or entry.startswith("socks5://"):
        return entry
    parts = entry.split(":")
    if len(parts) == 4:
        # Format: ip:port:user:pass
        ip, port, user, pwd = parts
        return f"http://{user}:{pwd}@{ip}:{port}"
    elif len(parts) == 2:
        # Format: ip:port
        return f"http://{parts[0]}:{parts[1]}"
    return entry

def get_configured_proxies():
    env_str = os.getenv("PROXIES", "").strip()
    proxies = []
    if env_str:
        for chunk in re.split(r'[,;\n\r]+', env_str):
            parsed = parse_proxy_entry(chunk)
            if parsed:
                proxies.append(parsed)
    return proxies

DEFAULT_PROXIES = get_configured_proxies()
proxy_pool = itertools.cycle(DEFAULT_PROXIES)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BANNED_DP_PATH = os.path.join(BASE_DIR, "default_banned_dp.jpg")
FONT_PATH = os.path.join(BASE_DIR, "font.ttf")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Monitored accounts dictionary
# Key: username.lower(), Value: dict(username=raw, chat_id=id, start_time=datetime, mode='unban'|'ban')
monitored_accounts = {}


def get_next_proxy():
    return next(proxy_pool)


def get_card_fonts():
    """Load bundled TrueType font for cross-platform Linux/Mac rendering"""
    if os.path.exists(FONT_PATH):
        try:
            return {
                "username": ImageFont.truetype(FONT_PATH, 48),
                "btn": ImageFont.truetype(FONT_PATH, 26),
                "stats_num": ImageFont.truetype(FONT_PATH, 34),
                "stats_lbl": ImageFont.truetype(FONT_PATH, 30),
                "name": ImageFont.truetype(FONT_PATH, 32),
            }
        except Exception as e:
            logging.error(f"Error loading font.ttf: {e}")

    d = ImageFont.load_default()
    return {"username": d, "btn": d, "stats_num": d, "stats_lbl": d, "name": d}


async def create_profile_card(username, followers, posts, following, pic_url=None, is_banned=False):
    """Creates a 1000x550 Pure Black High-Res Profile Card with zero-overlap dynamic layout"""
    width, height = 1000, 550
    img = Image.new('RGB', (width, height), color='#000000')
    draw = ImageDraw.Draw(img)

    fonts = get_card_fonts()
    font_username = fonts["username"]
    font_btn = fonts["btn"]
    font_stats_num = fonts["stats_num"]
    font_stats_lbl = fonts["stats_lbl"]
    font_name = fonts["name"]

    av_center = (180, 275)
    av_r = 100
    av_size = (av_r * 2, av_r * 2)

    avatar_drawn = False

    # For BANNED account, use default_banned_dp.jpg
    if is_banned and os.path.exists(DEFAULT_BANNED_DP_PATH):
        try:
            dp_img = Image.open(DEFAULT_BANNED_DP_PATH).convert("RGB").resize(av_size)
            mask = Image.new('L', av_size, 0)
            ImageDraw.Draw(mask).ellipse((0, 0, av_size[0], av_size[1]), fill=255)
            img.paste(dp_img, (av_center[0]-av_r, av_center[1]-av_r), mask)
            avatar_drawn = True
        except Exception as e:
            logging.error(f"Error loading default banned DP: {e}")

    # If unbanned & has pic_url, download avatar
    if not avatar_drawn and pic_url:
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(pic_url) as resp:
                    if resp.status == 200:
                        avatar_bytes = await resp.read()
                        av_img = Image.open(BytesIO(avatar_bytes)).convert("RGB").resize(av_size)
                        mask = Image.new('L', av_size, 0)
                        ImageDraw.Draw(mask).ellipse((0, 0, av_size[0], av_size[1]), fill=255)
                        img.paste(av_img, (av_center[0]-av_r, av_center[1]-av_r), mask)
                        avatar_drawn = True
        except Exception as e:
            logging.error(f"Avatar download error: {e}")

    if not avatar_drawn:
        draw.ellipse([av_center[0]-av_r, av_center[1]-av_r, av_center[0]+av_r, av_center[1]+av_r], fill='#262626')
        first_char = username[0].upper() if username else 'U'
        draw.text((av_center[0]-15, av_center[1]-25), first_char, font=font_username, fill='#ffffff')

    display_username = username if not is_banned else "UserNotFound"
    uname_x = 320
    uname_y = 200
    draw.text((uname_x, uname_y), display_username, font=font_username, fill='#ffffff')

    try:
        uname_w = draw.textlength(display_username, font=font_username)
    except Exception:
        uname_w = len(display_username) * 28

    btn_left = int(uname_x + uname_w + 25)
    btn_top = 205
    btn_w = 130
    btn_h = 50
    draw.rounded_rectangle([btn_left, btn_top, btn_left+btn_w, btn_top+btn_h], radius=10, fill='#0095f6')
    draw.text((btn_left+26, btn_top+10), 'Follow', font=font_btn, fill='#ffffff')

    dot_x = btn_left + btn_w + 25
    dot_y = btn_top + 22
    for offset in [0, 14, 28]:
        draw.ellipse([dot_x+offset, dot_y, dot_x+offset+7, dot_y+7], fill='#ffffff')

    posts_str = str(posts)
    folls_str = str(followers)
    follg_str = str(following)
    st_y = 275
    st_lbl_y = 278

    draw.text((320, st_y), posts_str, font=font_stats_num, fill='#ffffff')
    pw = draw.textlength(posts_str, font=font_stats_num) if hasattr(draw, 'textlength') else len(posts_str)*20
    draw.text((320 + pw + 10, st_lbl_y), 'posts', font=font_stats_lbl, fill='#a8a8a8')
    pw_lbl = draw.textlength('posts', font=font_stats_lbl) if hasattr(draw, 'textlength') else 75

    foll_x = int(320 + pw + 10 + pw_lbl + 35)
    draw.text((foll_x, st_y), folls_str, font=font_stats_num, fill='#ffffff')
    fw = draw.textlength(folls_str, font=font_stats_num) if hasattr(draw, 'textlength') else len(folls_str)*20
    draw.text((foll_x + fw + 10, st_lbl_y), 'followers', font=font_stats_lbl, fill='#a8a8a8')
    fw_lbl = draw.textlength('followers', font=font_stats_lbl) if hasattr(draw, 'textlength') else 120

    follg_x = int(foll_x + fw + 10 + fw_lbl + 35)
    draw.text((follg_x, st_y), follg_str, font=font_stats_num, fill='#ffffff')
    fgw = draw.textlength(follg_str, font=font_stats_num) if hasattr(draw, 'textlength') else len(follg_str)*20
    draw.text((follg_x + fgw + 10, st_lbl_y), 'following', font=font_stats_lbl, fill='#a8a8a8')

    sub_title = username if not is_banned else "UserNotFound"
    draw.text((320, 330), sub_title, font=font_name, fill='#ffffff')

    bio = BytesIO()
    img.save(bio, 'PNG')
    bio.seek(0)
    return bio


async def trigger_alert(uname: str, raw_username: str, chat_id: int, mode: str, res: dict, start_time: datetime):
    """Processes detection result and sends high-res pure black alert card immediately."""
    record = monitored_accounts.get(uname)
    t = datetime.now() - start_time
    h, r = divmod(int(t.total_seconds()), 3600)
    m, s = divmod(r, 60)
    elapsed_str = f"{h} hours, {m} minutes, {s} seconds"

    # UNBAN ALERT TRIGGER
    if mode == "unban" and res.get("status") == "active":
        msg = (
            f"Account Recovered | <a href='https://instagram.com/{raw_username}'>@{raw_username}</a> 🏆✅\n"
            f"<i>Followers: {res['followers']} | Following: {res['following']}</i>\n"
            f"⏱️ <i>Time taken: {elapsed_str}</i>"
        )
        try:
            card_img = await create_profile_card(
                raw_username, res['followers'], res['posts'], res['following'],
                pic_url=res.get('pic_url'), is_banned=False
            )
            await bot.send_photo(
                chat_id,
                photo=types.BufferedInputFile(card_img.read(), filename="card.png"),
                caption=msg
            )
        except Exception as e:
            logging.error(f"Error sending photo alert: {e}")
            await bot.send_message(chat_id, text=msg, disable_web_page_preview=False)

        if monitored_accounts.get(uname) is record:
            monitored_accounts.pop(uname, None)
        return True

    # BAN ALERT TRIGGER
    elif mode == "ban" and res.get("status") == "banned":
        msg = (
            f"🚨 <b>Super-Fast Ban Alert!</b>\n\n"
            f"<a href='https://instagram.com/{raw_username}'>@{raw_username}</a> has been <b>BANNED/DISABLED</b>!\n"
            f"<i>Status: UserNotFound</i>\n"
            f"⏱️ <i>Time taken: {elapsed_str}</i>"
        )
        try:
            card_img = await create_profile_card(raw_username, 0, 0, 0, is_banned=True)
            await bot.send_photo(
                chat_id,
                photo=types.BufferedInputFile(card_img.read(), filename="card.png"),
                caption=msg
            )
        except Exception as e:
            logging.error(f"Error sending ban photo alert: {e}")
            await bot.send_message(chat_id, text=msg, disable_web_page_preview=False)

        if monitored_accounts.get(uname) is record:
            monitored_accounts.pop(uname, None)
        return True

    return False


async def monitor_loop():
    checker = InstagramChecker(get_next_proxy, IG_SESSIONID, MAX_RESPONSE_BYTES, MAX_BACKOFF_SECONDS)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_CHECKS)

    async def on_result(uname, data, result):
        await trigger_alert(uname, data["username"], data["chat_id"], data["mode"],
                            result, data["start_time"])

    last_log = time.monotonic()
    # Reuse connections; do not accumulate cookies across usernames/proxies.
    async with aiohttp.ClientSession(connector=connector, cookie_jar=aiohttp.DummyCookieJar(),
                                     read_bufsize=8192, trust_env=False) as session:
        while True:
            await check_due_accounts(monitored_accounts, session, checker, on_result,
                                     CHECK_INTERVAL_SECONDS, MAX_BACKOFF_SECONDS,
                                     MAX_CONCURRENT_CHECKS)
            if time.monotonic() - last_log >= 60:
                logging.info("Proxy checks since startup: requests=%d consumed_body_bytes=%d "
                             "capped_responses=%d accounts=%d cooldown_seconds=%.0f",
                             checker.requests, checker.body_bytes, checker.capped_responses,
                             len(monitored_accounts), max(0, checker.cooldown_until - time.monotonic()))
                last_log = time.monotonic()
            await asyncio.sleep(0.5)


@dp.message(Command("start"))
@dp.message(Command("help"))
async def start_cmd(message: types.Message):
    welcome_text = (
        "🤖 <b>Low-Bandwidth Instagram Monitor Bot</b>\n\n"
        f"Check interval: {CHECK_INTERVAL_SECONDS}s (+ jitter); errors back off.\n\n"
        "Commands:\n"
        "• <code>/monitor &lt;username&gt;</code> — Track single IG account for UNBAN.\n"
        "• <code>/banmonitor &lt;username&gt;</code> — Track active IG account for BAN.\n"
        "• <code>/bulk user1, user2 ...</code> — Bulk track accounts.\n"
        "• <code>/stop &lt;username&gt;</code> — Stop tracking.\n"
        "• <code>/active</code> — View running monitors."
    )
    await message.answer(welcome_text)


@dp.message(Command("monitor"))
async def add_monitor(message: types.Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("❌ Usage: <code>/monitor &lt;username&gt;</code>")
    
    raw = args[1]
    username = raw.split("instagram.com/")[-1].replace("/", "").replace("@", "").strip()
    key = username.lower()

    if key in monitored_accounts:
        return await message.answer(f"⚠️ Already monitoring <b>@{username}</b>!")

    start_time = datetime.now()
    monitored_accounts[key] = {
        "username": username,
        "chat_id": message.chat.id,
        "start_time": start_time,
        "mode": "unban"
    }
    await message.answer(f"✅ Monitoring <b>@{username}</b> every ~{CHECK_INTERVAL_SECONDS}s.")


@dp.message(Command("banmonitor"))
async def add_banmonitor(message: types.Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("❌ Usage: <code>/banmonitor &lt;username&gt;</code>")
    
    raw = args[1]
    username = raw.split("instagram.com/")[-1].replace("/", "").replace("@", "").strip()
    key = username.lower()

    if key in monitored_accounts:
        return await message.answer(f"⚠️ Already monitoring <b>@{username}</b>!")

    start_time = datetime.now()
    monitored_accounts[key] = {
        "username": username,
        "chat_id": message.chat.id,
        "start_time": start_time,
        "mode": "ban"
    }
    await message.answer(f"🚨 Ban monitoring <b>@{username}</b> every ~{CHECK_INTERVAL_SECONDS}s.")


@dp.message(Command("bulk"))
async def add_bulk(message: types.Message):
    text = message.text.replace("/bulk", "").strip()
    if not text:
        return await message.answer("❌ Usage: <code>/bulk user1, user2, user3</code>")
    
    raw_list = text.replace(",", " ").split()
    added = []

    for u in raw_list:
        uname = u.split("instagram.com/")[-1].replace("/", "").replace("@", "").strip()
        key = uname.lower()
        if uname and key not in monitored_accounts:
            st = datetime.now()
            monitored_accounts[key] = {
                "username": uname,
                "chat_id": message.chat.id,
                "start_time": st,
                "mode": "unban"
            }
            added.append(f"@{uname}")

    if added:
        await message.answer(f"⚡ <b>{len(added)} accounts added for tracking!</b>\n" + ", ".join(added))
    else:
        await message.answer("⚠️ No new accounts were added.")


@dp.message(Command("stop"))
async def stop_monitor(message: types.Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("❌ Usage: <code>/stop &lt;username&gt;</code>")
    
    raw = args[1]
    username = raw.split("instagram.com/")[-1].replace("/", "").replace("@", "").strip()
    key = username.lower()

    if key in monitored_accounts:
        del monitored_accounts[key]
        await message.answer(f"🛑 Stopped monitoring <b>@{username}</b>.")
    else:
        await message.answer(f"❌ Not currently monitoring <b>@{username}</b>.")


@dp.message(Command("active"))
async def show_active(message: types.Message):
    user_tasks = [v for k, v in monitored_accounts.items() if v["chat_id"] == message.chat.id]
    if not user_tasks:
        return await message.answer("ℹ️ No active monitoring tasks.")

    lines = ["📊 <b>Active Monitors:</b>\n"]
    for t in user_tasks:
        mode_icon = "✅ Unban" if t["mode"] == "unban" else "🚨 Ban"
        elapsed = datetime.now() - t["start_time"]
        h, r = divmod(int(elapsed.total_seconds()), 3600)
        m, s = divmod(r, 60)
        lines.append(f"• @{t['username']} ({mode_icon}) - Running for {h}h {m}m {s}s")

    await message.answer("\n".join(lines))


async def main():
    if not DEFAULT_PROXIES:
        raise RuntimeError("Set PROXIES in .env before starting; no built-in credentials are used.")
    print(f"🚀 Low-bandwidth monitor starting (interval: {CHECK_INTERVAL_SECONDS}s)...")
    monitor_task = asyncio.create_task(monitor_loop())
    try:
        await dp.start_polling(bot)
    finally:
        monitor_task.cancel()
        with suppress(asyncio.CancelledError):
            await monitor_task


if __name__ == "__main__":
    asyncio.run(main())

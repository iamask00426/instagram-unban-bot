import os
import asyncio
import aiohttp
import logging
import random
import itertools
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
BOT_TOKEN = os.getenv("BOT_TOKEN", "8663562942:AAGLFTtUT2V-uH0t3eWHwwcVfxVBEXuqZJg")

# Free Rotating Proxies (Add Webshare or HTTP proxies here or via PROXIES env var)
# Format: "http://username:password@ip:port" or "http://ip:port"
DEFAULT_PROXIES = [
    p.strip() for p in os.getenv("PROXIES", "").split(",") if p.strip()
]
# Fallback free proxies if none specified in .env
if not DEFAULT_PROXIES:
    DEFAULT_PROXIES = [
        None  # Direct connection fallback
    ]

proxy_pool = itertools.cycle(DEFAULT_PROXIES)

USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Instagram 312.1.0.34.111",
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.230 Mobile Safari/537.36 Instagram 313.0.0.35.111",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

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


def format_num(count):
    try:
        num = float(count)
        if num >= 1_000_000:
            val = num / 1_000_000
            return f"{val:.1f}m" if val % 1 != 0 else f"{int(val)}m"
        elif num >= 1_000:
            val = num / 1_000
            return f"{val:.1f}k" if val % 1 != 0 else f"{int(val)}k"
        return str(int(num))
    except Exception:
        return str(count)


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


async def check_single_account(session: aiohttp.ClientSession, username: str) -> dict:
    """
    Direct asynchronous HTTP GET to Instagram's public API endpoint with rotating proxies and browser headers.
    """
    url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
    proxy = get_next_proxy()
    
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "X-IG-App-ID": "936619743392459",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://www.instagram.com/{username}/",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    try:
        async with session.get(url, headers=headers, proxy=proxy, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                user = data.get("data", {}).get("user")
                if user:
                    followers = user.get("edge_followed_by", {}).get("count", 0)
                    if followers is not None and followers > 0:
                        return {
                            "status": "active",
                            "username": user.get("username", username),
                            "followers": format_num(followers),
                            "posts": format_num(user.get("edge_owner_to_timeline_media", {}).get("count", 0)),
                            "following": format_num(user.get("edge_follow", {}).get("count", 0)),
                            "pic_url": user.get("profile_pic_url_hd") or user.get("profile_pic_url", ""),
                        }
                return {"status": "banned"}

            elif resp.status in [404, 400]:
                # Account is banned/deactivated or not found
                return {"status": "banned"}

            elif resp.status in [429, 403]:
                # Rate-limited or blocked, retry next round with another proxy
                logging.warning(f"Rate limited ({resp.status}) for @{username} on proxy {proxy}")
                return {"status": "rate_limited"}

            return {"status": "unknown"}

    except Exception as e:
        logging.debug(f"Request error for @{username}: {e}")
        return {"status": "error"}


async def monitor_loop():
    """
    Background loop checking batches of 10 accounts with 15-20s interval to prevent rate limits.
    """
    timeout = aiohttp.ClientTimeout(total=15)
    while True:
        try:
            if monitored_accounts:
                usernames = list(monitored_accounts.keys())
                batch_size = 10
                
                for i in range(0, len(usernames), batch_size):
                    batch = usernames[i:i + batch_size]
                    
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        tasks = [check_single_account(session, u) for u in batch]
                        results = await asyncio.gather(*tasks, return_exceptions=True)

                    for uname, res in zip(batch, results):
                        if isinstance(res, Exception) or not isinstance(res, dict):
                            continue

                        if uname not in monitored_accounts:
                            continue

                        data = monitored_accounts[uname]
                        raw_username = data.get("username", uname)
                        chat_id = data["chat_id"]
                        mode = data.get("mode", "unban")

                        t = datetime.now() - data["start_time"]
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

                            del monitored_accounts[uname]

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

                            del monitored_accounts[uname]

                    # 15-20 second polite delay between batches
                    await asyncio.sleep(random.uniform(15, 20))

            else:
                await asyncio.sleep(5)

        except Exception as e:
            logging.error(f"Unexpected error in monitor loop: {e}")
            await asyncio.sleep(10)


@dp.message(Command("start"))
@dp.message(Command("help"))
async def start_cmd(message: types.Message):
    welcome_text = (
        "🤖 <b>Zero-Cost Instagram Monitor Bot</b>\n\n"
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

    monitored_accounts[key] = {
        "username": username,
        "chat_id": message.chat.id,
        "start_time": datetime.now(),
        "mode": "unban"
    }
    await message.answer(f"⚡ Super-fast monitoring active for <b>@{username}</b>!")


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

    monitored_accounts[key] = {
        "username": username,
        "chat_id": message.chat.id,
        "start_time": datetime.now(),
        "mode": "ban"
    }
    await message.answer(f"🚨 Ban monitoring active for <b>@{username}</b>!")


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
            monitored_accounts[key] = {
                "username": uname,
                "chat_id": message.chat.id,
                "start_time": datetime.now(),
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
    print("🚀 Zero-Cost Instagram Monitor Bot is starting...")
    asyncio.create_task(monitor_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

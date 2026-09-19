import os
import asyncio
import contextlib
import ipaddress
import logging
from datetime import datetime
from io import BytesIO
from urllib.parse import urlsplit

import aiohttp
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from config import Settings, is_example, normalize_username
from monitoring import MonitorScheduler, ProfileChecker, profile_session

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BANNED_DP_PATH = os.path.join(BASE_DIR, "default_banned_dp.jpg")
FONT_PATH = os.path.join(BASE_DIR, "font.ttf")

# Importing this module neither reads .env nor constructs a network client.
bot = None
scheduler = None
dp = Dispatcher()


def safe_avatar_url(value):
    try:
        url = urlsplit(value)
        return (url.scheme == "https" and url.username is None and url.password is None
                and url.port in {None, 443} and not url.fragment
                and any((url.hostname or "").endswith("." + domain)
                        for domain in ("cdninstagram.com", "fbcdn.net")))
    except (ValueError, TypeError):
        return False


class PublicAvatarResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):
        self.resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=0):
        addresses = await self.resolver.resolve(host, port, family)
        if not addresses or any(not ipaddress.ip_address(a["host"]).is_global for a in addresses):
            raise OSError("Non-public avatar address rejected")
        return addresses

    async def close(self):
        await self.resolver.close()


async def download_avatar(url):
    if not safe_avatar_url(url):
        return None
    limit = 2 * 1024 * 1024
    resolver = PublicAvatarResolver()
    try:
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=1, resolver=resolver),
            timeout=aiohttp.ClientTimeout(total=5), cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False, auto_decompress=False,
        ) as session:
            async with session.get(url, allow_redirects=False, headers={"Accept-Encoding": "identity"}) as response:
                try:
                    if (response.status != 200 or response.headers.get("Content-Encoding", "identity") not in {"", "identity"}
                            or (response.content_length is not None and response.content_length > limit)):
                        return None
                    data = bytearray()
                    while len(data) < limit:
                        chunk = await response.content.read(min(16384, limit - len(data)))
                        if not chunk:
                            return bytes(data)
                        data.extend(chunk)
                    return bytes(data) if response.content.at_eof() else None
                finally:
                    response.close()
    finally:
        await resolver.close()


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
        except Exception:
            logging.warning("Unable to load bundled font")

    d = ImageFont.load_default()
    return {"username": d, "btn": d, "stats_num": d, "stats_lbl": d, "name": d}


async def create_profile_card(username, followers, posts, following, pic_url=None, is_unavailable=False):
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

    # For an unavailable account, use default_banned_dp.jpg
    if is_unavailable and os.path.exists(DEFAULT_BANNED_DP_PATH):
        try:
            dp_img = Image.open(DEFAULT_BANNED_DP_PATH).convert("RGB").resize(av_size)
            mask = Image.new('L', av_size, 0)
            ImageDraw.Draw(mask).ellipse((0, 0, av_size[0], av_size[1]), fill=255)
            img.paste(dp_img, (av_center[0]-av_r, av_center[1]-av_r), mask)
            avatar_drawn = True
        except Exception:
            logging.warning("Unable to load default avatar")

    # Optional direct avatar traffic; never another profile-details request.
    if not avatar_drawn and pic_url:
        try:
            avatar_bytes = await download_avatar(pic_url)
            if avatar_bytes:
                with Image.open(BytesIO(avatar_bytes)) as source:
                    if source.width * source.height > 16_000_000:
                        raise ValueError("Avatar dimensions exceed limit")
                    av_img = source.convert("RGB").resize(av_size)
                mask = Image.new('L', av_size, 0)
                ImageDraw.Draw(mask).ellipse((0, 0, av_size[0], av_size[1]), fill=255)
                img.paste(av_img, (av_center[0]-av_r, av_center[1]-av_r), mask)
                avatar_drawn = True
        except Exception:
            logging.warning("Avatar unavailable; using local placeholder")

    if not avatar_drawn:
        draw.ellipse([av_center[0]-av_r, av_center[1]-av_r, av_center[0]+av_r, av_center[1]+av_r], fill='#262626')
        first_char = username[0].upper() if username else 'U'
        draw.text((av_center[0]-15, av_center[1]-25), first_char, font=font_username, fill='#ffffff')

    display_username = username if not is_unavailable else "UserNotFound"
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

    sub_title = username if not is_unavailable else "UserNotFound"
    draw.text((320, 330), sub_title, font=font_name, fill='#ffffff')

    bio = BytesIO()
    img.save(bio, 'PNG')
    bio.seek(0)
    return bio


async def trigger_alert(record, current):
    """Deliver only cached detection metadata; stale records cannot send a fallback."""
    if not current():
        return False
    result = record.pending
    elapsed = (record.detected_at or datetime.now()) - record.start_time
    h, remainder = divmod(int(elapsed.total_seconds()), 3600)
    m, s = divmod(remainder, 60)
    elapsed_str = f"{h} hours, {m} minutes, {s} seconds"
    username = record.username
    unavailable = result.status == "unavailable"
    if unavailable:
        msg = (
            f"🚨 <b>Account unavailable</b>\n\n"
            f"<a href='https://instagram.com/{username}'>@{username}</a> returned repeated HTTP 404 responses.\n"
            f"<i>This does not prove the account is banned or disabled.</i>\n"
            f"⏱️ <i>Time taken: {elapsed_str}</i>"
        )
    else:
        msg = (
            f"Account active/available | <a href='https://instagram.com/{username}'>@{username}</a> 🏆✅\n"
            f"<i>Followers: {result.followers} | Following: {result.following}</i>\n"
            f"⏱️ <i>Time taken: {elapsed_str}</i>"
        )
    try:
        if not record.photo_prepared:
            try:
                image = await create_profile_card(
                    username, 0 if unavailable else result.followers,
                    0 if unavailable else result.posts, 0 if unavailable else result.following,
                    pic_url=None if unavailable else result.pic_url, is_unavailable=unavailable,
                )
                if not current():
                    return False
                record.photo = image.getvalue()
            finally:
                record.photo_prepared = True
        if not current():
            return False
        if record.photo is not None:
            await bot.send_photo(record.chat_id, photo=types.BufferedInputFile(record.photo, filename="card.png"), caption=msg)
            return current()
    except Exception:
        logging.warning("Photo delivery failed; attempting text fallback")
    if not current():
        return False
    # Telegram must not fetch an Instagram link preview as hidden enrichment.
    await bot.send_message(record.chat_id, text=msg, disable_web_page_preview=True)
    return current()


@dp.message(Command("start"))
@dp.message(Command("help"))
async def start_cmd(message: types.Message):
    await message.answer(
        "🤖 <b>Instagram Availability Monitor</b>\n\n"
        "• <code>/monitor &lt;username&gt;</code> — Wait until active/available.\n"
        "• <code>/banmonitor &lt;username&gt;</code> — Wait for confirmed unavailability (not proof of a ban).\n"
        "• <code>/bulk user1, user2 ...</code> — Bulk wait-until-active monitoring.\n"
        "• <code>/stop &lt;username&gt;</code> — Stop your monitor.\n"
        "• <code>/active</code> — View this chat's monitors."
    )


async def add_command(message, mode, command):
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        return await message.answer(f"❌ Usage: <code>/{command} &lt;username&gt;</code>")
    if not message.from_user:
        return await message.answer("❌ Monitoring requires an identifiable user.")
    try:
        username = normalize_username(args[1])
    except ValueError:
        return await message.answer("❌ Invalid Instagram username or profile URL.")
    record = scheduler.add(username, message.chat.id, message.from_user.id, mode)
    if record is None:
        return await message.answer(f"⚠️ Already monitoring <b>@{username}</b>!")
    label = "availability" if mode == "unban" else "unavailability (not proof of a ban)"
    await message.answer(f"⚡ Monitoring {label} for <b>@{username}</b>!")


@dp.message(Command("monitor"))
async def add_monitor(message: types.Message):
    await add_command(message, "unban", "monitor")


@dp.message(Command("banmonitor"))
async def add_banmonitor(message: types.Message):
    await add_command(message, "ban", "banmonitor")


@dp.message(Command("bulk"))
async def add_bulk(message: types.Message):
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("❌ Usage: <code>/bulk user1, user2, user3</code>")
    if not message.from_user:
        return await message.answer("❌ Monitoring requires an identifiable user.")
    added = []
    invalid = 0
    for value in args[1].replace(",", " ").split():
        try:
            username = normalize_username(value)
        except ValueError:
            invalid += 1
            continue
        if scheduler.add(username, message.chat.id, message.from_user.id, "unban"):
            added.append(f"@{username}")
    await message.answer(
        f"⚡ <b>{len(added)} accounts added.</b> Invalid entries skipped: {invalid}.\n" + ", ".join(added)
        if added or invalid else "⚠️ No new accounts were added."
    )


@dp.message(Command("stop"))
async def stop_monitor(message: types.Message):
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("❌ Usage: <code>/stop &lt;username&gt;</code>")
    try:
        username = normalize_username(args[1])
    except ValueError:
        return await message.answer("❌ Invalid Instagram username or profile URL.")
    if message.from_user and scheduler.stop(username, message.chat.id, message.from_user.id):
        await message.answer(f"🛑 Stopped monitoring <b>@{username}</b>.")
    else:
        await message.answer(f"❌ No monitor owned by you in this chat for <b>@{username}</b>.")


@dp.message(Command("active"))
async def show_active(message: types.Message):
    records = [r for r in scheduler.records.values() if r.chat_id == message.chat.id]
    if not records:
        return await message.answer("ℹ️ No active monitoring tasks.")
    lines = ["📊 <b>Active Monitors:</b>\n"]
    for record in records:
        mode = "✅ Availability" if record.mode == "unban" else "🚨 Unavailability"
        if record.pending:
            mode += " — alert delivery pending"
        elapsed = datetime.now() - record.start_time
        h, remainder = divmod(int(elapsed.total_seconds()), 3600)
        m, s = divmod(remainder, 60)
        lines.append(f"• @{record.username} ({mode}) - Running for {h}h {m}m {s}s")
    await message.answer("\n".join(lines))


async def main():
    global bot, scheduler
    # Only executable startup loads local secrets, never imports or tests.
    load_dotenv()
    try:
        settings = Settings.from_env(os.environ)
    except ValueError as error:
        # Only our value-free configuration validation messages may be printed.
        raise SystemExit(f"Startup configuration error: {error}") from None
    logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
    if os.getenv("IG_SESSIONID") and is_example(os.environ["IG_SESSIONID"]):
        logging.warning("Example IG_SESSIONID ignored; HTML checks do not require a session cookie")
    # Avoid framework exception/HTTP request logs containing tokens or proxy URLs.
    logging.getLogger("aiogram").setLevel(logging.CRITICAL)
    logging.getLogger("aiohttp").setLevel(logging.CRITICAL)
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        async with profile_session(settings) as session:
            checker = ProfileChecker(session, settings)
            scheduler = MonitorScheduler(settings, checker.check, trigger_alert)
            monitor_task = asyncio.create_task(scheduler.run())
            try:
                await dp.start_polling(bot)
            finally:
                monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor_task
                logging.info("Profile checks: attempts=%s outcomes=%s consumed_body_bytes=%s encoded_body_bytes=%s (not billed bytes)",
                             checker.metrics.request_attempts, dict(checker.metrics.outcomes),
                             checker.metrics.consumed_body_bytes, checker.metrics.encoded_body_bytes)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        raise SystemExit("Bot stopped unexpectedly; check configuration and service availability (details redacted).") from None

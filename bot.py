"""
Telegram Instagram Availability & Unban Monitor Bot
Equipped with full command suite matching Discord bot:
/unban, /ban, /tick, /list, /clear, /clearall, /fakeunban, /kill, /lookup,
/autoban, /2h, /1day, /7day, /1month, /reset, /ping, /help.
"""

from __future__ import annotations

import os
import asyncio
import logging
import time
from datetime import datetime
from io import BytesIO

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

import database
from config import Settings, parse_usernames
from monitoring import ProfileChecker, profile_session, CheckResult, fetch_user_id_by_username, resolve_username_by_uid
from card_generator import create_profile_card, format_count

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

bot: Bot | None = None
checker: ProfileChecker | None = None
profile_http_session = None
dp = Dispatcher()
_alerting_keys: set[tuple[str, int]] = set()

def format_elapsed(start_time: datetime) -> str:
    elapsed = datetime.now() - start_time
    total_seconds = max(0, int(elapsed.total_seconds()))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours} hours, {minutes} minutes, {seconds} seconds"

# --- ALERT SENDER FUNCTIONS ---

async def send_tg_unban_alert(chat_id: int, monitor_data: dict, result: CheckResult, is_fake: bool = False, custom_time: str = None):
    username_key = (monitor_data["username"], chat_id)
    if not is_fake:
        if username_key in _alerting_keys:
            return
        _alerting_keys.add(username_key)
    try:
        raw_user = monitor_data["raw_username"]
        if custom_time:
            elapsed_str = custom_time
        else:
            start_time = datetime.fromisoformat(monitor_data["created_at"]) if isinstance(monitor_data["created_at"], str) else monitor_data["created_at"]
            elapsed_str = format_elapsed(start_time)

        settings = database.get_server_settings(chat_id) if chat_id else {}
        style = settings.get("style", 1)

        folls_val = str(result.followers).replace(",", "") if result.followers is not None else "0"
        follg_val = str(result.following).replace(",", "") if result.following is not None else "0"
        caption = (
            f"Account Recovered | <a href='https://instagram.com/{raw_user}'>@{raw_user}</a> 🏆✅\n"
            f"<i>Followers: {folls_val} | Following: {follg_val}</i>\n"
            f"⏱️ <i>Time taken: {elapsed_str}</i>"
        )
        if style == 4:
            caption = (
                f"⚰️ <b>BACK FROM THE GRAVE!</b> 🧟‍♂️\n"
                f"Account Recovered | <a href='https://instagram.com/{raw_user}'>@{raw_user}</a> 🏆✅\n"
                f"<i>Followers: {folls_val} | Following: {follg_val}</i>\n"
                f"⏱️ <i>Time taken: {elapsed_str}</i>"
            )

        full_name = getattr(result, "full_name", "") or raw_user
        is_verified = getattr(result, "is_verified", False)
        card_io = None
        if style != 3:
            try:
                card_io = await create_profile_card(
                    raw_user, result.followers, result.posts, result.following,
                    pic_url=result.pic_url, is_unavailable=False, full_name=full_name,
                    is_verified=is_verified, style=style
                )
            except Exception as e:
                logging.error(f"Error creating card for @{raw_user}: {e}")

        try:
            if card_io:
                card_io.seek(0)
                photo_file = types.BufferedInputFile(card_io.getvalue(), filename="card.png")
                await bot.send_photo(chat_id, photo=photo_file, caption=caption, parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(chat_id, text=caption, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            logging.error(f"Failed delivering TG unban alert to {chat_id}: {e}")

        if chat_id and not is_fake:
            database.record_unban_stat(chat_id, monitor_data["username"], raw_user)

        now_ts = time.time()
        if not is_fake:
            uid = getattr(result, "user_id", None)
            if not uid and profile_http_session and checker:
                proxy = next(checker.proxies) if checker.proxies else None
                try:
                    uid = await fetch_user_id_by_username(profile_http_session, proxy, raw_user)
                except Exception as e:
                    logging.warning(f"Error fetching UID for @{raw_user}: {e}")

            if uid:
                database.transition_to_username_tracking(monitor_data["username"], chat_id, uid, now_ts)
                logging.info(f"TG: Started 24h username tracking immediately for @{raw_user} (UID: {uid})")
            else:
                database.remove_monitor(monitor_data["username"], chat_id)

            if settings.get("autoban"):
                database.add_monitors([raw_user], "ban", chat_id, chat_id, 0)
                logging.info(f"TG: Autoban engaged for @{raw_user} in chat {chat_id}")
    finally:
        if not is_fake:
            _alerting_keys.discard(username_key)

async def send_tg_ban_alert(chat_id: int, monitor_data: dict, is_fake: bool = False):
    username_key = (monitor_data["username"], chat_id)
    if not is_fake:
        if username_key in _alerting_keys:
            return
        _alerting_keys.add(username_key)
    try:
        raw_user = monitor_data["raw_username"]
        if "created_at" in monitor_data and monitor_data["created_at"]:
            start_time = datetime.fromisoformat(monitor_data["created_at"]) if isinstance(monitor_data["created_at"], str) else monitor_data["created_at"]
            elapsed_str = format_elapsed(start_time)
        else:
            elapsed_str = "0 hours, 0 minutes, 1 seconds"

        caption = (
            f"🚨 <b>Super-Fast Ban Alert!</b>\n\n"
            f"<a href='https://instagram.com/{raw_user}'>@{raw_user}</a> has been <b>BANNED/DISABLED</b>!\n"
            f"⏱️ <i>Time taken: {elapsed_str}</i>"
        )

        card_io = None
        try:
            card_io = await create_profile_card(
                raw_user, 0, 0, 0, pic_url=None, is_unavailable=True, full_name="UserNotFound"
            )
        except Exception as e:
            logging.error(f"Error creating ban card for @{raw_user}: {e}")

        try:
            if card_io:
                card_io.seek(0)
                photo_file = types.BufferedInputFile(card_io.getvalue(), filename="card.png")
                await bot.send_photo(chat_id, photo=photo_file, caption=caption, parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(chat_id, text=caption, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            logging.error(f"Failed delivering TG ban alert to {chat_id}: {e}")

        if not is_fake:
            database.remove_monitor(monitor_data["username"], chat_id)
    finally:
        if not is_fake:
            _alerting_keys.discard(username_key)

async def send_tg_tick_alert(chat_id: int, monitor_data: dict, result: CheckResult):
    username_key = (monitor_data["username"], chat_id)
    if username_key in _alerting_keys:
        return
    _alerting_keys.add(username_key)
    try:
        raw_user = monitor_data["raw_username"]
        start_time = datetime.fromisoformat(monitor_data["created_at"]) if isinstance(monitor_data["created_at"], str) else monitor_data["created_at"]
        elapsed_str = format_elapsed(start_time)

        folls_formatted = format_count(result.followers)
        caption = (
            f"🎉 <b>Verification Tick Detected!</b> | <a href='https://instagram.com/{raw_user}'>@{raw_user}</a> 🏆✅\n"
            f"Followers: {folls_formatted} | Following: {result.following}\n"
            f"⏱️ <i>Time taken: {elapsed_str}</i>"
        )

        full_name = getattr(result, "full_name", "") or raw_user
        card_io = None
        try:
            card_io = await create_profile_card(
                raw_user, result.followers, result.posts, result.following,
                pic_url=result.pic_url, is_unavailable=False, full_name=full_name,
                is_verified=True
            )
        except Exception as e:
            logging.error(f"Error creating tick card for @{raw_user}: {e}")

        try:
            if card_io:
                card_io.seek(0)
                photo_file = types.BufferedInputFile(card_io.getvalue(), filename="card.png")
                await bot.send_photo(chat_id, photo=photo_file, caption=caption, parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(chat_id, text=caption, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            logging.error(f"Failed delivering TG tick alert to {chat_id}: {e}")

        database.remove_monitor(monitor_data["username"], chat_id)
    finally:
        _alerting_keys.discard(username_key)

async def send_tg_username_change_alert(chat_id: int, old_name: str, new_name: str, uid: str):
    caption = (
        f"🔄 <b>Instagram Username Change Detected!</b>\n\n"
        f"<b>Old Username:</b> <code>@{old_name}</code>\n"
        f"<b>New Username:</b> <a href='https://instagram.com/{new_name}'>@{new_name}</a> 🏆\n"
        f"🆔 <b>Instagram UID:</b> <code>{uid}</code>\n\n"
        f"<i>24-Hour Post-Unban Username Tracker</i>"
    )
    try:
        await bot.send_message(chat_id, text=caption, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        logging.error(f"Failed delivering TG username change alert to {chat_id}: {e}")

async def instant_check(chat_id: int, monitor_data: dict):
    if not checker:
        return
    username = monitor_data["username"]
    mode = monitor_data["mode"]
    try:
        res = await checker.check(username)
        if mode == "unban" and res.status == "active":
            await send_tg_unban_alert(chat_id, monitor_data, res)
        elif mode == "ban" and res.status == "unavailable":
            await send_tg_ban_alert(chat_id, monitor_data)
        elif mode == "tick" and res.status == "active" and getattr(res, "is_verified", False):
            await send_tg_tick_alert(chat_id, monitor_data, res)
    except Exception as e:
        logging.debug(f"TG instant check exception for @{username}: {e}")

# --- BACKGROUND MONITORING LOOP ---

async def tg_monitoring_worker():
    logging.info("Telegram background monitoring loop started.")
    while True:
        try:
            # 1. Clean up expired 24h username tracking accounts
            expired = database.cleanup_expired_username_tracking(max_age_seconds=86400)
            for exp in expired:
                logging.info(f"TG: 24h Username tracking auto-stopped for @{exp['raw_username']} (UID: {exp.get('user_id')})")

            # 2. Check 24h username tracking accounts using their user_id
            tracking_records = database.get_username_tracking_monitors()
            for rec in tracking_records:
                uid = rec.get("user_id")
                if not uid:
                    continue
                old_username = rec.get("username")
                old_raw = rec.get("raw_username")
                chat_id = rec.get("guild_id") or rec.get("channel_id")

                try:
                    proxy = next(checker.proxies) if checker and checker.proxies else None
                    current_name = await resolve_username_by_uid(profile_http_session, proxy, uid)
                    if current_name and current_name.lower() != old_username.lower():
                        logging.info(f"TG: Username change detected for UID {uid}: @{old_raw} -> @{current_name}")
                        database.update_tracked_username(old_username, current_name, chat_id)
                        await send_tg_username_change_alert(chat_id, old_raw, current_name, uid)
                except Exception as e:
                    logging.debug(f"Error checking UID {uid} for username change in TG: {e}")

                await asyncio.sleep(0.5)

            # 3. Regular active monitors
            records = database.get_all_monitors()
            active_records = [r for r in records if r.get("status") != "tracking_username"]
            if not active_records and not tracking_records:
                await asyncio.sleep(2.0)
                continue

            for rec in active_records:
                username = rec["username"]
                mode = rec["mode"]
                chat_id = rec.get("guild_id") or rec.get("channel_id")

                try:
                    res = await checker.check(username)
                    if mode == "unban" and res.status == "active":
                        await send_tg_unban_alert(chat_id, rec, res)
                    elif mode == "ban" and res.status == "unavailable":
                        await send_tg_ban_alert(chat_id, rec)
                    elif mode == "tick" and res.status == "active" and getattr(res, "is_verified", False):
                        await send_tg_tick_alert(chat_id, rec, res)
                except Exception as e:
                    logging.debug(f"TG loop error checking @{username}: {e}")

                await asyncio.sleep(1.0)

            await asyncio.sleep(3.0)
        except Exception as e:
            logging.error(f"Unexpected error in TG monitor worker: {e}")
            await asyncio.sleep(5.0)

# --- BOT COMMAND HANDLERS (ENGLISH) ---

@dp.message(Command("start"))
@dp.message(Command("help"))
async def start_cmd(message: types.Message):
    help_text = (
        "🤖 <b>Instagram Monitor Bot — Command List</b>\n\n"
        "<b>⚡ Monitoring Commands</b>\n"
        "• <code>/unban &lt;usernames/urls&gt;</code> — Monitor and notify when Unbanned [Supports multiple]\n"
        "• <code>/ban &lt;usernames/urls&gt;</code> — Monitor and notify when Done / Banned [Supports multiple]\n"
        "• <code>/tick &lt;usernames/urls&gt;</code> — Monitor for verification tick\n"
        "• <code>/list [username/url]</code> — Show active monitors or check a specific username\n"
        "• <code>/clear &lt;username&gt;</code> — Remove username from monitoring\n"
        "• <code>/clearall</code> — Remove ALL monitored usernames in this chat\n\n"
        "<b>🎭 Simulation & Utility</b>\n"
        "• <code>/kill &lt;username/url&gt;</code> — Fake ban notification with card\n"
        "• <code>/fakeunban &lt;user&gt; &lt;followers&gt; [HH:MM:SS]</code> — Fake unban notification\n"
        "• <code>/lookup &lt;username/url&gt;</code> — Get all info about a profile\n"
        "• <code>/ping</code> — Check bot latency and heartbeat\n\n"
        "<b>📊 Unban Stats</b>\n"
        "• <code>/2h</code> — Unban stats for the last 2 hours\n"
        "• <code>/1day</code> — Unban stats for the last 24 hours\n"
        "• <code>/7day</code> — Unban stats for the last 7 days\n"
        "• <code>/1month</code> — Unban stats for the last 30 days\n"
        "• <code>/reset &lt;2h|1day|7day|1month&gt;</code> — Reset stats for that window\n\n"
        "<b>⚙️ Settings</b>\n"
        "• <code>/autoban &lt;on/off&gt;</code> — Auto ban-watch for 24h after recovery"
    )
    await message.answer(help_text, parse_mode=ParseMode.HTML)

@dp.message(Command("unban"))
async def unban_cmd(message: types.Message):
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("❌ Usage: <code>/unban &lt;usernames or urls&gt;</code>", parse_mode=ParseMode.HTML)
    usernames = parse_usernames(args[1])
    if not usernames:
        return await message.answer("❌ No valid Instagram usernames found.")

    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0
    added = database.add_monitors(usernames, "unban", chat_id, chat_id, user_id)

    code_list = " ".join([f"<code>{u}</code>" for u in added[:25]])
    more_text = f" and {len(added) - 25} more" if len(added) > 25 else ""
    await message.answer(
        f"⚡ <b>Monitoring Started</b>\nMonitoring for <b>{len(added)}</b> username(s) has started.\n{code_list}{more_text}",
        parse_mode=ParseMode.HTML
    )

    for u in added:
        mon_rec = {
            "username": u.lower(),
            "raw_username": u,
            "mode": "unban",
            "guild_id": chat_id,
            "channel_id": chat_id,
            "created_at": datetime.now()
        }
        asyncio.create_task(instant_check(chat_id, mon_rec))

@dp.message(Command("ban"))
async def ban_cmd(message: types.Message):
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("❌ Usage: <code>/ban &lt;usernames or urls&gt;</code>", parse_mode=ParseMode.HTML)
    usernames = parse_usernames(args[1])
    if not usernames:
        return await message.answer("❌ No valid Instagram usernames found.")

    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0
    added = database.add_monitors(usernames, "ban", chat_id, chat_id, user_id)

    code_list = " ".join([f"<code>{u}</code>" for u in added[:25]])
    more_text = f" and {len(added) - 25} more" if len(added) > 25 else ""
    await message.answer(
        f"🚨 <b>Ban Monitoring Started</b>\nMonitoring for <b>{len(added)}</b> username(s) has started.\n{code_list}{more_text}",
        parse_mode=ParseMode.HTML
    )

    for u in added:
        mon_rec = {
            "username": u.lower(),
            "raw_username": u,
            "mode": "ban",
            "guild_id": chat_id,
            "channel_id": chat_id,
            "created_at": datetime.now()
        }
        asyncio.create_task(instant_check(chat_id, mon_rec))

@dp.message(Command("tick"))
async def tick_cmd(message: types.Message):
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("❌ Usage: <code>/tick &lt;usernames or urls&gt;</code>", parse_mode=ParseMode.HTML)
    usernames = parse_usernames(args[1])
    if not usernames:
        return await message.answer("❌ No valid Instagram usernames found.")

    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0
    added = database.add_monitors(usernames, "tick", chat_id, chat_id, user_id)

    code_list = " ".join([f"<code>{u}</code>" for u in added[:25]])
    more_text = f" and {len(added) - 25} more" if len(added) > 25 else ""
    await message.answer(
        f"✅ <b>Tick Monitoring Started</b>\nMonitoring for <b>{len(added)}</b> username(s) has started.\n{code_list}{more_text}",
        parse_mode=ParseMode.HTML
    )

    for u in added:
        mon_rec = {
            "username": u.lower(),
            "raw_username": u,
            "mode": "tick",
            "guild_id": chat_id,
            "channel_id": chat_id,
            "created_at": datetime.now()
        }
        asyncio.create_task(instant_check(chat_id, mon_rec))

@dp.message(Command("clear"))
async def clear_cmd(message: types.Message):
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("❌ Usage: <code>/clear &lt;username&gt;</code>", parse_mode=ParseMode.HTML)
    usernames = parse_usernames(args[1])
    if not usernames:
        return await message.answer("❌ No valid Instagram usernames provided.")

    cleared, not_mon = database.remove_monitors(usernames, message.chat.id)
    lines = []
    if cleared:
        lines.append(f"✅ <b>Cleared ({len(cleared)}):</b> " + " ".join([f"<code>{u}</code>" for u in cleared]))
    if not_mon:
        lines.append(f"⚠️ <b>Not Monitored ({len(not_mon)}):</b> " + " ".join([f"<code>{u}</code>" for u in not_mon]))
    await message.answer("\n\n".join(lines) if lines else "❌ No monitors matched.", parse_mode=ParseMode.HTML)

@dp.message(Command("clearall"))
async def clearall_cmd(message: types.Message):
    count = database.clear_all_monitors(message.chat.id)
    await message.answer(f"🗑️ <b>Cleared {count} monitor(s)</b> from this chat.", parse_mode=ParseMode.HTML)

@dp.message(Command("list"))
@dp.message(Command("active"))
async def list_cmd(message: types.Message):
    args = (message.text or "").split(maxsplit=1)
    chat_id = message.chat.id
    records = database.get_all_monitors()
    records = [r for r in records if r["guild_id"] == chat_id or r.get("channel_id") == chat_id]

    if len(args) > 1:
        parsed = parse_usernames(args[1])
        if not parsed:
            return await message.answer("❌ Please provide a valid username.")
        target = parsed[0].lower()
        match = next((r for r in records if r["username"] == target), None)
        if match:
            st = datetime.fromisoformat(match["created_at"]) if isinstance(match["created_at"], str) else match["created_at"]
            elapsed = format_elapsed(st)
            mode_disp = "🔄 24h Name Watch" if match.get("status") == "tracking_username" else ("🏆 Unban" if match["mode"] == "unban" else ("🚨 Ban" if match["mode"] == "ban" else "✅ Tick"))
            return await message.answer(
                f"ℹ️ <b>Monitor Status: @{match['raw_username']}</b>\n• <b>Mode:</b> {mode_disp}\n• <b>Running for:</b> {elapsed}",
                parse_mode=ParseMode.HTML
            )
        else:
            return await message.answer(f"❌ <code>@{target}</code> is not currently monitored in this chat.", parse_mode=ParseMode.HTML)

    if not records:
        return await message.answer("ℹ️ No accounts are currently being monitored in this chat.")

    lines = [f"📊 <b>Active Monitors ({len(records)}):</b>\n"]
    for r in records[:25]:
        if r.get("status") == "tracking_username":
            mode_icon = "🔄 24h Name Watch"
        elif r["mode"] == "unban":
            mode_icon = "🏆 Unban"
        elif r["mode"] == "ban":
            mode_icon = "🚨 Ban"
        else:
            mode_icon = "✅ Tick"
        st = datetime.fromisoformat(r["created_at"]) if isinstance(r["created_at"], str) else r["created_at"]
        elapsed = format_elapsed(st)
        lines.append(f"• <b>@{r['raw_username']}</b> ({mode_icon}) — <i>Running for {elapsed}</i>")

    if len(records) > 25:
        lines.append(f"\n<i>...and {len(records) - 25} more active accounts</i>")

    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

@dp.message(Command("lookup"))
async def lookup_cmd(message: types.Message):
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("❌ Usage: <code>/lookup &lt;username or url&gt;</code>", parse_mode=ParseMode.HTML)
    parsed = parse_usernames(args[1])
    if not parsed:
        return await message.answer("❌ Invalid username.")
    target_user = parsed[0]

    status_msg = await message.answer(f"🔍 Looking up <code>@{target_user}</code> on Instagram...", parse_mode=ParseMode.HTML)
    try:
        res = await checker.check(target_user.lower())
        if res.status == "active":
            verified_badge = " ✅" if res.is_verified else ""
            folls = format_count(res.followers)
            info = (
                f"👤 <b>Instagram Profile:</b> <a href='https://instagram.com/{target_user}'>@{target_user}</a>{verified_badge}\n\n"
                f"• <b>Full Name:</b> {res.full_name or target_user}\n"
                f"• <b>Followers:</b> {folls} ({res.followers})\n"
                f"• <b>Following:</b> {res.following}\n"
                f"• <b>Posts:</b> {res.posts}\n"
                f"• <b>Status:</b> Active 🟢"
            )
            card_io = await create_profile_card(
                target_user, res.followers, res.posts, res.following,
                pic_url=res.pic_url, full_name=res.full_name, is_verified=res.is_verified
            )
            card_io.seek(0)
            photo_file = types.BufferedInputFile(card_io.getvalue(), filename="lookup.png")
            await message.answer_photo(photo=photo_file, caption=info, parse_mode=ParseMode.HTML)
        elif res.status == "unavailable":
            await message.answer(f"❌ <b>@{target_user}</b> is currently <b>BANNED / UNAVAILABLE</b> (HTTP 404).", parse_mode=ParseMode.HTML)
        else:
            await message.answer(f"⚠️ <b>@{target_user}</b> status: <code>{res.status}</code>.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await message.answer(f"❌ Lookup error: {e}")
    finally:
        try:
            await status_msg.delete()
        except Exception:
            pass

@dp.message(Command("kill"))
async def kill_cmd(message: types.Message):
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("❌ Usage: <code>/kill &lt;username or url&gt;</code>", parse_mode=ParseMode.HTML)
    parsed = parse_usernames(args[1])
    if not parsed:
        return await message.answer("❌ Invalid username.")
    target_user = parsed[0]

    mon_rec = {
        "username": target_user.lower(),
        "raw_username": target_user,
        "guild_id": message.chat.id,
        "channel_id": message.chat.id,
        "created_at": datetime.now()
    }
    await send_tg_ban_alert(message.chat.id, mon_rec, is_fake=True)
    await message.answer(f"💀 Fake ban alert dispatched for <code>@{target_user}</code>.", parse_mode=ParseMode.HTML)

@dp.message(Command("fakeunban"))
async def fakeunban_cmd(message: types.Message):
    text = (message.text or "").strip()
    parts = text.split()
    if len(parts) < 3:
        return await message.answer(
            "❌ Usage: <code>/fakeunban &lt;username&gt; &lt;followers&gt; [HH:MM:SS]</code>\nExample: <code>/fakeunban zuck 12.5M 01:23:45</code>",
            parse_mode=ParseMode.HTML
        )
    parsed = parse_usernames(parts[1])
    if not parsed:
        return await message.answer("❌ Invalid username.")
    target_user = parsed[0]
    followers = parts[2]
    elapsed_time = parts[3] if len(parts) > 3 else None

    if elapsed_time:
        p_time = elapsed_time.strip().split(":")
        if len(p_time) == 3 and all(p.strip().isdigit() for p in p_time):
            h, m, s = int(p_time[0]), int(p_time[1]), int(p_time[2])
            time_str = f"{h} hours, {m} minutes, {s} seconds"
        else:
            time_str = elapsed_time.strip()
    else:
        time_str = "12 hours, 34 minutes, 56 seconds"

    posts = "0"
    following = "0"
    pic_url = None
    full_name = target_user
    is_verified = False
    uid = None

    if checker:
        try:
            live_res = await asyncio.wait_for(checker.check(target_user.lower()), timeout=7.0)
            if live_res and live_res.status == "active":
                posts = live_res.posts if live_res.posts and live_res.posts != "?" else "0"
                following = live_res.following if live_res.following and live_res.following != "?" else "0"
                pic_url = live_res.pic_url
                full_name = live_res.full_name or target_user
                is_verified = live_res.is_verified
                uid = getattr(live_res, "user_id", None)
        except Exception as e:
            logging.warning(f"TG: Could not fetch live profile for fakeunban @{target_user}: {e}")

    res = CheckResult(
        status="active",
        username=target_user.lower(),
        followers=followers,
        following=following,
        posts=posts,
        pic_url=pic_url,
        full_name=full_name,
        is_verified=is_verified,
        user_id=uid
    )

    mon_rec = {
        "username": target_user.lower(),
        "raw_username": target_user,
        "guild_id": message.chat.id,
        "channel_id": message.chat.id,
        "created_at": datetime.now()
    }
    await send_tg_unban_alert(message.chat.id, mon_rec, res, is_fake=True, custom_time=time_str)
    await message.answer(f"🏆 Fake unban alert dispatched for <code>@{target_user}</code>.", parse_mode=ParseMode.HTML)

@dp.message(Command("autoban"))
async def autoban_cmd(message: types.Message):
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2 or args[1].lower() not in ("on", "off"):
        st = database.get_server_settings(message.chat.id)
        current = "ON 🟢" if st.get("autoban") else "OFF 🔴"
        return await message.answer(
            f"ℹ️ Autoban is currently <b>{current}</b>.\nUsage: <code>/autoban on</code> or <code>/autoban off</code>",
            parse_mode=ParseMode.HTML
        )
    val = 1 if args[1].lower() == "on" else 0
    database.set_server_setting(message.chat.id, "autoban", val)
    status_str = "ENABLED 🟢 (Will automatically watch recovered accounts for ban for 24h)" if val else "DISABLED 🔴"
    await message.answer(f"⚙️ Autoban has been <b>{status_str}</b>.", parse_mode=ParseMode.HTML)

# --- STATS COMMANDS ---

async def display_tg_unban_stats(message: types.Message, window_seconds: int, label: str):
    records = database.get_unban_stats(message.chat.id, window_seconds)
    if not records:
        return await message.answer(f"📊 <b>Unban Stats ({label})</b>\n\nNo accounts recovered during this window.")

    lines = [f"📊 <b>Unban Stats ({label})</b> — Total: <b>{len(records)}</b> unbanned\n"]
    for r in records[:25]:
        ub_time = r["unbanned_at"]
        lines.append(f"• <b>@{r['raw_username']}</b> — <i>{ub_time}</i>")
    if len(records) > 25:
        lines.append(f"\n<i>...and {len(records) - 25} more</i>")

    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

@dp.message(Command("2h"))
async def stats_2h_cmd(message: types.Message):
    await display_tg_unban_stats(message, 7200, "Last 2 Hours")

@dp.message(Command("1day"))
async def stats_1day_cmd(message: types.Message):
    await display_tg_unban_stats(message, 86400, "Last 24 Hours")

@dp.message(Command("7day"))
async def stats_7day_cmd(message: types.Message):
    await display_tg_unban_stats(message, 604800, "Last 7 Days")

@dp.message(Command("1month"))
async def stats_1month_cmd(message: types.Message):
    await display_tg_unban_stats(message, 2592000, "Last 30 Days")

@dp.message(Command("reset"))
async def reset_cmd(message: types.Message):
    args = (message.text or "").split(maxsplit=1)
    window_map = {
        "2h": (7200, "2 Hours"),
        "1day": (86400, "24 Hours"),
        "7day": (604800, "7 Days"),
        "1month": (2592000, "30 Days")
    }
    if len(args) < 2 or args[1].lower() not in window_map:
        return await message.answer("❌ Usage: <code>/reset &lt;2h / 1day / 7day / 1month&gt;</code>", parse_mode=ParseMode.HTML)
    sec, label = window_map[args[1].lower()]
    deleted = database.reset_unban_stats(message.chat.id, sec)
    await message.answer(f"🔄 Reset unban stats for the last <b>{label}</b> (cleared {deleted} entries).", parse_mode=ParseMode.HTML)

@dp.message(Command("ping"))
async def ping_cmd(message: types.Message):
    start = time.time()
    msg = await message.answer("🏓 Pinging...")
    latency = int((time.time() - start) * 1000)
    await msg.edit_text(f"🏓 <b>Pong!</b> Latency: <code>{latency}ms</code>. Bot is fully operational.", parse_mode=ParseMode.HTML)

# --- MAIN RUNNER ---

async def main():
    global bot, checker, profile_http_session
    database.init_db()

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or token.startswith("your_"):
        logging.error("BOT_TOKEN is missing or placeholder! Please set it in your environment variables.")
        return

    env_dict = dict(os.environ)
    if "PROXIES" not in env_dict and "PROXY" in env_dict:
        env_dict["PROXIES"] = env_dict["PROXY"]
    try:
        settings = Settings.from_env(env_dict)
    except Exception as e:
        logging.error(f"Configuration error: {e}")
        return

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    profile_http_session = profile_session(settings)
    checker = ProfileChecker(profile_http_session, settings)

    asyncio.create_task(tg_monitoring_worker())

    logging.info("Starting Telegram bot polling...")
    try:
        await dp.start_polling(bot)
    finally:
        if profile_http_session:
            await profile_http_session.close()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

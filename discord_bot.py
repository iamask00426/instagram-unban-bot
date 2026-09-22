from __future__ import annotations

import os
import re
import asyncio
import logging
from datetime import datetime
from io import BytesIO
from urllib.parse import urlsplit

import discord
from discord.ext import commands
from dotenv import load_dotenv

import database
from config import Settings
from monitoring import ProfileChecker, profile_session, CheckResult
from card_generator import create_profile_card
from telegram_forwarder import forward_to_telegram

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
checker = None
profile_http_session = None

def parse_usernames(text: str) -> list[str]:
    """
    Extract ONLY valid Instagram usernames from multiline text, URLs, or handles.
    """
    raw_tokens = re.split(r'[\r\n,\s]+', text)
    usernames = []
    seen = set()

    for token in raw_tokens:
        token = token.strip()
        if not token:
            continue

        # If it's an explicit URL
        if token.startswith(('http://', 'https://')):
            try:
                parsed = urlsplit(token)
                if parsed.hostname not in {'instagram.com', 'www.instagram.com'}:
                    continue
                path = parsed.path.strip('/')
                parts = [p for p in path.split('/') if p]
                if parts:
                    token = parts[0]
                else:
                    continue
            except Exception:
                continue
        elif 'instagram.com/' in token:
            try:
                parsed = urlsplit('https://' + token)
                path = parsed.path.strip('/')
                parts = [p for p in path.split('/') if p]
                if parts:
                    token = parts[0]
                else:
                    continue
            except Exception:
                continue

        token = token.split('?')[0].split('&')[0].strip('/')
        if token.startswith('@'):
            token = token[1:]

        token = token.lower()
        if re.fullmatch(r'[a-zA-Z0-9_.]{1,30}', token):
            if token not in seen:
                seen.add(token)
                usernames.append(token)

    return usernames

_alerting_keys: set[tuple[str, int]] = set()

def truncate_list_for_embed(items: list[str], max_chars: int = 1000) -> str:
    if not items:
        return "None"
    result = []
    current_len = 0
    for idx, item in enumerate(items):
        formatted = f"`{item}`"
        needed = len(formatted) + (1 if result else 0)
        remaining = len(items) - idx
        suffix = f" ... and {remaining} more"
        if current_len + needed + len(suffix) > max_chars:
            result.append(f"... and {remaining} more")
            break
        result.append(formatted)
        current_len += needed
    return " ".join(result)

def format_elapsed(start_time: datetime) -> str:
    elapsed = datetime.now() - start_time
    h, rem = divmod(int(elapsed.total_seconds()), 3600)
    m, s = divmod(rem, 60)
    return f"{h} hours, {m} minutes, {s} seconds"

async def get_target_channel(guild: discord.Guild, monitor_data: dict, mode: str) -> discord.TextChannel | None:
    """
    Resolve channel destination using hierarchy:
    1. Explicit DB setting (set via !setunbanchannel / !setbanchannel / !settickchannel)
    2. Specific channel names in guild (#unbans-here / #bans-here / #ticks-here)
    3. Environment variables (DISCORD_UNBAN_CHANNEL_ID / DISCORD_BAN_CHANNEL_ID / DISCORD_TICK_CHANNEL_ID)
    4. Channel where the monitor command was initiated
    """
    if not guild:
        return None

    settings = database.get_channels(guild.id)
    target_id = settings.get(f"{mode}_channel_id")
    if target_id:
        ch = guild.get_channel(target_id)
        if ch:
            return ch

    # Search channel by name
    if mode == "unban":
        target_names = ["unbans-here", "unbans", "unban-alerts"]
    elif mode == "ban":
        target_names = ["bans-here", "bans", "ban-alerts"]
    else:  # tick
        target_names = ["ticks-here", "ticks", "tick-alerts", "unbans-here"]

    for name in target_names:
        for ch in guild.text_channels:
            if ch.name.lower() == name:
                return ch

    # Check environment variable overrides
    env_map = {
        "unban": "DISCORD_UNBAN_CHANNEL_ID",
        "ban": "DISCORD_BAN_CHANNEL_ID",
        "tick": "DISCORD_TICK_CHANNEL_ID"
    }
    env_ch_id = os.getenv(env_map.get(mode, ""), "").strip()
    if env_ch_id.isdigit():
        ch = guild.get_channel(int(env_ch_id))
        if ch:
            return ch

    # Fallback to originating command channel
    origin_id = monitor_data.get("channel_id")
    if origin_id:
        return guild.get_channel(origin_id)

    return guild.text_channels[0] if guild.text_channels else None

async def deliver_alert_to_channel(ch: discord.TextChannel, content: str, card_io: BytesIO | None, style: int) -> bool:
    try:
        if style == 3 or not card_io:
            await ch.send(content=content)
            return True

        card_io.seek(0)
        file = discord.File(card_io, filename="card.png")
        if style == 2:
            embed = discord.Embed(description=content, color=0x2b2d31)
            embed.set_image(url="attachment://card.png")
            await ch.send(embed=embed, file=file)
        else:
            embed = discord.Embed(color=0x2b2d31)
            embed.set_image(url="attachment://card.png")
            await ch.send(content=content, embed=embed, file=file)
        return True
    except discord.Forbidden:
        try:
            await ch.send(content=content)
            return True
        except Exception:
            return False
    except Exception as e:
        logging.error(f"Failed delivering alert to {ch}: {e}")
        return False

async def forward_alert_if_configured(guild_id: int, text: str, card_io: BytesIO | None = None):
    if not guild_id:
        return
    st = database.get_server_settings(guild_id)
    token = st.get("tg_bot_token")
    chat_id = st.get("tg_chat_id")
    if token and chat_id:
        clean_text = text.replace("**", "<b>").replace("**", "</b>").replace("*", "<i>").replace("*", "</i>")
        await forward_to_telegram(token, chat_id, clean_text, card_io)

async def send_unban_alert(guild: discord.Guild, monitor_data: dict, result, is_fake: bool = False, custom_time: str = None):
    guild_id = monitor_data.get("guild_id", 0)
    username_key = (monitor_data["username"], guild_id)
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

        settings = database.get_server_settings(guild_id) if guild_id else {}
        style = settings.get("style", 1)

        channel = await get_target_channel(guild, monitor_data, "unban")

        content = (
            f"Account Recovered | [@{raw_user}](https://instagram.com/{raw_user}) 🏆✅\n"
            f"Followers: {result.followers} | Following: {result.following}\n"
            f"⏱️ *Time taken: {elapsed_str}*"
        )
        if style == 4:
            content = (
                f"⚰️ **BACK FROM THE GRAVE!** 🧟‍♂️\n"
                f"Account Recovered | [@{raw_user}](https://instagram.com/{raw_user}) 🏆✅\n"
                f"Followers: {result.followers} | Following: {result.following}\n"
                f"⏱️ *Time taken: {elapsed_str}*"
            )

        target_channels = []
        if channel:
            target_channels.append(channel)
        origin_id = monitor_data.get("channel_id")
        if guild and origin_id:
            origin_ch = guild.get_channel(origin_id)
            if origin_ch and origin_ch not in target_channels:
                target_channels.append(origin_ch)

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

        sent = False
        for ch in target_channels:
            if await deliver_alert_to_channel(ch, content, card_io, style):
                sent = True
                break

        if not sent:
            logging.error(f"Could not deliver unban alert for @{raw_user}")

        if guild_id and not is_fake:
            database.record_unban_stat(guild_id, monitor_data["username"], raw_user)

        if card_io:
            card_io.seek(0)
        asyncio.create_task(forward_alert_if_configured(guild_id, content, card_io))

        if not is_fake:
            database.remove_monitor(monitor_data["username"], guild_id)
            if settings.get("autoban"):
                database.add_monitors([raw_user], "ban", guild_id, monitor_data.get("channel_id", 0), 0)
                logging.info(f"Autoban engaged for @{raw_user} in guild {guild_id}")
    finally:
        if not is_fake:
            _alerting_keys.discard(username_key)

async def send_ban_alert(guild: discord.Guild, monitor_data: dict, is_fake: bool = False):
    guild_id = monitor_data.get("guild_id", 0)
    username_key = (monitor_data["username"], guild_id)
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

        settings = database.get_server_settings(guild_id) if guild_id else {}
        style = settings.get("style", 1)

        channel = await get_target_channel(guild, monitor_data, "ban")

        content = (
            f"🚨 **Super-Fast Ban Alert!**\n\n"
            f"[@{raw_user}](https://instagram.com/{raw_user}) has been **BANNED/DISABLED**!\n"
            f"*Status: UserNotFound*\n"
            f"⏱️ *Time taken: {elapsed_str}*"
        )

        target_channels = []
        if channel:
            target_channels.append(channel)
        origin_id = monitor_data.get("channel_id")
        if guild and origin_id:
            origin_ch = guild.get_channel(origin_id)
            if origin_ch and origin_ch not in target_channels:
                target_channels.append(origin_ch)

        card_io = None
        if style != 3:
            try:
                card_io = await create_profile_card(
                    raw_user, 0, 0, 0, pic_url=None, is_unavailable=True, full_name="UserNotFound", style=style
                )
            except Exception as e:
                logging.error(f"Error creating ban card for @{raw_user}: {e}")

        sent = False
        for ch in target_channels:
            if await deliver_alert_to_channel(ch, content, card_io, style):
                sent = True
                break

        if not sent:
            logging.error(f"Could not deliver ban alert for @{raw_user}")

        if card_io:
            card_io.seek(0)
        asyncio.create_task(forward_alert_if_configured(guild_id, content, card_io))

        if not is_fake:
            database.remove_monitor(monitor_data["username"], guild_id)
    finally:
        if not is_fake:
            _alerting_keys.discard(username_key)

async def send_tick_alert(guild: discord.Guild, monitor_data: dict, result):
    guild_id = monitor_data.get("guild_id", 0)
    username_key = (monitor_data["username"], guild_id)
    if username_key in _alerting_keys:
        return
    _alerting_keys.add(username_key)
    try:
        raw_user = monitor_data["raw_username"]
        start_time = datetime.fromisoformat(monitor_data["created_at"]) if isinstance(monitor_data["created_at"], str) else monitor_data["created_at"]
        elapsed_str = format_elapsed(start_time)

        settings = database.get_server_settings(guild_id) if guild_id else {}
        style = settings.get("style", 1)

        channel = await get_target_channel(guild, monitor_data, "tick")

        content = (
            f"🎉 **Verification Tick Detected!** | [@{raw_user}](https://instagram.com/{raw_user}) 🏆✅\n"
            f"Followers: {result.followers} | Following: {result.following}\n"
            f"⏱️ *Time taken: {elapsed_str}*"
        )

        target_channels = []
        if channel:
            target_channels.append(channel)
        origin_id = monitor_data.get("channel_id")
        if guild and origin_id:
            origin_ch = guild.get_channel(origin_id)
            if origin_ch and origin_ch not in target_channels:
                target_channels.append(origin_ch)

        full_name = getattr(result, "full_name", "") or raw_user
        card_io = None
        if style != 3:
            try:
                card_io = await create_profile_card(
                    raw_user, result.followers, result.posts, result.following,
                    pic_url=result.pic_url, is_unavailable=False, full_name=full_name,
                    is_verified=True, style=style
                )
            except Exception as e:
                logging.error(f"Error creating tick card for @{raw_user}: {e}")

        sent = False
        for ch in target_channels:
            if await deliver_alert_to_channel(ch, content, card_io, style):
                sent = True
                break

        if not sent:
            logging.error(f"Could not deliver tick alert for @{raw_user}")

        if card_io:
            card_io.seek(0)
        asyncio.create_task(forward_alert_if_configured(guild_id, content, card_io))

        database.remove_monitor(monitor_data["username"], guild_id)
    finally:
        _alerting_keys.discard(username_key)

async def instant_check(guild: discord.Guild, monitor_data: dict):
    """Executes immediate verification within 1-2s of command."""
    if not checker:
        return
    username = monitor_data["username"]
    mode = monitor_data["mode"]
    try:
        res = await checker.check(username)
        if mode == "unban" and res.status == "active":
            await send_unban_alert(guild, monitor_data, res)
        elif mode == "ban" and res.status == "unavailable":
            await send_ban_alert(guild, monitor_data)
        elif mode == "tick" and res.status == "active" and getattr(res, "is_verified", False):
            await send_tick_alert(guild, monitor_data, res)
    except Exception as e:
        logging.debug(f"Instant check exception for @{username}: {e}")

async def monitoring_worker():
    """Background monitoring loop that continuously cycles through stored accounts."""
    await bot.wait_until_ready()
    logging.info("Discord monitoring loop worker started.")
    
    while not bot.is_closed():
        try:
            records = database.get_all_monitors()
            if not records:
                await asyncio.sleep(2.0)
                continue

            for rec in records:
                if bot.is_closed():
                    break
                username = rec["username"]
                mode = rec["mode"]
                guild_id = rec["guild_id"]
                guild = bot.get_guild(guild_id)

                try:
                    res = await checker.check(username)
                    if mode == "unban" and res.status == "active":
                        await send_unban_alert(guild, rec, res)
                    elif mode == "ban" and res.status == "unavailable":
                        await send_ban_alert(guild, rec)
                    elif mode == "tick" and res.status == "active" and getattr(res, "is_verified", False):
                        await send_tick_alert(guild, rec, res)
                except Exception as e:
                    logging.debug(f"Error checking @{username}: {e}")

                await asyncio.sleep(1.0)

            await asyncio.sleep(5.0)

        except Exception as e:
            logging.error(f"Unexpected error in Discord monitor worker: {e}")
            await asyncio.sleep(5.0)

# --- BOT COMMANDS ---

@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user.name} ({bot.user.id})")
    database.init_db()

@bot.command(name="unban", aliases=["bulk", "monitor"])
async def unban_command(ctx, *, raw_input: str = None):
    if not raw_input:
        return await ctx.send("❌ Usage: `!unban <list of usernames or links>`")

    usernames = parse_usernames(raw_input)
    if not usernames:
        return await ctx.send("❌ No valid Instagram usernames found in your input.")

    guild_id = ctx.guild.id if ctx.guild else 0
    channel_id = ctx.channel.id
    added = database.add_monitors(usernames, "unban", guild_id, channel_id, ctx.author.id)

    embed = discord.Embed(
        title="Monitoring Started",
        description=f"Monitoring for {len(added)} username(s) has started.",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)

    for u in added:
        mon_rec = {
            "username": u.lower(),
            "raw_username": u,
            "mode": "unban",
            "guild_id": guild_id,
            "channel_id": channel_id,
            "created_at": datetime.now()
        }
        asyncio.create_task(instant_check(ctx.guild, mon_rec))

@bot.command(name="ban", aliases=["banmonitor"])
async def ban_command(ctx, *, raw_input: str = None):
    if not raw_input:
        return await ctx.send("❌ Usage: `!ban <list of usernames or links>`")

    usernames = parse_usernames(raw_input)
    if not usernames:
        return await ctx.send("❌ No valid Instagram usernames found in your input.")

    guild_id = ctx.guild.id if ctx.guild else 0
    channel_id = ctx.channel.id
    added = database.add_monitors(usernames, "ban", guild_id, channel_id, ctx.author.id)

    embed = discord.Embed(
        title="Monitoring Started",
        description=f"Monitoring for {len(added)} username(s) has started.",
        color=discord.Color.red()
    )
    await ctx.send(embed=embed)

    for u in added:
        mon_rec = {
            "username": u.lower(),
            "raw_username": u,
            "mode": "ban",
            "guild_id": guild_id,
            "channel_id": channel_id,
            "created_at": datetime.now()
        }
        asyncio.create_task(instant_check(ctx.guild, mon_rec))

@bot.command(name="tick")
async def tick_command(ctx, *, raw_input: str = None):
    if not raw_input:
        return await ctx.send("❌ Usage: `!tick <list of usernames or links>`")

    usernames = parse_usernames(raw_input)
    if not usernames:
        return await ctx.send("❌ No valid Instagram usernames found in your input.")

    guild_id = ctx.guild.id if ctx.guild else 0
    channel_id = ctx.channel.id
    added = database.add_monitors(usernames, "tick", guild_id, channel_id, ctx.author.id)

    embed = discord.Embed(
        title="Tick Monitoring Started",
        description=f"Monitoring for {len(added)} username(s) for verification tick has started.",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed)

    for u in added:
        mon_rec = {
            "username": u.lower(),
            "raw_username": u,
            "mode": "tick",
            "guild_id": guild_id,
            "channel_id": channel_id,
            "created_at": datetime.now()
        }
        asyncio.create_task(instant_check(ctx.guild, mon_rec))

@bot.command(name="list", aliases=["active"])
async def list_command(ctx, *, target: str = None):
    guild_id = ctx.guild.id if ctx.guild else 0
    records = database.get_all_monitors()
    if guild_id:
        records = [r for r in records if r["guild_id"] == guild_id]

    if target:
        parsed = parse_usernames(target)
        if not parsed:
            return await ctx.send("❌ Please provide a valid username or URL to check.")
        search_user = parsed[0].lower()
        match = next((r for r in records if r["username"] == search_user), None)
        if match:
            st = datetime.fromisoformat(match["created_at"]) if isinstance(match["created_at"], str) else match["created_at"]
            elapsed = format_elapsed(st)
            mode_disp = "🏆 Unban" if match["mode"] == "unban" else ("🚨 Ban" if match["mode"] == "ban" else "✅ Tick")
            embed = discord.Embed(
                title=f"Monitor Status: @{match['raw_username']}",
                color=discord.Color.green()
            )
            embed.add_field(name="Mode", value=mode_disp, inline=True)
            embed.add_field(name="Running Time", value=elapsed, inline=True)
            return await ctx.send(embed=embed)
        else:
            return await ctx.send(f"❌ `@{search_user}` is not currently being monitored in this server.")

    if not records:
        return await ctx.send("ℹ️ No accounts are currently being monitored.")

    lines = []
    for r in records:
        mode_icon = "🏆 Unban" if r["mode"] == "unban" else ("🚨 Ban" if r["mode"] == "ban" else "✅ Tick")
        st = datetime.fromisoformat(r["created_at"]) if isinstance(r["created_at"], str) else r["created_at"]
        elapsed = format_elapsed(st)
        lines.append(f"• **@{r['raw_username']}** ({mode_icon}) — *Running for {elapsed}*")

    embed = discord.Embed(
        title=f"Active Monitors ({len(records)})",
        description="\n".join(lines[:25]),
        color=discord.Color.purple()
    )
    if len(records) > 25:
        embed.set_footer(text=f"Showing first 25 of {len(records)} active accounts")
    await ctx.send(embed=embed)

@bot.command(name="kill")
async def kill_command(ctx, *, raw_input: str = None):
    if not raw_input:
        return await ctx.send("❌ Usage: `!kill <username or url>`")
    parsed = parse_usernames(raw_input)
    if not parsed:
        return await ctx.send("❌ No valid Instagram username found.")
    target_user = parsed[0]
    mon_data = {
        "username": target_user.lower(),
        "raw_username": target_user,
        "guild_id": ctx.guild.id if ctx.guild else 0,
        "channel_id": ctx.channel.id,
        "created_at": datetime.now()
    }
    await send_ban_alert(ctx.guild, mon_data, is_fake=True)
    await ctx.send(f"💀 Fake ban alert dispatched for `@{target_user}`.")

@bot.command(name="fakeunban")
async def fakeunban_command(ctx, username: str = None, followers: str = None, *, elapsed_time: str = None):
    if not username or not followers:
        return await ctx.send("❌ Usage: `!fakeunban <username> <followers> [HH:MM:SS]`\nExample: `!fakeunban zuck 12.5M 01:23:45`")
    parsed = parse_usernames(username)
    if not parsed:
        return await ctx.send("❌ Invalid username.")
    target_user = parsed[0]

    if elapsed_time:
        parts = elapsed_time.strip().split(":")
        if len(parts) == 3 and all(p.strip().isdigit() for p in parts):
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            time_str = f"{h} hours, {m} minutes, {s} seconds"
        else:
            time_str = elapsed_time.strip()
    else:
        time_str = "12 hours, 34 minutes, 56 seconds"

    # Fetch live profile data if available so card has real avatar (pp), real posts, following & full name
    posts = "0"
    following = "0"
    pic_url = None
    full_name = target_user
    is_verified = False

    if checker:
        try:
            live_res = await asyncio.wait_for(checker.check(target_user.lower()), timeout=7.0)
            if live_res and live_res.status == "active":
                posts = live_res.posts if live_res.posts and live_res.posts != "?" else "0"
                following = live_res.following if live_res.following and live_res.following != "?" else "0"
                pic_url = live_res.pic_url
                full_name = live_res.full_name or target_user
                is_verified = live_res.is_verified
        except Exception as e:
            logging.warning(f"Could not fetch live profile for fakeunban @{target_user}: {e}")

    res = CheckResult(
        status="active",
        username=target_user.lower(),
        followers=followers,
        following=following,
        posts=posts,
        pic_url=pic_url,
        full_name=full_name,
        is_verified=is_verified
    )

    mon_data = {
        "username": target_user.lower(),
        "raw_username": target_user,
        "guild_id": ctx.guild.id if ctx.guild else 0,
        "channel_id": ctx.channel.id,
        "created_at": datetime.now()
    }
    await send_unban_alert(ctx.guild, mon_data, res, is_fake=True, custom_time=time_str)
    await ctx.send(f"🏆 Fake unban alert dispatched for `@{target_user}`.")

@bot.command(name="lookup")
async def lookup_command(ctx, *, raw_input: str = None):
    if not raw_input:
        return await ctx.send("❌ Usage: `!lookup <username or url>`")
    parsed = parse_usernames(raw_input)
    if not parsed:
        return await ctx.send("❌ Invalid username or URL.")
    target_user = parsed[0]

    msg = await ctx.send(f"🔍 Looking up `@{target_user}` on Instagram...")
    try:
        res = await checker.check(target_user.lower())
        if res.status == "active":
            embed = discord.Embed(
                title=f"Instagram Profile: @{target_user}",
                url=f"https://instagram.com/{target_user}",
                color=discord.Color.green()
            )
            embed.add_field(name="Status", value="✅ Active / Available", inline=True)
            embed.add_field(name="Full Name", value=getattr(res, "full_name", "") or target_user, inline=True)
            embed.add_field(name="Verified", value="Yes (Blue Tick) ✅" if getattr(res, "is_verified", False) else "No", inline=True)
            embed.add_field(name="Followers", value=str(res.followers), inline=True)
            embed.add_field(name="Following", value=str(res.following), inline=True)
            embed.add_field(name="Posts", value=str(res.posts), inline=True)
            if res.pic_url:
                embed.set_thumbnail(url=res.pic_url)
            await msg.edit(content=None, embed=embed)
        elif res.status == "unavailable":
            embed = discord.Embed(
                title=f"Instagram Profile: @{target_user}",
                description="🚨 **Status: UserNotFound (Banned / Disabled / Available)**",
                color=discord.Color.red()
            )
            await msg.edit(content=None, embed=embed)
        else:
            await msg.edit(content=f"⚠️ Profile `@{target_user}` check result: `{res.status}`")
    except Exception as e:
        await msg.edit(content=f"❌ Error checking profile: {e}")

@bot.command(name="clear")
async def clear_command(ctx, *, raw_input: str = None):
    if not raw_input:
        return await ctx.send("❌ Usage: `!clear <list of usernames or links>`")

    usernames = parse_usernames(raw_input)
    if not usernames:
        return await ctx.send("❌ No valid usernames to clear.")

    guild_id = ctx.guild.id if ctx.guild else 0
    cleared, not_monitored = database.remove_monitors(usernames, guild_id)

    embed = discord.Embed(
        title="Cleared Monitors",
        color=discord.Color.blue()
    )

    cleared_text = truncate_list_for_embed(cleared, max_chars=1000)
    not_mon_text = truncate_list_for_embed(not_monitored, max_chars=1000)

    embed.add_field(name=f"Cleared [{len(cleared)}] username(s):", value=cleared_text, inline=False)
    embed.add_field(name=f"Not being monitored ([{len(not_monitored)}]):", value=not_mon_text, inline=False)

    await ctx.send(embed=embed)

@bot.command(name="clearall")
@commands.has_permissions(manage_channels=True)
async def clear_all_command(ctx):
    if not ctx.guild:
        return await ctx.send("❌ This command must be used within a server.")
    count = database.clear_all_monitors(ctx.guild.id)
    embed = discord.Embed(
        title="Cleared All Monitors",
        description=f"🗑️ Successfully removed all **{count}** monitored account(s) for this server.",
        color=discord.Color.orange()
    )
    await ctx.send(embed=embed)

@bot.command(name="tg")
@commands.has_permissions(manage_channels=True)
async def tg_command(ctx, bot_token: str = None, user_id: str = None):
    if not ctx.guild:
        return await ctx.send("❌ This command must be used within a server.")
    if not bot_token or not user_id:
        return await ctx.send("❌ Usage: `!tg <bot_token> <user_id>`\nExample: `!tg 123456:ABC-DEF 987654321`")

    database.set_server_setting(ctx.guild.id, "tg_bot_token", bot_token.strip())
    database.set_server_setting(ctx.guild.id, "tg_chat_id", user_id.strip())

    asyncio.create_task(forward_to_telegram(
        bot_token, user_id, f"✅ <b>Telegram Linked!</b> Notifications for Discord server <b>{ctx.guild.name}</b> are now active."
    ))

    embed = discord.Embed(
        title="Telegram Notifications Linked",
        description=f"✅ Alerts will now be cross-posted to Telegram User/Chat ID: `{user_id}`.",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed)

@bot.command(name="style")
@commands.has_permissions(manage_channels=True)
async def style_command(ctx, style_choice: int = None):
    if not ctx.guild:
        return await ctx.send("❌ This command must be used within a server.")
    if style_choice not in [1, 2, 3, 4]:
        embed = discord.Embed(
            title="Notification Style Options",
            description=(
                "**1** = Text + Card Image Attachment (Default)\n"
                "**2** = Embed Frame Container\n"
                "**3** = Text Only (No Image)\n"
                "**4** = Back From The Grave (Custom Banner)"
            ),
            color=discord.Color.gold()
        )
        embed.set_footer(text="Usage: !style <1/2/3/4>")
        return await ctx.send(embed=embed)

    database.set_server_setting(ctx.guild.id, "style", style_choice)
    desc_map = {
        1: "Text + Card Image Attachment (Default)",
        2: "Embed Frame Container",
        3: "Text Only (Fast & Bandwidth-light)",
        4: "Back From The Grave (Grave Banner)"
    }
    await ctx.send(f"🎨 Alert style updated to **Style {style_choice}** — *{desc_map[style_choice]}*.")

@bot.command(name="autoban")
@commands.has_permissions(manage_channels=True)
async def autoban_command(ctx, setting: str = None):
    if not ctx.guild:
        return await ctx.send("❌ This command must be used within a server.")
    if not setting or setting.lower() not in ["on", "off"]:
        return await ctx.send("❌ Usage: `!autoban on` or `!autoban off`")

    is_on = setting.lower() == "on"
    database.set_server_setting(ctx.guild.id, "autoban", 1 if is_on else 0)
    status_str = "ENABLED ✅ (Recovered accounts will be auto-monitored for 24h ban)" if is_on else "DISABLED ❌"
    await ctx.send(f"🛡️ Auto-ban watch is now **{status_str}**.")

@bot.command(name="ping")
async def ping_command(ctx):
    latency = round(bot.latency * 1000)
    await ctx.send(f"🏓 Pong! Latency: `{latency}ms`")

async def display_unban_stats(ctx, window_seconds: int, window_label: str):
    if not ctx.guild:
        return await ctx.send("❌ This command must be used within a server.")
    stats = database.get_unban_stats(ctx.guild.id, window_seconds)
    total = len(stats)

    embed = discord.Embed(
        title=f"🏆 Unban Stats — Last {window_label}",
        color=discord.Color.green()
    )
    embed.add_field(name="Total Recoveries", value=f"**{total}** account(s)", inline=False)
    if stats:
        sample_users = " ".join(f"`@{s['raw_username']}`" for s in stats[:20])
        if total > 20:
            sample_users += f" ... and {total - 20} more"
        embed.add_field(name="Recovered Accounts", value=sample_users, inline=False)
        latest_time = stats[0]["unbanned_at"]
        embed.set_footer(text=f"Latest Recovery: {latest_time}")
    else:
        embed.add_field(name="Recovered Accounts", value="None recorded in this window.", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="2h")
async def stats_2h(ctx):
    await display_unban_stats(ctx, 7200, "2 Hours")

@bot.command(name="1day")
async def stats_1day(ctx):
    await display_unban_stats(ctx, 86400, "24 Hours")

@bot.command(name="7day")
async def stats_7day(ctx):
    await display_unban_stats(ctx, 604800, "7 Days")

@bot.command(name="1month")
async def stats_1month(ctx):
    await display_unban_stats(ctx, 2592000, "30 Days")

@bot.command(name="reset")
@commands.has_permissions(manage_channels=True)
async def reset_stats_cmd(ctx, window: str = None):
    if not ctx.guild:
        return await ctx.send("❌ This command must be used within a server.")
    window_map = {
        "2h": (7200, "2 Hours"),
        "1day": (86400, "24 Hours"),
        "7day": (604800, "7 Days"),
        "1month": (2592000, "30 Days")
    }
    if not window or window.lower() not in window_map:
        return await ctx.send("❌ Usage: `!reset <2h / 1day / 7day / 1month>`")

    sec, label = window_map[window.lower()]
    deleted = database.reset_unban_stats(ctx.guild.id, sec)
    await ctx.send(f"🔄 Reset unban stats for the last **{label}** (cleared {deleted} entry/entries).")

@bot.command(name="setunbanchannel")
@commands.has_permissions(manage_channels=True)
async def set_unban_channel_cmd(ctx, channel: discord.TextChannel = None):
    if not ctx.guild:
        return await ctx.send("❌ This command must be used within a server.")
    target = channel or ctx.channel
    database.set_channel(ctx.guild.id, "unban", target.id)
    await ctx.send(f"✅ Recovery alerts will now route to {target.mention} (`#unbans-here`).")

@bot.command(name="setbanchannel")
@commands.has_permissions(manage_channels=True)
async def set_ban_channel_cmd(ctx, channel: discord.TextChannel = None):
    if not ctx.guild:
        return await ctx.send("❌ This command must be used within a server.")
    target = channel or ctx.channel
    database.set_channel(ctx.guild.id, "ban", target.id)
    await ctx.send(f"✅ Ban alerts will now route to {target.mention} (`#bans-here`).")

@bot.command(name="settickchannel")
@commands.has_permissions(manage_channels=True)
async def set_tick_channel_cmd(ctx, channel: discord.TextChannel = None):
    if not ctx.guild:
        return await ctx.send("❌ This command must be used within a server.")
    target = channel or ctx.channel
    database.set_channel(ctx.guild.id, "tick", target.id)
    await ctx.send(f"✅ Tick alerts will now route to {target.mention} (`#ticks-here`).")

@bot.command(name="channels")
async def show_channels_cmd(ctx):
    if not ctx.guild:
        return await ctx.send("❌ This command must be used within a server.")
    settings = database.get_channels(ctx.guild.id)
    unban_ch = ctx.guild.get_channel(settings.get("unban_channel_id") or 0)
    ban_ch = ctx.guild.get_channel(settings.get("ban_channel_id") or 0)
    tick_ch = ctx.guild.get_channel(settings.get("tick_channel_id") or 0)

    unban_disp = unban_ch.mention if unban_ch else "#unbans-here (Default / Auto-detect)"
    ban_disp = ban_ch.mention if ban_ch else "#bans-here (Default / Auto-detect)"
    tick_disp = tick_ch.mention if tick_ch else "#ticks-here (Default / Auto-detect)"

    embed = discord.Embed(title="Channel Routing Settings", color=discord.Color.gold())
    embed.add_field(name="Unbans Channel", value=unban_disp, inline=False)
    embed.add_field(name="Bans Channel", value=ban_disp, inline=False)
    embed.add_field(name="Ticks Channel", value=tick_disp, inline=False)
    await ctx.send(embed=embed)

@bot.command(name="help")
async def help_command(ctx):
    embed = discord.Embed(
        title="🤖 Instagram Monitor Bot — Command List",
        description="Replace 'username' with command target (e.g. `!ban zuck`).",
        color=discord.Color.gold()
    )
    embed.add_field(
        name="⚡ Monitoring Commands",
        value=(
            "• `!unban <usernames/urls>` — Monitor and notify when Unbanned\n"
            "• `!ban <usernames/urls>` — Monitor and notify when Done / Banned\n"
            "• `!tick <usernames/urls>` — Monitor for verification tick\n"
            "• `!list [username/url]` — Show active monitors or check a specific username\n"
            "• `!clear <username>` — Remove username from monitoring\n"
            "• `!clearall` — Remove ALL usernames from monitoring in this server"
        ),
        inline=False
    )
    embed.add_field(
        name="🎭 Simulation & Utility",
        value=(
            "• `!kill <username/url>` — Fake ban notification with card\n"
            "• `!fakeunban <user> <followers> [HH:MM:SS]` — Fake unban notification\n"
            "• `!lookup <username/url>` — Get all info about a profile (stats, avatar, etc.)\n"
            "• `!ping` — Check bot latency and heartbeat"
        ),
        inline=False
    )
    embed.add_field(
        name="📊 Unban Stats",
        value=(
            "• `!2h` — Unban stats for the last 2 hours\n"
            "• `!1day` — Unban stats for the last 24 hours\n"
            "• `!7day` — Unban stats for the last 7 days\n"
            "• `!1month` — Unban stats for the last 30 days\n"
            "• `!reset <2h|1day|7day|1month>` — Reset stats for that window"
        ),
        inline=False
    )
    embed.add_field(
        name="⚙️ Server Configuration (Admin)",
        value=(
            "• `!style 1/2/3/4` — Notification style (1=text+img, 2=embed, 3=text, 4=grave)\n"
            "• `!autoban on/off` — Auto ban-watch for 24h after recovery\n"
            "• `!tg <bot_token> <user_id>` — Set Telegram bot for alerts\n"
            "• `!setunbanchannel [#channel]` — Route unbans\n"
            "• `!setbanchannel [#channel]` — Route bans\n"
            "• `!settickchannel [#channel]` — Route ticks\n"
            "• `!channels` — View configured routing"
        ),
        inline=False
    )
    await ctx.send(embed=embed)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command (requires **Manage Channels**).")
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send("❌ The specified channel was not found.")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        logging.error(f"Command error in {ctx.command}: {error}")

async def main():
    global checker, profile_http_session
    database.init_db()

    env_dict = dict(os.environ)
    try:
        settings = Settings.from_env(env_dict)
    except ValueError as e:
        logging.error(f"Configuration error: {e}")
        return

    if not DISCORD_BOT_TOKEN:
        logging.error("DISCORD_BOT_TOKEN is missing! Set it in your .env or environment variables.")
        return

    profile_http_session = profile_session(settings)
    checker = ProfileChecker(profile_http_session, settings)

    asyncio.create_task(monitoring_worker())
    try:
        await bot.start(DISCORD_BOT_TOKEN)
    finally:
        if profile_http_session:
            await profile_http_session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

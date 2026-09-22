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
from monitoring import ProfileChecker, profile_session
from card_generator import create_profile_card

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
    1. Explicit DB setting (set via !setunbanchannel / !setbanchannel)
    2. Specific channel names in guild (#unbans-here / #bans-here)
    3. Environment variables (DISCORD_UNBAN_CHANNEL_ID / DISCORD_BAN_CHANNEL_ID)
    4. Channel where the monitor command was initiated
    """
    if not guild:
        return None

    settings = database.get_channels(guild.id)
    target_id = settings.get("unban_channel_id") if mode == "unban" else settings.get("ban_channel_id")
    if target_id:
        ch = guild.get_channel(target_id)
        if ch:
            return ch

    # Search channel by name
    target_names = ["unbans-here", "unbans", "unban-alerts"] if mode == "unban" else ["bans-here", "bans", "ban-alerts"]
    for name in target_names:
        for ch in guild.text_channels:
            if ch.name.lower() == name:
                return ch

    # Check environment variable overrides
    env_key = "DISCORD_UNBAN_CHANNEL_ID" if mode == "unban" else "DISCORD_BAN_CHANNEL_ID"
    env_ch_id = os.getenv(env_key, "").strip()
    if env_ch_id.isdigit():
        ch = guild.get_channel(int(env_ch_id))
        if ch:
            return ch

    # Fallback to originating command channel
    origin_id = monitor_data.get("channel_id")
    if origin_id:
        return guild.get_channel(origin_id)

    return guild.text_channels[0] if guild.text_channels else None

async def send_unban_alert(guild: discord.Guild, monitor_data: dict, result):
    username_key = (monitor_data["username"], monitor_data.get("guild_id", 0))
    if username_key in _alerting_keys:
        return
    _alerting_keys.add(username_key)
    try:
        raw_user = monitor_data["raw_username"]
        start_time = datetime.fromisoformat(monitor_data["created_at"]) if isinstance(monitor_data["created_at"], str) else monitor_data["created_at"]
        elapsed_str = format_elapsed(start_time)
        channel = await get_target_channel(guild, monitor_data, "unban")

        content = (
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
        card_io = None
        try:
            card_io = await create_profile_card(
                raw_user, result.followers, result.posts, result.following,
                pic_url=result.pic_url, is_unavailable=False, full_name=full_name
            )
        except Exception as e:
            logging.error(f"Error creating Discord card for @{raw_user}: {e}")

        sent = False
        for ch in target_channels:
            try:
                if card_io:
                    card_io.seek(0)
                    file = discord.File(card_io, filename="card.png")
                    embed = discord.Embed(color=0x2b2d31)
                    embed.set_image(url="attachment://card.png")
                    await ch.send(content=content, embed=embed, file=file)
                else:
                    await ch.send(content=content)
                sent = True
                break
            except discord.Forbidden:
                try:
                    await ch.send(content=content)
                    sent = True
                    break
                except Exception:
                    continue
            except Exception as e:
                logging.error(f"Failed sending alert to {ch}: {e}")
                continue

        if not sent:
            logging.error(f"Could not deliver unban alert for @{raw_user}")

        database.remove_monitor(monitor_data["username"], monitor_data.get("guild_id"))
    finally:
        _alerting_keys.discard(username_key)

async def send_ban_alert(guild: discord.Guild, monitor_data: dict):
    username_key = (monitor_data["username"], monitor_data.get("guild_id", 0))
    if username_key in _alerting_keys:
        return
    _alerting_keys.add(username_key)
    try:
        raw_user = monitor_data["raw_username"]
        start_time = datetime.fromisoformat(monitor_data["created_at"]) if isinstance(monitor_data["created_at"], str) else monitor_data["created_at"]
        elapsed_str = format_elapsed(start_time)
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
        try:
            card_io = await create_profile_card(
                raw_user, 0, 0, 0, pic_url=None, is_unavailable=True, full_name="UserNotFound"
            )
        except Exception as e:
            logging.error(f"Error creating ban card for @{raw_user}: {e}")

        sent = False
        for ch in target_channels:
            try:
                if card_io:
                    card_io.seek(0)
                    file = discord.File(card_io, filename="card.png")
                    embed = discord.Embed(color=0x2b2d31)
                    embed.set_image(url="attachment://card.png")
                    await ch.send(content=content, embed=embed, file=file)
                else:
                    await ch.send(content=content)
                sent = True
                break
            except discord.Forbidden:
                try:
                    await ch.send(content=content)
                    sent = True
                    break
                except Exception:
                    continue
            except Exception as e:
                logging.error(f"Failed sending ban alert to {ch}: {e}")
                continue

        if not sent:
            logging.error(f"Could not deliver ban alert for @{raw_user}")

        database.remove_monitor(monitor_data["username"], monitor_data.get("guild_id"))
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
                except Exception as e:
                    logging.debug(f"Error checking @{username}: {e}")

                # Bandwidth saving delay between individual checks
                await asyncio.sleep(1.0)

            # Polite delay between full sweeps
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

    # Trigger instant checks in background
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

    # Trigger instant checks in background
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

@bot.command(name="channels")
async def show_channels_cmd(ctx):
    if not ctx.guild:
        return await ctx.send("❌ This command must be used within a server.")
    settings = database.get_channels(ctx.guild.id)
    unban_ch = ctx.guild.get_channel(settings.get("unban_channel_id") or 0)
    ban_ch = ctx.guild.get_channel(settings.get("ban_channel_id") or 0)

    unban_disp = unban_ch.mention if unban_ch else "#unbans-here (Default / Auto-detect)"
    ban_disp = ban_ch.mention if ban_ch else "#bans-here (Default / Auto-detect)"

    embed = discord.Embed(title="Channel Routing Settings", color=discord.Color.gold())
    embed.add_field(name="Unbans Channel", value=unban_disp, inline=False)
    embed.add_field(name="Bans Channel", value=ban_disp, inline=False)
    await ctx.send(embed=embed)

@bot.command(name="active", aliases=["list"])
async def active_command(ctx):
    records = database.get_all_monitors()
    if ctx.guild:
        records = [r for r in records if r["guild_id"] == ctx.guild.id]

    if not records:
        return await ctx.send("ℹ️ No accounts are currently being monitored.")

    lines = []
    for r in records:
        mode_icon = "🏆 Unban" if r["mode"] == "unban" else "🚨 Ban"
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

@bot.command(name="help")
async def help_command(ctx):
    embed = discord.Embed(
        title="🤖 Instagram Monitor Bot - Commands",
        description="Monitor Instagram accounts for unbans or bans in real-time.",
        color=discord.Color.gold()
    )
    embed.add_field(
        name="🏆 `!unban <usernames/links>` (Aliases: `!bulk`, `!monitor`)",
        value="Start monitoring accounts for unban / recovery. Supports multi-line input & profile links.",
        inline=False
    )
    embed.add_field(
        name="🚨 `!ban <usernames/links>` (Aliases: `!banmonitor`)",
        value="Start monitoring active accounts for ban / deactivation.",
        inline=False
    )
    embed.add_field(
        name="🗑️ `!clear <usernames/links>`",
        value="Remove specified accounts from monitoring.",
        inline=False
    )
    embed.add_field(
        name="📋 `!active` (Alias: `!list`)",
        value="List all accounts currently being monitored with running timer.",
        inline=False
    )
    embed.add_field(
        name="⚙️ `!channels`",
        value="View current alert routing channel settings for this server.",
        inline=False
    )
    embed.add_field(
        name="🔧 Channel Configuration (Admin)",
        value="• `!setunbanchannel [#channel]` - Route recovery alerts\n• `!setbanchannel [#channel]` - Route ban alerts",
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

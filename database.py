"""
Async SQLite database layer for high-performance non-blocking operations.
Replaces blocking sqlite3 calls with aiosqlite for true async I/O.
"""
import aiosqlite
import asyncio
from contextlib import asynccontextmanager
import os
import shutil
import logging
from datetime import datetime
from typing import List, Dict, Tuple, Optional

def resolve_db_path() -> str:
    """Determine optimal database path (persistent volume or local)."""
    env_path = os.getenv("SQLITE_DB_PATH")
    if env_path:
        d = os.path.dirname(os.path.abspath(env_path))
        if d:
            os.makedirs(d, exist_ok=True)
        return env_path

    candidate_dirs = ["/data", "/app/data", "/mnt/data"]
    default_local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitors.db")

    for c_dir in candidate_dirs:
        try:
            if os.path.exists(c_dir) and os.access(c_dir, os.W_OK):
                target = os.path.join(c_dir, "monitors.db")
                if os.path.exists(default_local) and not os.path.exists(target):
                    try:
                        shutil.copy2(default_local, target)
                        logging.info(f"Auto-migrated database from {default_local} to persistent volume at {target}")
                    except Exception as e:
                        logging.warning(f"Could not copy local DB to volume: {e}")
                return target
        except Exception:
            pass

    return default_local

DB_PATH = resolve_db_path()

_shared_conn = None
_conn_lock = asyncio.Lock()

@asynccontextmanager
async def get_connection():
    global _shared_conn
    async with _conn_lock:
        if _shared_conn is None:
            _shared_conn = await get_connection()
            await _shared_conn.execute("PRAGMA journal_mode = WAL")
            await _shared_conn.execute("PRAGMA synchronous = NORMAL")
            await _shared_conn.execute("PRAGMA busy_timeout = 30000")
            _shared_conn.row_factory = aiosqlite.Row
        yield _shared_conn

async def init_db():
    """Initialize database schema with async operations."""
    async with get_connection() as conn:
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA synchronous = NORMAL")
        await conn.execute("PRAGMA busy_timeout = 30000")

        cursor = await conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='monitors'")
        row = await cursor.fetchone()

        if row and ("'tick'" not in row[0] or "PRIMARY KEY (username, guild_id)" not in row[0]):
            await conn.execute("ALTER TABLE monitors RENAME TO monitors_old")
            await conn.execute("""
                CREATE TABLE monitors (
                    username TEXT NOT NULL,
                    raw_username TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('unban', 'ban', 'tick')),
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    added_by INTEGER NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (username, guild_id)
                )
            """)
            await conn.execute("""
                INSERT OR IGNORE INTO monitors (username, raw_username, mode, guild_id, channel_id, added_by, created_at)
                SELECT username, raw_username, mode, guild_id, channel_id, added_by, created_at FROM monitors_old
            """)
            await conn.execute("DROP TABLE monitors_old")
        elif not row:
            await conn.execute("""
                CREATE TABLE monitors (
                    username TEXT NOT NULL,
                    raw_username TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('unban', 'ban', 'tick')),
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    added_by INTEGER NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (username, guild_id)
                )
            """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS server_settings (
                guild_id INTEGER PRIMARY KEY,
                unban_channel_id INTEGER,
                ban_channel_id INTEGER,
                tick_channel_id INTEGER,
                usernamechange_channel_id INTEGER,
                style INTEGER DEFAULT 1,
                autoban INTEGER DEFAULT 0,
                tg_bot_token TEXT,
                tg_chat_id TEXT
            )
        """)

        # Add columns if missing
        cursor = await conn.execute("PRAGMA table_info(monitors)")
        cols = [col[1] for col in await cursor.fetchall()]
        if "user_id" not in cols:
            await conn.execute("ALTER TABLE monitors ADD COLUMN user_id TEXT")
        if "status" not in cols:
            await conn.execute("ALTER TABLE monitors ADD COLUMN status TEXT DEFAULT 'monitoring'")
        if "unbanned_at" not in cols:
            await conn.execute("ALTER TABLE monitors ADD COLUMN unbanned_at REAL")

        cursor = await conn.execute("PRAGMA table_info(server_settings)")
        st_cols = [col[1] for col in await cursor.fetchall()]
        if "usernamechange_channel_id" not in st_cols:
            await conn.execute("ALTER TABLE server_settings ADD COLUMN usernamechange_channel_id INTEGER")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS unban_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                raw_username TEXT NOT NULL,
                unbanned_at TIMESTAMP NOT NULL
            )
        """)

        await conn.execute("CREATE INDEX IF NOT EXISTS idx_monitors_status ON monitors(status)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_monitors_created ON monitors(created_at ASC)")
        await conn.commit()

async def add_monitors(usernames: List[str], mode: str, guild_id: int, channel_id: int, added_by: int) -> List[str]:
    """Add monitors with async batch insert."""
    added = []
    now = datetime.now()
    async with get_connection() as conn:
        for raw_name in usernames:
            clean = raw_name.strip()
            key = clean.lower()
            if not key:
                continue
            await conn.execute("""
                INSERT INTO monitors (username, raw_username, mode, guild_id, channel_id, added_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username, guild_id) DO UPDATE SET
                    raw_username = excluded.raw_username,
                    mode = excluded.mode,
                    channel_id = excluded.channel_id,
                    added_by = excluded.added_by,
                    status = 'monitoring',
                    unbanned_at = NULL,
                    created_at = excluded.created_at
            """, (key, clean, mode, guild_id, channel_id, added_by, now))
            added.append(clean)
        await conn.commit()
    return added

async def remove_monitors(usernames: List[str], guild_id: Optional[int] = None) -> Tuple[List[str], List[str]]:
    """Remove monitors with async operations."""
    cleared = []
    not_monitored = []
    async with get_connection() as conn:
        for raw_name in usernames:
            clean = raw_name.strip()
            key = clean.lower()
            if not key:
                continue
            if guild_id:
                cursor = await conn.execute("SELECT raw_username FROM monitors WHERE username = ? AND guild_id = ?", (key, guild_id))
            else:
                cursor = await conn.execute("SELECT raw_username FROM monitors WHERE username = ?", (key,))
            rows = await cursor.fetchall()
            if rows:
                if guild_id:
                    await conn.execute("DELETE FROM monitors WHERE username = ? AND guild_id = ?", (key, guild_id))
                else:
                    await conn.execute("DELETE FROM monitors WHERE username = ?", (key,))
                for r in rows:
                    if r[0] not in cleared:
                        cleared.append(r[0])
            else:
                not_monitored.append(clean)
        await conn.commit()
    return cleared, not_monitored

async def remove_monitor(username: str, guild_id: Optional[int] = None):
    """Remove single monitor."""
    async with get_connection() as conn:
        if guild_id:
            await conn.execute("DELETE FROM monitors WHERE username = ? AND guild_id = ?", (username.lower(), guild_id))
        else:
            await conn.execute("DELETE FROM monitors WHERE username = ?", (username.lower(),))
        await conn.commit()

async def get_all_monitors(guild_id: Optional[int] = None) -> List[Dict]:
    """Get all monitors with async query."""
    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        if guild_id:
            cursor = await conn.execute("SELECT * FROM monitors WHERE guild_id = ? ORDER BY created_at ASC", (guild_id,))
        else:
            cursor = await conn.execute("SELECT * FROM monitors ORDER BY created_at ASC")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

async def get_active_monitors(guild_id: Optional[int] = None) -> List[Dict]:
    """Get only active monitors (non-tracking) - CRITICAL for performance."""
    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        if guild_id:
            cursor = await conn.execute(
                "SELECT * FROM monitors WHERE (status IS NULL OR status != 'tracking_username') AND guild_id = ? ORDER BY created_at ASC",
                (guild_id,)
            )
        else:
            cursor = await conn.execute(
                "SELECT * FROM monitors WHERE (status IS NULL OR status != 'tracking_username') ORDER BY created_at ASC"
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

async def clear_all_monitors(guild_id: int) -> int:
    """Clear all monitors for a guild."""
    async with get_connection() as conn:
        cursor = await conn.execute("DELETE FROM monitors WHERE guild_id = ?", (guild_id,))
        count = cursor.rowcount
        await conn.commit()
        return count

async def record_unban_stat(guild_id: int, username: str, raw_username: str):
    """Record unban event."""
    now = datetime.now()
    async with get_connection() as conn:
        await conn.execute("""
            INSERT INTO unban_history (guild_id, username, raw_username, unbanned_at)
            VALUES (?, ?, ?, ?)
        """, (guild_id, username.lower(), raw_username, now))
        await conn.commit()

async def get_unban_stats(guild_id: int, window_seconds: int) -> List[Dict]:
    """Get unban statistics."""
    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("""
            SELECT * FROM unban_history
            WHERE guild_id = ? AND (strftime('%s', 'now') - strftime('%s', unbanned_at)) <= ?
            ORDER BY unbanned_at DESC
        """, (guild_id, window_seconds))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

async def reset_unban_stats(guild_id: int, window_seconds: int) -> int:
    """Reset unban statistics."""
    async with get_connection() as conn:
        cursor = await conn.execute("""
            DELETE FROM unban_history
            WHERE guild_id = ? AND (strftime('%s', 'now') - strftime('%s', unbanned_at)) <= ?
        """, (guild_id, window_seconds))
        count = cursor.rowcount
        await conn.commit()
        return count

async def set_server_setting(guild_id: int, key: str, value):
    """Set server setting."""
    valid_cols = {"unban_channel_id", "ban_channel_id", "tick_channel_id", "usernamechange_channel_id", "style", "autoban", "tg_bot_token", "tg_chat_id"}
    if key not in valid_cols:
        raise ValueError(f"Invalid server setting key: {key}")
    async with get_connection() as conn:
        await conn.execute("""
            INSERT INTO server_settings (guild_id)
            VALUES (?)
            ON CONFLICT(guild_id) DO NOTHING
        """, (guild_id,))
        await conn.execute(f"UPDATE server_settings SET {key} = ? WHERE guild_id = ?", (value, guild_id))
        await conn.commit()

async def get_server_settings(guild_id: int) -> Dict:
    """Get server settings."""
    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM server_settings WHERE guild_id = ?", (guild_id,))
        row = await cursor.fetchone()
        if row:
            return dict(row)
        return {
            "guild_id": guild_id,
            "unban_channel_id": None,
            "ban_channel_id": None,
            "tick_channel_id": None,
            "usernamechange_channel_id": None,
            "style": 1,
            "autoban": 0,
            "tg_bot_token": None,
            "tg_chat_id": None
        }

async def transition_to_username_tracking(username: str, guild_id: int, user_id: str, unbanned_at: Optional[float] = None):
    """Transition monitor to username tracking mode."""
    import time
    if unbanned_at is None:
        unbanned_at = time.time()
    async with get_connection() as conn:
        await conn.execute("""
            UPDATE monitors
            SET status = 'tracking_username',
                user_id = ?,
                unbanned_at = ?
            WHERE username = ? AND guild_id = ?
        """, (str(user_id) if user_id else None, float(unbanned_at), username.lower(), guild_id))
        await conn.commit()

async def get_username_tracking_monitors() -> List[Dict]:
    """Get monitors in username tracking mode."""
    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM monitors WHERE status = 'tracking_username' ORDER BY unbanned_at ASC")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

async def update_tracked_username(old_username: str, new_username: str, guild_id: int):
    """Update tracked username after change detection."""
    async with get_connection() as conn:
        await conn.execute("""
            UPDATE monitors
            SET username = ?,
                raw_username = ?
            WHERE username = ? AND guild_id = ?
        """, (new_username.lower(), new_username, old_username.lower(), guild_id))
        await conn.commit()

async def cleanup_expired_username_tracking(max_age_seconds: float = 86400) -> List[Dict]:
    """Clean up expired username tracking monitors."""
    import time
    now = time.time()
    expired = []
    async with get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM monitors WHERE status = 'tracking_username'")
        rows = await cursor.fetchall()
        for r in rows:
            ub = r["unbanned_at"] or 0
            if (now - ub) >= max_age_seconds:
                expired.append(dict(r))
        if expired:
            for exp in expired:
                await conn.execute("DELETE FROM monitors WHERE username = ? AND guild_id = ?", (exp["username"], exp["guild_id"]))
            await conn.commit()
    return expired

import sqlite3
import os
import shutil
import logging
from datetime import datetime

def resolve_db_path() -> str:
    # 1. Explicit environment variable if provided
    env_path = os.getenv("SQLITE_DB_PATH")
    if env_path:
        d = os.path.dirname(os.path.abspath(env_path))
        if d:
            os.makedirs(d, exist_ok=True)
        return env_path

    # 2. Check for standard Railway Persistent Volume mount directories
    candidate_dirs = ["/data", "/app/data", "/mnt/data"]
    default_local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitors.db")

    for c_dir in candidate_dirs:
        try:
            if os.path.exists(c_dir) and os.access(c_dir, os.W_OK):
                target = os.path.join(c_dir, "monitors.db")
                # Auto-migrate existing local DB file into persistent volume so no data is lost
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

def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='monitors'")
        row = cursor.fetchone()
        if row and ("'tick'" not in row[0] or "PRIMARY KEY (username, guild_id)" not in row[0]):
            cursor.execute("ALTER TABLE monitors RENAME TO monitors_old")
            cursor.execute("""
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
            cursor.execute("""
                INSERT OR IGNORE INTO monitors (username, raw_username, mode, guild_id, channel_id, added_by, created_at)
                SELECT username, raw_username, mode, guild_id, channel_id, added_by, created_at FROM monitors_old
            """)
            cursor.execute("DROP TABLE monitors_old")
        elif not row:
            cursor.execute("""
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

        cursor.execute("""
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

        # Migration: Add columns to monitors if missing
        cursor.execute("PRAGMA table_info(monitors)")
        cols = [col[1] for col in cursor.fetchall()]
        if "user_id" not in cols:
            cursor.execute("ALTER TABLE monitors ADD COLUMN user_id TEXT")
        if "status" not in cols:
            cursor.execute("ALTER TABLE monitors ADD COLUMN status TEXT DEFAULT 'monitoring'")
        if "unbanned_at" not in cols:
            cursor.execute("ALTER TABLE monitors ADD COLUMN unbanned_at REAL")

        # Migration: Add usernamechange_channel_id to server_settings if missing
        cursor.execute("PRAGMA table_info(server_settings)")
        st_cols = [col[1] for col in cursor.fetchall()]
        if "usernamechange_channel_id" not in st_cols:
            cursor.execute("ALTER TABLE server_settings ADD COLUMN usernamechange_channel_id INTEGER")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS unban_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                raw_username TEXT NOT NULL,
                unbanned_at TIMESTAMP NOT NULL
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_monitors_status ON monitors(status)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_monitors_created ON monitors(created_at ASC)"
        )
        conn.commit()

def add_monitors(usernames: list[str], mode: str, guild_id: int, channel_id: int, added_by: int) -> list[str]:
    added = []
    now = datetime.now()
    with get_connection() as conn:
        cursor = conn.cursor()
        for raw_name in usernames:
            clean = raw_name.strip()
            key = clean.lower()
            if not key:
                continue
            cursor.execute("""
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
        conn.commit()
    return added

def remove_monitors(usernames: list[str], guild_id: int = None) -> tuple[list[str], list[str]]:
    cleared = []
    not_monitored = []
    with get_connection() as conn:
        cursor = conn.cursor()
        for raw_name in usernames:
            clean = raw_name.strip()
            key = clean.lower()
            if not key:
                continue
            if guild_id:
                cursor.execute("SELECT raw_username FROM monitors WHERE username = ? AND guild_id = ?", (key, guild_id))
            else:
                cursor.execute("SELECT raw_username FROM monitors WHERE username = ?", (key,))
            rows = cursor.fetchall()
            if rows:
                if guild_id:
                    cursor.execute("DELETE FROM monitors WHERE username = ? AND guild_id = ?", (key, guild_id))
                else:
                    cursor.execute("DELETE FROM monitors WHERE username = ?", (key,))
                for r in rows:
                    if r["raw_username"] not in cleared:
                        cleared.append(r["raw_username"])
            else:
                not_monitored.append(clean)
        conn.commit()
    return cleared, not_monitored

def remove_monitor(username: str, guild_id: int = None):
    with get_connection() as conn:
        cursor = conn.cursor()
        if guild_id:
            cursor.execute("DELETE FROM monitors WHERE username = ? AND guild_id = ?", (username.lower(), guild_id))
        else:
            cursor.execute("DELETE FROM monitors WHERE username = ?", (username.lower(),))
        conn.commit()

def get_all_monitors(guild_id: int = None) -> list[dict]:
    with get_connection() as conn:
        cursor = conn.cursor()
        if guild_id:
            cursor.execute("SELECT * FROM monitors WHERE guild_id = ? ORDER BY created_at ASC", (guild_id,))
        else:
            cursor.execute("SELECT * FROM monitors ORDER BY created_at ASC")
        rows = cursor.fetchall()
        return [dict(r) for r in rows]

def get_active_monitors(guild_id: int = None) -> list[dict]:
    """Return only monitors with status != 'tracking_username' — filtered in SQL."""
    with get_connection() as conn:
        cursor = conn.cursor()
        if guild_id:
            cursor.execute(
                "SELECT * FROM monitors WHERE status != 'tracking_username' AND guild_id = ? ORDER BY created_at ASC",
                (guild_id,)
            )
        else:
            cursor.execute(
                "SELECT * FROM monitors WHERE status != 'tracking_username' ORDER BY created_at ASC"
            )
        rows = cursor.fetchall()
        return [dict(r) for r in rows]

def clear_all_monitors(guild_id: int) -> int:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM monitors WHERE guild_id = ?", (guild_id,))
        count = cursor.rowcount
        conn.commit()
        return count

def record_unban_stat(guild_id: int, username: str, raw_username: str):
    now = datetime.now()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO unban_history (guild_id, username, raw_username, unbanned_at)
            VALUES (?, ?, ?, ?)
        """, (guild_id, username.lower(), raw_username, now))
        conn.commit()

def get_unban_stats(guild_id: int, window_seconds: int) -> list[dict]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM unban_history
            WHERE guild_id = ? AND (strftime('%s', 'now') - strftime('%s', unbanned_at)) <= ?
            ORDER BY unbanned_at DESC
        """, (guild_id, window_seconds))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]

def reset_unban_stats(guild_id: int, window_seconds: int) -> int:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM unban_history
            WHERE guild_id = ? AND (strftime('%s', 'now') - strftime('%s', unbanned_at)) <= ?
        """, (guild_id, window_seconds))
        count = cursor.rowcount
        conn.commit()
        return count

def set_server_setting(guild_id: int, key: str, value):
    valid_cols = {"unban_channel_id", "ban_channel_id", "tick_channel_id", "usernamechange_channel_id", "style", "autoban", "tg_bot_token", "tg_chat_id"}
    if key not in valid_cols:
        raise ValueError(f"Invalid server setting key: {key}")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO server_settings (guild_id)
            VALUES (?)
            ON CONFLICT(guild_id) DO NOTHING
        """, (guild_id,))
        cursor.execute(f"UPDATE server_settings SET {key} = ? WHERE guild_id = ?", (value, guild_id))
        conn.commit()

def get_server_settings(guild_id: int) -> dict:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM server_settings WHERE guild_id = ?", (guild_id,))
        row = cursor.fetchone()
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

def set_channel(guild_id: int, channel_type: str, channel_id: int):
    col = f"{channel_type}_channel_id"
    set_server_setting(guild_id, col, channel_id)

def get_channels(guild_id: int) -> dict:
    st = get_server_settings(guild_id)
    return {
        "unban_channel_id": st.get("unban_channel_id"),
        "ban_channel_id": st.get("ban_channel_id"),
        "tick_channel_id": st.get("tick_channel_id"),
        "usernamechange_channel_id": st.get("usernamechange_channel_id")
    }

def transition_to_username_tracking(username: str, guild_id: int, user_id: str, unbanned_at: float = None):
    import time
    if unbanned_at is None:
        unbanned_at = time.time()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE monitors
            SET status = 'tracking_username',
                user_id = ?,
                unbanned_at = ?
            WHERE username = ? AND guild_id = ?
        """, (str(user_id) if user_id else None, float(unbanned_at), username.lower(), guild_id))
        conn.commit()

def get_username_tracking_monitors() -> list[dict]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM monitors WHERE status = 'tracking_username' ORDER BY unbanned_at ASC")
        rows = cursor.fetchall()
        return [dict(r) for r in rows]

def update_tracked_username(old_username: str, new_username: str, guild_id: int):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE monitors
            SET username = ?,
                raw_username = ?
            WHERE username = ? AND guild_id = ?
        """, (new_username.lower(), new_username, old_username.lower(), guild_id))
        conn.commit()

def cleanup_expired_username_tracking(max_age_seconds: float = 86400) -> list[dict]:
    import time
    now = time.time()
    expired = []
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM monitors WHERE status = 'tracking_username'")
        rows = cursor.fetchall()
        for r in rows:
            ub = r["unbanned_at"] or 0
            if (now - ub) >= max_age_seconds:
                expired.append(dict(r))
        if expired:
            for exp in expired:
                cursor.execute("DELETE FROM monitors WHERE username = ? AND guild_id = ?", (exp["username"], exp["guild_id"]))
            conn.commit()
    return expired

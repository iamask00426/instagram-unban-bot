import sqlite3
import os
from datetime import datetime

DB_PATH = os.getenv("SQLITE_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitors.db"))

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS monitors (
                username TEXT PRIMARY KEY,
                raw_username TEXT NOT NULL,
                mode TEXT NOT NULL CHECK(mode IN ('unban', 'ban')),
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                added_by INTEGER NOT NULL,
                created_at TIMESTAMP NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS channel_settings (
                guild_id INTEGER PRIMARY KEY,
                unban_channel_id INTEGER,
                ban_channel_id INTEGER
            )
        """)
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
            cursor.execute("SELECT username FROM monitors WHERE username = ?", (key,))
            existing = cursor.fetchone()
            if not existing:
                cursor.execute("""
                    INSERT INTO monitors (username, raw_username, mode, guild_id, channel_id, added_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (key, clean, mode, guild_id, channel_id, added_by, now))
                added.append(clean)
            else:
                # Update existing mode and channel if user re-adds
                cursor.execute("""
                    UPDATE monitors SET raw_username = ?, mode = ?, guild_id = ?, channel_id = ?, added_by = ?, created_at = ?
                    WHERE username = ?
                """, (clean, mode, guild_id, channel_id, added_by, now, key))
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
            cursor.execute("SELECT raw_username FROM monitors WHERE username = ?", (key,))
            row = cursor.fetchone()
            if row:
                cursor.execute("DELETE FROM monitors WHERE username = ?", (key,))
                cleared.append(row["raw_username"])
            else:
                not_monitored.append(clean)
        conn.commit()
    return cleared, not_monitored

def remove_monitor(username: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM monitors WHERE username = ?", (username.lower(),))
        conn.commit()

def get_all_monitors() -> list[dict]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM monitors ORDER BY created_at ASC")
        rows = cursor.fetchall()
        return [dict(r) for r in rows]

def set_channel(guild_id: int, channel_type: str, channel_id: int):
    col = "unban_channel_id" if channel_type == "unban" else "ban_channel_id"
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO channel_settings (guild_id, unban_channel_id, ban_channel_id)
            VALUES (?, NULL, NULL)
            ON CONFLICT(guild_id) DO NOTHING
        """, (guild_id,))
        cursor.execute(f"UPDATE channel_settings SET {col} = ? WHERE guild_id = ?", (channel_id, guild_id))
        conn.commit()

def get_channels(guild_id: int) -> dict:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT unban_channel_id, ban_channel_id FROM channel_settings WHERE guild_id = ?", (guild_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return {"unban_channel_id": None, "ban_channel_id": None}

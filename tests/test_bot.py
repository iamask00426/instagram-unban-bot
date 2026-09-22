import os
import tempfile
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from io import BytesIO

import database
import bot
from monitoring import CheckResult

class FakeMessage:
    def __init__(self, text, chat=1, user=2):
        self.text = text
        self.chat = SimpleNamespace(id=chat)
        self.from_user = SimpleNamespace(id=user) if user is not None else None
        self.answers = []

    async def answer(self, text, parse_mode=None):
        self.answers.append(text)
        return self

    async def edit_text(self, text, parse_mode=None):
        self.answers.append(text)
        return self

    async def answer_photo(self, photo, caption=None, parse_mode=None):
        self.answers.append(caption or "photo")
        return self

class TestTelegramBot(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_tg_monitors.db")
        database.DB_PATH = self.db_path
        database.init_db()

        self.mock_bot = SimpleNamespace(
            send_photo=AsyncMock(),
            send_message=AsyncMock()
        )
        self.orig_bot = bot.bot
        bot.bot = self.mock_bot

    async def asyncTearDown(self):
        bot.bot = self.orig_bot
        self.tmp_dir.cleanup()

    async def test_help_command(self):
        msg = FakeMessage("/help")
        await bot.start_cmd(msg)
        self.assertTrue(len(msg.answers) > 0)
        self.assertIn("Command List", msg.answers[0])
        self.assertIn("/unban", msg.answers[0])
        self.assertIn("/ban", msg.answers[0])
        self.assertIn("/tick", msg.answers[0])

    async def test_unban_command(self):
        msg = FakeMessage("/unban user1, user2 @user3 https://instagram.com/user4")
        await bot.unban_cmd(msg)
        self.assertTrue(len(msg.answers) > 0)
        self.assertIn("Monitoring Started", msg.answers[0])
        self.assertIn("4", msg.answers[0])

        records = database.get_all_monitors()
        self.assertEqual(len(records), 4)
        names = [r["username"] for r in records]
        self.assertIn("user1", names)
        self.assertIn("user4", names)

    async def test_ban_and_tick_command(self):
        msg_ban = FakeMessage("/ban baduser")
        await bot.ban_cmd(msg_ban)
        self.assertIn("Ban Monitoring Started", msg_ban.answers[0])

        msg_tick = FakeMessage("/tick tickuser")
        await bot.tick_cmd(msg_tick)
        self.assertIn("Tick Monitoring Started", msg_tick.answers[0])

        records = database.get_all_monitors()
        self.assertEqual(len(records), 2)

    async def test_clear_and_clearall(self):
        database.add_monitors(["alpha", "beta"], "unban", 1, 1, 1)
        msg_clear = FakeMessage("/clear alpha delta")
        await bot.clear_cmd(msg_clear)
        self.assertIn("Cleared (1)", msg_clear.answers[0])
        self.assertIn("Not Monitored (1)", msg_clear.answers[0])

        msg_clearall = FakeMessage("/clearall")
        await bot.clearall_cmd(msg_clearall)
        self.assertIn("Cleared 1 monitor(s)", msg_clearall.answers[0])
        self.assertEqual(len(database.get_all_monitors()), 0)

    async def test_fakeunban_command(self):
        msg = FakeMessage("/fakeunban testuser 38.4M 01:23:45")
        await bot.fakeunban_cmd(msg)
        self.assertTrue(len(msg.answers) > 0)
        self.assertIn("Fake unban alert dispatched", msg.answers[0])
        self.mock_bot.send_photo.assert_called_once()

    async def test_kill_command(self):
        msg = FakeMessage("/kill testuser")
        await bot.kill_cmd(msg)
        self.assertTrue(len(msg.answers) > 0)
        self.assertIn("Fake ban alert dispatched", msg.answers[0])
        self.mock_bot.send_photo.assert_called_once()

    async def test_autoban_command(self):
        msg_on = FakeMessage("/autoban on")
        await bot.autoban_cmd(msg_on)
        self.assertIn("ENABLED", msg_on.answers[0])
        st = database.get_server_settings(1)
        self.assertEqual(st["autoban"], 1)

        msg_off = FakeMessage("/autoban off")
        await bot.autoban_cmd(msg_off)
        self.assertIn("DISABLED", msg_off.answers[0])
        st = database.get_server_settings(1)
        self.assertEqual(st["autoban"], 0)

    async def test_stats_and_reset_commands(self):
        database.record_unban_stat(1, "recovered_user", "Recovered_User")
        msg_2h = FakeMessage("/2h")
        await bot.stats_2h_cmd(msg_2h)
        self.assertIn("Total: <b>1</b> unbanned", msg_2h.answers[0])

        msg_reset = FakeMessage("/reset 2h")
        await bot.reset_cmd(msg_reset)
        self.assertIn("Reset unban stats", msg_reset.answers[0])

        msg_2h_empty = FakeMessage("/2h")
        await bot.stats_2h_cmd(msg_2h_empty)
        self.assertIn("No accounts recovered", msg_2h_empty.answers[0])

    async def test_ping_command(self):
        msg = FakeMessage("/ping")
        await bot.ping_cmd(msg)
        self.assertIn("Pong!", msg.answers[-1])

if __name__ == "__main__":
    unittest.main()

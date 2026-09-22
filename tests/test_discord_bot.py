import unittest
import os
import tempfile
import database
from discord_bot import parse_usernames, truncate_list_for_embed

class TestDiscordBot(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_monitors.db")
        database.DB_PATH = self.db_path
        database.init_db()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_multiline_username_parsing(self):
        raw_text = """
        https://instagram.com/user_one/
        instagram.com/user_two?utm=123
        @user_three
        USER_FOUR
        user_one
        https://www.instagram.com/invalid/extra/path/
        https://notinstagram.com/fakeuser
        """
        usernames = parse_usernames(raw_text)
        self.assertEqual(usernames, ["user_one", "user_two", "user_three", "user_four", "invalid"])

    def test_truncate_list_for_embed(self):
        short_list = ["user1", "user2"]
        self.assertEqual(truncate_list_for_embed(short_list), "`user1` `user2`")
        self.assertEqual(truncate_list_for_embed([]), "None")

        long_list = [f"very_long_instagram_username_{i}" for i in range(100)]
        result = truncate_list_for_embed(long_list, max_chars=100)
        self.assertTrue(len(result) <= 100)
        self.assertIn("more", result)

    def test_database_persistence_and_removal(self):
        # 1. Add monitors
        added = database.add_monitors(["alpha", "beta", "gamma"], "unban", 111, 222, 333)
        self.assertEqual(len(added), 3)

        records = database.get_all_monitors()
        self.assertEqual(len(records), 3)

        # 2. Clear subset
        cleared, not_mon = database.remove_monitors(["alpha", "delta"], 111)
        self.assertEqual(cleared, ["alpha"])
        self.assertEqual(not_mon, ["delta"])

        records = database.get_all_monitors()
        self.assertEqual(len(records), 2)
        remaining = [r["username"] for r in records]
        self.assertIn("beta", remaining)
        self.assertIn("gamma", remaining)
        self.assertNotIn("alpha", remaining)

    def test_multi_guild_isolation(self):
        # Both guild 100 and guild 200 monitor the same account
        database.add_monitors(["shared_user"], "unban", 100, 11, 1)
        database.add_monitors(["shared_user"], "unban", 200, 22, 2)

        records = database.get_all_monitors()
        self.assertEqual(len(records), 2)

        # Clearing in guild 100 does not delete guild 200's monitor
        cleared, _ = database.remove_monitors(["shared_user"], 100)
        self.assertEqual(cleared, ["shared_user"])

        remaining = database.get_all_monitors()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["guild_id"], 200)

    def test_channel_routing_settings(self):
        database.set_channel(999, "unban", 12345)
        database.set_channel(999, "ban", 67890)
        database.set_channel(999, "tick", 11223)

        settings = database.get_channels(999)
        self.assertEqual(settings["unban_channel_id"], 12345)
        self.assertEqual(settings["ban_channel_id"], 67890)
        self.assertEqual(settings["tick_channel_id"], 11223)

    def test_clear_all_monitors(self):
        database.add_monitors(["user1", "user2"], "unban", 555, 1, 1)
        database.add_monitors(["user3"], "ban", 555, 1, 1)
        database.add_monitors(["other"], "unban", 666, 1, 1)

        cleared = database.clear_all_monitors(555)
        self.assertEqual(cleared, 3)

        # Other guild monitors remain intact
        rem = database.get_all_monitors()
        self.assertEqual(len(rem), 1)
        self.assertEqual(rem[0]["guild_id"], 666)

    def test_unban_history_and_stats_reset(self):
        database.record_unban_stat(777, "user_one", "User_One")
        database.record_unban_stat(777, "user_two", "User_Two")
        database.record_unban_stat(888, "user_three", "User_Three")

        stats_2h = database.get_unban_stats(777, 7200)
        self.assertEqual(len(stats_2h), 2)

        stats_other = database.get_unban_stats(888, 7200)
        self.assertEqual(len(stats_other), 1)

        # Reset 777 window
        reset_cnt = database.reset_unban_stats(777, 7200)
        self.assertEqual(reset_cnt, 2)
        self.assertEqual(len(database.get_unban_stats(777, 7200)), 0)
        self.assertEqual(len(database.get_unban_stats(888, 7200)), 1)

    def test_server_settings_style_and_autoban(self):
        database.set_server_setting(444, "style", 4)
        database.set_server_setting(444, "autoban", 1)
        database.set_server_setting(444, "tg_bot_token", "123:ABC")
        database.set_server_setting(444, "tg_chat_id", "999888")

        st = database.get_server_settings(444)
        self.assertEqual(st["style"], 4)
        self.assertEqual(st["autoban"], 1)
        self.assertEqual(st["tg_bot_token"], "123:ABC")
        self.assertEqual(st["tg_chat_id"], "999888")

    def test_tick_mode_persistence(self):
        added = database.add_monitors(["tick_user"], "tick", 333, 10, 1)
        self.assertEqual(added, ["tick_user"])
        records = database.get_all_monitors()
        match = next((r for r in records if r["username"] == "tick_user"), None)
        self.assertIsNotNone(match)
    async def test_deliver_alert_embed_frame(self):
        from unittest.mock import AsyncMock, MagicMock
        from io import BytesIO
        from discord_bot import deliver_alert_to_channel

        mock_ch = MagicMock()
        mock_ch.send = AsyncMock()
        fake_card = BytesIO(b"fake_image_bytes")

        # Style 1 (default) must send both content and embed container
        res = await deliver_alert_to_channel(mock_ch, "Alert Text", fake_card, style=1)
        self.assertTrue(res)
        mock_ch.send.assert_called_once()
        kwargs = mock_ch.send.call_args.kwargs
        self.assertEqual(kwargs.get("content"), "Alert Text")
        self.assertIsNotNone(kwargs.get("embed"))
        self.assertIsNotNone(kwargs.get("file"))

if __name__ == "__main__":
    unittest.main()

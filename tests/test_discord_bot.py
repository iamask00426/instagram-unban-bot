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

    def test_format_count(self):
        from card_generator import format_count
        self.assertEqual(format_count("38383773"), "38.4M")
        self.assertEqual(format_count("2000000"), "2.0M")
        self.assertEqual(format_count("736373"), "736.4K")
        self.assertEqual(format_count("13069"), "13.1K")
        self.assertEqual(format_count("10000"), "10K")
        self.assertEqual(format_count("950"), "950")
        self.assertEqual(format_count("12.5M"), "12.5M")
        self.assertEqual(format_count("10k"), "10K")
        self.assertEqual(format_count("38.4m"), "38.4M")

    def test_username_tracking_lifecycle(self):
        import time
        # 1. Add monitor in unban mode
        database.add_monitors(["target_user"], "unban", 777, 10, 1)
        records = database.get_all_monitors()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "monitoring")

        # 2. Transition to username tracking with UID immediately upon unban
        unban_ts = time.time()
        database.transition_to_username_tracking("target_user", 777, "62439747944", unban_ts)

        tracking = database.get_username_tracking_monitors()
        self.assertEqual(len(tracking), 1)
        self.assertEqual(tracking[0]["username"], "target_user")
        self.assertEqual(tracking[0]["user_id"], "62439747944")
        self.assertEqual(tracking[0]["status"], "tracking_username")
        self.assertAlmostEqual(tracking[0]["unbanned_at"], unban_ts, delta=1.0)

        # 3. Simulate username change: target_user -> new_cool_name
        database.update_tracked_username("target_user", "new_cool_name", 777)
        tracking_updated = database.get_username_tracking_monitors()
        self.assertEqual(len(tracking_updated), 1)
        self.assertEqual(tracking_updated[0]["username"], "new_cool_name")
        self.assertEqual(tracking_updated[0]["raw_username"], "new_cool_name")
        self.assertEqual(tracking_updated[0]["user_id"], "62439747944")

        # 4. Expiration check: active account within 24h should NOT be cleaned up
        exp_recent = database.cleanup_expired_username_tracking(max_age_seconds=86400)
        self.assertEqual(len(exp_recent), 0)

        # 5. Fast forward past 24 hours: should auto-stop and delete from DB
        database.transition_to_username_tracking("new_cool_name", 777, "62439747944", time.time() - 86401)
        exp_old = database.cleanup_expired_username_tracking(max_age_seconds=86400)
        self.assertEqual(len(exp_old), 1)
        self.assertEqual(exp_old[0]["username"], "new_cool_name")
        self.assertEqual(len(database.get_username_tracking_monitors()), 0)

    def test_usernamechange_channel_setting(self):
        database.set_channel(888, "usernamechange", 555666)
        channels = database.get_channels(888)
        self.assertEqual(channels["usernamechange_channel_id"], 555666)

    async def test_send_username_change_alert(self):
        from unittest.mock import AsyncMock, MagicMock
        from discord_bot import send_username_change_alert

        mock_guild = MagicMock()
        mock_guild.id = 999
        mock_ch = MagicMock()
        mock_ch.send = AsyncMock()
        mock_ch.name = "usernamechange"
        mock_guild.text_channels = [mock_ch]
        mock_guild.get_channel.return_value = mock_ch

        mon_data = {"username": "olduser", "guild_id": 999, "channel_id": 123}
        await send_username_change_alert(mock_guild, mon_data, "olduser", "newuser", "12345678")

        mock_ch.send.assert_called_once()
        kwargs = mock_ch.send.call_args.kwargs
        self.assertIn("@olduser", kwargs["content"])
        self.assertIn("@newuser", kwargs["content"])
        self.assertIn("12345678", kwargs["content"])

if __name__ == "__main__":
    unittest.main()

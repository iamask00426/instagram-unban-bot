import unittest
import os
import tempfile
import database
from discord_bot import parse_usernames, truncate_list_for_embed

class TestDiscordBot(unittest.TestCase):
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

        settings = database.get_channels(999)
        self.assertEqual(settings["unban_channel_id"], 12345)
        self.assertEqual(settings["ban_channel_id"], 67890)

if __name__ == "__main__":
    unittest.main()

import unittest
import os
import tempfile
import database
from discord_bot import parse_usernames

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

    def test_channel_routing_settings(self):
        database.set_channel(999, "unban", 12345)
        database.set_channel(999, "ban", 67890)

        settings = database.get_channels(999)
        self.assertEqual(settings["unban_channel_id"], 12345)
        self.assertEqual(settings["ban_channel_id"], 67890)

if __name__ == "__main__":
    unittest.main()

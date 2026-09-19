import unittest

from config import Settings, is_example, normalize_username, parse_proxy_entry


# Deliberately fake, syntax-only configuration. No startup or network requests.
ENV = {"BOT_TOKEN": "987654321:" + "x" * 30, "PROXIES": "http://proxy.invalid:8080"}


class ConfigurationTests(unittest.TestCase):
    def test_html_defaults_fast_interval_and_bounded_limits(self):
        settings = Settings.from_env(ENV)
        self.assertEqual(settings.mode, "html")
        self.assertEqual(settings.check_interval, 2)
        self.assertEqual(settings.body_limit, 131072)
        self.assertEqual(settings.concurrency, 10)
        self.assertEqual(settings.session_id, "")
        self.assertEqual(settings.unavailable_confirmations, 2)

    def test_missing_secrets_and_proxies_rejected_without_fallback(self):
        for env in ({}, {"BOT_TOKEN": ENV["BOT_TOKEN"]}, {"PROXIES": ENV["PROXIES"]},
                    {**ENV, "BOT_TOKEN": "your_bot_token_here"}, {**ENV, "PROXIES": ";,\n"}):
            with self.subTest(keys=list(env)):
                with self.assertRaises(ValueError):
                    Settings.from_env(env)

    def test_session_placeholders_do_not_enable_api(self):
        for placeholder in ("your_instagram_sessionid_here", "optional_instagram_session_cookie_here",
                            "EXAMPLE_SESSION", "placeholder", "123456789%3Atest", ""):
            with self.subTest(placeholder=placeholder):
                self.assertTrue(is_example(placeholder))
                self.assertEqual(Settings.from_env({**ENV, "IG_SESSIONID": placeholder}).session_id, "")
                with self.assertRaisesRegex(ValueError, "real IG_SESSIONID"):
                    Settings.from_env({**ENV, "IG_CHECK_MODE": "json", "IG_SESSIONID": placeholder})

    def test_valid_session_requires_explicit_json_mode(self):
        env = {**ENV, "IG_SESSIONID": "offline_cookie_not_for_real_use"}
        self.assertEqual(Settings.from_env(env).mode, "html")
        self.assertEqual(Settings.from_env(env).session_id, "")
        settings = Settings.from_env({**env, "IG_CHECK_MODE": "json"})
        self.assertEqual(settings.mode, "json")
        self.assertEqual(settings.session_id, env["IG_SESSIONID"])

    def test_optional_substring_is_not_a_placeholder(self):
        value = "offline_optional_cookie"
        self.assertFalse(is_example(value))
        self.assertEqual(Settings.from_env({**ENV, "IG_CHECK_MODE": "json", "IG_SESSIONID": value}).session_id, value)

    def test_cookie_injection_and_bad_mode_rejected(self):
        for value in ("abc;other=value", "abc\r\nInjected", "abc def", "abc,def"):
            with self.assertRaises(ValueError):
                Settings.from_env({**ENV, "IG_SESSIONID": value})
        with self.assertRaises(ValueError):
            Settings.from_env({**ENV, "IG_CHECK_MODE": "fallback"})

    def test_proxy_formats_and_escaped_credentials(self):
        self.assertEqual(parse_proxy_entry("proxy.invalid:8080"), "http://proxy.invalid:8080")
        self.assertEqual(parse_proxy_entry("proxy.invalid:8080:u:p@ss"), "http://u:p%40ss@proxy.invalid:8080")
        self.assertEqual(parse_proxy_entry("https://proxy.invalid:443"), "https://proxy.invalid:443")
        settings = Settings.from_env({**ENV, "PROXIES": "one.invalid:80;two.invalid:81,\nthree.invalid:82"})
        self.assertEqual(len(settings.proxies), 3)

    def test_invalid_and_socks_proxy_rejection_has_no_credential_echo(self):
        for value in ("socks5://user:secret@proxy.invalid:1080", "socks4://proxy.invalid:1080", "garbage",
                      "http://proxy.invalid", "http://proxy.invalid:noport", "http://proxy.invalid:80000",
                      "http://proxy.invalid:80/path", "http://proxy.invalid:80/?q=x", "http://proxy.invalid:80/#x",
                      "http://proxy.invalid:80\nInjected", ""):
            with self.subTest(value=value.split(":")[0]):
                with self.assertRaises(ValueError) as caught:
                    parse_proxy_entry(value)
                self.assertNotIn("secret", str(caught.exception))
                if value:
                    self.assertNotIn(value, str(caught.exception))

    def test_numeric_limits_and_finite_values(self):
        for key, value in (("CHECK_INTERVAL_SECONDS", "nan"), ("CHECK_INTERVAL_SECONDS", "0"),
                           ("CHECK_CONCURRENCY", "0"), ("CHECK_CONCURRENCY", "1.5"),
                           ("PROFILE_BODY_LIMIT_BYTES", "99999999999"), ("REQUEST_TIMEOUT_SECONDS", "inf"),
                           ("UNAVAILABLE_CONFIRMATIONS", "1"), ("BACKOFF_MAX_SECONDS", "1"),
                           ("CONFIRMATION_INTERVAL_SECONDS", "-1")):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    Settings.from_env({**ENV, key: value})
        settings = Settings.from_env({**ENV, "CHECK_INTERVAL_SECONDS": "1.5", "CHECK_CONCURRENCY": "3",
                                      "PROFILE_BODY_LIMIT_BYTES": "65536"})
        self.assertEqual((settings.check_interval, settings.concurrency, settings.body_limit), (1.5, 3, 65536))


class UsernameTests(unittest.TestCase):
    def test_normalization(self):
        for value in ("Alice", " @Alice ", "https://instagram.com/Alice/", "http://www.instagram.com/Alice",
                      "instagram.com/Alice", "www.instagram.com/Alice/"):
            with self.subTest(value=value):
                self.assertEqual(normalize_username(value), "alice")
        self.assertEqual(normalize_username("a_b.c9"), "a_b.c9")
        self.assertEqual(normalize_username("a" * 30), "a" * 30)

    def test_reject_unsafe_usernames_urls_and_html(self):
        for value in ("", "@", "@@alice", "a/b", "../alice", "a..b", ".alice", "alice.", "a" * 31,
                      "a b", "<b>alice</b>", "álîce", "alice?x=y", "https://evil.test/alice",
                      "https://instagram.com.evil.test/alice", "https://evil@instagram.com/alice",
                      "https://instagram.com:443/alice", "https://instagram.com/alice?x=y",
                      "https://instagram.com/alice#fragment", "https://instagram.com/a/b",
                      "https://instagram.com/%61lice", "//instagram.com/alice", "a\nb"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_username(value)

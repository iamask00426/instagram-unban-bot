import asyncio
import importlib
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

import bot
from monitoring import CheckResult, MonitorScheduler, ProfileChecker
from test_config import ENV
from test_monitoring import Clock, FakeResponse, FakeSession, SETTINGS, profile


class FakeMessage:
    def __init__(self, text, chat=1, user=2):
        self.text = text
        self.chat = SimpleNamespace(id=chat)
        self.from_user = SimpleNamespace(id=user) if user is not None else None
        self.answers = []

    async def answer(self, text):
        self.answers.append(text)


class BotIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = Clock()
        self.telegram = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
        self.checker = ProfileChecker(FakeSession(FakeResponse(profile())), SETTINGS)
        self.scheduler = MonitorScheduler(SETTINGS, self.checker.check, bot.trigger_alert, self.clock)
        self.old_bot, self.old_scheduler = bot.bot, bot.scheduler
        bot.bot, bot.scheduler = self.telegram, self.scheduler
        self.card = AsyncMock(return_value=BytesIO(b"offline card"))
        self.card_patch = patch.object(bot, "create_profile_card", self.card)
        self.card_patch.start()

    async def asyncTearDown(self):
        self.card_patch.stop()
        await self.scheduler.close()
        bot.bot, bot.scheduler = self.old_bot, self.old_scheduler

    async def turn(self):
        self.scheduler.dispatch_due()
        await asyncio.gather(*list(self.scheduler.tasks))

    async def test_monitor_fake_network_prefix_metadata_one_request_and_honest_alert(self):
        message = FakeMessage("/monitor @ALICE")
        await bot.add_monitor(message)
        self.assertIn("alice", self.scheduler.records)
        self.assertEqual(self.checker.metrics.request_attempts, 0)
        self.assertFalse(self.scheduler.tasks)  # commands do not launch instant tasks
        await self.turn()
        await self.turn()
        self.assertEqual(self.checker.metrics.request_attempts, 1)
        self.assertNotIn("alice", self.scheduler.records)
        args, kwargs = self.card.call_args
        self.assertEqual(args, ("alice", "0", "3", "2"))
        self.assertEqual(kwargs["pic_url"], "https://scontent.cdninstagram.com/avatar.jpg?a=1&b=2")
        caption = self.telegram.send_photo.call_args.kwargs["caption"]
        self.assertIn("active/available", caption)
        self.assertNotIn("Recovered", caption)
        self.telegram.send_message.assert_not_awaited()

    async def test_photo_and_text_failure_retries_cached_card_without_new_profile_request(self):
        self.telegram.send_photo.side_effect = [RuntimeError("offline"), None]
        self.telegram.send_message.side_effect = RuntimeError("offline")
        await bot.add_monitor(FakeMessage("/monitor alice"))
        await self.turn()
        await self.turn()
        record = self.scheduler.records["alice"]
        self.assertIsNotNone(record.pending)
        self.assertEqual(record.next_due, 105)
        self.assertTrue(record.photo_prepared)
        self.assertEqual(self.checker.metrics.request_attempts, 1)
        self.clock.value = 105
        await self.turn()
        self.assertEqual(self.checker.metrics.request_attempts, 1)
        self.card.assert_awaited_once()
        self.assertEqual(self.telegram.send_photo.await_count, 2)
        self.assertNotIn("alice", self.scheduler.records)
        self.assertTrue(self.telegram.send_message.call_args.kwargs["disable_web_page_preview"])

    async def test_render_failure_falls_back_to_text_once_without_enrichment(self):
        self.card.side_effect = ValueError("bad image")
        await bot.add_monitor(FakeMessage("/monitor alice"))
        await self.turn()
        await self.turn()
        self.telegram.send_photo.assert_not_awaited()
        self.telegram.send_message.assert_awaited_once()
        self.assertEqual(self.checker.metrics.request_attempts, 1)
        self.assertFalse(self.scheduler.records)

    async def test_banmonitor_repeated_404s_not_ban_proof(self):
        self.checker.session.response = FakeResponse(status=404, forbid_read=True)
        await bot.add_banmonitor(FakeMessage("/banmonitor alice"))
        await self.turn()
        self.telegram.send_photo.assert_not_awaited()
        self.clock.value = 105
        await self.turn()
        await self.turn()
        self.assertEqual(self.checker.metrics.request_attempts, 2)
        caption = self.telegram.send_photo.call_args.kwargs["caption"]
        self.assertIn("unavailable", caption)
        self.assertIn("does not prove", caption)
        self.assertNotIn("BANNED/DISABLED", caption)
        self.assertTrue(self.card.call_args.kwargs["is_unavailable"])

    async def test_generic_200_never_sends_ban_alert(self):
        self.checker.session.response = FakeResponse(b"<head><title>Instagram</title></head>")
        await bot.add_banmonitor(FakeMessage("/banmonitor alice"))
        await self.turn()
        self.assertEqual(self.scheduler.records["alice"].next_due, 110)
        self.telegram.send_photo.assert_not_awaited()
        self.telegram.send_message.assert_not_awaited()

    async def test_command_normalization_rejects_injection_and_duplicate_tasks(self):
        for command in ("/monitor <b>alice</b>", "/monitor https://evil.test/alice", "/monitor alice/bob", "/monitor"):
            message = FakeMessage(command)
            await bot.add_monitor(message)
            self.assertTrue(message.answers)
            self.assertFalse(self.scheduler.records)
        await bot.add_monitor(FakeMessage("/monitor https://www.instagram.com/ALICE/"))
        await bot.add_banmonitor(FakeMessage("/banmonitor @alice", user=999))
        self.assertEqual(len(self.scheduler.records), 1)
        self.assertEqual(self.scheduler.records["alice"].owner_id, 2)
        self.assertEqual(self.scheduler.records["alice"].mode, "unban")
        self.assertFalse(self.scheduler.tasks)

    async def test_bulk_deduplicates_skips_unsafe_and_uses_same_scheduler(self):
        message = FakeMessage("/bulk@OfflineBot Alice, bob @ALICE <evil> https://instagram.com/carol/")
        await bot.add_bulk(message)
        self.assertEqual(set(self.scheduler.records), {"alice", "bob", "carol"})
        self.assertIn("Invalid entries skipped: 1", message.answers[0])
        self.assertFalse(self.scheduler.tasks)
        self.assertEqual(self.checker.metrics.request_attempts, 0)

    async def test_stop_command_owner_and_chat_protection(self):
        await bot.add_monitor(FakeMessage("/monitor alice"))
        for chat, user in ((1, 3), (3, 2), (1, None)):
            await bot.stop_monitor(FakeMessage("/stop alice", chat, user))
            self.assertIn("alice", self.scheduler.records)
        await bot.stop_monitor(FakeMessage("/stop @ALICE"))
        self.assertNotIn("alice", self.scheduler.records)
        self.assertEqual(self.checker.metrics.request_attempts, 0)

    async def test_anonymous_add_and_bulk_rejected(self):
        await bot.add_monitor(FakeMessage("/monitor alice", user=None))
        await bot.add_bulk(FakeMessage("/bulk alice bob", user=None))
        self.assertFalse(self.scheduler.records)

    async def test_active_chat_scope_and_pending_delivery_label(self):
        await bot.add_monitor(FakeMessage("/monitor alice"))
        await bot.add_monitor(FakeMessage("/monitor bob", chat=9))
        self.scheduler.records["alice"].pending = CheckResult("active")
        message = FakeMessage("/active")
        await bot.show_active(message)
        self.assertIn("@alice", message.answers[0])
        self.assertIn("delivery pending", message.answers[0])
        self.assertNotIn("@bob", message.answers[0])

    async def test_stop_readd_during_card_generation_cannot_send_stale_alert(self):
        async def render(*args, **kwargs):
            self.scheduler.stop("alice", 1, 2)
            self.scheduler.add("alice", 1, 2, "ban")
            # Cancellation may race completion; identity guard is still mandatory.
            return BytesIO(b"old card")
        self.card.side_effect = render
        old = self.scheduler.add("alice", 1, 2, "unban")
        old.pending = CheckResult("active")
        await bot.trigger_alert(old, lambda: self.scheduler.current(old))
        self.telegram.send_photo.assert_not_awaited()
        self.telegram.send_message.assert_not_awaited()
        self.assertEqual(self.scheduler.records["alice"].mode, "ban")

    async def test_stop_readd_during_photo_error_blocks_text_fallback(self):
        async def send(*args, **kwargs):
            self.scheduler.stop("alice", 1, 2)
            self.scheduler.add("alice", 1, 2, "ban")
            raise RuntimeError("failed after stop")
        self.telegram.send_photo.side_effect = send
        old = self.scheduler.add("alice", 1, 2, "unban")
        old.pending = CheckResult("active")
        delivered = await bot.trigger_alert(old, lambda: self.scheduler.current(old))
        self.assertFalse(delivered)
        self.telegram.send_message.assert_not_awaited()
        self.assertIsNot(self.scheduler.records["alice"], old)

    async def test_stop_readd_after_success_does_not_remove_new_monitor(self):
        async def send(*args, **kwargs):
            self.scheduler.stop("alice", 1, 2)
            self.scheduler.add("alice", 1, 2, "ban")
        self.telegram.send_photo.side_effect = send
        old = self.scheduler.add("alice", 1, 2, "unban")
        old.pending = CheckResult("active")
        self.assertFalse(await bot.trigger_alert(old, lambda: self.scheduler.current(old)))
        self.assertEqual(self.scheduler.records["alice"].mode, "ban")


class AvatarTests(unittest.IsolatedAsyncioTestCase):
    def test_only_https_instagram_cdn_avatar_urls(self):
        self.assertTrue(bot.safe_avatar_url("https://scontent.cdninstagram.com/a?x=1&y=2"))
        self.assertTrue(bot.safe_avatar_url("https://instagram.fabc.fbcdn.net/avatar.jpg"))
        for url in ("http://scontent.cdninstagram.com/a", "https://evil.test/a", "file:///tmp/a",
                    "https://127.0.0.1/a", "https://scontent.cdninstagram.com.evil.test/a",
                    "https://evilcdninstagram.com/a", "https://u:p@scontent.cdninstagram.com/a",
                    "https://scontent.cdninstagram.com:444/a", "https://[::1]/a", "invalid"):
            with self.subTest(url=url):
                self.assertFalse(bot.safe_avatar_url(url))

    async def test_unsafe_urls_never_construct_network_session(self):
        with patch.object(bot.aiohttp, "ClientSession", side_effect=AssertionError("No network")):
            self.assertIsNone(await bot.download_avatar("http://127.0.0.1/private"))

    async def test_private_dns_result_rejected_before_connect(self):
        resolver = bot.PublicAvatarResolver()
        try:
            with patch.object(resolver.resolver, "resolve", AsyncMock(return_value=[{"host": "127.0.0.1"}])):
                with self.assertRaises(OSError):
                    await resolver.resolve("scontent.cdninstagram.com", 443)
            with patch.object(resolver.resolver, "resolve", AsyncMock(return_value=[{"host": "10.0.0.1"}])):
                with self.assertRaises(OSError):
                    await resolver.resolve("scontent.cdninstagram.com", 443)
        finally:
            await resolver.close()

    async def fake_download(self, response):
        session = AvatarSession(response)
        response.content_length = None
        with patch.object(bot.aiohttp, "ClientSession", return_value=session) as factory:
            result = await bot.download_avatar("https://scontent.cdninstagram.com/avatar.jpg")
            # The fake session does not own its real connector; close it explicitly.
            await factory.call_args.kwargs["connector"].close()
        self.assertTrue(response.closed)
        self.assertFalse(session.calls[0][1]["allow_redirects"])
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        return result, response

    async def test_avatar_redirect_and_encoding_do_not_read_body(self):
        for response in (FakeResponse(status=302, forbid_read=True),
                         FakeResponse(headers={"Content-Encoding": "gzip"}, forbid_read=True)):
            result, response = await self.fake_download(response)
            self.assertIsNone(result)
            self.assertEqual(response.content.reads, 0)

    async def test_avatar_read_limit_and_small_image(self):
        result, response = await self.fake_download(FakeResponse(b"x" * (2 * 1024 * 1024 + 1), chunk=16384))
        self.assertIsNone(result)
        self.assertEqual(response.content.position, 2 * 1024 * 1024)
        result, _ = await self.fake_download(FakeResponse(b"fake small image"))
        self.assertEqual(result, b"fake small image")

    async def test_card_layout_still_1000_by_550_and_no_extra_request_without_avatar(self):
        with patch.object(bot, "download_avatar", AsyncMock(side_effect=AssertionError("No avatar request"))) as download:
            for unavailable in (False, True):
                card = await bot.create_profile_card("alice", 0, 3, 2, is_unavailable=unavailable)
                with Image.open(card) as image:
                    self.assertEqual(image.size, (1000, 550))
                    self.assertEqual(image.getpixel((0, 0)), (0, 0, 0))
            download.assert_not_awaited()

    async def test_oversize_image_dimensions_fall_back_to_local_avatar(self):
        source = SimpleNamespace(width=20000, height=20000)
        class ImageContext:
            def __enter__(self):
                return source
            def __exit__(self, *args):
                return False
        with patch.object(bot, "download_avatar", AsyncMock(return_value=b"fake")), patch.object(bot.Image, "open", return_value=ImageContext()):
            card = await bot.create_profile_card("alice", 0, 0, 0, pic_url="https://scontent.cdninstagram.com/a")
            self.assertGreater(len(card.getvalue()), 0)


class AvatarSession(FakeSession):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class StartupConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_session_placeholder_warns_safely_before_bot_construction(self):
        env = {**ENV, "IG_SESSIONID": "optional_instagram_session_cookie_here"}
        with patch.object(bot.os, "environ", env), patch.object(bot, "load_dotenv") as dotenv, \
                patch.object(bot.logging, "basicConfig"), patch.object(bot.logging, "getLogger"), \
                patch.object(bot.logging, "warning") as warning, \
                patch.object(bot, "Bot", side_effect=RuntimeError("offline startup boundary")), \
                patch.object(bot, "profile_session", side_effect=AssertionError("No network session")):
            with self.assertRaisesRegex(RuntimeError, "offline startup boundary"):
                await bot.main()
            dotenv.assert_called_once_with()  # Mocked: no .env is read.
            warning.assert_called_once_with(
                "Example IG_SESSIONID ignored; HTML checks do not require a session cookie")
            self.assertNotIn(env["IG_SESSIONID"], str(warning.call_args))

    async def test_legacy_session_placeholder_json_rejected_before_bot_construction(self):
        env = {**ENV, "IG_SESSIONID": "optional_instagram_session_cookie_here", "IG_CHECK_MODE": "json"}
        with patch.object(bot.os, "environ", env), patch.object(bot, "load_dotenv"), \
                patch.object(bot, "Bot") as construct, \
                patch.object(bot, "profile_session", side_effect=AssertionError("No network session")):
            with self.assertRaisesRegex(SystemExit, "real IG_SESSIONID") as caught:
                await bot.main()
            construct.assert_not_called()
            self.assertNotIn(env["IG_SESSIONID"], str(caught.exception))


class ImportSafetyTests(unittest.TestCase):
    def test_import_never_loads_dotenv_constructs_bot_or_client(self):
        with patch("dotenv.load_dotenv", side_effect=AssertionError("No .env access")), \
                patch("aiogram.Bot", side_effect=AssertionError("No bot construction")), \
                patch("aiohttp.ClientSession", side_effect=AssertionError("No network session")):
            importlib.reload(bot)
            self.assertIsNone(bot.bot)
            self.assertIsNone(bot.scheduler)
        # Restore actual imported dependency functions, still without startup.
        importlib.reload(bot)

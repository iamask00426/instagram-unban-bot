import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import format_datetime
import gzip
import json
import unittest
from unittest.mock import AsyncMock
import zlib

import aiohttp
from aiohttp import web

from config import Settings
from monitoring import (CheckResult, MonitorScheduler, ProfileChecker,
                        backoff, profile_session, retry_after_seconds)


SETTINGS = Settings(bot_token="offline", proxies=("http://proxy.invalid:8080",))


def profile(username="alice", image=True):
    return (f'<meta content="https://www.instagram.com/{username}/" property="og:url">'
            + ('<meta property="og:image" content="https://scontent.cdninstagram.com/avatar.jpg?a=1&amp;b=2">' if image else '')
            + '<meta content="0 Followers, 2 Following, 3 Posts - café" property="og:description">').encode()


def json_user(username="alice", followers=0):
    return {"data": {"user": {"username": username, "edge_followed_by": {"count": followers},
                             "edge_follow": {"count": 2}, "edge_owner_to_timeline_media": {"count": 3},
                             "profile_pic_url": "https://scontent.cdninstagram.com/avatar.jpg"}}}


class FakeContent:
    def __init__(self, data=b"", chunk=1024, forbid_read=False):
        self.data, self.chunk, self.forbid_read = data, chunk, forbid_read
        self.position = 0
        self.reads = 0

    async def read(self, n):
        self.reads += 1
        if self.forbid_read:
            raise AssertionError("Error/redirect body must not be read")
        end = min(len(self.data), self.position + min(n, self.chunk))
        value = self.data[self.position:end]
        self.position = end
        return value

    def at_eof(self):
        return self.position == len(self.data)


class FakeResponse:
    def __init__(self, data=b"", status=200, headers=None, chunk=1024, forbid_read=False):
        self.status, self.headers = status, headers or {}
        self.content = FakeContent(data, chunk, forbid_read)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.close()

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response):
        self.response, self.calls = response, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class ProfileCheckerTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, data, *, settings=SETTINGS, **kwargs):
        response = FakeResponse(data, **kwargs)
        session = FakeSession(response)
        checker = ProfileChecker(session, settings)
        result = await checker.check("alice")
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(session.calls[0][1]["allow_redirects"])
        self.assertFalse(session.calls[0][1]["auto_decompress"])
        self.assertTrue(response.closed)
        self.assertEqual(checker.metrics.request_attempts, 1)
        self.assertEqual(checker.metrics.outcomes[result.status], 1)
        return result, response, checker

    async def test_verified_prefix_after_8k_stops_early_and_reuses_metadata(self):
        data = b"<html><head><!--" + b"x" * 9200 + b"-->" + profile() + b"x" * 250000
        result, response, checker = await self.check(data)
        self.assertEqual(result.status, "active")
        self.assertEqual((result.followers, result.following, result.posts), ("0", "2", "3"))
        self.assertEqual(result.pic_url, "https://scontent.cdninstagram.com/avatar.jpg?a=1&b=2")
        self.assertLess(response.content.position, 11000)
        self.assertEqual(checker.metrics.consumed_body_bytes, response.content.position)
        self.assertLess(response.content.position, len(data))

    async def test_arbitrary_chunk_splits_utf8_entities_case_and_quotes(self):
        data = ("<HEAD><TITLE>café</TITLE><META CONTENT='Alice (&#64;alice) • Instagram' PROPERTY='OG:TITLE'>"
                "<META content='https://scontent.cdninstagram.com/a?x=1&amp;y=2' property=og:image>"
                "<MeTa CoNtEnT='0 Followers, 0 Following, 0 Posts - café' PrOpErTy=og:description>").encode()
        for chunk in (1, 2, 3, 7, 31, 1000):
            with self.subTest(chunk=chunk):
                result, _, _ = await self.check(data, chunk=chunk)
                self.assertEqual(result.status, "active")
                self.assertEqual(result.pic_url, "https://scontent.cdninstagram.com/a?x=1&y=2")
                self.assertEqual(result.posts, "0")

    async def test_missing_avatar_does_not_trigger_another_request(self):
        result, response, _ = await self.check(profile(image=False) + b"x" * 50000)
        self.assertEqual(result.status, "active")
        self.assertIsNone(result.pic_url)
        self.assertLess(response.content.position, 2000)

    async def test_title_identity_alone_is_grounded(self):
        for title in ("Alice (@ALICE)", "Alice (@ALICE) on Instagram", "Alice (@ALICE) • Instagram",
                      "Alice (@ALICE) • Instagram photos and videos"):
            data = profile().replace(b'https://www.instagram.com/alice/', title.encode()).replace(b'og:url', b'og:title')
            for chunk in (1, 2, 7, 31, 1024):
                with self.subTest(title=title, chunk=chunk):
                    result, _, _ = await self.check(data, chunk=chunk)
                    self.assertEqual(result.status, "active")

    async def test_title_embedded_ambiguous_and_conflicting_handles_unknown(self):
        for title in ("Fan (@alice) (@bob) • Instagram photos and videos",
                      "Fan (@bob) (@alice) • Instagram photos and videos",
                      "Fan (@alice) (@alice) • Instagram photos and videos",
                      "Fan @bob (@alice) • Instagram photos and videos",
                      "Fan (@alice) posts about Bob", "Fan (@alice) is on Instagram"):
            prefix = (f'<meta property="og:title" content="{title}">'
                      '<meta property="og:description" content="1 Followers, 2 Following, 3 Posts">').encode()
            for tail in (b"", b'<meta property="og:url" content="https://www.instagram.com/bob/">'):
                for chunk in (1, 2, 7, 31, 1024):
                    with self.subTest(title=title, chunk=chunk, canonical=bool(tail)):
                        result, _, _ = await self.check(prefix + tail, chunk=chunk)
                        self.assertEqual(result.status, "unknown")

    async def test_mismatch_generic_login_and_missing_identity_are_unknown(self):
        examples = [
            profile("bob"),
            b"<head><title>Instagram</title></head>",
            b"<head><title>Page Not Found</title></head>",
            b"<title>Login \xe2\x80\xa2 Instagram</title>" + profile(),
            b"<title>Log in to Instagram</title>" + profile(),
            b"<title>Challenge</title>" + profile(),
            b'<meta property="og:description" content="1 Followers, 2 Following, 3 Posts">',
            profile().replace(b"www.instagram.com", b"www.instagram.com.evil.test"),
            profile().replace(b"www.instagram.com", b"evil.test@www.instagram.com"),
            profile().replace(b"https://", b"ftp://"),
            b'<meta property="og:title" content="Bob (@bob)">' + profile(),
            b'<meta property="og:url" content="https://instagram.com/bob/">' + profile(),
        ]
        for data in examples:
            with self.subTest(data=data[:70]):
                result, _, _ = await self.check(data)
                self.assertEqual(result.status, "unknown")

    async def test_incomplete_malformed_and_no_counts_are_unknown(self):
        for data in (profile()[:-2], b'<meta content="unfinished', profile().replace(b"0 Followers", b"-1 Followers"),
                     profile().replace(b"3 Posts", b"many Posts"),
                     profile().replace(b'property="og:url"', b'property="og:url" property="og:title"'),
                     b"\xff" + profile()):
            with self.subTest(data=data[:50]):
                result, _, _ = await self.check(data)
                self.assertEqual(result.status, "unknown")

    async def test_head_or_body_boundary_prevents_later_evidence(self):
        for boundary in (b"</head>", b"<body>", b"</html>"):
            result, response, _ = await self.check(b"<head>" + boundary + profile() + b"x" * 5000)
            self.assertEqual(result.status, "unknown")
            self.assertLessEqual(response.content.position, 1024)

    async def test_cap_exact_and_one_byte_beyond(self):
        data = profile(image=False)
        for limit, expected in ((len(data), "active"), (len(data) - 1, "unknown"), (len(data) + 1, "active")):
            result, response, checker = await self.check(data + b"x" * 10000, settings=replace(SETTINGS, body_limit=limit))
            self.assertEqual(result.status, expected)
            self.assertLessEqual(checker.metrics.consumed_body_bytes, limit)
            self.assertLessEqual(response.content.position, limit)

    async def test_empty_and_default_128k_cap(self):
        result, _, _ = await self.check(b"")
        self.assertEqual(result.status, "unknown")
        result, response, checker = await self.check(b"x" * 200000 + profile())
        self.assertEqual(result.status, "unknown")
        self.assertEqual(checker.metrics.consumed_body_bytes, 128 * 1024)
        self.assertEqual(response.content.position, 128 * 1024)

    async def test_gzip_and_deflate_streaming(self):
        data = b"<head><!--" + b"x" * 9222 + b"-->" + profile() + b"x" * 1000000
        for encoding, compress in (("gzip", gzip.compress), ("deflate", zlib.compress)):
            result, _, checker = await self.check(compress(data), headers={"Content-Encoding": encoding}, chunk=23)
            self.assertEqual(result.status, "active")
            self.assertLess(checker.metrics.consumed_body_bytes, 11000)
            self.assertLess(checker.metrics.encoded_body_bytes, 1000)

    async def test_compression_bomb_and_bad_encodings_are_bounded(self):
        result, _, checker = await self.check(gzip.compress(b"x" * 5_000_000 + profile()),
                                               headers={"Content-Encoding": "gzip"})
        self.assertEqual(result.status, "unknown")
        self.assertEqual(checker.metrics.consumed_body_bytes, SETTINGS.body_limit)
        for encoding in ("gzip", "deflate", "br"):
            result, _, _ = await self.check(b"not encoded", headers={"Content-Encoding": encoding})
            self.assertEqual(result.status, "unknown")

    async def test_status_first_never_reads_known_error_redirect_body_or_falls_back(self):
        for mode in ("html", "json"):
            for status, expected in ((404, "unavailable"), (400, "error"), (402, "error"), (500, "error"),
                                     (401, "challenge"), (403, "challenge"), (429, "challenge"),
                                     (301, "unknown"), (302, "unknown"), (307, "unknown"), (204, "error")):
                with self.subTest(status=status, mode=mode):
                    result, response, checker = await self.check(profile(), status=status, forbid_read=True,
                        settings=replace(SETTINGS, mode=mode, session_id="offline_cookie"), headers={"Retry-After": "45"})
                    self.assertEqual(result.status, expected)
                    self.assertEqual(result.retry_after, 45)
                    self.assertEqual(response.content.reads, 0)
                    self.assertEqual(checker.metrics.consumed_body_bytes, 0)

    async def test_json_identity_zero_followers_and_counts_reused(self):
        data = json.dumps(json_user()).encode()
        result, _, _ = await self.check(data, settings=replace(SETTINGS, mode="json"), chunk=1)
        self.assertEqual(result.status, "active")
        self.assertEqual((result.followers, result.following, result.posts), ("0", "2", "3"))
        self.assertIsNotNone(result.pic_url)

    async def test_json_bad_schema_missing_user_and_mismatch_unknown(self):
        for data in ({}, [], {"data": None}, {"data": []}, {"data": {"user": None}}, json_user("bob"),
                     json_user(followers=None), json_user(followers=False), json_user(followers=-1),
                     json_user(followers="0"), json_user(followers=10**100), {"data": {"user": {"username": "alice"}}}):
            with self.subTest(data=data):
                result, _, _ = await self.check(json.dumps(data).encode(), settings=replace(SETTINGS, mode="json"))
                self.assertEqual(result.status, "unknown")

    async def test_json_incomplete_and_exact_cap(self):
        data = json.dumps(json_user()).encode()
        for chunk in (1, 7, 1024):
            for limit, expected in ((len(data), "active"), (len(data) - 1, "unknown"), (len(data) + 1, "active")):
                with self.subTest(chunk=chunk, limit=limit):
                    result, _, checker = await self.check(data, chunk=chunk,
                        settings=replace(SETTINGS, mode="json", body_limit=limit))
                    self.assertEqual(result.status, expected)
                    self.assertLessEqual(checker.metrics.consumed_body_bytes, limit)
                    self.assertLessEqual(checker.metrics.encoded_body_bytes, limit)
            for tail in (b"garbage", b" ", b"{}"):
                result, _, _ = await self.check(data + tail, chunk=chunk,
                    settings=replace(SETTINGS, mode="json", body_limit=len(data)))
                self.assertEqual(result.status, "unknown")  # A valid capped prefix is not EOF.
        result, _, _ = await self.check(data[:-2], settings=replace(SETTINGS, mode="json"))
        self.assertEqual(result.status, "unknown")

    async def test_json_trailing_garbage_and_concatenated_documents_chunk_invariant(self):
        data = json.dumps(json_user()).encode()
        for tail in (b"garbage", data, b"\n{}", b"\x00"):
            for chunk in (1, 2, 7, len(data), 1024):
                with self.subTest(tail=tail[:10], chunk=chunk):
                    result, response, _ = await self.check(data + tail, chunk=chunk,
                                                          settings=replace(SETTINGS, mode="json"))
                    self.assertEqual(result.status, "unknown")
                    self.assertEqual(response.content.position, len(data + tail))

    async def test_json_utf8_final_state_and_valid_trailing_whitespace(self):
        data = json.dumps({**json_user(), "display_name": "café"}, ensure_ascii=False).encode()
        for tail, expected in ((b" \r\n\t", "active"), (b"\xc3", "unknown"), (b"\xff", "unknown")):
            for chunk in (1, 2, 7, 1024):
                with self.subTest(tail=tail, chunk=chunk):
                    result, _, _ = await self.check(data + tail, chunk=chunk,
                                                    settings=replace(SETTINGS, mode="json"))
                    self.assertEqual(result.status, expected)

    async def test_json_cap_without_verified_transport_eof_unknown(self):
        data = json.dumps(json_user()).encode()
        response = FakeResponse(data)
        response.content.at_eof = lambda: False  # Transport has not delivered its ending yet.
        checker = ProfileChecker(FakeSession(response), replace(SETTINGS, mode="json", body_limit=len(data)))
        self.assertEqual((await checker.check("alice")).status, "unknown")
        self.assertEqual(response.content.position, len(data))
        self.assertTrue(response.closed)

    async def test_json_compressed_requires_complete_stream_without_trailing_data(self):
        data = json.dumps(json_user()).encode()
        for encoding, compress in (("gzip", gzip.compress), ("deflate", zlib.compress)):
            encoded = compress(data)
            for payload, expected in ((encoded, "active"), (encoded[:-1], "unknown"),
                                      (encoded + b"garbage", "unknown"),
                                      (encoded + compress(b"{}"), "unknown"),
                                      (compress(data + b"garbage"), "unknown")):
                for chunk in (1, 7, len(encoded), 1024):
                    with self.subTest(encoding=encoding, length=len(payload), chunk=chunk):
                        result, _, checker = await self.check(payload, chunk=chunk,
                            headers={"Content-Encoding": encoding}, settings=replace(SETTINGS, mode="json"))
                        self.assertEqual(result.status, expected)
                        self.assertLessEqual(checker.metrics.consumed_body_bytes, SETTINGS.body_limit)
                        self.assertLessEqual(checker.metrics.encoded_body_bytes, SETTINGS.body_limit)

    async def test_stream_exception_closes_response_and_counts_partial_consumption(self):
        response = FakeResponse(b"<head>" + b"x" * 1024)
        original_read = response.content.read
        async def read(n):
            if response.content.reads:
                raise aiohttp.ClientPayloadError("truncated response")
            return await original_read(n)
        response.content.read = read
        checker = ProfileChecker(FakeSession(response), SETTINGS)
        result = await checker.check("alice")
        self.assertEqual(result.status, "error")
        self.assertTrue(response.closed)
        self.assertEqual(checker.metrics.consumed_body_bytes, 1024)
        self.assertEqual(checker.metrics.outcomes["error"], 1)

    async def test_no_proxy_cannot_construct_checker(self):
        with self.assertRaises(ValueError):
            ProfileChecker(FakeSession(FakeResponse()), replace(SETTINGS, proxies=()))

    async def test_compressed_exact_and_over_decoded_cap(self):
        data = profile(image=False)
        for limit, expected in ((len(data), "active"), (len(data) - 1, "unknown")):
            result, _, checker = await self.check(gzip.compress(data), headers={"Content-Encoding": "gzip"},
                settings=replace(SETTINGS, body_limit=limit))
            self.assertEqual(result.status, expected)
            self.assertLessEqual(checker.metrics.consumed_body_bytes, limit)

    async def test_cancellation_closes_active_response(self):
        response = FakeResponse()
        waiting = asyncio.Event()
        async def read(n):
            waiting.set()
            await asyncio.Event().wait()
        response.content.read = read
        checker = ProfileChecker(FakeSession(response), SETTINGS)
        task = asyncio.create_task(checker.check("alice"))
        await waiting.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(response.closed)
        self.assertEqual(checker.metrics.request_attempts, 1)
        self.assertEqual(checker.metrics.outcomes["cancelled"], 1)

    async def test_single_get_json_endpoint_no_html_fallback_on_exception(self):
        for exception in (aiohttp.ClientError("sensitive proxy details"), TimeoutError(), RuntimeError("unexpected")):
            session = FakeSession(exception)
            checker = ProfileChecker(session, replace(SETTINGS, mode="json", session_id="offline"))
            self.assertEqual((await checker.check("alice")).status, "error")
            self.assertEqual(len(session.calls), 1)
            self.assertIn("/api/v1/", session.calls[0][0])
            self.assertEqual(checker.metrics.request_attempts, 1)
            self.assertEqual(checker.metrics.outcomes["error"], 1)

    async def test_html_no_cookie_no_head_and_round_robin_proxies(self):
        session = FakeSession(FakeResponse(profile()))
        checker = ProfileChecker(session, replace(SETTINGS, proxies=("http://one.invalid:80", "http://two.invalid:80")))
        for _ in range(3):
            await checker.check("alice")
        self.assertEqual([call[1]["proxy"] for call in session.calls],
                         ["http://one.invalid:80", "http://two.invalid:80", "http://one.invalid:80"])
        for url, options in session.calls:
            self.assertEqual(url, "https://www.instagram.com/alice/")
            self.assertNotIn("Cookie", options["headers"])

    async def test_shared_session_configuration(self):
        async with profile_session(SETTINGS) as session:
            self.assertIsInstance(session.cookie_jar, aiohttp.DummyCookieJar)
            self.assertFalse(session.trust_env)
            self.assertFalse(session.auto_decompress)
            self.assertFalse(getattr(session, "_retry_connection", False))
            self.assertEqual(session.connector.limit, SETTINGS.concurrency)
            self.assertEqual(session.connector.limit_per_host, SETTINGS.concurrency)
        self.assertTrue(session.closed)


class RetryAfterTests(unittest.TestCase):
    def test_delta_date_malformed_past_and_clamped(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        for value, expected in ((None, 0), ("", 0), ("0", 0), (" 15 ", 15), ("90000", 300), ("9" * 1000, 300),
                                ("-1", 0), ("nan", 0), ("1.5", 0), ("garbage", 0),
                                (format_datetime(datetime.fromtimestamp(now + 45, timezone.utc)), 45),
                                (format_datetime(datetime.fromtimestamp(now - 45, timezone.utc)), 0)):
            with self.subTest(value=value):
                self.assertEqual(retry_after_seconds(value, 300, now), expected)

    def test_exponential_backoff_is_bounded(self):
        self.assertEqual([backoff(10, 300, n) for n in (1, 2, 3, 10, 100000)], [10, 20, 40, 300, 300])


class Clock:
    value = 100.0

    def __call__(self):
        return self.value


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = Clock()
        self.check = AsyncMock(return_value=CheckResult("active", "alice"))
        self.deliver = AsyncMock(return_value=True)
        self.scheduler = MonitorScheduler(SETTINGS, self.check, self.deliver, self.clock)

    async def asyncTearDown(self):
        await self.scheduler.close()

    async def turn(self):
        self.scheduler.dispatch_due()
        await asyncio.gather(*list(self.scheduler.tasks))

    def add(self, username="alice", mode="ban"):
        return self.scheduler.add(username, 1, 2, mode)

    async def test_one_immediate_check_no_duplicates_and_two_seconds_after_completion(self):
        record = self.add()
        self.assertIsNone(self.add())
        self.scheduler.dispatch_due()
        self.scheduler.dispatch_due()
        await asyncio.gather(*self.scheduler.tasks)
        self.check.assert_awaited_once_with("alice")
        self.assertEqual(record.next_due, 102)
        await self.turn()
        self.assertEqual(self.check.await_count, 1)
        self.clock.value = 102
        await self.turn()
        self.assertEqual(self.check.await_count, 2)

    async def test_interval_starts_after_completion_not_start(self):
        async def check(_):
            self.clock.value = 120
            return CheckResult("active")
        self.scheduler.check = check
        record = self.add()
        await self.turn()
        self.assertEqual(record.next_due, 122)

    async def test_normal_unavailable_wait_until_active_keeps_fast_interval(self):
        self.check.return_value = CheckResult("unavailable")
        record = self.add(mode="unban")
        await self.turn()
        self.assertEqual(record.next_due, 102)
        self.assertIsNone(record.pending)

    async def test_confirmed_unavailable_requires_spaced_consecutive_results(self):
        self.check.return_value = CheckResult("unavailable")
        record = self.add()
        await self.turn()
        self.assertIsNone(record.pending)
        self.assertEqual(record.next_due, 105)
        self.clock.value = 104.9
        await self.turn()
        self.assertEqual(self.check.await_count, 1)
        self.clock.value = 105
        await self.turn()
        self.assertEqual(record.pending.status, "unavailable")
        await self.turn()
        self.assertNotIn("alice", self.scheduler.records)
        self.deliver.assert_awaited_once()

    async def test_inconclusive_or_opposite_result_resets_confirmation(self):
        for status in ("unknown", "error", "challenge", "active"):
            record = self.scheduler.records.get("alice") or self.add()
            record.unavailable_count = 1
            record.next_due = self.clock()
            self.check.return_value = CheckResult(status)
            await self.turn()
            self.assertEqual(record.unavailable_count, 0)
            self.assertIsNone(record.pending)

    async def test_unknown_challenge_errors_retry_after_and_backoff_reset(self):
        record = self.add()
        for status, retry, delay in (("unknown", 0, 10), ("challenge", 70, 70),
                                     ("error", 0, 40), ("active", 0, 2), ("unknown", 0, 10)):
            self.check.return_value = CheckResult(status, retry_after=retry)
            await self.turn()
            self.assertEqual(record.next_due, self.clock() + delay)
            self.clock.value = record.next_due

    async def test_unexpected_exceptions_and_bad_result_defer_not_hot_loop(self):
        record = self.add()
        self.check.side_effect = RuntimeError("unexpected")
        await self.turn()
        self.assertEqual(record.next_due, 110)
        await self.turn()
        self.assertEqual(self.check.await_count, 1)
        self.clock.value = 110
        self.check.side_effect = None
        self.check.return_value = None
        await self.turn()
        self.assertEqual(record.next_due, 130)
        self.clock.value = 130
        self.check.return_value = CheckResult("unknown", retry_after="bad")
        await self.turn()
        self.assertGreater(record.next_due, 130)

    async def test_pending_alert_failures_retry_cached_result_without_instagram_checks(self):
        record = self.add(mode="unban")
        self.deliver.side_effect = [False, RuntimeError("Telegram down"), True]
        await self.turn()
        self.assertIsNotNone(record.pending)
        await self.turn()
        self.assertEqual(record.next_due, 105)
        self.clock.value = 104
        await self.turn()
        self.assertEqual(self.deliver.await_count, 1)
        self.clock.value = 105
        await self.turn()
        self.assertEqual(record.next_due, 115)
        self.clock.value = 115
        await self.turn()
        self.check.assert_awaited_once()
        self.assertEqual(self.deliver.await_count, 3)
        self.assertNotIn("alice", self.scheduler.records)

    async def test_delivery_backoff_bounded(self):
        record = self.add(mode="unban")
        record.pending = CheckResult("active")
        record.delivery_failures = 100
        self.deliver.return_value = False
        await self.turn()
        self.assertEqual(record.next_due, 400)
        self.check.assert_not_awaited()

    async def test_bounded_concurrency_and_fairness_with_many_immediate_adds(self):
        settings = replace(SETTINGS, concurrency=2)
        gates, started, active = {}, [], set()
        async def check(username):
            started.append(username)
            active.add(username)
            self.assertLessEqual(len(active), 2)
            await gates.setdefault(username, asyncio.Event()).wait()
            active.remove(username)
            return CheckResult("active")
        self.scheduler = MonitorScheduler(settings, check, self.deliver, self.clock)
        for username in ("a", "b", "c", "d", "e"):
            self.add(username)
        self.scheduler.dispatch_due()
        await asyncio.sleep(0)
        self.assertEqual(started, ["a", "b"])
        self.scheduler.dispatch_due()
        self.assertEqual(len(self.scheduler.tasks), 2)
        for username in ("a", "b"):
            gates[username].set()
        await asyncio.gather(*self.scheduler.tasks)
        self.clock.value = 102
        self.scheduler.dispatch_due()
        await asyncio.sleep(0)
        self.assertEqual(started, ["a", "b", "c", "d"])
        for username in ("c", "d"):
            gates[username].set()
        await asyncio.gather(*self.scheduler.tasks)
        self.scheduler.dispatch_due()
        await asyncio.sleep(0)
        self.assertEqual(started[4], "e")
        for event in gates.values():
            event.set()
        await asyncio.gather(*self.scheduler.tasks)

    async def test_delivery_shares_total_concurrency_bound(self):
        self.scheduler.settings = replace(SETTINGS, concurrency=1)
        record = self.add(mode="unban")
        record.pending = CheckResult("active")
        self.add("bob")
        gate = asyncio.Event()
        async def deliver(*_):
            await gate.wait()
            return True
        self.scheduler.deliver = deliver
        self.scheduler.dispatch_due()
        await asyncio.sleep(0)
        self.check.assert_not_awaited()
        self.assertEqual(len(self.scheduler.tasks), 1)
        gate.set()
        await asyncio.gather(*self.scheduler.tasks)
        await self.turn()
        self.check.assert_awaited_once_with("bob")

    async def test_stop_requires_matching_chat_and_owner(self):
        record = self.add()
        self.assertFalse(self.scheduler.stop("alice", 1, 999))
        self.assertFalse(self.scheduler.stop("alice", 999, 2))
        self.assertIs(self.scheduler.records["alice"], record)
        self.assertTrue(self.scheduler.stop("alice", 1, 2))

    async def test_stopped_readded_check_cannot_mark_new_record_pending(self):
        started = asyncio.Event()
        async def check(_):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return CheckResult("active")  # emulate transport that races cancellation
        self.scheduler.check = check
        old = self.add(mode="unban")
        self.scheduler.dispatch_due()
        await started.wait()
        self.assertTrue(self.scheduler.stop("alice", 1, 2))
        new = self.add(mode="unban")
        self.scheduler.dispatch_due()
        self.assertEqual(len(self.scheduler.tasks), 1)
        await asyncio.gather(*self.scheduler.tasks)
        self.assertIs(self.scheduler.records["alice"], new)
        self.assertIsNone(new.pending)
        self.assertIsNone(old.pending)
        self.deliver.assert_not_awaited()

    async def test_stopped_readded_delivery_cannot_remove_new_record(self):
        started = asyncio.Event()
        async def deliver(record, current):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.assertFalse(current())
                return True
        self.scheduler.deliver = deliver
        record = self.add(mode="unban")
        record.pending = CheckResult("active")
        self.scheduler.dispatch_due()
        await started.wait()
        self.scheduler.stop("alice", 1, 2)
        new = self.add()
        await asyncio.gather(*self.scheduler.tasks)
        self.assertIs(self.scheduler.records["alice"], new)

    async def test_real_run_wakes_for_first_check_and_shuts_down_cleanly(self):
        runner = asyncio.create_task(self.scheduler.run())
        try:
            await asyncio.sleep(0)
            self.add(mode="unban")
            for _ in range(30):
                await asyncio.sleep(0)
                if self.deliver.await_count:
                    break
            self.check.assert_awaited_once()
            self.deliver.assert_awaited_once()
        finally:
            runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runner
        self.assertTrue(self.scheduler.closing)
        self.assertFalse(self.scheduler.tasks)

    async def test_close_cancels_inflight_work(self):
        async def wait(_):
            await asyncio.sleep(60)
        self.scheduler.check = wait
        self.add()
        self.scheduler.dispatch_due()
        tasks = list(self.scheduler.tasks)
        await asyncio.sleep(0)
        await self.scheduler.close()
        self.assertTrue(all(task.cancelled() for task in tasks))
        self.assertFalse(self.scheduler.tasks)


class LocalStreamingTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnected_get_is_not_retried_by_aiohttp(self):
        requests = []
        async def disconnect(reader, writer):
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(disconnect, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            async with profile_session(SETTINGS) as session:
                class LocalOnlySession:
                    def get(self, url, **kwargs):
                        kwargs.pop("proxy")
                        return session.get(f"http://127.0.0.1:{port}/alice/", **kwargs)
                checker = ProfileChecker(LocalOnlySession(), SETTINGS)
                self.assertEqual((await checker.check("alice")).status, "error")
                self.assertEqual(len(requests), 1)
                self.assertEqual(checker.metrics.request_attempts, 1)
        finally:
            server.close()
            await server.wait_closed()

    async def test_loopback_redirect_is_not_followed_and_cookies_not_accumulated(self):
        requests = []
        async def handler(request):
            requests.append(request.path)
            response = web.Response(status=302, headers={"Location": "/other"})
            response.set_cookie("unwanted", "offline")
            return response
        app = web.Application()
        app.router.add_get("/{name}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with profile_session(SETTINGS) as session:
                class LocalOnlySession:
                    def get(self, url, **kwargs):
                        kwargs.pop("proxy")
                        return session.get(f"http://127.0.0.1:{port}/alice", **kwargs)
                checker = ProfileChecker(LocalOnlySession(), SETTINGS)
                self.assertEqual((await checker.check("alice")).status, "unknown")
                self.assertEqual(requests, ["/alice"])
                self.assertEqual(len(session.cookie_jar), 0)
        finally:
            await runner.cleanup()

    async def test_loopback_real_gzip_stream_closes_without_draining_tail(self):
        closed = asyncio.Event()
        written = 0
        async def handler(request):
            nonlocal written
            response = web.StreamResponse(headers={"Content-Type": "text/html", "Content-Encoding": "gzip"})
            await response.prepare(request)
            encoder = zlib.compressobj(wbits=16 + zlib.MAX_WBITS)
            prefix = b"<head><!--" + b"x" * 9222 + b"-->" + profile()
            await response.write(encoder.compress(prefix) + encoder.flush(zlib.Z_SYNC_FLUSH))
            try:
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    chunk = encoder.compress(b"tail" * 16384) + encoder.flush(zlib.Z_SYNC_FLUSH)
                    await response.write(chunk)
                    written += 1
            except (ConnectionResetError, RuntimeError):
                closed.set()
            return response
        app = web.Application()
        app.router.add_get("/alice/", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with profile_session(SETTINGS) as session:
                class LocalOnlySession:
                    def get(self, url, **kwargs):
                        kwargs.pop("proxy")
                        return session.get(f"http://127.0.0.1:{port}/alice/", **kwargs)
                checker = ProfileChecker(LocalOnlySession(), SETTINGS)
                result = await checker.check("alice")
                self.assertEqual(result.status, "active")
                self.assertLess(checker.metrics.consumed_body_bytes, 11000)
                self.assertEqual(checker.metrics.request_attempts, 1)
                await asyncio.wait_for(closed.wait(), 2)
                self.assertLess(written, 100)
        finally:
            await runner.cleanup()

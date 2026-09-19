import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from monitoring import InstagramChecker, check_due_accounts, schedule_next


class FakeContent:
    def __init__(self, body, chunk_size=4096):
        self.body = body
        self.chunk_size = chunk_size
        self.bytes_read = 0
        self.reads = 0

    async def read(self, n):
        self.reads += 1
        data, self.body = self.body[:min(n, self.chunk_size)], self.body[min(n, self.chunk_size):]
        self.bytes_read += len(data)
        return data


class FakeResponse:
    def __init__(self, body=b"", status=200, headers=None, chunk_size=4096):
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(body, chunk_size)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class CheckerTests(unittest.IsolatedAsyncioTestCase):
    def checker(self, **kwargs):
        return InstagramChecker(lambda: "http://fake-proxy.invalid:80", **kwargs)

    async def test_html_stops_before_large_body(self):
        head = b'<html><head><meta content="12 Followers, 2 Following, 3 Posts" property="og:description"></head>'
        response = FakeResponse(head + b"x" * 1_000_000)
        session = FakeSession(response)
        checker = self.checker()
        result = await checker.check(session, "example")
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["followers"], "12")
        self.assertLessEqual(response.content.bytes_read, 4096)
        self.assertEqual(checker.body_bytes, response.content.bytes_read)
        self.assertTrue(response.closed)
        self.assertEqual(len(session.calls), 1)
        options = session.calls[0][1]
        self.assertFalse(options["allow_redirects"])
        self.assertEqual(options["headers"]["Accept-Encoding"], "gzip, deflate")
        self.assertEqual(options["proxy"], "http://fake-proxy.invalid:80")

    async def test_split_meta_and_utf8(self):
        body = ('<head><title>Éxample</title><meta property="og:image" content="https://example.com/p?a=1&amp;b=2">'
                '<meta property="og:description" content="0 Followers, 0 Following, 0 Posts"></head>').encode()
        response = FakeResponse(body, chunk_size=1)
        result = await self.checker().check(FakeSession(response), "example")
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["pic_url"], "https://example.com/p?a=1&b=2")

    async def test_html_cap_is_unknown_not_banned(self):
        response = FakeResponse(b"x" * 50_000)
        checker = self.checker(max_response_bytes=8192)
        result = await checker.check(FakeSession(response), "example")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(response.content.bytes_read, 8192)
        self.assertEqual(checker.capped_responses, 1)

    async def test_generic_title_not_banned(self):
        response = FakeResponse(b"<head><title>Instagram</title></head>" + b"x" * 50_000)
        result = await self.checker().check(FakeSession(response), "example")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(response.content.reads, 1)

    async def test_rate_limits_redirects_and_errors_do_not_read_body(self):
        for status in (301, 302, 401, 403, 429, 400, 404, 500, 503):
            with self.subTest(status=status):
                response = FakeResponse(b"x" * 50_000, status)
                session = FakeSession(response)
                result = await self.checker(session_id="test").check(session, "example")
                self.assertEqual(response.content.reads, 0)
                self.assertEqual(len(session.calls), 1)  # No fallback request.
                expected = "banned" if status == 404 else "unknown" if status in (400, 500, 503) else "rate_limited"
                self.assertEqual(result["status"], expected)

    async def test_shared_cooldown_and_retry_after(self):
        now = [100]
        checker = self.checker(clock=lambda: now[0])
        response = FakeResponse(status=429, headers={"Retry-After": "7200"})
        session = FakeSession(response)
        await checker.check(session, "one")
        await checker.check(session, "two")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(checker.cooldown_until, 7300)
        now[0] = 7301
        await checker.check(session, "two")
        self.assertEqual(len(session.calls), 2)

    async def test_repeated_limits_extend_cooldown(self):
        now = [0]
        checker = self.checker(clock=lambda: now[0])
        checker.rate_limited()
        self.assertEqual(checker.cooldown_until, 300)
        now[0] = 300
        checker.rate_limited()
        self.assertEqual(checker.cooldown_until, 900)

    async def test_login_page_cooldown(self):
        response = FakeResponse('<head><title>Login • Instagram</title></head>'.encode())
        checker = self.checker(clock=lambda: 0)
        result = await checker.check(FakeSession(response), "example")
        self.assertEqual(result["status"], "rate_limited")
        self.assertEqual(checker.cooldown_until, 300)

    async def test_zero_follower_api_account_is_active(self):
        data = {"data": {"user": {"username": "example", "edge_followed_by": {"count": 0}}}}
        response = FakeResponse(json.dumps(data).encode(), headers={"Content-Type": "application/json"})
        result = await self.checker(session_id="test").check(FakeSession(response), "example")
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["followers"], "0")

    async def test_missing_api_user_is_unknown(self):
        response = FakeResponse(b'{"data":{"user":null}}', headers={"Content-Type": "application/json"})
        result = await self.checker(session_id="test").check(FakeSession(response), "example")
        self.assertEqual(result["status"], "unknown")

    async def test_api_html_challenge_no_read_or_fallback(self):
        response = FakeResponse(b"x" * 50_000, headers={"Content-Type": "text/html"})
        session = FakeSession(response)
        result = await self.checker(session_id="test").check(session, "example")
        self.assertEqual(result["status"], "rate_limited")
        self.assertEqual(response.content.reads, 0)
        self.assertEqual(len(session.calls), 1)

    async def test_api_cap_and_malformed_json_do_not_fallback(self):
        for body in (b"x" * 50_000, b"broken"):
            response = FakeResponse(body, headers={"Content-Type": "application/json"})
            session = FakeSession(response)
            checker = self.checker(session_id="test", max_response_bytes=8192)
            result = await checker.check(session, "example")
            self.assertIn(result["status"], ("error", "unknown"))
            self.assertLessEqual(response.content.bytes_read, 8192)
            self.assertEqual(len(session.calls), 1)

    async def test_no_proxy_no_direct_request(self):
        checker = InstagramChecker(lambda: None)
        session = FakeSession(FakeResponse())
        self.assertEqual((await checker.check(session, "example"))["status"], "error")
        self.assertEqual(session.calls, [])


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    def test_interval_backoff_and_reset(self):
        record = {}
        schedule_next(record, "active", 60, 3600, now=100)
        self.assertGreaterEqual(record["next_check"], 160)
        self.assertLessEqual(record["next_check"], 166)
        schedule_next(record, "error", 60, 3600, now=100)
        self.assertGreaterEqual(record["next_check"], 220)
        schedule_next(record, "unknown", 60, 3600, now=100)
        self.assertGreaterEqual(record["next_check"], 340)
        for _ in range(20):
            schedule_next(record, "error", 60, 3600, now=100)
        self.assertLessEqual(record["next_check"], 4060)
        schedule_next(record, "banned", 60, 3600, now=100)
        self.assertEqual(record["failures"], 0)
        self.assertLessEqual(record["next_check"], 166)

    async def test_immediate_first_check_then_no_duplicate(self):
        checker = InstagramChecker(lambda: None)
        checker.check = AsyncMock(return_value={"status": "active"})
        accounts = {"one": {}}
        callback = AsyncMock()
        for _ in range(3):
            await check_due_accounts(accounts, None, checker, callback, 60, 3600, 3, clock=lambda: 100)
        self.assertEqual(checker.check.await_count, 1)
        self.assertEqual(callback.await_count, 1)

    async def test_bulk_concurrency_and_not_due_accounts(self):
        checker = InstagramChecker(lambda: None)
        inflight = 0
        peak = 0

        async def check(session, username):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0)
            inflight -= 1
            return {"status": "active"}

        checker.check = AsyncMock(side_effect=check)
        accounts = {str(i): {} for i in range(10)}
        callback = AsyncMock()
        for _ in range(4):
            await check_due_accounts(accounts, None, checker, callback, 60, 3600, 3, clock=lambda: 100)
        self.assertEqual(checker.check.await_count, 10)
        self.assertEqual(peak, 3)

    async def test_stop_and_readd_discards_stale_result(self):
        checker = InstagramChecker(lambda: None)
        old = {}
        accounts = {"one": old}

        async def check(session, username):
            accounts[username] = {}
            return {"status": "active"}

        checker.check = check
        callback = AsyncMock()
        await check_due_accounts(accounts, None, checker, callback, 60, 3600, 3, clock=lambda: 100)
        callback.assert_not_awaited()
        self.assertEqual(accounts["one"], {})
        self.assertIsNot(accounts["one"], old)

    async def test_shared_cooldown_skips_whole_batch(self):
        checker = InstagramChecker(lambda: None)
        checker.cooldown_until = 200
        checker.check = AsyncMock()
        await check_due_accounts({"one": {}}, None, checker, AsyncMock(), 60, 3600, 3, clock=lambda: 100)
        checker.check.assert_not_awaited()

    async def test_alert_failure_still_respects_interval(self):
        checker = InstagramChecker(lambda: None)
        checker.check = AsyncMock(return_value={"status": "active"})
        accounts = {"one": {}}
        callback = AsyncMock(side_effect=RuntimeError("Telegram unavailable"))
        with self.assertLogs(level="ERROR"):
            await check_due_accounts(accounts, None, checker, callback, 60, 3600, 3, clock=lambda: 100)
        await check_due_accounts(accounts, None, checker, callback, 60, 3600, 3, clock=lambda: 100)
        self.assertEqual(checker.check.await_count, 1)


if __name__ == "__main__":
    unittest.main()

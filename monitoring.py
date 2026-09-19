"""Bandwidth-conscious Instagram checks and per-account scheduling."""

import asyncio
import json
import logging
import random
import time
from html.parser import HTMLParser

import aiohttp


class ProfileHead(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.finished = False
        self.in_title = False
        self.title = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            self.meta[attrs.get("property", "")] = attrs.get("content", "")
        elif tag == "title":
            self.in_title = True
        elif tag == "body":
            self.finished = True

    def handle_endtag(self, tag):
        if tag == "head":
            self.finished = True
        elif tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data


def format_num(count):
    try:
        num = float(count)
        for divisor, suffix in ((1_000_000, "m"), (1_000, "k")):
            if num >= divisor:
                val = num / divisor
                return f"{val:.1f}{suffix}" if val % 1 else f"{int(val)}{suffix}"
        return str(int(num))
    except (TypeError, ValueError):
        return str(count)


class InstagramChecker:
    def __init__(self, next_proxy, session_id="", max_response_bytes=131072,
                 max_backoff=3600, clock=time.monotonic):
        self.next_proxy = next_proxy
        self.session_id = session_id
        self.max_response_bytes = max_response_bytes
        self.max_backoff = max_backoff
        self.clock = clock
        self.cooldown_until = 0.0
        self.rate_limit_streak = 0
        self.requests = 0
        # Application-consumed, decompressed bytes; NOT proxy billing totals.
        self.body_bytes = 0
        self.capped_responses = 0

    def rate_limited(self, retry_after=None):
        self.rate_limit_streak = min(self.rate_limit_streak + 1, 10)
        delay = min(self.max_backoff, 300 * 2 ** (self.rate_limit_streak - 1))
        try:
            # Retry-After may be delta seconds or an HTTP date.
            delay = max(delay, float(retry_after))
        except (TypeError, ValueError):
            if retry_after:
                from datetime import datetime, timezone
                from email.utils import parsedate_to_datetime
                try:
                    delay = max(delay, (parsedate_to_datetime(retry_after) -
                                       datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
        self.cooldown_until = max(self.cooldown_until, self.clock() + delay)
        return {"status": "rate_limited"}

    async def read_chunk(self, response, remaining):
        chunk = await response.content.read(min(4096, remaining))
        self.body_bytes += len(chunk)
        return chunk

    async def read_json(self, response):
        body = bytearray()
        while len(body) < self.max_response_bytes:
            chunk = await self.read_chunk(response, self.max_response_bytes - len(body))
            if not chunk:
                return json.loads(body)
            body.extend(chunk)
        self.capped_responses += 1
        return None

    async def read_profile_head(self, response):
        import codecs
        parser = ProfileHead()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        size = 0
        while size < self.max_response_bytes:
            chunk = await self.read_chunk(response, self.max_response_bytes - size)
            if not chunk:
                break
            size += len(chunk)
            parser.feed(decoder.decode(chunk))
            # No need for scripts/body or even the rest of the head once OG is found.
            if parser.finished or parser.meta.get("og:description") or "Login" in parser.title:
                return parser
        if size >= self.max_response_bytes:
            self.capped_responses += 1
        return parser

    async def check(self, session, username):
        import re
        if self.clock() < self.cooldown_until:
            return {"status": "rate_limited"}
        proxy = self.next_proxy()
        if not proxy:
            # Never silently switch Instagram traffic to the host IP.
            return {"status": "error"}
        headers = {"Accept-Encoding": "gzip, deflate", "Accept-Language": "en-US,en;q=0.9"}
        if self.session_id:
            url = "https://www.instagram.com/api/v1/users/web_profile_info/"
            params = {"username": username}
            headers.update({
                "User-Agent": "Mozilla/5.0",
                "X-IG-App-ID": "936619743392459",
                "Cookie": f"sessionid={self.session_id};",
                "Accept": "application/json",
            })
        else:
            url = f"https://www.instagram.com/{username}/"
            params = None
            headers.update({
                "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
                "Accept": "text/html",
            })
        try:
            self.requests += 1
            async with session.get(url, params=params, headers=headers, proxy=proxy,
                                   allow_redirects=False,
                                   timeout=aiohttp.ClientTimeout(total=10, sock_connect=3)) as response:
                if response.status == 404:
                    self.rate_limit_streak = 0
                    return {"status": "banned"}
                if response.status in (401, 403, 429) or 300 <= response.status < 400:
                    # Do not download challenge bodies or follow login redirects.
                    return self.rate_limited(response.headers.get("Retry-After"))
                if response.status != 200:
                    return {"status": "unknown"}
                if self.session_id:
                    if "json" not in response.headers.get("Content-Type", "").lower():
                        return self.rate_limited()
                    data = await self.read_json(response)
                    if not isinstance(data, dict):
                        return {"status": "unknown"}
                    user = (data.get("data") or {}).get("user")
                    if not isinstance(user, dict) or not user.get("username"):
                        return {"status": "unknown"}
                    self.rate_limit_streak = 0
                    # An existing account with zero followers is still active.
                    return {
                        "status": "active", "username": user["username"],
                        "followers": format_num((user.get("edge_followed_by") or {}).get("count", 0)),
                        "posts": format_num((user.get("edge_owner_to_timeline_media") or {}).get("count", 0)),
                        "following": format_num((user.get("edge_follow") or {}).get("count", 0)),
                        "pic_url": user.get("profile_pic_url", ""),
                    }
                head = await self.read_profile_head(response)
                if "Login" in head.title:
                    return self.rate_limited()
                match = re.search(
                    r'([\d.,kKmMbB]+)\s+Followers,\s*([\d.,kKmMbB]+)\s+Following,\s*([\d.,kKmMbB]+)\s+Posts',
                    head.meta.get("og:description", ""), re.IGNORECASE)
                if match:
                    self.rate_limit_streak = 0
                    return {
                        "status": "active", "username": username,
                        "followers": match[1], "following": match[2], "posts": match[3],
                        "pic_url": head.meta.get("og:image"),
                    }
                # Generic pages, partial reads and challenges are NOT proof of a ban.
                return {"status": "unknown"}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, AttributeError):
            # Exception messages can contain proxy credentials or session cookies.
            logging.debug("Instagram request failed for @%s", username)
            return {"status": "error"}


def schedule_next(account, status, interval, max_backoff, now=None):
    now = time.monotonic() if now is None else now
    if status in ("active", "banned"):
        account["failures"] = 0
        delay = interval
    else:
        failures = min(account.get("failures", 0) + 1, 10)
        account["failures"] = failures
        delay = min(max_backoff, interval * 2 ** failures)
    account["next_check"] = now + delay + random.uniform(0, delay * 0.1)


async def check_due_accounts(accounts, session, checker, on_result, interval, max_backoff,
                             concurrency, clock=time.monotonic):
    """One scheduler owns checks; new accounts are due immediately, without extra tasks."""
    if clock() < checker.cooldown_until:
        return
    due = [(key, record) for key, record in list(accounts.items())
           if record.get("next_check", 0) <= clock()][:concurrency]

    async def check_one(key, record):
        result = await checker.check(session, key)
        if accounts.get(key) is not record:
            return  # Stopped/re-added while the network request was in flight.
        schedule_next(record, result["status"], interval, max_backoff, clock())
        await on_result(key, record, result)

    results = await asyncio.gather(*(check_one(key, record) for key, record in due),
                                   return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logging.error("Monitor check/alert failed (%s)", type(result).__name__)

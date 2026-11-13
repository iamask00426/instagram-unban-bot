"""Single-request profile checks and a bounded, identity-safe monitor scheduler.

Body counters measure bytes consumed by this application, NOT socket or billed
bytes. aiohttp/TLS/the proxy may read ahead before an early response.close().
"""

from __future__ import annotations

import asyncio
import codecs
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import itertools
import json
import re
import time
from urllib.parse import urlsplit
import zlib
import logging

import aiohttp

from config import Settings, normalize_username


@dataclass
class CheckResult:
    status: str
    username: str = ""
    followers: str = "?"
    following: str = "?"
    posts: str = "?"
    pic_url: str | None = None
    retry_after: float = 0
    full_name: str = ""
    is_verified: bool = False
    user_id: str | None = None


@dataclass
class Metrics:
    request_attempts: int = 0
    outcomes: Counter = field(default_factory=Counter)
    consumed_body_bytes: int = 0  # decompressed, passed to the parser
    encoded_body_bytes: int = 0  # read from aiohttp's raw content stream
    webshare_requests: int = 0
    dataimpulse_requests: int = 0
    direct_requests: int = 0


def format_num(count):
    num = float(count)
    for scale, suffix in ((1_000_000, "m"), (1_000, "k")):
        if num >= scale:
            value = num / scale
            return f"{value:.1f}{suffix}" if value % 1 else f"{int(value)}{suffix}"
    return str(int(num))


COUNTS = re.compile(
    r"^\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?[kmb]?)\s+followers,\s*"
    r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?[kmb]?)\s+following,\s*"
    r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?[kmb]?)\s+posts\b", re.I
)


class ProfileHTMLParser(HTMLParser):
    def __init__(self, username):
        super().__init__(convert_charrefs=True)
        self.username = username
        self.metadata = {}
        self.boundary = False
        self.invalid = False
        self.in_title = False
        self.title = ""
        self.is_verified = False
        self.user_id = None

    def handle_starttag(self, tag, attrs):
        if self.boundary:
            return
        if tag == "body":
            self.boundary = True
            return
        if tag == "title":
            self.in_title = True
        if tag != "meta":
            return
        keys = [key for key, _ in attrs]
        if len(set(keys)) != len(keys):
            self.invalid = True
            return
        attrs = dict(attrs)
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        value = attrs.get("content")
        if key.startswith("og:") and value is not None:
            if key in self.metadata and self.metadata[key] != value:
                self.invalid = True
            self.metadata[key] = value

    def handle_endtag(self, tag):
        if tag in {"head", "html"}:
            self.boundary = True
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title and not self.boundary:
            self.title += data

    def result(self):
        if self.invalid or re.search(r"\blog\s*in\b|\blogin\b|challenge|page not found", self.title, re.I):
            return None
        identities = []
        if "og:url" in self.metadata:
            try:
                url = urlsplit(self.metadata["og:url"])
                if url.scheme not in {"http", "https"} or url.hostname not in {"instagram.com", "www.instagram.com"}:
                    return None
                identities.append(normalize_username(self.metadata["og:url"]))
            except ValueError:
                return None
        title = self.metadata.get("og:title", "")
        # The profile handle is terminal, before an optional Instagram suffix.
        # A handle embedded in the display name cannot establish identity.
        title_user = re.search(
            r"\(@([A-Za-z0-9_.]+)\)(?:\s+(?:•|on)\s+Instagram(?:\s+photos and videos)?)?\s*\Z",
            title, re.I,
        )
        handles = re.findall(r"@([A-Za-z0-9_.]+)", title)
        if handles:
            if not title_user or handles != [title_user.group(1)]:
                return None
            try:
                identities.append(normalize_username(title_user.group(1)))
            except ValueError:
                return None
        if not identities or any(user != self.username for user in identities):
            return None
        counts = COUNTS.match(self.metadata.get("og:description", ""))
        if not counts:
            return None
        full_name = ""
        if title_user:
            full_name = title[:title_user.start()].strip()
        if not full_name:
            full_name = self.username
        return CheckResult("active", self.username, counts[1], counts[2], counts[3],
                           self.metadata.get("og:image") or None, 0, full_name, self.is_verified, self.user_id)


class BoundedBody:
    """Bound both encoded consumption and decompressor output without body buffering.

    Sessions MUST use auto_decompress=False. gzip/deflate are explicitly requested;
    unsupported encodings are inconclusive rather than unbounded decoding.
    """
    CHUNK = 1024

    def __init__(self, response, limit, metrics):
        self.response, self.limit, self.metrics = response, limit, metrics
        self.decoded = self.encoded = 0
        self.complete = False  # Verified transport EOF, not merely a byte cap.
        encoding = response.headers.get("Content-Encoding", "identity").lower().strip()
        if encoding not in {"identity", "", "gzip", "deflate"}:
            raise ValueError("Unsupported content encoding")
        self.decompressor = (zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
                             if encoding in {"gzip", "deflate"} else None)

    async def __aiter__(self):
        pending = b""
        while self.decoded < self.limit:
            if not pending:
                if self.encoded >= self.limit:
                    break
                pending = await self.response.content.read(min(self.CHUNK, self.limit - self.encoded,
                                                               self.limit - self.decoded))
                self.encoded += len(pending)
                self.metrics.encoded_body_bytes += len(pending)
                if not pending:
                    break
            if self.decompressor:
                chunk = self.decompressor.decompress(pending, min(self.CHUNK, self.limit - self.decoded))
                pending = self.decompressor.unconsumed_tail
                if self.decompressor.unused_data:
                    return  # Trailing bytes/another compressed member are unverified.
            else:
                chunk, pending = pending, b""
            self.decoded += len(chunk)
            self.metrics.consumed_body_bytes += len(chunk)
            if chunk:
                yield chunk
        self.complete = (not pending and self.response.content.at_eof()
                         and (self.decompressor is None or self.decompressor.eof))


def json_profile(data, username):
    if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
        return None
    user = data["data"].get("user")
    if not isinstance(user, dict) or not isinstance(user.get("username"), str) or user["username"].lower() != username:
        return None
    counts = []
    for key in ("edge_followed_by", "edge_follow", "edge_owner_to_timeline_media"):
        value = user.get(key)
        count = value.get("count") if isinstance(value, dict) else None
        if type(count) is not int or count < 0 or count > 10**15:
            return None
        counts.append(format_num(count))
    pic = user.get("profile_pic_url_hd") or user.get("profile_pic_url")
    full_name = user.get("full_name") or username
    is_verified = bool(user.get("is_verified", False))
    uid = str(user.get("id") or user.get("pk") or "") or None
    return CheckResult("active", username, *counts, pic if isinstance(pic, str) else None, 0, full_name, is_verified, uid)


async def read_profile(response, username, settings, metrics):
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    parser = ProfileHTMLParser(username)
    text = ""
    try:
        body = BoundedBody(response, settings.body_limit, metrics)
        async for chunk in body:
            decoded = decoder.decode(chunk)
            if not parser.user_id:
                m_uid = re.search(r'[\"\'\\]*profile_id[\"\'\\]*[:=]\s*[\"\']?(\d{5,})[\"\']?', decoded)
                if not m_uid:
                    m_uid = re.search(r'\"id\":\"(\d{5,})\"', decoded)
                if m_uid:
                    parser.user_id = m_uid.group(1)
            if '"is_verified":true' in decoded or '"is_verified": true' in decoded:
                parser.is_verified = True
            if settings.mode == "html":
                parser.feed(decoded)
                result = parser.result()
                if result:
                    if not result.user_id and parser.user_id:
                        result.user_id = parser.user_id
                    return result
                if parser.boundary or parser.invalid:
                    break
            else:
                text += decoded
        if settings.mode == "json" and body.complete:
            text += decoder.decode(b"", final=True)
            res = json_profile(json.loads(text), username) or CheckResult("unknown")
            if parser.user_id and not res.user_id:
                res.user_id = parser.user_id
            return res
    except (ValueError, zlib.error, RecursionError):
        pass
    if parser.title and re.search(r"\blog\s*in\b|\blogin\b|challenge", parser.title, re.I):
        return CheckResult("challenge")
    if parser.boundary and (not parser.metadata.get("og:title") and not parser.metadata.get("og:description")):
        return CheckResult("unavailable")
    return CheckResult("unknown")


def retry_after_seconds(value, maximum, now=None):
    if not value:
        return 0
    try:
        if re.fullmatch(r"\d+", value.strip()):
            delay = int(value.strip())
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = date.timestamp() - (time.time() if now is None else now)
        return max(0, min(delay, maximum))
    except (ValueError, TypeError, OverflowError):
        return 0


class ProfileChecker:
    def __init__(self, session, settings: Settings, metrics=None):
        self.session, self.settings = session, settings
        self.metrics = metrics if metrics is not None else Metrics()
        self.proxies = itertools.cycle(settings.proxies) if settings.proxies else None
        # Always keep high-reputation static residential & rotating residential proxies in dedicated cycles
        from config import RESIDENTIAL_PROXIES, ROTATING_RESIDENTIAL_PROXIES
        self.residential_cycle = itertools.cycle(RESIDENTIAL_PROXIES) if RESIDENTIAL_PROXIES else None
        self.rotating_cycle = itertools.cycle(ROTATING_RESIDENTIAL_PROXIES) if ROTATING_RESIDENTIAL_PROXIES else None
        self._proxy_cooldowns = {}

    def _mark_cooldown(self, proxy: str | None, seconds: float = 60.0):
        if not proxy:
            return
        # Never cooldown rotating residential proxies — every connection is already a brand new residential IP!
        from config import ROTATING_RESIDENTIAL_PROXIES
        if proxy in ROTATING_RESIDENTIAL_PROXIES:
            return
        self._proxy_cooldowns[proxy] = time.monotonic() + seconds

    def _cleanup_cooldowns(self):
        now = time.monotonic()
        self._proxy_cooldowns = {p: t for p, t in self._proxy_cooldowns.items() if t > now}

    def _get_proxy(self, cycle_iter):
        if not cycle_iter:
            return None
        now = time.monotonic()
        # Find a proxy that is not temporarily cooled down
        for _ in range(25):
            p = next(cycle_iter)
            if self._proxy_cooldowns.get(p, 0) <= now:
                return p
        return next(cycle_iter)

    async def _execute_single_check(self, username, proxy, timeout_sec: float = 6.0):
        headers = {
            "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        }
        url = f"https://www.instagram.com/{username}/"
        if self.settings.mode == "json":
            url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
            headers.update({"X-IG-App-ID": "936619743392459", "Cookie": f"sessionid={self.settings.session_id};",
                            "Accept": "application/json"})
        else:
            headers["Accept"] = "text/html"
        self.metrics.request_attempts += 1
        if proxy is None:
            self.metrics.direct_requests += 1
        elif "dataimpulse" in proxy.lower():
            self.metrics.dataimpulse_requests += 1
        else:
            self.metrics.webshare_requests += 1
        try:
            async with self.session.get(
                url, headers=headers, proxy=proxy, allow_redirects=False,
                auto_decompress=False, timeout=aiohttp.ClientTimeout(total=timeout_sec),
            ) as response:
                try:
                    retry = retry_after_seconds(response.headers.get("Retry-After"), self.settings.backoff_max)
                    if response.status == 404:
                        result = CheckResult("unavailable")
                    elif response.status in {401, 403, 429}:
                        self._mark_cooldown(proxy, 60.0)
                        result = CheckResult("challenge")
                    elif 300 <= response.status < 400:
                        loc = response.headers.get("Location", "")
                        if "is_from_rle" in loc or "login" in loc.lower():
                            self._mark_cooldown(proxy, 120.0)
                            result = CheckResult("rate_limited")
                        else:
                            result = CheckResult("unknown")
                    elif response.status != 200:
                        logging.warning(f"Profile check HTTP status {response.status} for @{username} via {proxy}")
                        result = CheckResult(f"HTTP_{response.status}")
                    else:
                        result = await read_profile(response, username, self.settings, self.metrics)
                        if result.status == "challenge":
                            self._mark_cooldown(proxy, 90.0)
                    result.retry_after = retry
                finally:
                    response.close()
        except asyncio.CancelledError:
            self.metrics.outcomes["cancelled"] += 1
            raise
        except Exception as e:
            err_str = str(e)
            if "402" in err_str or "Payment Required" in err_str:
                self._mark_cooldown(proxy, 86400.0)
            elif isinstance(e, (asyncio.TimeoutError, aiohttp.ServerTimeoutError, aiohttp.ClientConnectorError)):
                self._mark_cooldown(proxy, 30.0)
            logging.debug(f"Profile check error for @{username} via {proxy}: {type(e).__name__}: {e}")
            result = CheckResult(f"error_{type(e).__name__}")
        return result

    async def check(self, username, prefer_rotating: bool = False):
        username = normalize_username(username)
        self._cleanup_cooldowns()

        result = None
        trail = []  # (tier, status) of every attempt, logged if the final result is inconclusive

        # Tier 0: Instant Check Priority with DataImpulse Rotating Residential (Fastest & 100% Reliable, 2 quick attempts)
        if prefer_rotating and self.rotating_cycle:
            for _ in range(2):
                p_rot = self._get_proxy(self.rotating_cycle)
                if p_rot:
                    rot_res = await self._execute_single_check(username, p_rot, timeout_sec=5.5)
                    trail.append(("T0-DI", rot_res.status))
                    if rot_res.status in {"active", "unavailable"}:
                        self.metrics.outcomes[rot_res.status] += 1
                        return rot_res

        # Tier 1: Routine checks on UNLIMITED Shared & Static Residential Proxies (with auto-failover)
        proxy_attempts = 1 if (self.proxies or self.residential_cycle) else 0
        for _ in range(proxy_attempts):
            p_primary = None
            if self.proxies:
                p_primary = self._get_proxy(self.proxies)
            elif self.residential_cycle:
                p_primary = self._get_proxy(self.residential_cycle)

            if not p_primary:
                break

            result = await self._execute_single_check(username, p_primary)
            trail.append(("T1-static", result.status))
            if result.status in {"active", "unavailable"}:
                self.metrics.outcomes[result.status] += 1
                return result

        # Tier 2: Rotating Residential Fallback (DataImpulse 90M+ Pool)
        # Trigger ONLY on rate_limited or challenge — not on unknown/generic errors (those drain bandwidth)
        from config import ROTATING_RESIDENTIAL_PROXIES
        if ROTATING_RESIDENTIAL_PROXIES and self.rotating_cycle and result is not None and result.status in {"rate_limited", "challenge"}:
            p_rot = self._get_proxy(self.rotating_cycle)
            if p_rot:
                rot_res = await self._execute_single_check(username, p_rot)
                trail.append(("T2-DI", rot_res.status))
                if rot_res.status in {"active", "unavailable"}:
                    self.metrics.outcomes[rot_res.status] += 1
                    return rot_res
                elif result is None:
                    result = rot_res

        # Tier 3: Direct Connection Fallback
        # If proxies are rate-limited or errored out, clean direct check so alerts are NEVER dropped.
        if result is None or result.status in {"rate_limited", "challenge", "unknown"} or result.status.startswith("error_") or result.status.startswith("HTTP_"):
            try:
                direct_res = await self._execute_single_check(username, None)
                trail.append(("T3-direct", direct_res.status))
                if direct_res.status in {"active", "unavailable"}:
                    self.metrics.outcomes[direct_res.status] += 1
                    return direct_res
                if result is None:
                    result = direct_res
            except Exception as e:
                logging.debug(f"Direct fallback check error for @{username}: {e}")

        if result is None:
            result = CheckResult("unknown")

        self.metrics.outcomes[result.status] += 1
        logging.info("CHECK inconclusive @%s: %s", username, ", ".join(f"{t}={st}" for t, st in trail) or result.status)
        return result

    def get_proxy_stats(self) -> dict:
        total = self.metrics.webshare_requests + self.metrics.dataimpulse_requests + self.metrics.direct_requests
        ws_pct = (self.metrics.webshare_requests / total * 100) if total > 0 else 0.0
        di_pct = (self.metrics.dataimpulse_requests / total * 100) if total > 0 else 0.0
        dir_pct = (self.metrics.direct_requests / total * 100) if total > 0 else 0.0
        return {
            "total": total,
            "webshare_requests": self.metrics.webshare_requests,
            "webshare_pct": ws_pct,
            "dataimpulse_requests": self.metrics.dataimpulse_requests,
            "dataimpulse_pct": di_pct,
            "direct_requests": self.metrics.direct_requests,
            "direct_pct": dir_pct,
        }



def profile_session(settings):
    import ssl
    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = True
    ssl_ctx.verify_mode = ssl.CERT_REQUIRED

    conn_limit = max(100, settings.concurrency * 4)
    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=conn_limit, limit_per_host=conn_limit, ssl=ssl_ctx),
        cookie_jar=aiohttp.DummyCookieJar(), trust_env=False, auto_decompress=False,
        timeout=aiohttp.ClientTimeout(total=settings.request_timeout), read_bufsize=4096,
    )
    # aiohttp >=3.12 otherwise silently retries disconnected GETs. There is no
    # public switch; older supported versions do not implement this retry.
    if hasattr(session, "_retry_connection"):
        session._retry_connection = False
    return session


@dataclass(eq=False)
class MonitorRecord:
    username: str
    chat_id: int
    owner_id: int
    mode: str
    start_time: datetime
    next_due: float
    failures: int = 0
    unavailable_count: int = 0
    pending: CheckResult | None = None
    delivery_failures: int = 0
    photo: bytes | None = None
    photo_prepared: bool = False
    detected_at: datetime | None = None


def backoff(base, maximum, failures):
    return min(maximum, base * 2 ** min(max(failures - 1, 0), 20))


class MonitorScheduler:
    """One owner of first checks, subsequent checks and cached notification retries.

    Work (including delivery) shares the concurrency bound. Deadlines are ordered
    oldest-first; each record can have at most one in-flight task. Monotonic time
    controls scheduling; wall time is used only for display and HTTP dates.
    """
    def __init__(self, settings, check, deliver, clock=time.monotonic):
        self.settings, self.check, self.deliver, self.clock = settings, check, deliver, clock
        self.records = {}
        self.tasks = {}  # task -> record; stopped tasks still occupy a slot until done
        self.wakeup = asyncio.Event()
        self.closing = False

    def current(self, record):
        return not self.closing and self.records.get(record.username) is record

    def add(self, username, chat_id, owner_id, mode):
        username = normalize_username(username)
        if username in self.records:
            return None
        if mode not in {"unban", "ban"}:
            raise ValueError("Invalid monitor mode")
        record = MonitorRecord(username, chat_id, owner_id, mode, datetime.now(), self.clock())
        self.records[username] = record
        self.wakeup.set()
        return record

    def stop(self, username, chat_id, owner_id):
        username = normalize_username(username)
        record = self.records.get(username)
        if record is None or (record.chat_id, record.owner_id) != (chat_id, owner_id):
            return False
        del self.records[username]
        for task, active in self.tasks.items():
            if active is record:
                task.cancel()
        self.wakeup.set()
        return True

    async def _work(self, record):
        try:
            if not self.current(record):
                return
            if record.pending is not None:
                try:
                    delivered = await self.deliver(record, lambda: self.current(record))
                except Exception:
                    delivered = False
                if not self.current(record):
                    return
                if delivered:
                    del self.records[record.username]
                else:
                    record.delivery_failures += 1
                    record.next_due = self.clock() + backoff(
                        self.settings.alert_backoff_base, self.settings.alert_backoff_max, record.delivery_failures)
                return
            try:
                result = await self.check(record.username)
                if not isinstance(result, CheckResult):
                    raise ValueError("Invalid check result")
            except Exception:
                result = CheckResult("error")
            if not self.current(record):
                return
            delay = self.settings.check_interval
            if result.status == "unavailable":
                record.failures = 0
                record.unavailable_count += 1
                if record.mode == "ban":
                    delay = max(delay, self.settings.confirmation_interval)
            else:
                record.unavailable_count = 0
                if result.status == "active":
                    record.failures = 0
                elif result.status != "unknown":
                    record.failures += 1
                    delay = max(delay, backoff(self.settings.backoff_base, self.settings.backoff_max, record.failures))
            target = (record.mode == "unban" and result.status == "active") or (
                record.mode == "ban" and record.unavailable_count >= self.settings.unavailable_confirmations)
            if target:
                record.pending = result
                record.detected_at = datetime.now()
                record.next_due = self.clock()
            else:
                record.next_due = self.clock() + max(delay, result.retry_after)
        except Exception:
            # Even unexpected processing/delivery bugs must not cause a hot loop.
            if self.current(record):
                record.failures += 1
                record.next_due = self.clock() + backoff(
                    self.settings.backoff_base, self.settings.backoff_max, record.failures)
        finally:
            self.wakeup.set()

    def dispatch_due(self):
        """A nonblocking scheduling turn, also usable with a fake clock in tests."""
        for task in list(self.tasks):
            if task.done():
                self.tasks.pop(task)
                if not task.cancelled():
                    task.result()
        if self.closing:
            return
        busy = {record.username for record in self.tasks.values()}
        due = sorted((r for r in self.records.values() if r.username not in busy and r.next_due <= self.clock()),
                     key=lambda r: r.next_due)
        for record in due[:max(0, self.settings.concurrency - len(self.tasks))]:
            self.tasks[asyncio.create_task(self._work(record))] = record

    async def run(self):
        try:
            while not self.closing:
                self.wakeup.clear()
                self.dispatch_due()
                busy = {r.username for r in self.tasks.values()}
                deadlines = [r.next_due for r in self.records.values() if r.username not in busy]
                delay = (max(0, min(deadlines) - self.clock())
                         if deadlines and len(self.tasks) < self.settings.concurrency else None)
                try:
                    if delay is None:
                        await self.wakeup.wait()
                    else:
                        await asyncio.wait_for(self.wakeup.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.close()

    async def close(self):
        self.closing = True
        self.wakeup.set()
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

async def fetch_user_id_by_username(session: aiohttp.ClientSession, proxy: str | None, username: str) -> str | None:
    """Fetch Instagram UID for a username using lightweight HTML metadata check."""
    username = normalize_username(username)
    url = f"https://www.instagram.com/{username}/"
    headers = {
        "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
    }
    try:
        async with session.get(url, headers=headers, proxy=proxy,
                               timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                raw = await resp.content.read(64 * 1024)
                text = raw.decode("utf-8", errors="ignore")
                m = re.search(r'[\"\'\\]*profile_id[\"\'\\]*[:=]\s*[\"\']?(\d{5,})[\"\']?', text)
                if not m:
                    m = re.search(r'\"id\":\"(\d{5,})\"', text)
                if m:
                    return m.group(1)
    except Exception as e:
        logging.warning(f"Error fetching UID for @{username}: {e}")
    return None

async def resolve_username_by_uid(session: aiohttp.ClientSession, proxy: str | None, user_id: str) -> str | None:
    """Resolve current Instagram username for a given user_id."""
    if not user_id:
        return None
    url = f"https://i.instagram.com/api/v1/users/{user_id}/info/"
    headers = {
        "User-Agent": "Instagram 275.0.0.27.98 Android (30/11; 480dpi; 1080x2400; OnePlus; GM1910; OnePlus7Pro; qcom; en_US; 314591244)",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
    }
    try:
        async with session.get(url, headers=headers, proxy=proxy, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                return data.get("user", {}).get("username")
    except Exception as e:
        logging.warning(f"Error resolving username for UID {user_id}: {e}")
    return None

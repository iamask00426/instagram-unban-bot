# Instagram Availability Monitor

Telegram profile monitoring with bounded, status-first streaming checks and the existing black profile cards. **Availability is not proof of recovery; unavailability is not proof of a ban.**

## Commands

- `/monitor <username>` — wait until the requested profile is verifiably active/available; no opposite baseline is required.
- `/banmonitor <username>` — compatibility command name: wait for consecutive HTTP 404 responses (two by default, at least five seconds apart). Alerts say **unavailable**, not banned/disabled.
- `/bulk user1, user2 ...` — add wait-until-active monitors.
- `/stop <username>` — stop a monitor created by the same Telegram user in the same chat.
- `/active` — list this chat's monitors, including pending alerts.
- `/start`, `/help` — command help.

Use an ASCII username, `@username`, or a plain Instagram profile URL (no query/fragment). There is still one global monitor per normalized username; another user cannot replace or stop it.

## Setup

Use Python 3.10+ (offline suite validated with Python 3.11.11):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
# Edit .env locally with your own secrets, then:
.venv/bin/python bot.py
```

**Do not overwrite an existing `.env`.** Startup loads `.env`, validates configuration, and requires an explicit `BOT_TOKEN` and at least one proxy in `PROXIES`. No source-embedded token/proxy defaults, implicit environment proxy, or direct Instagram fallback exists. HTTP(S) proxies are supported; SOCKS is rejected. Proxy entries may be HTTP(S) URLs, `host:port`, or `host:port:user:password`, separated by commas, semicolons or newlines. Percent-encode special characters in URL credentials.

If credentials were previously committed, treat them as exposed: the owner must rotate/revoke them before operational use. Removing defaults does not erase Git history or rotate credentials. Existing placeholder cookies and unauthorised/expired proxies must be fixed by the owner; this implementation was **not** validated against live Telegram, Instagram or paid proxies.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `BOT_TOKEN` | required | Explicit Telegram bot token |
| `PROXIES` | required | Explicit HTTP(S) proxy list; no direct fallback |
| `IG_CHECK_MODE` | `html` | `html` or explicitly selected `json` |
| `IG_SESSIONID` | empty | Real session cookie required only for JSON mode |
| `CHECK_INTERVAL_SECONDS` | `2` | Normal delay **after each account's check completes**, not a 60-second polling interval |
| `CHECK_CONCURRENCY` | `10` | Maximum total concurrent check/delivery tasks and profile connector connections |
| `PROFILE_BODY_LIMIT_BYTES` | `131072` | Per-check application-consumed decompressed **and** encoded body caps (128 KiB each) |
| `REQUEST_TIMEOUT_SECONDS` | `10` | Total timeout per profile request |
| `BACKOFF_BASE_SECONDS` | `10` | First unknown/challenge/error delay; doubles on consecutive failures |
| `BACKOFF_MAX_SECONDS` | `300` | Maximum exponential delay and accepted `Retry-After` delay |
| `UNAVAILABLE_CONFIRMATIONS` | `2` | Consecutive 404 observations needed for `/banmonitor` (minimum two) |
| `CONFIRMATION_INTERVAL_SECONDS` | `5` | Minimum interval between `/banmonitor` 404 confirmations |

Examples/placeholders in `IG_SESSIONID`, including the legacy `optional_instagram_session_cookie_here`, are ignored with a safe warning in HTML mode and rejected in JSON mode. Merely setting a cookie **does not enable JSON**. JSON mode uses one `web_profile_info` GET, verifies the returned username and count schema (including zero followers), and never falls back to HTML. Unlike HTML prefix checks, JSON requires the complete bounded document and verified end-of-stream (including UTF-8/compression completion); missing user/data, trailing garbage/documents, invalid JSON or an unverified end at the cap is unknown. A cookie's validity cannot be established offline.

## Check and alert behaviour

- One scheduler owns immediate first checks and subsequent checks, oldest due first. There are no independent instant tasks, HEAD probes, redirects, API-to-HTML fallbacks or automatic disconnected-GET retries.
- HTTP errors/redirects are classified without reading their bodies. Only HTTP 404 yields `unavailable`; 400/402/5xx are errors, 401/403/429 are challenges, and redirects are unknown.
- HTML is incrementally decoded as UTF-8 and parsed through metadata, end-head/body-start, or the cap. Active requires a matching profile identity (`og:url` and/or a terminal `(@username)` in `og:title`, optionally followed by an Instagram suffix) **and** usable followers/following/posts metadata. Titles containing multiple or ambiguous handles are inconclusive. Zero counts are valid. Generic HTTP 200, login pages, mismatches, malformed/truncated prefixes and missing evidence never prove a ban; they are inconclusive.
- Counts and any avatar URL already found in that same bounded prefix are reused in the alert. **A second “phase 2” details fetch is generally unnecessary and is not implemented.** Missing avatar metadata is simply omitted; the local placeholder remains available.
- Unknown/challenge/error results back off exponentially. Valid delta/date `Retry-After` is respected up to the configured maximum; invalid/past values do not disable normal backoff. Conclusive normal checks retain the two-second default. Confirmation spacing, request time and queue capacity also affect latency.
- Once a target is detected, the result/card is cached for notification delivery. Failed photo delivery falls back to text; if both fail, only cached delivery retries (5, 10, 20… seconds, capped at 300), **not another Instagram check**. Link previews are disabled. Stop/re-add identity guards cover asynchronous lifecycle steps, and shutdown cancels/awaits tasks.
- Optional avatar download is separate **direct traffic**, never an Instagram details fetch: HTTPS Instagram/Facebook CDN hosts only, public resolved addresses, no redirects or proxy environment, five-second timeout, 2 MiB body cap and 16-million-pixel image limit. Cards retain their 1000×550 layout. Rendered cards are cached across delivery retries.

## Bandwidth evidence and limits

Previously supplied **direct HTTP/1.1** measurements (not rerun here) found:

- Active-profile headers about 7.4 KB; matching OG identity, counts and avatar around decoded byte 9,222, using roughly 2.8–3.1 KB compressed prefix.
- Early-stop fresh connections used approximately 17–25 KB socket bytes versus a full response around 252,652 bytes (235,934 compressed body / 1.24 MB decoded).
- HEAD returned 200 for both an active and a synthetic handle. The synthetic likely-missing page returned generic 200 with no usable evidence, consuming about 33–39 KB through end-head: correctly **unknown**, not banned.
- Warm HEAD saved roughly 6.3 KB connection setup, but draining large HTML solely to reuse HTTP/1.1 is wasteful. Paid proxy CONNECT returned 402; provider billing was not measured.

These are observations, **not guaranteed billed-byte savings**. This implementation requests gzip/deflate and bounds decompression itself. It closes early instead of draining the remainder; an unread HTTP/1.1 response normally forfeits that connection's reuse. The shared session bounds resources and rejects cookie accumulation; it is not a promise of connection reuse. OS/TLS/aiohttp/proxy read-ahead can receive additional bytes before closure. Headers, CONNECT, handshakes, retries outside this process and provider accounting are outside the body cap. The cap is not a wire/billing quota.

Counters track request attempts, outcomes, application-consumed decoded body bytes, and encoded body bytes read from aiohttp; aggregate totals are logged at shutdown. They are **not billable bytes**. The synthetic loopback tests prove streaming and closure only, not Instagram behaviour or proxy billing.

## Offline tests

```sh
python3 -m venv /tmp/ig-monitor-tests
/tmp/ig-monitor-tests/bin/python -m pip install -r requirements.txt
/tmp/ig-monitor-tests/bin/python -m unittest discover -s tests -v
/tmp/ig-monitor-tests/bin/python -m py_compile bot.py config.py monitoring.py tests/test_*.py
git diff --check
```

Tests use fake configuration, mocked streams/clocks/Telegram, local image assets and loopback HTTP only. Imports do not read `.env`, construct a bot, or start network activity. Coverage includes chunk splits/UTF-8/entities, metadata beyond 8 KiB, caps/compression, JSON identity, status-first/no-fallback checks, cooldowns, fairness/concurrency, stale stop/re-add results, cached alerts, ownership and safe avatars. Recent aiohttp's private `_retry_connection` switch must be disabled to prevent hidden GET retries; the loopback disconnected-server regression should be rerun on dependency upgrades.

## Residual operational limits

This remains an in-memory single-process bot: monitor/pending-alert state and metrics are lost on restart. There are no spend budgets, account quotas, durable storage, admin allowlist or full multitenant access-control system. Ownership protects stop/replacement, not who may add monitors. Fast polling can still be expensive or challenged; many accounts or slow Telegram delivery may delay checks within the shared bound. English metadata/CDN formats can change and lead to unknown/placeholder results. A Telegram request already in flight cannot be recalled by `/stop`; an accepted-but-timed-out Telegram request can produce duplicate alerts on retry. No live availability, session, proxy billing or credential-rotation validation is claimed.

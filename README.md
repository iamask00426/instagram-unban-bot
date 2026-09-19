# Instagram Status & Ban Monitor Bot 🚀

Telegram Bot for periodic monitoring of Instagram account availability (Unban detection & Ban alerts). Defaults prioritize limited proxy bandwidth over second-by-second detection.

## Features ✨
- 🟢 `/monitor <username>` — Monitor disabled account until it gets **UNBANNED** (`Banned ➔ Active`).
- 🔴 `/banmonitor <username>` — Monitor active account until it gets **BANNED** (`Active ➔ Banned`).
- 🛑 `/stop <username>` — Stop active monitoring for a specific handle.
- 📊 `/active` — View all currently active monitoring tasks.

## Setup & Installation 🛠️

1. Clone the repository:
   ```bash
   git clone https://github.com/iamask00426/instagram-unban-bot.git
   cd instagram-unban-bot
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Configure environment variables:
   Copy `.env.example` to `.env`, fill in your Bot Token from @BotFather and your `PROXIES`. Leave `IG_SESSIONID` empty unless you have a valid Instagram session:
   ```bash
   cp .env.example .env
   ```

4. Run the Bot:
   ```bash
   python bot.py
   ```

## Proxy bandwidth audit and changes

The previous loop checked each account about every 1–2 seconds (plus request time, with longer cycles for large batches), downloaded complete HTML pages, and sometimes made **two requests per check**: API followed by HTML. Separate instant-check tasks could duplicate background checks. Failed/challenged requests retried at the same aggressive pace, and each batch created a new HTTP session.

The new defaults in `.env.example`:

| Setting | Default | Effect |
| --- | --- | --- |
| `CHECK_INTERVAL_SECONDS` | `60` | Minimum normal interval per account, plus up to 10% jitter; minimum configurable value is 10s |
| `MAX_BACKOFF_SECONDS` | `3600` | Exponential error/unknown backoff ceiling before jitter |
| `MAX_CONCURRENT_CHECKS` | `3` | Bounds all checks, including newly added/bulk accounts |
| `MAX_RESPONSE_BYTES` | `131072` | Caps application-consumed, decompressed response bodies at 128 KiB |

- New accounts are due immediately through one scheduler—no duplicate instant checks.
- One request per check. With `IG_SESSIONID`, use JSON only; without it, stream HTML only until OpenGraph metadata or the end of the head. Large bodies are not deliberately consumed.
- No redirect following, error-page reads, or API-to-HTML fallback. An expired cookie needs replacing (or clearing to select HTML mode); it no longer causes an expensive second request.
- 401/403/429, redirects and recognized login challenges pause **all** checks for at least five minutes, increasing on repeated limits. Server `Retry-After` takes precedence when longer. Normal errors/unknowns back off per account.
- Explicit gzip/deflate support and a shared HTTP session. Compression was already supported by aiohttp; the primary savings are fewer requests and early stream termination.
- Zero-follower accounts are active; ambiguous/truncated responses and HTTP 400 no longer produce false ban alerts. HTTP 404 means unavailable, not proof that Instagram banned an account (renames/deletions can look the same).
- Avatar downloads and Telegram uploads are direct, not through the configured Instagram proxies. An avatar may be omitted if its metadata appears after the early stopping point.

For a small batch, moving from a 1.5s loop to 60s reduces scheduled checks by roughly **40×**, ignoring request duration and backoff. This is an estimate, not measured billing savings. Normal detection may now take about a minute; queueing, slow requests and backoff can make it longer. Set `CHECK_INTERVAL_SECONDS=300` for more savings and roughly five-minute detection.

Logs report cumulative request attempts, consumed body bytes, capped responses and cooldown time every minute. Body bytes are **not** billable proxy traffic: compression, headers, TLS and network read-ahead differ. Early close/response caps cannot strictly cap provider-billed bytes. Compare your provider dashboard over equal time windows with the same monitored accounts to measure actual savings. Repeated capped responses may require increasing `MAX_RESPONSE_BYTES`, at a bandwidth cost.

## Credentials and validation

Real tokens/proxy passwords must only be in your ignored `.env` or deployment secrets. Built-in credentials were removed; rotate previously committed credentials because removing them does not erase Git history. Instagram checks will not silently fall back to your host IP when proxies are missing. For existing deployments, configure `PROXIES` before restarting.

Run offline regression tests (no Instagram, Telegram or paid proxy traffic):

```bash
python -m unittest discover -s tests -v
```

Tests cover bounded reads, early HTML stopping, no fallback/redirects, rate-limit cooldowns, scheduling/backoff, bulk concurrency and stopped/re-added accounts.

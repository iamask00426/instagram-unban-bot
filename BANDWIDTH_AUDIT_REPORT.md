# Proxy Bandwidth Audit Report

**Audit date:** 2026-09-19 (UTC)

**Repository:** `instagram-unban-bot`

**Original baseline:** commit `6c90ffa` — `Ultra-fast alerts: instant check on command and 1-2s monitoring loop`

**Current version reviewed:** local, uncommitted changes in `bot.py`, `monitoring.py`, `.env.example`, `README.md`, and `tests/test_monitoring.py`.

## 1. Executive summary

**The main bandwidth problem is the frequency and size of Instagram requests—not Telegram images.** The original implementation repeatedly fetched full profile pages, potentially followed an unsuccessful API request with another HTML request, and did not slow down after failures or rate limits. Separate immediate checks could overlap the monitoring loop.

Changes made before this report address those primary causes: 60-second polling, bounded/early response reading, one application-level request per check, a single scheduler, and backoff. However, **the bot still does not enforce a daily/monthly proxy allowance**. Unrestricted monitor creation, indefinite monitoring, and exposed historical credentials remain important risks.

**Conclusion:** the original design is unsuitable for a small proxy allowance. The modified code is substantially more conservative by design, but actual billed savings have not been measured and remaining budget/access controls should be addressed before treating it as quota-safe.

## 2. Scope and evidence limits

Reviewed the original source and current working files, traced explicit proxy usage, reran the offline regression suite, and ran two additional synthetic edge-case probes.

- No Instagram, Telegram, or paid proxy requests were made during this audit.
- No provider dashboard, invoices, production logs, running-process configuration, or live response sizes were inspected.
- The local changes are not evidence of deployment or restart.
- All savings figures below are illustrative calculations, not measured production results.
- `baseline bot.py:L` means the file at commit `6c90ffa`; other line references refer to the current working files.
- No credentials are reproduced in this report.

## 3. Traffic map

| Operation | Uses configured Instagram proxy? | Frequency / significance |
| --- | --- | --- |
| Instagram JSON profile request | Yes | Repeated while monitoring; primary quota consumer when a session cookie is set |
| Instagram HTML profile request | Yes | Repeated while monitoring; primary quota consumer without a cookie, or as a baseline fallback |
| Profile avatar download | No explicit proxy | Only when building an applicable alert card; `bot.py:130–144` |
| Telegram polling/messages/photo uploads | No explicit proxy | Separate bot traffic; `bot.py:70`, `225–254`, `418` |
| Font loading, card rendering, bundled banned avatar | No network | Local file/CPU work |

This describes application configuration. A system-level tunnel or network gateway could change routing, but none was inspected. This is an HTTP client, not a browser: fetching HTML does **not** automatically fetch its linked scripts, images, or stylesheets.

## 4. Original bandwidth findings

### B1 — High: overly frequent polling

**Evidence:** baseline `bot.py:389–425`: batches of ten accounts, followed by a random 1–2 second sleep. There is no per-account next-check deadline.

For up to ten accounts, every account can be checked once per batch cycle. A cycle includes the slowest request in that batch, alert processing, and the sleep; it is not always exactly 1–2 seconds. Larger account sets require multiple sequential batches.

**Impact:** even modest responses accumulate rapidly when accounts remain monitored for hours or days.

**Current disposition:** mitigated. `bot.py:27` defaults to 60 seconds; `monitoring.py:196–222` schedules each account after its result, adds jitter, and checks only due accounts. This deliberately increases detection latency.

### B2 — High: complete HTML and challenge-page consumption

**Evidence:** baseline `bot.py:284–290` calls `await resp.text()` before handling 403/429. Baseline `bot.py:256` reads JSON without a response-size limit. GET requests do not disable redirects.

**Impact:** the bot consumes complete documents when it only needs a small amount of profile metadata. Login/error documents and redirect destinations can also consume quota without useful status information.

**Current disposition:** mitigated at the application layer. `monitoring.py:86–117` reads in bounded chunks and stops HTML parsing after useful metadata or the head. `monitoring.py:146–159` disables redirects and avoids deliberately reading error/challenge bodies. Default cap: 128 KiB of decompressed, application-consumed data.

**Limit:** network read-ahead may already have transferred additional bytes. This cap is not a hard cap on provider billing or all memory allocated by the HTTP stack.

### B3 — High: failed API checks can generate a second request

**Evidence:** baseline `bot.py:243–284`: an inconclusive API attempt falls through to the crawler request.

**Impact:** an expired session, transport error, unexpected response, or rate limit can cause two explicit requests per logical check. Automatic redirects can add further HTTP exchanges.

**Current disposition:** mitigated. `monitoring.py:128–148` selects JSON or HTML, never both within one check. A bad session must now be replaced or cleared; HTML is no longer an automatic recovery path.

### B4 — High: no error or rate-limit backoff

**Evidence:** baseline `bot.py:288–315` returns `rate_limited`, `unknown`, or `error`; the baseline monitor loop still uses the same short delay.

**Impact:** failures can continue consuming quota at approximately the normal aggressive rate, with little chance of producing useful results.

**Current disposition:** mitigated for handled outcomes. `monitoring.py:68–84` implements a shared cooldown; `196–206` exponentially delays unsuccessful account checks. With defaults, rate-limit cooldown starts at 300 seconds. A longer `Retry-After` is honored. Already in-flight requests are not canceled.

### B5 — Medium: overlapping immediate/background checks

**Evidence:** baseline `bot.py:377–385`, `472`, `497`, `522`: a newly registered account gets an independent immediate-check task while also being visible to the regular loop.

**Impact:** duplicate initial requests are possible; `/bulk` creates an immediate task per added account outside the background batch limit.

**Current disposition:** mitigated. `monitoring.py:208–227` is the single check scheduler. New entries are due immediately and share the configured concurrency limit. They may still wait behind an existing batch.

### B6 — Low: per-batch connection/session churn

**Evidence:** baseline `bot.py:381`, `403` creates new HTTP sessions for immediate checks and batches.

**Impact:** connection setup may add overhead, but it is secondary to polling frequency and body size.

**Current disposition:** partially mitigated by the shared session at `bot.py:263–285`. Savings must not be overstated: early response termination can close connections, proxies rotate, and the installed aiohttp connector's default idle keepalive timeout is 15 seconds—shorter than the new normal interval. Connection reuse is possible, not guaranteed.

## 5. Remaining risks in the current code

These are findings/recommendations only; no further application changes were made while preparing this report.

| ID / priority | Evidence and remaining issue | Recommendation |
| --- | --- | --- |
| R1 — High | `bot.py:302–375`: monitor commands have no user/chat allowlist, per-user rate limit, or maximum monitored-account count. Anyone able to use the bot can add workload. Concurrency limits simultaneous requests, not total usage. | Restrict authorized users/chats; impose global/per-user account caps and command throttling. |
| R2 — High | `bot.py:75`, `318–323`, `364–369`; `monitoring.py:196–222`: there is no byte/request budget or monitor expiration. `start_time` is used for display, not TTL enforcement. Accounts can remain monitored indefinitely. | Add monitor TTLs, a persistent daily request budget, and an operator-visible pause. Prefer provider-side quota enforcement for a true billing ceiling. |
| R3 — High/security | Baseline `bot.py:21`, `25–37` contains embedded credentials; baseline `290`, `315` can log credential-bearing proxy URLs. Source removal does not revoke credentials or erase Git history. | Rotate bot/proxy credentials and review historical log access. Existing proxy values were moved into the ignored local `.env`, but rotation was not performed or verified. Compromised proxy credentials could consume quota independently of this bot; no such misuse was established. |
| R4 — Medium | `monitoring.py:64–65`, `86–89`; `bot.py:280–283`: metrics count application request attempts and consumed, decompressed body bytes—not provider-billed bytes. Headers, TLS, buffered unread data, compression, and transport behavior differ. | Use the provider dashboard/API for billing; add outcome/per-endpoint counters and reconcile over equal observation windows. |
| R5 — Medium | `monitoring.py:218–227`: an unexpected exception from `checker.check()` occurs before the next deadline is recorded. The outer gather logs it but leaves the account due. An offline probe reproduced repeated calls at the same clock value. Normal handled network errors do back off. | Ensure every failed attempt, including unexpected exceptions, schedules a retry deadline; add a regression test. Otherwise repeated unexpected failures could run every scheduler tick (~0.5s). Production occurrence was not observed. |
| R6 — Medium/correctness | `monitoring.py:149–151` treats one HTTP 404 as `banned`; `bot.py:238–244` announces BANNED/DISABLED. Unavailable profiles can also be renamed, deleted, or temporarily inaccessible. | Report “unavailable” unless stronger evidence exists; require spaced confirmation before a final ban alert, balancing extra requests against false positives. |
| R7 — Medium/isolation | `bot.py:75`, `314–324`, `388–390`: state is keyed only by username; another chat cannot independently subscribe to it, and `/stop` does not verify ownership. | Maintain per-chat subscriptions over a shared, deduplicated account check; authorize removals. Preserve one network check per username. |
| R8 — Low/correctness | `monitoring.py:91–99`: a valid JSON body exactly equal to the cap is rejected without attempting to parse it. An offline probe returned `unknown` for a complete, valid JSON body at the boundary. | Parse a complete accumulated body when possible without raising the read limit; test below/at/above-cap payloads. |
| R9 — Medium/reliability | `monitoring.py:112–117`, `175–188`: HTML parsing relies on English follower-count metadata before the cap. All redirects trigger shared cooldown, including potentially benign redirects. JSON-only mode does not recover from invalid cookies through HTML. | Validate sanitized representative responses and legitimate redirects offline; expose persistent authentication/parse failures to the operator. Avoid restoring automatic full-page fallbacks. |

Additional configuration caveat: README wording says rate-limit cooldown is “at least five minutes,” but `monitoring.py:70` uses `min(max_backoff, 300 * ...)`. A custom `MAX_BACKOFF_SECONDS` below 300 shortens that cooldown. The five-minute statement is true for the default configuration, not every supported configuration.

## 6. Illustrative impact and budgeting

A useful planning model is:

```text
Daily proxy bytes ≈ active accounts × (86,400 / effective interval seconds)
                    × mean billed bytes per complete logical check
```

“Effective interval” includes request duration and scheduling delays. “Bytes per logical check” must include any fallback/redirect traffic and whatever overhead the provider bills.

### Polling-frequency comparison only

Assume a small batch, negligible request time, no failures/alerts, no redirects/fallback, no jitter, and **50 KiB billed per check**. That size is hypothetical, not measured.

| Nominal interval | Checks/account/day | Traffic/account/day | Traffic/10 accounts/day |
| --- | ---: | ---: | ---: |
| 1.5 seconds (old mean sleep) | 57,600 | 2.95 GB | 29.49 GB |
| 60 seconds (new default) | 1,440 | 0.074 GB | 0.737 GB |
| 300 seconds (optional) | 288 | 0.015 GB | 0.147 GB |

GB is decimal. These figures deliberately hold response size constant to isolate polling frequency. The current scheduler adds 0–10% jitter, request time, and possibly queue delays; the original loop also includes request/batch time. Do not extrapolate the old small-batch interval unchanged to large account sets.

The simplified change from 1.5 to 60 seconds means **40× fewer scheduled checks, or 97.5% fewer checks**. It does **not** establish a 97.5% reduction in billable bandwidth. Early stopping and removing fallbacks may help further; read-ahead, payload differences, and longer baseline request times change the real result.

To size an interval against an allowance:

```text
Daily budget = remaining allowance / remaining days
Required interval ≈ accounts × 86,400 × measured bytes/check / daily budget
```

Use consistent byte units, include startup bursts/retries, and reserve headroom. A fixed interval alone is not enforcement.

## 7. Validation results

Executed in a temporary virtual environment with Python 3.11.11 and aiohttp 3.14.3:

```bash
python -m unittest discover -s tests -v
```

**Result: 19 tests passed.** Coverage includes early HTML stopping, bounded JSON/HTML reads, split metadata/UTF-8, no explicit fallback, redirect configuration, error-body avoidance, shared cooldown, zero-follower accounts, per-account backoff, bulk concurrency, and stale stopped/re-added records.

Two additional offline probes identified R5 and R8. These are not part of the passing regression suite and remain unresolved. The original modification turn also completed syntax checks and a bot import/configuration smoke test without network access.

**Not validated:** real proxy transport/CONNECT behavior, actual response compression/read-ahead, live Instagram compatibility, live alert delivery, production account counts, provider billing, or deployment state. Mocked tests demonstrate application decisions, not end-to-end billed savings.

## 8. Recommended action plan

1. **Immediate:** rotate exposed credentials; restrict who can create monitors; choose an acceptable detection delay and account cap.
2. **Before claiming quota safety:** add persistent usage limits/monitor TTLs and configure a provider-side quota ceiling where available.
3. **Next code fixes:** cover unexpected-exception backoff (R5), correct ban wording/confirmation (R6), and resolve cross-chat ownership (R7).
4. **Before deployment:** verify `PROXIES` is configured, confirm cookie mode, and review the existing uncommitted changes. Production startup now requires explicit proxy configuration.
5. **Measure with a small canary:** record provider bytes over an approved observation window, monitored account count, request attempts, capped responses, outcomes, and alert latency. Do not run the old aggressive loop merely to manufacture a baseline; use existing provider history if available.
6. **Tune using evidence:** start at 60 seconds if roughly one-minute detection is acceptable, or 300 seconds for tighter budgets. Increase the response cap only when representative samples show useful metadata requires it. Do not increase concurrency to solve a byte-budget problem.

**Audit deliverable:** this report. Existing application edits were reviewed, not expanded during report preparation. No restart, deployment, credential rotation, or live quota measurement was performed.

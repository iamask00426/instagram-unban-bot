# Proxy Bandwidth Audit

**Baseline:** `main` at `6c90ffa`. Source-only audit; no application changes or live requests. Production bandwidth was not measured.

## Main findings

| Issue | Evidence | Bandwidth impact |
| --- | --- | --- |
| Aggressive polling without failure backoff | `bot.py:389–432` | Batches of 10 concurrent checks, followed by 1–2s sleep. Errors, challenges, and unknown results repeat indefinitely. |
| Full HTML reads and conditional API fallback | `bot.py:243–291` | Downloads entire pages, including 403/429 bodies. Some API failures trigger a second HTML request. Redirects can add traffic. |
| Duplicate initial checks | `bot.py:377–405,472,497,509–522` | Instant checks overlap the background loop. Bulk commands launch tasks outside its concurrency bound. |
| Notification failures retain monitors | `bot.py:333–371` | If both photo and text delivery fail, the account remains monitored—even after the target status was detected. |
| Invalid configuration and parsing keep checks unproductive | `.env.example:2`; `bot.py:257–310` | Nonempty cookie placeholder enables API attempts; zero-follower accounts and unexpected HTML can be misclassified. |
| Uncontrolled workload and exposed credentials | `bot.py:21–36,290,450–522` | No authorization, account cap, or expiry. Exposed proxy credentials could also be used externally; misuse is unproven. |

## Cost model

```text
Checks/day ≈ 86,400 × monitored accounts / full-sweep duration
Bytes/day ≈ checks/day × average provider-billed bytes/check + extra traffic
```

A sweep includes every sequential batch's request time, alert processing, and sleep—not just 1–2 seconds.

**Illustration only:** 10 accounts, a 2s sweep, and 20 KiB billed/check produce approximately **8.85 GB/day**.

## Recommended order

1. Verify deployed commit, replicas, monitor counts, cookie validity, and provider usage; rotate exposed credentials.
2. Back off inconclusive checks and separate notification retries from Instagram checks.
3. Deduplicate initial/background checks, bound bulk concurrency, and avoid unnecessary fallback.
4. Evaluate bounded response reads and session reuse against provider measurements.
5. Agree on acceptable alert delay before increasing the polling interval.

## Important limits

- Telegram and avatar traffic are not explicitly routed through these proxies.
- aiohttp normally supports compression and does not automatically download HTML-linked assets.
- Application read limits do **not** guarantee equivalent billing limits because of buffering and compression.
- With no monitors or outstanding checks, this loop should generate no Instagram proxy traffic.

**Needed to identify the dominant production cause:** GB/day, monitored-account count, whether the session cookie is populated/valid, request outcomes, and maximum acceptable alert delay.

# Streamed profile checks implementation plan

## Approved scope
- Preserve commands and card layout; `/monitor` waits for verified availability and `/banmonitor` waits for spaced, confirmed HTTP 404 unavailability (not proof of a ban).
- Default to a single streamed HTML GET per check; optional explicitly configured JSON mode has no fallback. Reuse verified prefix metadata in alerts, with no details request.
- Keep normal sampling at configurable 2 seconds after completion. Bound concurrent work, decoded body consumption (128 KiB default), and error/unknown/retry delays.
- Keep monitoring and pending notification delivery separate, cancel/ignore stale records, and require explicit startup secrets/proxies without logging credentials.
- Preserve the user-owned audit, existing assets, and dependency policy; no live external checks, bot startup, deployment, commits, or `.env` access.

## Implementation sequence
1. Add import-safe configuration, strict username/proxy validation, bounded incremental HTML/JSON readers, and status/counter helpers.
2. Replace instant/batch loops with one fair bounded scheduler, shared cookieless session, cached alert retries, identity guards, and clean shutdown.
3. Integrate honest alerts and ownership checks while retaining cards; constrain optional direct avatar traffic.
4. Add extensive offline parser/transport/scheduling/configuration/bot integration tests; document configuration, measured evidence and residual limits.
5. Run the suite in an isolated `/tmp` virtualenv, syntax/diff checks, and confirm the audit is unchanged and nothing is staged.

## Validation strategy
Mock chunked/error streams and clocks; use only loopback HTTP for transport tests and fake Telegram for integration. Cover identity, metadata beyond 8 KiB, split syntax/UTF-8, zero counts, caps/incomplete JSON, redirects/errors without reads or fallbacks, backoff/Retry-After, bounded fair scheduling, stop/re-add races, cached delivery retries, configuration and command protections. Local streaming results are not proxy billing measurements.

## Completion and validation
- Completed all five implementation steps. Added `config.py` and `monitoring.py`; integrated commands/cards and cached alert delivery in `bot.py`; updated README and `.env.example`; added three offline test modules.
- Normal checks retain the configurable two-second post-completion default. HTML/JSON use one bounded GET; redirects, fallback, error-body reads and recent aiohttp's implicit disconnected-GET retry are disabled. A loopback regression verifies the latter private compatibility switch.
- **82 offline tests passed** after the focused review revisions, with `PYTHONASYNCIODEBUG=1` and `ResourceWarning` treated as errors. Coverage includes mocked bot/network integration and three loopback transport tests (stream closure, no redirect/cookie accumulation, no hidden disconnected-GET retry).
- Passed `py_compile` for implementation/tests, `git diff --check`, and `pip check`. No files staged.
- Re-runnable interpreter: `/tmp/ig-streamed-checks-311-venv/bin/python` (CPython 3.11.11). Installed project requirements only in this isolated environment; tested aiohttp 3.14.3, aiogram 3.31.0 and Pillow 12.3.0.
- Initial Python 3.14 virtualenv bootstrap failed because its `pyexpat` could not resolve a libexpat symbol. Paused for supervisor approval; repaired the test environment using an already installed Python 3.11 interpreter and a new `/tmp` virtualenv. No global interpreter/package changes.
- User-owned audit unchanged: SHA-256 `b8df4575f0140f8f3cfaebababa9c64ceed4c412d8a165dc3703f42d34797fe3`. No `.env` access, bot startup, live Instagram/Telegram/proxy checks, merge/cherry-pick, commit, push or deployment.
- Residual limits are documented: no billed-byte guarantee; network read-ahead and connection setup remain; no live credential/proxy validation; English metadata can change; state/budgets/access controls remain intentionally limited; in-flight Telegram sends may arrive after stop or duplicate after ambiguous timeouts. Final independent review passed with no remaining findings.

### Focused independent-review revisions
- Completed all three supervisor-approved findings: anchored title-only identity at terminal `(@username)` before a recognized Instagram suffix (or title end), rejecting multiple/ambiguous handles; recognized the exact legacy session-cookie placeholder; required complete bounded JSON through verified transport/compression EOF and final UTF-8 decoding rather than accepting a root prefix.
- Added eight regression methods and expanded existing matrices: title spoof/multiple-handle chunk splits; exact legacy-placeholder suppression, safe mocked startup warning and JSON rejection; JSON trailing garbage/second documents, UTF-8 completion, compression truncation/trailing members, exact caps and unverified EOF. The scheduler, commands, cards, and two-second normal interval are unchanged.
- Full validation passed: 82 offline tests, `py_compile`, `pip check`, and `git diff --check` using `/tmp/ig-streamed-checks-311-venv/bin/python`. Audit checksum still matches the recorded value and no files are staged. Startup-warning tests mock `.env` loading and abort before bot construction; no real bot or external requests were made.
- Conservative limit retained: JSON reaching a cap without verified EOF remains unknown, even if its consumed prefix is syntactically valid. Updated README to state this and the title/legacy-placeholder rules. Final independent review passed. The parent independently reran all 82 tests and retained the final log at `/tmp/ig-streamed-parent-final-tests.log`; this supersedes the earlier 74-test log.

### Follow-up scheduler audit — unresolved before merge
A later parent audit reran all 82 tests successfully and added offline stress probes. These identified bandwidth-policy risks not covered by the prior passing review:
- Per-account two-second scheduling can raise aggregate request volume versus main's sequential batches. At a synthetic 50 ms request duration, 100 accounts produced 2,960 checks per steady-state minute versus 390 in the main scheduling model. A global request-rate limit remains absent.
- Challenge/429 cooldown is per account, not shared across the affected proxy/authentication context. A synthetic 100-account challenge wave attempted all 100 initial checks before their individual 300-second deadlines took effect.
- `Retry-After` is capped by `BACKOFF_MAX_SECONDS`; with defaults a server-directed 7,200-second delay becomes 300 seconds. Server cooldown policy should be separated from the local exponential-backoff ceiling.
These are open recommendations, not implemented fixes or production measurements. The implementation should remain a draft pending disposition of these findings. Final parent test log: `/tmp/ig-streamed-audit-tests.log`.

### Commands for independent rerun
```sh
PYTHONASYNCIODEBUG=1 /tmp/ig-streamed-checks-311-venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -v
/tmp/ig-streamed-checks-311-venv/bin/python -m py_compile bot.py config.py monitoring.py tests/test_*.py
/tmp/ig-streamed-checks-311-venv/bin/python -m pip check
git diff --check
git diff --cached --name-only
shasum -a 256 BANDWIDTH_AUDIT_REPORT.md
```

# PostgreSQL State Persistence Plan — Audit Report

## Scope and verdict

Reviewed `POSTGRES_STATE_PLAN.md` against the current implementation in `monitoring.py`, `bot.py`, and `config.py`, and the unresolved scheduler findings in `IMPLEMENTATION_PLAN.md`.

**Verdict: the proposed architecture is sound. Clarify concurrency, recovery, and persistence invariants before implementation.** The plan is not approval to provision a database or deploy the bot.

This was a static review. No database connection, credential inspection, provisioning, application execution, or test execution was performed for this report. Existing test counts are documented project history, not newly verified results.

## What the plan gets right

- PostgreSQL is the source of truth; memory is an execution cache.
- Monitor UUIDs isolate stopped/re-added handles from old results.
- Detection and outbox creation commit atomically.
- Pending notifications retry without repeating profile checks.
- External requests run outside database transactions.
- UTC deadlines survive restarts; monotonic deadlines remain process-local.
- Delivery duplicates and external-request crash windows are explicitly acknowledged.
- Runtime database failures pause work rather than falling back to volatile state.
- Provisioning, migration privileges, and runtime access are separated.
- Global request-rate and shared-cooldown problems are acknowledged rather than claimed to be solved by persistence.

## Findings requiring clarification

### 1. Monitor UUIDs are not leadership fencing

**Priority: High**  
**References:** `POSTGRES_STATE_PLAN.md`, sections 5–6; `monitoring.py`, `MonitorScheduler.current` and `_work`.

A UUID protects against stop/re-add races, but a monitor restored after a database disconnect retains its UUID. An old task can therefore still match the database record after recovery. An advisory lock does not automatically prevent writes through other database connections or recall external requests.

**Recommendation:** Invalidate the current execution epoch when ownership becomes uncertain. Cancel/drain old tasks before resuming, and reject old-epoch results and delivery continuations. Define lock-connection health checks, bounded failure detection, and database operation timeouts. Reacquire ownership and reload committed state before allowing commands or scheduling again.

The existing object-identity guard can contribute to this protection if restoration replaces records and all relevant paths retain the guard; UUID-only checks are not a substitute.

**Test:** Suspend a task, lose the lock connection, recover/reload the same monitor UUID, then release the old task. Verify it cannot overwrite restored state or initiate another alert send. Already-issued external requests remain an explicitly documented limitation.

### 2. Persistence failures must escape ordinary task backoff

**Priority: High**  
**References:** `POSTGRES_STATE_PLAN.md`, section 6; `monitoring.py`, `MonitorScheduler._work`.

The current scheduler broadly catches processing and delivery exceptions and reschedules work. If repository calls are added inside those handlers without distinguishing persistence failures, a failed commit could be treated as an ordinary check or notification failure rather than pausing the system.

**Recommendation:** Introduce an explicit persistence/ownership failure path that closes the scheduling and mutation gates. Do not advance the execution cache on a failed or uncertain commit. Resolve uncertain commit outcomes by reloading committed state after recovery.

**Test:** Inject failures during result persistence, notification retry persistence, and success deletion. Verify no new checks/deliveries start until recovery, and state-changing commands cannot report uncommitted success.

### 3. Database awaits introduce command/cache races

**Priority: High**  
**References:** `POSTGRES_STATE_PLAN.md`, section 5; `monitoring.py`, `add` and `stop`; `bot.py`, command handlers.

Current add/stop operations are synchronous. Database integration introduces await points between durable changes and cache updates. Without ordering protection, a delayed add/cache update could resurrect a monitor already removed by another command, or a delayed removal could affect a re-added generation.

**Recommendation:** Serialize conflicting mutations or use current-generation/revision guards around cache application. Include owner/chat predicates in the durable stop operation. Define `/bulk` partial-success behavior when a database failure occurs partway through the command.

**Test:** Interleave add, stop, re-add, and cache publication. Verify the cache converges to committed state and acknowledgements describe only committed operations.

### 4. UTC conversion must cover the entire application

**Priority: Medium**  
**References:** `POSTGRES_STATE_PLAN.md`, sections 2 and 8; `monitoring.py`, monitor creation/detection; `bot.py`, `trigger_alert` and `show_active`.

The current code uses naive `datetime.now()`. Restoring timezone-aware database timestamps without updating those call sites can cause naive/aware subtraction errors or incorrect elapsed times.

**Recommendation:** Use timezone-aware UTC consistently for creation, detection, restoration, and display calculations. Explicitly map `created_at` to the existing monitoring start time. Document the deadline clock source and wall-clock-to-monotonic conversion.

**Test:** Restore monitoring and pending records under a non-UTC host timezone and verify elapsed times and future deadlines. Include overdue deadlines and clock-offset cases.

### 5. Define schema and card-cache invariants

**Priority: Medium**  
**References:** `POSTGRES_STATE_PLAN.md`, sections 4–5.

The schema is appropriately minimal but leaves nullability, pending-state consistency, payload bounds, and card-cache persistence timing implicit.

**Recommendation:** Specify:

- Required `NOT NULL` fields and defaults, including the outbox foreign key and retry deadline.
- A usable deadline for every monitoring record; pending records must ignore any retained check deadline.
- Exactly one outbox row for each pending monitor, enforced through transactional repository operations and validated during restoration.
- Payload shape/size limits and safe handling of restored avatar URLs.
- When `photo_prepared` and `photo_bytes` commit together, including a durable text-only fallback.
- Safe behavior for inconsistent restored state: quarantine or fail closed, rather than silently rechecking or losing alerts.

**Test:** Exercise invalid/inconsistent rows, oversized payloads/cards, and crashes around card preparation. Verify prepared cards or text-only fallback survive restart without unnecessary avatar downloads.

## Corrections to the initial audit

The earlier audit overstated several recommendations as correctness blockers:

| Earlier concern | Correct assessment |
| --- | --- |
| A separate start-time column is required | `created_at` can represent start time; document the mapping. |
| Version/timestamp migrations cannot reject incompatible schemas | Explicit supported-version checks can reject incompatibility. Checksums improve drift detection; dirty-state tracking is not essential when migrations and version recording commit atomically. |
| Pending records require `next_check_at = NULL` | Optional convention. Ignoring that field while pending is sufficient. |
| Notification leases are required | Not required for the stated single-worker, at-least-once design. Pre-send attempt/deadline recording is optional crash-loop protection. |
| Optional card caching contradicts zero additional checks | No contradiction: avatar fetching is distinct from profile checking. Cache persistence timing still needs definition. |
| Rate policy must block all persistence development | It remains a production decision, but isolated persistence implementation/testing can proceed independently. |
| Bounded loading requires replacing the memory scheduler | Batches do not bound total memory, but a DB-backed scheduler is a scalability option, not a correctness requirement for this scope. |

## Operational readiness items

Before production use:

- Verify the target database, restricted role, endpoint/session behavior, and TLS certificate verification.
- Define migration serialization and supported schema versions; use atomic migrations where applicable.
- Decide the unresolved aggregate request-rate, shared-cooldown, and `Retry-After` policies.
- Establish backup retention, recovery objectives, and a restore drill. Restoring older state can replay previously delivered alerts.
- Monitor database/lock health, pending-alert age, retry volume, and storage growth without logging secrets or private payloads.
- Validate the existing offline suite and new disposable-PostgreSQL integration tests. No live Instagram or Telegram requests are needed for recovery tests.

## Recommended disposition

Accept the design direction. Resolve the five findings above in the plan and implementation tests. Keep provisioning and deployment separately approved, and retain the documented single-worker and at-least-once limitations.

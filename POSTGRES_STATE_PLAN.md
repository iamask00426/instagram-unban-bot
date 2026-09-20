# PostgreSQL State Persistence Plan

**Status:** proposal only. No database, schema, application, dependency, or environment changes have been made.

## 1. Goal and prerequisite

Persist monitors, check deadlines, failure backoff, and pending notifications so restarts/deployments do not lose monitoring state or trigger unnecessary Instagram checks.

Use a **new database, proposed name `instagram_monitor`, on the existing PostgreSQL instance**. Do not reuse, modify, or delete another application's database.

**Current blocker:** inspection found no PostgreSQL URI or recognized DB configuration in this repository's `.env` or the current process environment. The instance address, maintenance database, TLS requirements, and database-creation privileges remain unverified. No connection was attempted and no credentials are recorded here.

Before implementation, configure the existing instance connection securely in `.env` or deployment secrets. Do not paste credentials into the plan, chat, logs, or Git.

## 2. Proposed architecture

```text
Telegram commands → PostgreSQL transaction → in-memory scheduler
                                               ↓
                                      bounded Instagram check
                                               ↓
                                   persist result + next deadline
                                               ↓ target detected
                                     durable notification outbox
                                               ↓
                                      Telegram delivery/retry
```

- PostgreSQL is the source of truth; the existing in-memory scheduler remains the execution cache.
- Use `asyncpg` with a small bounded connection pool, initially 1–5 connections.
- Keep the current single-worker deployment. Use a dedicated PostgreSQL session advisory lock to reject a second active scheduler for the same bot/database; do not use a transaction-pooling endpoint for that lock.
- Runtime state uses UTC `TIMESTAMPTZ`. Never persist Python monotonic-clock values: they are process-specific. Convert saved wall-clock deadlines into local monotonic deadlines on restoration.
- Do not query or write PostgreSQL on every scheduler tick. Persist commands, completed check outcomes, and notification transitions.
- Persistence does not by itself fix the aggregate request-rate, shared-cooldown, or `Retry-After` issues documented in `IMPLEMENTATION_PLAN.md`.

## 3. Database provisioning — later, explicit action

1. Verify the supplied instance connection, PostgreSQL version, TLS settings, direct/session-pooling endpoint, and privileges without printing credentials.
2. Check whether `instagram_monitor` already exists. If it does, inspect ownership and schema; never assume it is disposable or overwrite it.
3. Using an authorized provisioning connection, create the new database outside a transaction. Managed providers may require their administration API instead of SQL `CREATE DATABASE`.
4. Provision/reuse an approved restricted application role. Keep database creation and migration privileges separate from routine runtime access where supported.
5. Configure `DATABASE_URL` for the new database. Keep any administrative/provisioning URL separate and unavailable to ordinary bot runtime where practical.
6. Apply versioned migrations and verify the application role can access only the intended schema/tables.

Application startup must **not** automatically create a database, grant roles, or run destructive migrations. No changes to existing instance databases are authorized by this plan.

## 4. Minimal schema

### `schema_migrations`

Version and applied timestamp, managed by a separate migration command.

### `monitors`

| Fields | Purpose |
| --- | --- |
| `id UUID PRIMARY KEY` | Durable record/generation identity for stop/re-add safety |
| `username TEXT UNIQUE` | Normalized handle; preserve current one-monitor-per-username behavior |
| `chat_id BIGINT`, `owner_id BIGINT` | Telegram routing and existing ownership protection |
| `mode` | `unban` or `ban`, enforced by a constraint |
| `state` | `monitoring` or `alert_pending` |
| `created_at`, `updated_at` | UTC lifecycle timestamps |
| `next_check_at TIMESTAMPTZ` | Durable scheduling deadline |
| `failures`, `unavailable_count` | Preserve error backoff and spaced confirmation progress |
| `last_status`, `last_checked_at` | Minimal diagnostic state; no raw response bodies |

Index due monitoring rows by `next_check_at`. Use constraints for valid states and nonnegative counters.

### `notification_outbox`

| Fields | Purpose |
| --- | --- |
| `id UUID PRIMARY KEY`, `monitor_id UUID UNIQUE REFERENCES monitors ON DELETE CASCADE` | At most one pending target notification per monitor generation |
| `payload JSONB` | Validated detection snapshot: handle, status, counts, optional avatar URL |
| `detected_at TIMESTAMPTZ` | Preserve detection time across delayed delivery/restarts |
| `attempts`, `next_attempt_at` | Durable notification backoff |
| `photo_prepared BOOLEAN`, `photo_bytes BYTEA NULL` | Optional bounded rendered-card cache; avoid repeated avatar fetches |
| `last_error_code TEXT NULL` | Sanitized category only, never raw exception text |

Apply a small explicit card-size limit, initially 1 MiB; fall back to text if exceeded. Do not store downloaded HTML, cookies, bot tokens, proxy credentials, or arbitrary error messages. Avatar URLs may contain temporary signatures: treat stored payloads as private and never log them.

On successful delivery, delete the monitor and associated outbox row transactionally. This minimal design retains active/pending state, not permanent account history.

## 5. State transitions and crash behavior

### Add / stop

- **Add:** validate input → insert and commit → acknowledge and wake the scheduler. A uniqueness constraint handles duplicate additions.
- **Stop:** check owner/chat → delete the exact monitor UUID and its outbox in one transaction → cancel/remove the local task. Never delete by username alone after awaiting network operations.
- Late check results update only their original UUID and valid state. A stopped/re-added handle has a different UUID and cannot inherit an old result.

### Check completion

- Persist status, counters, and next deadline before updating the execution cache.
- If the target is detected, insert the unique outbox row and mark the monitor `alert_pending` in the **same transaction**.
- Do not start notification delivery until that transaction commits. `alert_pending` rows never issue Instagram checks solely because Telegram delivery failed.
- Do not keep a database transaction open while contacting Instagram, downloading an avatar, rendering a card, or contacting Telegram.

### Notification delivery

- Restore cached detection/card data and retry without rechecking Instagram.
- Persist retry count/deadline on failure; remove state on success.
- Delivery is **at least once, not exactly once**: Telegram may accept a message before a timeout or process crash prevents recording success. A duplicate alert remains possible.
- An Instagram request may also repeat after a crash between receiving its result and committing it. Database persistence cannot make an external HTTP request and a SQL transaction atomic.

## 6. Startup, shutdown, and database failures

- Validate configuration and migration version, connect, acquire the single-scheduler lock, then load active monitors and pending outbox rows in bounded batches.
- Restore saved deadlines and counters rather than treating every account as newly added. Stagger overdue work within the chosen rate/concurrency policy; do not create an unbounded startup burst.
- If shared proxy/auth cooldowns are implemented, persist their UTC expiry separately and restore them before any checks. Define aggregate request-rate policy before claiming quota-safe restart behavior.
- On startup DB failure, fail clearly without starting monitoring. On runtime DB/lock failure, pause new outbound checks/deliveries and reject state-changing commands rather than silently falling back to volatile memory.
- Existing in-flight HTTP/Telegram operations cannot always be recalled. Stop local scheduling when leadership loss is detected; stale-generation checks still protect restored records. This is not a multi-worker exactly-once system.
- After reconnecting, reacquire ownership and reload committed state before resuming. Do not blindly flush an old execution cache over the database.
- On graceful shutdown, stop scheduling, finish/cancel bounded work, close HTTP clients, release the lock, and close the pool. Correctness must not depend on a final bulk save.

## 7. Implementation sequence

1. Confirm connection configuration, target database name, and creation privileges; approve provisioning.
2. Add an isolated PostgreSQL integration-test database and versioned schema migrations.
3. Add import-safe database settings and an async repository layer with transactional state operations.
4. Integrate add/stop commands, persisted check completion, and UUID-based stale-result protection.
5. Add durable outbox/card retry state and restoration.
6. Add startup ownership lock, DB-failure pause/recovery, overdue scheduling, and graceful shutdown.
7. Update `.env.example`, README, operational checks, and backup/restore instructions. Leave real `.env` changes to the approved setup step.
8. Run offline/unit and PostgreSQL integration tests before an explicitly approved deployment.

Use a focused follow-up change to the current streaming implementation; do not silently replace the scheduler, alter normal polling latency, or add multi-worker behavior.

## 8. Local test and acceptance plan

Retain the existing 82 tests and add tests against a disposable local PostgreSQL database—not another application's database on the supplied instance.

- Migration from empty DB, repeat migration, and incompatible schema rejection.
- Add/delete ownership, duplicate handles, and stop/re-add UUID isolation.
- Restart restores mode, ownership, start time, counters, and future deadlines.
- Pending alerts survive restart and retry with **zero additional Instagram checks**.
- Detection/outbox transaction commits both records or neither.
- DB disconnect or write failure pauses external work and cannot acknowledge unsaved commands.
- Second scheduler cannot acquire ownership; ownership-loss recovery reloads committed state.
- Overdue monitors resume with bounded work; shutdown and crash-boundary cases do not create orphaned outbox rows.
- Mocked Telegram timeout demonstrates the documented duplicate-delivery possibility.
- Logs redact credentials; tokens/proxy secrets and raw profile responses never enter DB rows.

**Acceptance:** a process restart preserves active and alert-pending monitoring state, with no live Instagram/Telegram calls needed to test recovery. Instance provisioning, credentials, privileges, and operational backups must be verified before production use.

## 9. Decisions needed before execution

- Supply the existing instance connection securely; confirm whether it permits a separate database and application role.
- Approve `instagram_monitor` as the new database name.
- Confirm the deployment will initially run one active bot worker and has a session-capable DB connection.
- Decide the global request-rate/shared-cooldown policy identified by the current draft PR's audit.
- Confirm backup retention and the acceptable notification delivery semantics described above.

**Current deliverable is this plan only. No database has been created.**

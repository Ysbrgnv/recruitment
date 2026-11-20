# Architecture

## 1. Purpose and scope

Recruitment Assistant coordinates candidate search, AI-assisted evaluation, browser automation, persistence, and recruiter-facing results. The architecture separates synchronous HTTP handling from durable background work and isolates browser automation in a dedicated service.

## 2. Runtime topology

```text
Browser / Recruiter
        |
        v
+-------------------+
| Web service       |
| FastAPI / ASGI    |
+---------+---------+
          |
          v
+-------------------+       +---------------------+
| PostgreSQL        |<----->| General worker      |
| queue + state     |       | HH, AI, Gmail       |
+---------+---------+       +---------------------+
          |
          +---------------->+---------------------+
                            | Browser worker      |
                            | X-Ray orchestration |
                            +----------+----------+
                                       |
                                       v
                            +---------------------+
                            | Browser service     |
                            | Node.js + Playwright|
                            +---------------------+
```

Docker Compose defines PostgreSQL, configuration initialization, the browser service, web, two worker lanes, bootstrap, and maintenance/backup roles.

## 3. Application layers

### `app/core`

Cross-cutting runtime concerns:

- validated environment configuration;
- retry and timeout policies;
- security and request guards;
- structured logging and telemetry;
- process limits and runtime configuration;
- user-facing and infrastructure error types.

### `app/domain`

Deterministic business rules that do not depend directly on infrastructure. Query Generation V3 belongs here.

### `app/integrations`

External-system adapters:

- `hh/`: HeadHunter OAuth, token locking, HTTP requests, scheduling;
- `ai/`: NanoGPT client and query interpretation;
- `gmail/`: result delivery;
- `xray/`: public-profile search planning and browser-driven execution.

### `app/persistence`

Durability and consistency:

- PostgreSQL backend and connection pool;
- durable task queue;
- operation store and transaction scope;
- task/state tracking;
- compatibility state access where required by existing workflows.

### `app/services`

Application use cases and orchestration:

- recruitment workflow;
- candidate search and evaluation;
- query validation and rendering;
- result delivery;
- retention and cleanup.

### `app/web`

ASGI startup, HTTP routes, health endpoints, and static UI delivery.

### `app/workers`

Background execution and monitoring, including progress watchdog responsibilities.

### `browser_service`

A separate Node.js/Playwright process. Isolation prevents browser lifecycle and resource consumption from being coupled directly to the Python web process.

## 4. Process responsibilities

| Process | Primary responsibility |
|---|---|
| `web` | UI/API, task submission, status and health endpoints |
| `worker-general` | HeadHunter, AI, Gmail, and non-browser background work |
| `worker-browser` | Browser-lane workflows and X-Ray orchestration |
| `browser` | Playwright contexts and browser isolation |
| `db` | PostgreSQL queue, state, operations, and durable coordination |
| `init-config` | Seeds runtime HH configuration into a protected volume |
| `bootstrap` | Database initialization and schema bootstrap |
| `backup` | Backup creation and restore verification |

## 5. Main workflow

1. A recruiter selects or submits a vacancy through the web UI.
2. The web service creates durable task and operation records.
3. PostgreSQL-backed queue routing assigns work to a general or browser lane.
4. HeadHunter search and Candidate Pool logic collect candidates.
5. X-Ray workflows use the browser worker and isolated browser service when enabled.
6. AI services interpret queries and evaluate candidates according to workflow rules.
7. Results and progress are persisted.
8. Gmail delivery sends the final output when required.
9. The UI polls durable state rather than depending on in-memory worker state.

## 6. Candidate Pool V3

Candidate Pool V3 is designed around bounded external calls and recoverable progress.

Key properties:

- pool target derived from the requested candidate count;
- bounded pagination and new-candidate collection;
- checkpoint and pool-version routing;
- snapshot TTL and recovery thresholds;
- optional shadow-mode experience filtering;
- deterministic retry budget for HeadHunter calls.

The retry envelope is configuration-driven:

```text
max_step_duration =
    max_http_attempts * (connect_timeout + read_timeout)
    + max_retry_after * (max_http_attempts - 1)
    + checkpoint_margin
```

Current defaults:

```text
4 * (10 + 30) + 30 * 3 + 30 = 280 seconds
```

Required ordering:

```text
worker timeout > maximum step duration
operation-lock TTL > worker timeout
queue visibility timeout > worker timeout
```

Current defaults are 300, 360, and 360 seconds respectively.

## 7. HeadHunter authentication and retries

HeadHunter requests use the `requests` timeout tuple:

```python
(connect_timeout, read_timeout)
```

The transient retry contract permits at most four HTTP attempts. Backoff delays are 2, 4, and 8 seconds. A valid `Retry-After` replaces the delay for the current retry and is capped at 30 seconds. Authentication recovery is kept separate from the transient retry counter to avoid creating an accidental second transient loop.

## 8. Durability and concurrency

- PostgreSQL is the authoritative queue and coordination store for the Compose runtime.
- Separate general and browser lanes prevent browser-heavy tasks from blocking ordinary work.
- Operation ownership and lock TTLs fence concurrent processing.
- Queue visibility timeout exceeds worker timeout to reduce premature redelivery.
- Checkpoints allow a workflow to resume without repeating all external work.

Default local parallelism:

```text
GENERAL_CONSUMERS=16
XRAY_CONSUMERS=8
BROWSER_MAX_SESSIONS=8
```

These settings require capacity testing before production use.

## 9. Security boundaries

- Secrets are not stored in Git.
- Runtime secrets are mounted through Docker secret files.
- Application services run as non-root users.
- Filesystems are read-only except explicit tmpfs and mounted volumes.
- Linux capabilities are dropped.
- `no-new-privileges` is enabled.
- Database and browser networks are internal.
- Only services requiring external providers join the egress network.
- Browser contexts are isolated and checked by a smoke test.

The optional Cloudflare tunnel is intended only for controlled demonstrations. It does not provide application authentication.

## 10. Data and privacy

The project processes recruitment data. Public repositories, tests, screenshots, and demonstration environments must contain only synthetic candidate data. Real resumes, names, emails, phone numbers, national identifiers, employer secrets, and production database dumps must not be committed.

## 11. Testing strategy

- `tests/concurrency/`: parallel execution and HH error behavior.
- `tests/regression/`: offline behavioral and contract regression.
- `tests/integration/`: disposable PostgreSQL integration tests.
- Docker test image: reproducible Python and dependency environment.
- `--network none`: verifies that the offline suite does not require external providers.
- Gitleaks: scans Git history for known secret formats.

Current verified results:

```text
HH retry contract: 8 passed
Concurrency + regression: 487 passed
Gitleaks: no leaks found
```

## 12. Known boundaries

- Live HeadHunter, Gmail, AI-provider, and Playwright-provider flows are not represented by the offline regression result.
- The documented retry envelope models the contracted transient-attempt path. Authentication-recovery timing should be reviewed separately if it becomes part of a strict end-to-end deadline.
- The current repository contains both ASGI and lower-level HTTP components. Their boundary should remain documented and should be simplified only through a separately tested refactor.
- SQLite-compatible state facilities and the PostgreSQL runtime coexist for compatibility and testability. PostgreSQL remains the Compose runtime queue and coordination backend.
- Backup verification is not a complete disaster-recovery certification.
- Production throughput limits require load and capacity testing.

## 13. Change discipline

Changes to retry budgets, timeout envelopes, queue visibility, lock TTLs, checkpoint behavior, or authentication recovery must be introduced through explicit contracts and regression tests. Avoid changing these values as an unreviewed configuration-only adjustment because they are coupled across HTTP behavior, worker lifetime, operation ownership, and queue redelivery.

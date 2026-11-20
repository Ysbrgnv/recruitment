# Recruitment Assistant

Recruitment Assistant is a containerized recruitment automation service that combines candidate search through HeadHunter and X-Ray, AI-assisted resume evaluation, recruiter workflow orchestration, and email delivery of results.

## Key capabilities

- HeadHunter candidate search with resilient authentication and retry handling.
- Candidate Pool V3 with checkpointing, snapshot recovery, and bounded HTTP retries.
- X-Ray search through an isolated browser service.
- AI-assisted query interpretation and candidate evaluation.
- Durable PostgreSQL-backed task queue and operation tracking.
- Separate web, general-worker, browser-worker, and browser-service processes.
- Gmail delivery of recruitment results.
- Backup verification, health checks, diagnostics, and operational scripts.

## Technology stack

- Python 3.12
- FastAPI and Uvicorn
- PostgreSQL 16 with psycopg 3
- Docker and Docker Compose v2
- Playwright browser automation
- Node.js browser service
- pytest and Gitleaks

## Architecture overview

```text
Recruiter / Web UI
        |
        v
   Web service
        |
        +--------------------+
        |                    |
        v                    v
PostgreSQL queue       Browser service
        |                    |
        +---------+----------+
                  |
          +-------+-------+
          |               |
          v               v
   General worker    Browser worker
          |               |
          +---+-------+---+
              |       |
              v       v
         HH / AI   X-Ray / Playwright
              |
              v
          Gmail delivery
```

The application follows a layered structure:

- `app/core/`: configuration, security, errors, retry policy, telemetry, and process limits.
- `app/domain/`: domain rules such as deterministic query generation.
- `app/integrations/`: HeadHunter, AI, Gmail, and X-Ray adapters.
- `app/persistence/`: PostgreSQL access, durable queue, operations, and transactions.
- `app/services/`: recruitment workflows and application services.
- `app/web/`: ASGI application and HTTP layer.
- `app/workers/`: background processing and progress control.
- `browser_service/`: isolated Node.js browser service.
- `entrypoints/`: web and worker process entrypoints.
- `scripts/`: setup, diagnostics, backup, and operational utilities.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for component boundaries, runtime flows, and resilience contracts.

## Repository structure

```text
app/                    application code
browser_service/        isolated browser service
docker/                 container adapters and health checks
docs/                   architecture and historical notes
entrypoints/             process entrypoints
scripts/                 setup, diagnostics, backup, operations
tests/concurrency/       concurrency and HH failure tests
tests/integration/       PostgreSQL integration tests
tests/regression/        offline regression suite
ui/                      static web interface
compose.yaml             local multi-service runtime
Dockerfile               application, test, maintenance images
START_LOCAL.ps1          full local startup
STOP_LOCAL.ps1           safe stop without volume deletion
UPDATE_WEB.ps1           web-only rebuild and recreation
```

## Requirements

- Windows PowerShell 5.1 or newer.
- Docker Desktop using Linux containers.
- Docker Compose v2.
- Internet access for the first image build and external integrations.

Local Python is not required for the normal Docker-based startup.

## Configuration

1. Create a private local configuration from the public template:

```powershell
Copy-Item .env.example .env
```

2. Fill the required values in `.env`:

```text
HH_CLIENT_ID
HH_CLIENT_SECRET
HH_REDIRECT_URI
EMPLOYER_ID
NANO_GPT_API_KEY
GMAIL_USER
GMAIL_APP_PASSWORD
```

`HH_BEARER` and `HH_REFRESH_TOKEN` may be populated by the authentication flow. Do not commit `.env`, `docker.local.env`, `deploy/secrets/`, logs, databases, backups, or real candidate data.

The public `.env.example` also documents the bounded HH timeout and retry envelope:

- connect timeout: 10 seconds;
- read timeout: 30 seconds;
- maximum HTTP attempts: 4;
- maximum `Retry-After`: 30 seconds;
- checkpoint margin: 30 seconds;
- worker timeout: 300 seconds;
- operation-lock TTL: 360 seconds;
- queue visibility timeout: 360 seconds.

## Local startup

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\START_LOCAL.ps1
```

The startup script builds the application and browser images, prepares local runtime secrets, validates Compose, starts PostgreSQL, bootstraps the database, synchronizes configuration, starts the web and worker services, checks browser isolation, and prints service status.

After startup:

- application: `http://localhost:8765`
- liveness endpoint: `http://localhost:8765/health/live`

## Stop and update

Stop runtime services without deleting Docker volumes:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\STOP_LOCAL.ps1
```

Rebuild and recreate only the web service:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\UPDATE_WEB.ps1
```

Do not run `docker compose down -v` unless permanent removal of local database volumes is intentional and a verified backup exists.

## Temporary external access

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\START_TUNNEL.ps1
```

This requires `cloudflared`. The tunnel does not replace authentication or access control. Do not expose real resumes or personal data through an unsecured demonstration endpoint.

## Runtime roles

`docker/container_entry.py` supports these roles:

- `web`: starts `entrypoints/web.py`;
- `general`: starts the general worker lane;
- `browser`: starts the browser worker lane;
- `check-gmail`: runs Gmail diagnostics;
- `smoke-browser`: verifies browser-context isolation;
- `bootstrap`: initializes the database;
- `backup`: performs one backup verification run;
- `backup-loop`: performs periodic backup verification.

Default local parallelism:

- `GENERAL_CONSUMERS=16`
- `XRAY_CONSUMERS=8`
- `BROWSER_MAX_SESSIONS=8`

These values are local defaults, not universal production sizing guidance.

## Candidate Pool V3 resilience contract

Candidate Pool V3 uses bounded HH requests and checkpoint-aware recovery:

- maximum four transient HTTP requests;
- retry delays of 2, 4, and 8 seconds;
- `Retry-After` capped at 30 seconds;
- configurable connect/read timeouts passed to `requests` as a tuple;
- checkpoint margin included in the effective worker-timeout envelope;
- operation-lock TTL and queue visibility timeout must exceed the worker timeout.

The current default envelope is:

```text
4 x (10 s connect + 30 s read)
+ 3 x 30 s maximum Retry-After
+ 30 s checkpoint margin
= 280 s maximum configured step envelope
```

The worker timeout is 300 seconds, while operation-lock TTL and queue visibility timeout are 360 seconds.

## Testing

### Reproducible offline suite

Build the test image:

```powershell
docker build --target test -t recruitment-test .
```

Run the concurrency and regression suites without network access:

```powershell
docker run --rm --network none `
  --tmpfs /tmp:rw,nosuid,nodev,size=256m `
  --env RECRUITMENT_DB_PATH=/tmp/recruitment-tests.db `
  --env LOG_DIR=/tmp/recruitment-logs `
  --env APP_ENV_FILE=/tmp/recruitment-test.env `
  --env TASK_QUEUE_BACKEND=local `
  --env PYTHONDONTWRITEBYTECODE=1 `
  --entrypoint python `
  recruitment-test `
  -m pytest -p no:cacheprovider -q `
  tests/concurrency tests/regression
```

Latest verified result for the current implementation:

```text
487 passed in 3.30s
exit code 0
```

### HH retry contract

```powershell
docker run --rm --network none `
  --tmpfs /tmp:rw,nosuid,nodev,size=256m `
  --env RECRUITMENT_DB_PATH=/tmp/recruitment-tests.db `
  --env LOG_DIR=/tmp/recruitment-logs `
  --env APP_ENV_FILE=/tmp/recruitment-test.env `
  --env TASK_QUEUE_BACKEND=local `
  --env PYTHONDONTWRITEBYTECODE=1 `
  --entrypoint python `
  recruitment-test `
  -m pytest -p no:cacheprovider -q `
  tests/regression/test_hh_retry_contract_v3.py
```

Latest verified result: `8 passed`, exit code `0`.

### PostgreSQL integration tests

```powershell
python -m pytest -p no:cacheprovider -q tests/integration/test_multi_recruiter_postgres.py
```

Use only a disposable PostgreSQL instance through `TEST_DATABASE_ADMIN_URL` and `TEST_DATABASE_APP_URL`. Never point integration tests at a production database.

## Secret scanning

The repository history was checked with Gitleaks:

```powershell
gitleaks git . --redact --verbose
```

Latest verified result: no leaks found.

## Security controls

- Secrets are stored outside Git and exposed to containers through runtime secret files.
- Application containers run as non-root users with read-only filesystems.
- Linux capabilities are dropped and `no-new-privileges` is enabled.
- PostgreSQL and browser networks are internal Docker networks.
- Internet access is isolated to services attached to the egress network.
- Real resumes and candidate personal data must not be committed or used in public demonstrations.
- External tunnels do not replace authentication and access control.

These controls reduce risk but do not constitute a formal security certification.

## Backup verification

The maintenance image contains the PostgreSQL 16 client. The backup workflow creates a backup and performs restore verification according to the current implementation.

Manual one-time run:

```powershell
docker compose --env-file docker.local.env --profile maintenance run --rm backup backup
```

## Current verification status

Verified on the current implementation:

- Docker test image build completed successfully;
- Python syntax check completed successfully;
- HH retry contract: 8 passed;
- offline concurrency and regression suite: 487 passed;
- `git diff --check`: clean;
- Gitleaks: no leaks found.

Not claimed by this README:

- production certification;
- live end-to-end validation against HeadHunter, Gmail, the AI provider, or Playwright providers;
- disaster-recovery certification;
- performance sizing for production traffic.

## License

No open-source license has been selected yet. Until a license is added, all rights remain reserved.

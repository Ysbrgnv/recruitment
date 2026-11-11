"""Disposable PostgreSQL integration tests for session-aware task creation.

Required environment variables:
    TEST_DATABASE_ADMIN_URL  PostgreSQL URL for the disposable database owner/admin.
    TEST_DATABASE_APP_URL    PostgreSQL URL for a DML-only runtime role.

Safety contract:
- refuses non-loopback database hosts;
- refuses the production database/project names used by the local stack;
- never loads .env files or Docker secrets;
- uses only synthetic task, vacancy, email, cookie, and session values;
- performs schema initialization only through the admin URL;
- exercises create/get/list through the runtime URL.
"""
from __future__ import annotations

import hashlib
import importlib
import inspect
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

ADMIN_ENV = "TEST_DATABASE_ADMIN_URL"
APP_ENV = "TEST_DATABASE_APP_URL"
DATABASE_ENV = "DATABASE_URL"
SAFE_HOSTS = {"127.0.0.1", "localhost", "::1"}
SAFE_CONTAINER_PREFIX = "mr-pg-"
FORBIDDEN_DATABASES = {"recruitment"}
SYNTHETIC_VACANCY = "900000001"
SYNTHETIC_EMAIL = "integration@example.invalid"
ACTIVE_STATUSES = (
    "queued",
    "running",
    "waiting_approval",
    "waiting_manual",
    "waiting_browser",
    "waiting_query",
    "interrupted",
)
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _required_url(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(f"{name} is required for the disposable PostgreSQL test", pytrace=False)
    parsed = urlparse(value)
    if parsed.scheme not in {"postgres", "postgresql"}:
        pytest.fail(f"{name} must be a PostgreSQL URL", pytrace=False)
    if parsed.hostname not in SAFE_HOSTS and not (parsed.hostname or "").startswith(SAFE_CONTAINER_PREFIX):
        pytest.fail(
            f"{name} must target loopback or a synthetic {SAFE_CONTAINER_PREFIX}* container; "
            f"got host={parsed.hostname!r}",
            pytrace=False,
        )
    database = parsed.path.lstrip("/")
    if not database or database in FORBIDDEN_DATABASES or "clean-v3" in database:
        pytest.fail(f"Unsafe disposable database name: {database!r}", pytrace=False)
    return value


def _session(raw_cookie: str) -> str:
    return hashlib.sha256(raw_cookie.encode("utf-8")).hexdigest()


RAW_COOKIE_A = "synthetic-cookie-a-" + "a" * 40
RAW_COOKIE_B = "synthetic-cookie-b-" + "b" * 40
SESSION_A = _session(RAW_COOKIE_A)
SESSION_B = _session(RAW_COOKIE_B)


def _connect(url: str):
    return psycopg.connect(url, autocommit=True, row_factory=dict_row)


def _reload_app_state(database_url: str):
    os.environ[DATABASE_ENV] = database_url
    os.environ["TASK_QUEUE_BACKEND"] = "postgres"
    os.environ.pop("APP_SCHEMA_MANAGED", None)
    from app.persistence import db_pool
    from app.persistence import pg_backend
    from app.persistence import app_state

    # Modules read some runtime settings during import. Reload only the project's
    # database-facing modules, never dotenv/runtime_config.
    importlib.reload(db_pool)
    importlib.reload(pg_backend)
    return importlib.reload(app_state)


def _grant_runtime_dml(admin_url: str, app_url: str):
    """Grant only runtime DML in the disposable database after admin init."""
    runtime_role = urlparse(app_url).username
    if not runtime_role or runtime_role == urlparse(admin_url).username:
        pytest.fail("Runtime role must be distinct from the disposable admin role", pytrace=False)
    with _connect(admin_url) as con:
        role = sql.Identifier(runtime_role)
        con.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(role))
        con.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
        con.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO {}").format(role))
        con.execute(sql.SQL("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(role))


def _create_task(app_state, task_id: str, owner_session: str | None):
    """Call the current public API while failing clearly on an obsolete signature."""
    signature = inspect.signature(app_state.create_task)
    if "owner_session" not in signature.parameters:
        pytest.fail("app_state.create_task() has no owner_session parameter", pytrace=False)
    return app_state.create_task(
        task_id,
        SYNTHETIC_VACANCY,
        1,
        SYNTHETIC_EMAIL,
        "hh",
        owner_session=owner_session,
    )


def _active_rows(admin_url: str):
    with _connect(admin_url) as con:
        return con.execute(
            """SELECT id,status,data FROM tasks
               WHERE vacancy_id=%s AND email=%s AND mode='hh' AND status=ANY(%s)
               ORDER BY id""",
            (SYNTHETIC_VACANCY, SYNTHETIC_EMAIL, list(ACTIVE_STATUSES)),
        ).fetchall()


def _task_owner(task):
    return task.get("owner_session") if task else None


def _truncate_runtime_data(admin_url: str):
    """Clear only the disposable database; preserve schema and migrations."""
    with _connect(admin_url) as con:
        names = [
            row["tablename"]
            for row in con.execute(
                """SELECT tablename FROM pg_tables
                   WHERE schemaname='public' AND tablename<>'migrations'"""
            ).fetchall()
        ]
        if names:
            statement = sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(
                sql.SQL(", ").join(sql.Identifier(name) for name in names)
            )
            con.execute(statement)


@pytest.fixture(scope="session")
def database_urls():
    admin_url = _required_url(ADMIN_ENV)
    app_url = _required_url(APP_ENV)
    if urlparse(admin_url).path != urlparse(app_url).path:
        pytest.fail("Admin and runtime URLs must target the same disposable database", pytrace=False)

    admin_state = _reload_app_state(admin_url)
    admin_state.init_db()
    # Idempotency is part of the contract.
    admin_state.init_db()
    _grant_runtime_dml(admin_url, app_url)

    runtime_state = _reload_app_state(app_url)
    yield admin_url, app_url, runtime_state


@pytest.fixture(autouse=True)
def clean_database(database_urls):
    admin_url, app_url, _ = database_urls
    _truncate_runtime_data(admin_url)
    runtime_state = _reload_app_state(app_url)
    yield admin_url, app_url, runtime_state


def test_same_session_sequential_create(clean_database):
    admin_url, _, app_state = clean_database
    first_id = uuid.uuid4().hex
    second_id = uuid.uuid4().hex

    first_task_id, first_created = _create_task(app_state, first_id, SESSION_A)
    second_task_id, second_created = _create_task(app_state, second_id, SESSION_A)

    assert first_created is True
    assert second_created is False
    assert first_task_id == second_task_id == first_id
    rows = _active_rows(admin_url)
    assert len(rows) == 1
    assert _task_owner(app_state.get_task(first_id)) == SESSION_A


def test_cross_session_create_and_owned_lists(clean_database):
    _, _, app_state = clean_database
    task_a = uuid.uuid4().hex
    task_b = uuid.uuid4().hex

    returned_a, created_a = _create_task(app_state, task_a, SESSION_A)
    returned_b, created_b = _create_task(app_state, task_b, SESSION_B)

    assert created_a is True and created_b is True
    assert returned_a == task_a and returned_b == task_b and returned_a != returned_b
    assert _task_owner(app_state.get_task(task_a)) == SESSION_A
    assert _task_owner(app_state.get_task(task_b)) == SESSION_B

    listed_a = app_state.list_tasks(SYNTHETIC_VACANCY, owner_session=SESSION_A)
    listed_b = app_state.list_tasks(SYNTHETIC_VACANCY, owner_session=SESSION_B)
    assert [task["task_id"] for task in listed_a] == [task_a]
    assert [task["task_id"] for task in listed_b] == [task_b]


def test_legacy_ownerless_task_is_not_reused_by_owned_session(clean_database):
    _, _, app_state = clean_database
    legacy_id = uuid.uuid4().hex
    owned_id = uuid.uuid4().hex

    returned_legacy, legacy_created = _create_task(app_state, legacy_id, None)
    legacy_before = app_state.get_task(legacy_id)
    returned_owned, owned_created = _create_task(app_state, owned_id, SESSION_A)

    assert legacy_created is True and returned_legacy == legacy_id
    assert owned_created is True and returned_owned == owned_id
    assert app_state.get_task(legacy_id) == legacy_before
    assert _task_owner(app_state.get_task(legacy_id)) is None
    assert _task_owner(app_state.get_task(owned_id)) == SESSION_A
    assert [task["task_id"] for task in app_state.list_tasks(SYNTHETIC_VACANCY, owner_session=SESSION_A)] == [owned_id]


def test_owned_lists_exclude_legacy_ownerless_task(clean_database):
    _, _, app_state = clean_database
    legacy_id = uuid.uuid4().hex
    task_a = uuid.uuid4().hex
    task_b = uuid.uuid4().hex
    _create_task(app_state, legacy_id, None)
    _create_task(app_state, task_a, SESSION_A)
    _create_task(app_state, task_b, SESSION_B)

    assert {task["task_id"] for task in app_state.list_tasks(SYNTHETIC_VACANCY, owner_session=SESSION_A)} == {task_a}
    assert {task["task_id"] for task in app_state.list_tasks(SYNTHETIC_VACANCY, owner_session=SESSION_B)} == {task_b}


def test_creating_task_b_does_not_mutate_task_a(clean_database):
    _, _, app_state = clean_database
    task_a = uuid.uuid4().hex
    task_b = uuid.uuid4().hex
    _create_task(app_state, task_a, SESSION_A)
    app_state.update_task(
        task_a,
        status="waiting_query",
        checkpoint={"marker": "checkpoint-a"},
        progress={"marker": "progress-a"},
        query="query-a",
    )
    before = app_state.get_task(task_a)

    returned_b, created_b = _create_task(app_state, task_b, SESSION_B)
    after = app_state.get_task(task_a)

    assert created_b is True and returned_b == task_b
    for field in ("status", "checkpoint", "progress", "query", "owner_session"):
        assert after[field] == before[field]


def test_parallel_same_session_create_is_deduplicated(clean_database):
    admin_url, app_url, _ = clean_database
    workers = 4
    barrier = threading.Barrier(workers)
    task_ids = [uuid.uuid4().hex for _ in range(workers)]

    app_state = _reload_app_state(app_url)

    def create(task_id):
        barrier.wait(timeout=10)
        return _create_task(app_state, task_id, SESSION_A)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(create, task_ids))

    created = [result for result in results if result[1] is True]
    returned_ids = {result[0] for result in results}
    rows = _active_rows(admin_url)
    assert len(created) == 1, f"Expected one creator, got results={results!r}"
    assert len(returned_ids) == 1, f"Expected one returned task_id, got results={results!r}"
    assert len(rows) == 1, f"Expected one active row, found {len(rows)} rows={rows!r}"


def test_parallel_cross_session_create_is_isolated(clean_database):
    admin_url, app_url, _ = clean_database
    barrier = threading.Barrier(2)
    task_a = uuid.uuid4().hex
    task_b = uuid.uuid4().hex

    app_state = _reload_app_state(app_url)

    def create(task_id, owner):
        barrier.wait(timeout=10)
        return _create_task(app_state, task_id, owner)

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(create, task_a, SESSION_A)
        future_b = pool.submit(create, task_b, SESSION_B)
        result_a = future_a.result(timeout=20)
        result_b = future_b.result(timeout=20)

    assert result_a == (task_a, True)
    assert result_b == (task_b, True)
    rows = _active_rows(admin_url)
    assert len(rows) == 2
    runtime_state = _reload_app_state(app_url)
    assert _task_owner(runtime_state.get_task(task_a)) == SESSION_A
    assert _task_owner(runtime_state.get_task(task_b)) == SESSION_B


def test_only_session_hashes_are_persisted(clean_database):
    admin_url, _, app_state = clean_database
    task_a = uuid.uuid4().hex
    task_b = uuid.uuid4().hex
    _create_task(app_state, task_a, SESSION_A)
    _create_task(app_state, task_b, SESSION_B)

    with _connect(admin_url) as con:
        rows = con.execute("SELECT data::text AS payload FROM tasks ORDER BY id").fetchall()
    payload = "\n".join(row["payload"] for row in rows)
    assert SESSION_A in payload and SESSION_B in payload
    assert RAW_COOKIE_A not in payload and RAW_COOKIE_B not in payload


def test_admin_init_drops_legacy_index_preserves_schema_and_is_idempotent(clean_database):
    admin_url, _, _ = clean_database
    with _connect(admin_url) as con:
        con.execute("CREATE INDEX sentinel_tasks_index ON tasks(id)")
        con.execute(
            """CREATE UNIQUE INDEX one_active_task
               ON tasks(vacancy_id,email,mode)
               WHERE status IN ('queued','running','waiting_approval','waiting_manual','waiting_browser')"""
        )
        table_oid = con.execute("SELECT 'public.tasks'::regclass::oid AS oid").fetchone()["oid"]

    admin_state = _reload_app_state(admin_url)
    admin_state.init_db()
    admin_state.init_db()

    with _connect(admin_url) as con:
        indexes = {
            row["indexname"]
            for row in con.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname='public' AND tablename='tasks'"
            ).fetchall()
        }
        after_oid = con.execute("SELECT 'public.tasks'::regclass::oid AS oid").fetchone()["oid"]
    assert "one_active_task" not in indexes
    assert "sentinel_tasks_index" in indexes
    assert after_oid == table_oid


def test_runtime_role_is_dml_only_and_not_schema_owner(clean_database):
    admin_url, app_url, app_state = clean_database
    runtime_role = urlparse(app_url).username
    assert runtime_role

    with _connect(admin_url) as con:
        role_checks = con.execute(
            """SELECT
                 has_schema_privilege(%s,'public','USAGE') AS schema_usage,
                 has_schema_privilege(%s,'public','CREATE') AS schema_create,
                 pg_has_role(current_user,%s,'MEMBER') AS admin_is_member""",
            (runtime_role, runtime_role, runtime_role),
        ).fetchone()
        owner_count = con.execute(
            """SELECT COUNT(*) AS n FROM pg_class c
               JOIN pg_namespace n ON n.oid=c.relnamespace
               JOIN pg_roles r ON r.oid=c.relowner
               WHERE n.nspname='public' AND r.rolname=%s""",
            (runtime_role,),
        ).fetchone()["n"]

    assert role_checks["schema_usage"] is True
    assert role_checks["schema_create"] is False
    assert owner_count == 0

    task_id = uuid.uuid4().hex
    returned, created = _create_task(app_state, task_id, SESSION_A)
    assert returned == task_id and created is True
    assert app_state.get_task(task_id)["task_id"] == task_id
    assert [task["task_id"] for task in app_state.list_tasks(SYNTHETIC_VACANCY, owner_session=SESSION_A)] == [task_id]

    with _connect(app_url) as con:
        with pytest.raises(psycopg.Error):
            con.execute("CREATE TABLE must_not_be_created(id integer)")
        with pytest.raises(psycopg.Error):
            con.execute("ALTER TABLE tasks ADD COLUMN must_not_exist integer")


def test_administrative_ownerless_create_keeps_legacy_deduplication(clean_database):
    admin_url, _, app_state = clean_database
    owned_id = uuid.uuid4().hex
    first_id = uuid.uuid4().hex
    second_id = uuid.uuid4().hex

    owned_returned, owned_created = _create_task(app_state, owned_id, SESSION_A)
    first_returned, first_created = _create_task(app_state, first_id, None)
    second_returned, second_created = _create_task(app_state, second_id, None)

    assert owned_created is True and owned_returned == owned_id
    assert first_created is True
    assert second_created is False
    assert first_returned == second_returned == first_id
    rows = _active_rows(admin_url)
    assert len(rows) == 2
    assert _task_owner(app_state.get_task(owned_id)) == SESSION_A
    assert _task_owner(app_state.get_task(first_id)) is None

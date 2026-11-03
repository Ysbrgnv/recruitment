"""RED tests for browser-session-aware active-task deduplication.

Offline only: SQLite is temporary and all task submission is mocked. No HH, AI,
Gmail, Google, browser service, or PostgreSQL connection is used.
"""
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("TASK_QUEUE_BACKEND", "local")
os.environ.pop("DATABASE_URL", None)
os.environ.pop("APP_SCHEMA_MANAGED", None)

import app_state as state
import operation_store as operations
import asgi_app
import web_server



class MultiRecruiterIsolationTests(unittest.TestCase):
    VACANCY_ID = "123"
    EMAIL = "shared@example.com"
    COOKIE_A = "a" * 64
    COOKIE_B = "b" * 64
    ACTOR_A = hashlib.sha256(COOKIE_A.encode()).hexdigest()
    ACTOR_B = hashlib.sha256(COOKIE_B.encode()).hexdigest()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db_patch = patch.object(state, "DB_PATH", Path(self.tmp.name) / "isolation.db")
        db_patch.start()
        self.addCleanup(db_patch.stop)
        state.init_db()
        operations.init()
        submit_patch = patch.object(web_server, "submit_task", return_value=None)
        submit_patch.start()
        self.addCleanup(submit_patch.stop)

    @staticmethod
    def payload(response):
        return json.loads(response.body.decode("utf-8"))

    @staticmethod
    def headers(cookie):
        return [
            (b"content-type", b"application/json"),
            (b"cookie", f"task_session={cookie}".encode()),
        ]

    def create_http(self, cookie, mode="hh"):
        endpoint = "find-resumes" if mode == "hh" else "xray-search"
        body = {
            "requested_count": 1,
            "responsible_email": self.EMAIL,
        }
        if mode == "xray":
            body["xray_sources"] = ["linkedin.com"]
        return asgi_app.dispatch(
            "POST",
            f"/api/vacancy/{self.VACANCY_ID}/{endpoint}",
            self.headers(cookie),
            json.dumps(body).encode(),
        )

    def get_task_http(self, cookie, task_id):
        return asgi_app.dispatch(
            "GET",
            f"/api/tasks/{task_id}",
            [(b"cookie", f"task_session={cookie}".encode())],
            b"",
        )

    def list_http(self, cookie):
        return asgi_app.dispatch(
            "GET",
            f"/api/vacancy/{self.VACANCY_ID}/tasks",
            [(b"cookie", f"task_session={cookie}".encode())],
            b"",
        )

    def task(self, task_id):
        return state.get_task(task_id)

    def test_cross_session_identical_create_produces_independent_tasks(self):
        first = self.create_http(self.COOKIE_A)
        second = self.create_http(self.COOKIE_B)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        task_a = self.payload(first)
        task_b = self.payload(second)
        self.assertTrue(task_a["created"])
        self.assertTrue(task_b["created"])
        self.assertNotEqual(task_a["task_id"], task_b["task_id"])
        self.assertEqual(self.task(task_a["task_id"])["owner_session"], self.ACTOR_A)
        self.assertEqual(self.task(task_b["task_id"])["owner_session"], self.ACTOR_B)

    def test_same_session_identical_create_returns_existing_task(self):
        first = self.create_http(self.COOKIE_A)
        second = self.create_http(self.COOKIE_A)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        first_data = self.payload(first)
        second_data = self.payload(second)
        self.assertTrue(first_data["created"])
        self.assertFalse(second_data["created"])
        self.assertEqual(first_data["task_id"], second_data["task_id"])
        with state.database() as con:
            count = con.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_owner_hash_is_persisted_and_raw_cookie_is_never_stored(self):
        response_a = self.create_http(self.COOKIE_A)
        response_b = self.create_http(self.COOKIE_B)
        task_a = self.task(self.payload(response_a)["task_id"])
        task_b = self.task(self.payload(response_b)["task_id"])
        self.assertEqual(task_a["owner_session"], self.ACTOR_A)
        self.assertEqual(task_b["owner_session"], self.ACTOR_B)
        self.assertNotEqual(task_a["owner_session"], self.COOKIE_A)
        self.assertNotEqual(task_b["owner_session"], self.COOKIE_B)
        with state.database() as con:
            stored = "\n".join(row["data"] for row in con.execute("SELECT data FROM tasks"))
        self.assertNotIn(self.COOKIE_A, stored)
        self.assertNotIn(self.COOKIE_B, stored)

    def test_vacancy_task_lists_are_isolated_by_session(self):
        response_a = self.create_http(self.COOKIE_A)
        response_b = self.create_http(self.COOKIE_B)
        task_a = self.payload(response_a)["task_id"]
        task_b = self.payload(response_b)["task_id"]
        list_a = self.payload(self.list_http(self.COOKIE_A))
        list_b = self.payload(self.list_http(self.COOKIE_B))
        self.assertEqual([item["task_id"] for item in list_a], [task_a])
        self.assertEqual([item["task_id"] for item in list_b], [task_b])

    def test_task_access_is_isolated_in_both_directions(self):
        response_a = self.create_http(self.COOKIE_A)
        response_b = self.create_http(self.COOKIE_B)
        task_a = self.payload(response_a)["task_id"]
        task_b = self.payload(response_b)["task_id"]
        self.assertEqual(self.get_task_http(self.COOKIE_A, task_a).status_code, 200)
        self.assertEqual(self.get_task_http(self.COOKIE_A, task_b).status_code, 403)
        self.assertEqual(self.get_task_http(self.COOKIE_B, task_b).status_code, 200)
        self.assertEqual(self.get_task_http(self.COOKIE_B, task_a).status_code, 403)

    def test_legacy_ownerless_active_task_is_not_adopted_or_reused(self):
        legacy_id = "1" * 32
        state.create_task(legacy_id, self.VACANCY_ID, 1, self.EMAIL, "hh")
        self.assertIsNone(self.task(legacy_id).get("owner_session"))
        response = self.create_http(self.COOKIE_A)
        self.assertEqual(response.status_code, 202)
        created = self.payload(response)
        self.assertTrue(created["created"])
        self.assertNotEqual(created["task_id"], legacy_id)
        self.assertIsNone(self.task(legacy_id).get("owner_session"))
        self.assertEqual(self.task(created["task_id"])["owner_session"], self.ACTOR_A)

    def test_creating_task_b_does_not_modify_task_a(self):
        response_a = self.create_http(self.COOKIE_A)
        task_a_id = self.payload(response_a)["task_id"]
        state.update_task(
            task_a_id,
            checkpoint={"cursor": "a"},
            progress={"stage": "a"},
            query="query-a",
            status="waiting_manual",
        )
        before = self.task(task_a_id)
        response_b = self.create_http(self.COOKIE_B)
        self.assertEqual(response_b.status_code, 202)
        after = self.task(task_a_id)
        for key in ("owner_session", "checkpoint", "progress", "query", "status"):
            self.assertEqual(after.get(key), before.get(key), key)

    def test_backend_create_task_deduplicates_by_owner_session(self):
        task_a, created_a = state.create_task(
            "a" * 32,
            self.VACANCY_ID,
            1,
            self.EMAIL,
            "hh",
            owner_session=self.ACTOR_A,
        )
        task_a_repeat, created_a_repeat = state.create_task(
            "b" * 32,
            self.VACANCY_ID,
            1,
            self.EMAIL,
            "hh",
            owner_session=self.ACTOR_A,
        )
        task_b, created_b = state.create_task(
            "c" * 32,
            self.VACANCY_ID,
            1,
            self.EMAIL,
            "hh",
            owner_session=self.ACTOR_B,
        )
        self.assertTrue(created_a)
        self.assertFalse(created_a_repeat)
        self.assertEqual(task_a_repeat, task_a)
        self.assertTrue(created_b)
        self.assertNotEqual(task_b, task_a)
        self.assertEqual(self.task(task_a)["owner_session"], self.ACTOR_A)
        self.assertEqual(self.task(task_b)["owner_session"], self.ACTOR_B)

    def test_parallel_same_session_create_has_one_winner_in_sqlite(self):
        barrier = threading.Barrier(2)

        def create(index):
            barrier.wait()
            return state.create_task(
                f"{index + 10:032x}",
                self.VACANCY_ID,
                1,
                self.EMAIL,
                "hh",
                owner_session=self.ACTOR_A,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, (0, 1)))
        self.assertEqual(sum(1 for _, created in results if created), 1)
        self.assertEqual(len({task_id for task_id, _ in results}), 1)
        with state.database() as con:
            count = con.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

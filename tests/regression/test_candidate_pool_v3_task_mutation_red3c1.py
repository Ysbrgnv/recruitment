"""RED-3C-1: atomic task mutation and JSON state_version CAS contract.

Production code must not be changed until this suite has been run and the
expected RED result has been captured.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.persistence import app_state


class TaskMutationContractRed3C1(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "red3c1.sqlite3"
        self.env = patch.dict(
            os.environ,
            {
                "RECRUITMENT_DB_PATH": str(self.db_path),
                "DATABASE_URL": "",
                "TASK_QUEUE_BACKEND": "local",
                "APP_SCHEMA_MANAGED": "false",
            },
            clear=False,
        )
        self.env.start()
        self.old_db_path = app_state.DB_PATH
        app_state.DB_PATH = self.db_path
        app_state.init_db()
        app_state.create_task(
            "task-3c-1",
            "vacancy-1",
            5,
            "owner@example.com",
            "hh",
            owner_session="owner-session-a",
        )

    def tearDown(self):
        app_state.DB_PATH = self.old_db_path
        self.env.stop()
        self.tmp.cleanup()

    def _raw_data(self):
        con = sqlite3.connect(self.db_path)
        try:
            row = con.execute(
                "SELECT data FROM tasks WHERE id=?", ("task-3c-1",)
            ).fetchone()
            return json.loads(row[0])
        finally:
            con.close()

    def _mutate(self, expected_version, **fields):
        mutate = getattr(app_state, "mutate_task", None)
        self.assertTrue(
            callable(mutate),
            "RED-3C-1 requires app.persistence.app_state.mutate_task",
        )
        return mutate(
            "task-3c-1",
            expected_state_version=expected_version,
            **fields,
        )

    def test_legacy_task_without_version_is_version_zero(self):
        task = app_state.get_task("task-3c-1")
        self.assertEqual(task.get("state_version", 0), 0)

        result = self._mutate(0, checkpoint={"marker": "first"})

        self.assertTrue(result.applied)
        self.assertFalse(result.conflict)
        self.assertEqual(result.state_version, 1)
        saved = app_state.get_task("task-3c-1")
        self.assertEqual(saved["state_version"], 1)
        self.assertEqual(saved["checkpoint"], {"marker": "first"})

    def test_successful_mutation_increments_exactly_once(self):
        first = self._mutate(0, checkpoint={"marker": "one"})
        second = self._mutate(
            first.state_version,
            progress={"marker": "two"},
        )

        self.assertTrue(second.applied)
        self.assertEqual(second.state_version, 2)
        self.assertEqual(app_state.get_task("task-3c-1")["state_version"], 2)

    def test_stale_mutation_returns_conflict_and_changes_nothing(self):
        applied = self._mutate(0, checkpoint={"winner": True})
        self.assertTrue(applied.applied)
        before = self._raw_data()

        stale = self._mutate(
            0,
            checkpoint={"stale": True},
            owner_session="owner-session-stale",
        )

        self.assertFalse(stale.applied)
        self.assertTrue(stale.conflict)
        self.assertEqual(stale.state_version, 1)
        self.assertEqual(self._raw_data(), before)

    def test_partial_mutation_preserves_owner_and_unmentioned_fields(self):
        app_state.update_task(
            "task-3c-1",
            query="approved query",
            checkpoint={"old": True},
        )

        result = self._mutate(0, checkpoint={"new": True})

        self.assertTrue(result.applied)
        saved = app_state.get_task("task-3c-1")
        self.assertEqual(saved["owner_session"], "owner-session-a")
        self.assertEqual(saved["query"], "approved query")
        self.assertEqual(saved["checkpoint"], {"new": True})

    def test_unknown_task_is_not_reported_as_conflict(self):
        mutate = getattr(app_state, "mutate_task", None)
        self.assertTrue(callable(mutate))
        with self.assertRaises(KeyError):
            mutate(
                "missing-task",
                expected_state_version=0,
                checkpoint={"x": 1},
            )

    def test_legacy_update_task_does_not_treat_cas_control_as_data(self):
        with self.assertRaises(TypeError):
            app_state.update_task(
                "task-3c-1",
                expected_state_version=0,
                checkpoint={"must-not-save": True},
            )
        saved = app_state.get_task("task-3c-1")
        self.assertNotIn("expected_state_version", saved)
        self.assertNotIn("checkpoint", saved)


if __name__ == "__main__":
    unittest.main()

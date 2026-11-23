"""RED-3B: isolated Candidate Pool V3 mass-failure guard contracts.

Handler-level state rehydration only. Production V3 routing remains disabled
and is not asserted here. All external boundaries are mocked.
"""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from app.core import settings
from app.core.errors import HHUnavailableError, UserFacingError
from app.services import recruitment_service as service


GUARD_KEY = "details_failure_guard"
GUARD_FIELDS = {
    "schema_version": 1,
    "completed_resume_ids": [],
    "recent_outcomes": [],
    "consecutive_failures": 0,
    "attempts_total": 0,
    "failures_total": 0,
    "not_found_failures": 0,
    "malformed_failures": 0,
    "generic_failures": 0,
    "trigger": None,
}


def _details_error(kind, message="synthetic details error"):
    return HHUnavailableError(
        message,
        error_kind=kind,
        request_scope="details",
        endpoint="/resumes/{id}",
    )


def _critical_error(kind, scope="details"):
    return HHUnavailableError(
        "synthetic critical error",
        error_kind=kind,
        request_scope=scope,
        endpoint="/resumes/{id}",
    )


def _candidate(resume_id):
    return (resume_id, {"id": resume_id, "title": f"Candidate {resume_id}"}, [])


def _state(**overrides):
    value = copy.deepcopy(GUARD_FIELDS)
    value.update(copy.deepcopy(overrides))
    return value


class _MissingGuard(dict):
    def __init__(self):
        super().__init__(
            schema_version=-1,
            completed_resume_ids=[],
            recent_outcomes=[],
            consecutive_failures=-1,
            attempts_total=-1,
            failures_total=-1,
            not_found_failures=-1,
            malformed_failures=-1,
            generic_failures=-1,
            trigger={},
        )


def _guard(metrics):
    value = metrics.get(GUARD_KEY)
    return value if isinstance(value, dict) else _MissingGuard()


def _pool_ids(pool):
    return [item[0] for item in pool]


def expect_guard_error(test, call):
    try:
        call()
    except UserFacingError as exc:
        return exc
    except (KeyError, IndexError, AttributeError, TypeError):
        raise
    except Exception as exc:
        test.fail(
            f"expected UserFacingError guard, got {type(exc).__name__}: {exc}"
        )
    else:
        test.fail("expected UserFacingError guard, no exception raised")


class FailureGuardHarness:
    def invoke(
        self,
        *,
        items=None,
        outcomes=None,
        metrics=None,
        existing_pool=None,
        pool_exclusions=None,
        history_blocked=None,
        target_pool=100,
        requested_count=5,
        scan_limit=100,
        window=20,
        min_sample=10,
        rate_threshold=0.50,
        consecutive_limit=5,
        checkpoint_callback=None,
    ):
        self.search_calls = []
        self.details_calls = []
        self.progress_calls = []
        self.checkpoints = []
        self.live_metrics = metrics if metrics is not None else {}
        items = copy.deepcopy(items if items is not None else [])
        outcomes = dict(outcomes if outcomes is not None else {})
        existing_pool = copy.deepcopy(existing_pool if existing_pool is not None else [])
        pool_exclusions = copy.deepcopy(
            pool_exclusions if pool_exclusions is not None else {}
        )
        history_blocked = set(history_blocked or ())

        def search(params):
            self.search_calls.append(copy.deepcopy(params))
            return {"items": copy.deepcopy(items), "pages": 1}

        def details(resume_id):
            self.details_calls.append(resume_id)
            outcome = outcomes.get(resume_id, {"id": resume_id})
            if isinstance(outcome, BaseException):
                raise outcome
            return copy.deepcopy(outcome)

        def progress(*args):
            self.progress_calls.append(copy.deepcopy(args))

        def checkpoint(pool, current_metrics, exclusions):
            snapshot = (
                copy.deepcopy(pool),
                copy.deepcopy(current_metrics),
                copy.deepcopy(exclusions),
            )
            self.checkpoints.append(snapshot)
            if checkpoint_callback is not None:
                checkpoint_callback(pool, current_metrics, exclusions)

        with (
            patch.object(settings, "CANDIDATE_POOL_V3_MAX_PAGES", 10),
            patch.object(settings, "CANDIDATE_POOL_V3_SCAN_LIMIT", scan_limit),
            patch.object(settings, "CANDIDATE_POOL_V3_FAILURE_WINDOW", window),
            patch.object(settings, "CANDIDATE_POOL_V3_FAILURE_MIN_SAMPLE", min_sample),
            patch.object(
                settings,
                "CANDIDATE_POOL_V3_FAILURE_RATE_THRESHOLD",
                rate_threshold,
            ),
            patch.object(
                settings,
                "CANDIDATE_POOL_V3_CONSECUTIVE_FAILURE_LIMIT",
                consecutive_limit,
            ),
            patch.object(service, "search_resumes", side_effect=search),
            patch.object(service, "fetch_resume_details", side_effect=details),
            patch.object(
                service,
                "was_sent_to_ai",
                side_effect=lambda resume_id, vacancy_id: resume_id in history_blocked,
            ),
            patch.object(service, "reset_history_cache"),
        ):
            result = service._search_candidate_pool_v3(
                "vacancy-synthetic",
                {"id": "vacancy-synthetic"},
                {},
                '"approved query"',
                target_pool,
                requested_count,
                self.live_metrics,
                progress,
                pool_exclusions=pool_exclusions,
                existing_pool=existing_pool,
                checkpoint=checkpoint,
                plan=None,
            )
        return result


class FailureGuardStateTests(FailureGuardHarness, unittest.TestCase):
    def test_missing_guard_state_initializes_schema_version_one(self):
        self.invoke(items=[])
        state = _guard(self.live_metrics)
        self.assertIsNotNone(state)
        self.assertEqual(state.get("schema_version"), 1)

    def test_fresh_guard_state_starts_empty(self):
        self.invoke(items=[])
        state = _guard(self.live_metrics)
        self.assertEqual(state, GUARD_FIELDS)

    def test_valid_guard_state_is_not_aliased_to_fixture(self):
        fixture = _state(recent_outcomes=["success"], completed_resume_ids=["old"])
        metrics = {GUARD_KEY: fixture}
        before = copy.deepcopy(fixture)
        self.invoke(items=[{"id": "new"}], metrics=metrics)
        self.assertEqual(fixture, before)
        self.assertIsNot(_guard(self.live_metrics), fixture)

    def test_legacy_metrics_without_guard_state_are_supported(self):
        metrics = {"scanned": 4, "load_failed": 2}
        result = self.invoke(items=[], metrics=metrics)
        self.assertIsInstance(result, dict)
        self.assertEqual(metrics["scanned"], 4)
        self.assertEqual(metrics["load_failed"], 2)
        self.assertEqual(_guard(metrics).get("attempts_total"), 0)

    def test_malformed_guard_state_fails_closed_without_leaking_raw_state(self):
        raw = {
            "schema_version": 999,
            "completed_resume_ids": ["resume-sensitive-id"],
            "token": "synthetic-token",
            "query": "secret-query",
        }
        metrics = {GUARD_KEY: raw}
        exc = expect_guard_error(self, lambda: self.invoke(items=[], metrics=metrics))
        self.assertIn("состояние защиты", str(exc).casefold())
        for secret in ("resume-sensitive-id", "synthetic-token", "secret-query"):
            self.assertNotIn(secret, str(exc))
        self.assertEqual(metrics[GUARD_KEY], raw)

    def test_rolling_window_is_bounded_by_runtime_setting(self):
        state = _state(
            recent_outcomes=["success", "not_found", "success"],
            attempts_total=3,
            failures_total=1,
            not_found_failures=1,
            completed_resume_ids=["a", "b", "c"],
        )
        metrics = {GUARD_KEY: state}
        self.invoke(items=[{"id": "d"}], metrics=metrics, window=3)
        self.assertEqual(len(_guard(metrics)["recent_outcomes"]), 3)
        self.assertEqual(_guard(metrics)["recent_outcomes"], ["not_found", "success", "success"])


class FailureGuardAttemptAccountingTests(FailureGuardHarness, unittest.TestCase):
    def test_one_details_call_creates_one_attempt_and_success(self):
        self.invoke(items=[{"id": "a"}])
        state = _guard(self.live_metrics)
        self.assertEqual(self.details_calls, ["a"])
        self.assertEqual(state["attempts_total"], 1)
        self.assertEqual(state["failures_total"], 0)
        self.assertEqual(state["recent_outcomes"], ["success"])

    def test_non_attempt_items_do_not_change_guard_accounting(self):
        existing = [_candidate("existing")]
        exclusions = {"excluded": {"reason": "existing"}}
        items = [
            {},
            {"id": "existing"},
            {"id": "excluded"},
            {"id": "blocked"},
            {"id": "dup"},
            {"id": "dup"},
        ]
        self.invoke(
            items=items,
            existing_pool=existing,
            pool_exclusions=exclusions,
            history_blocked={"blocked"},
            target_pool=10,
        )
        state = _guard(self.live_metrics)
        self.assertEqual(self.details_calls, ["dup"])
        self.assertEqual(state["attempts_total"], 1)
        self.assertEqual(state["completed_resume_ids"], ["dup"])

    def test_empty_search_page_is_not_details_attempt(self):
        self.invoke(items=[])
        state = _guard(self.live_metrics)
        self.assertEqual(self.details_calls, [])
        self.assertEqual(state["attempts_total"], 0)
        self.assertEqual(state["recent_outcomes"], [])

    def test_success_resets_streak_and_retains_rolling_failures(self):
        metrics = {
            GUARD_KEY: _state(
                recent_outcomes=["not_found", "generic_error"],
                consecutive_failures=2,
                attempts_total=2,
                failures_total=2,
                not_found_failures=1,
                generic_failures=1,
                completed_resume_ids=["old-a", "old-b"],
            )
        }
        self.invoke(items=[{"id": "success"}], metrics=metrics)
        state = _guard(metrics)
        self.assertEqual(state["recent_outcomes"], ["not_found", "generic_error", "success"])
        self.assertEqual(state["consecutive_failures"], 0)
        self.assertEqual(state["failures_total"], 2)

    def test_success_checkpoint_contains_candidate_and_completed_id_together(self):
        self.invoke(items=[{"id": "a"}])
        matching = [
            snapshot
            for snapshot in self.checkpoints
            if "a" in _pool_ids(snapshot[0])
            and "a" in (_guard(snapshot[1]) or {}).get("completed_resume_ids", [])
        ]
        self.assertTrue(matching)


class FailureGuardTriggerTests(FailureGuardHarness, unittest.TestCase):
    def test_four_consecutive_failures_do_not_trigger(self):
        items = [{"id": f"f{i}"} for i in range(4)]
        outcomes = {item["id"]: RuntimeError("ordinary") for item in items}
        result = self.invoke(items=items, outcomes=outcomes)
        self.assertIsInstance(result, dict)
        self.assertEqual(_guard(self.live_metrics)["consecutive_failures"], 4)
        self.assertIsNone(_guard(self.live_metrics)["trigger"])

    def test_fifth_consecutive_failure_triggers_after_checkpoint(self):
        items = [{"id": f"f{i}"} for i in range(5)]
        outcomes = {item["id"]: RuntimeError("ordinary") for item in items}
        exc = expect_guard_error(self, lambda: self.invoke(items=items, outcomes=outcomes))
        self.assertIn("слишком много резюме не удалось загрузить", str(exc).casefold())
        self.assertTrue(self.checkpoints)
        state = _guard(self.checkpoints[-1][1])
        self.assertEqual(state["trigger"]["kind"], "consecutive")
        self.assertEqual(state["consecutive_failures"], 5)

    def test_success_breaks_consecutive_streak(self):
        items = [{"id": "f1"}, {"id": "f2"}, {"id": "ok"}, {"id": "f3"}, {"id": "f4"}]
        outcomes = {
            "f1": RuntimeError("x"), "f2": RuntimeError("x"),
            "f3": RuntimeError("x"), "f4": RuntimeError("x"),
        }
        self.invoke(items=items, outcomes=outcomes, target_pool=10)
        state = _guard(self.live_metrics)
        self.assertEqual(state["consecutive_failures"], 2)
        self.assertIsNone(state["trigger"])

    def test_mixed_ordinary_failures_share_streak_and_reason_counters(self):
        items = [{"id": "nf"}, {"id": "mf"}, {"id": "ge"}]
        outcomes = {
            "nf": _details_error("not_found"),
            "mf": _details_error("malformed_response"),
            "ge": RuntimeError("generic"),
        }
        self.invoke(items=items, outcomes=outcomes, target_pool=10)
        state = _guard(self.live_metrics)
        self.assertEqual(state["consecutive_failures"], 3)
        self.assertEqual(state["failures_total"], 3)
        self.assertEqual(
            (state["not_found_failures"], state["malformed_failures"], state["generic_failures"]),
            (1, 1, 1),
        )

    def test_consecutive_wins_when_streak_and_rate_match(self):
        state = _state(
            recent_outcomes=["success", "not_found", "success", "generic_error", "success", "not_found", "generic_error", "generic_error", "generic_error"],
            consecutive_failures=4,
            attempts_total=9,
            failures_total=6,
            not_found_failures=2,
            generic_failures=4,
            completed_resume_ids=[f"old{i}" for i in range(9)],
        )
        metrics = {GUARD_KEY: state}
        exc = expect_guard_error(
            self,
            lambda: self.invoke(
                items=[{"id": "trigger"}],
                outcomes={"trigger": RuntimeError("x")},
                metrics=metrics,
            ),
        )
        self.assertIsInstance(exc, UserFacingError)
        trigger = _guard(self.checkpoints[-1][1])["trigger"]
        self.assertEqual(trigger["kind"], "consecutive")
        self.assertEqual(trigger["sample_size"], 10)

    def test_rate_guard_boundaries_and_inclusive_threshold(self):
        cases = (
            (9, 9, False),
            (10, 5, True),
            (10, 4, False),
            (20, 9, False),
            (20, 10, True),
        )
        for attempts, failures, triggers in cases:
            with self.subTest(attempts=attempts, failures=failures):
                outcomes = []
                remaining_failures = failures
                for index in range(attempts):
                    slots_left = attempts - index
                    if remaining_failures and (index % 2 == 0 or remaining_failures == slots_left):
                        outcomes.append("generic_error")
                        remaining_failures -= 1
                    else:
                        outcomes.append("success")
                state = _state(
                    recent_outcomes=outcomes[:-1],
                    consecutive_failures=1 if outcomes[:-1] and outcomes[-2] != "success" else 0,
                    attempts_total=attempts - 1,
                    failures_total=outcomes[:-1].count("generic_error"),
                    generic_failures=outcomes[:-1].count("generic_error"),
                    completed_resume_ids=[f"old{i}" for i in range(attempts - 1)],
                )
                metrics = {GUARD_KEY: state}
                final = outcomes[-1]
                kwargs = {
                    "items": [{"id": "last"}],
                    "metrics": metrics,
                    "consecutive_limit": 99,
                }
                if final == "generic_error":
                    kwargs["outcomes"] = {"last": RuntimeError("x")}
                if triggers:
                    expect_guard_error(self, lambda: self.invoke(**kwargs))
                    trigger = _guard(self.checkpoints[-1][1])["trigger"]
                    self.assertEqual(trigger["kind"], "rate")
                    self.assertEqual(trigger["sample_size"], attempts)
                    self.assertEqual(trigger["failure_count"], failures)
                    self.assertAlmostEqual(trigger["failure_rate"], failures / attempts)
                else:
                    result = self.invoke(**kwargs)
                    self.assertIsInstance(result, dict)
                    self.assertIsNone(_guard(metrics)["trigger"])

    def test_twenty_first_outcome_evicts_oldest(self):
        initial = ["not_found"] + ["success"] * 19
        metrics = {
            GUARD_KEY: _state(
                recent_outcomes=initial,
                attempts_total=20,
                failures_total=1,
                not_found_failures=1,
                completed_resume_ids=[f"old{i}" for i in range(20)],
            )
        }
        self.invoke(items=[{"id": "new"}], metrics=metrics, window=20)
        state = _guard(metrics)
        self.assertEqual(len(state["recent_outcomes"]), 20)
        self.assertEqual(state["recent_outcomes"], ["success"] * 20)


class FailureGuardClassificationTests(FailureGuardHarness, unittest.TestCase):
    def test_each_ordinary_failure_updates_only_its_reason_counter(self):
        cases = (
            (_details_error("not_found"), "not_found", (1, 0, 0)),
            (_details_error("malformed_response"), "malformed_response", (0, 1, 0)),
            (RuntimeError("generic"), "generic_error", (0, 0, 1)),
        )
        for error, expected_outcome, counters in cases:
            with self.subTest(outcome=expected_outcome):
                metrics = {}
                self.invoke(items=[{"id": "a"}], outcomes={"a": error}, metrics=metrics)
                state = _guard(metrics)
                self.assertEqual(state["recent_outcomes"], [expected_outcome])
                self.assertEqual(
                    (state["not_found_failures"], state["malformed_failures"], state["generic_failures"]),
                    counters,
                )
                self.assertEqual(metrics["load_failed"], 1)
                self.assertEqual(state["completed_resume_ids"], ["a"])

    def test_critical_details_errors_propagate_without_guard_mutation(self):
        kinds = (
            "timeout", "network_error", "rate_limited", "server_error",
            "forbidden", "auth_failed", "auth_revoked", "auth_disabled",
            "client_error",
        )
        for kind in kinds:
            with self.subTest(kind=kind):
                error = _critical_error(kind)
                metrics = {GUARD_KEY: _state()}
                with self.assertRaises(HHUnavailableError) as caught:
                    self.invoke(items=[{"id": "a"}], outcomes={"a": error}, metrics=metrics)
                self.assertIs(caught.exception, error)
                self.assertEqual(_guard(metrics), GUARD_FIELDS)
                self.assertEqual(self.checkpoints, [])

    def test_wrong_scope_and_legacy_errors_propagate_unchanged(self):
        errors = (
            _critical_error("not_found", scope="search"),
            _critical_error("not_found", scope=None),
            HHUnavailableError("legacy"),
        )
        for error in errors:
            with self.subTest(scope=getattr(error, "request_scope", None)):
                metrics = {GUARD_KEY: _state()}
                with self.assertRaises(HHUnavailableError) as caught:
                    self.invoke(items=[{"id": "a"}], outcomes={"a": error}, metrics=metrics)
                self.assertIs(caught.exception, error)
                self.assertEqual(_guard(metrics), GUARD_FIELDS)
                self.assertEqual(self.checkpoints, [])


class FailureGuardCheckpointTests(FailureGuardHarness, unittest.TestCase):
    def test_trigger_checkpoint_is_complete_safe_and_precedes_exception(self):
        items = [{"id": "accepted"}] + [{"id": f"f{i}"} for i in range(5)]
        outcomes = {f"f{i}": RuntimeError("exception-sensitive-text") for i in range(5)}
        callback_order = []
        def saved(*args): callback_order.append("checkpoint")
        exc = expect_guard_error(
            self,
            lambda: self.invoke(
                items=items,
                outcomes=outcomes,
                target_pool=10,
                checkpoint_callback=saved,
            ),
        )
        callback_order.append("exception")
        self.assertEqual(callback_order[-2:], ["checkpoint", "exception"])
        pool, metrics, exclusions = self.checkpoints[-1]
        self.assertIn("accepted", _pool_ids(pool))
        state = _guard(metrics)
        trigger = state["trigger"]
        self.assertEqual(
            set(trigger),
            {"kind", "consecutive_failures", "sample_size", "failure_count", "failure_rate"},
        )
        self.assertNotIn("stop_reason", metrics)
        self.assertNotIn("exception-sensitive-text", str(exc))
        self.assertEqual(exclusions, {})

    def test_triggering_failure_adds_exactly_one_checkpoint_delta(self):
        state = _state(
            recent_outcomes=["generic_error"] * 4,
            consecutive_failures=4,
            attempts_total=4,
            failures_total=4,
            generic_failures=4,
            completed_resume_ids=["a", "b", "c", "d"],
        )
        metrics = {GUARD_KEY: state}

        # Each invoke() initializes a fresh checkpoint collector.
        before = 0

        expect_guard_error(
            self,
            lambda: self.invoke(
                items=[{"id": "e"}],
                outcomes={"e": RuntimeError("x")},
                metrics=metrics,
            ),
        )

        self.assertEqual(
            len(self.checkpoints),
            before + 1,
            "triggering details outcome must add exactly one checkpoint",
        )

        snapshot = self.checkpoints[-1]
        snapshot_metrics = snapshot[1]

        self.assertIn(
            GUARD_KEY,
            snapshot_metrics,
            "trigger checkpoint did not contain details failure guard state",
        )

        snapshot_guard = snapshot_metrics[GUARD_KEY]

        self.assertIn(
            "e",
            snapshot_guard.get("completed_resume_ids", []),
            "trigger checkpoint did not contain the completed resume ID",
        )

        self.assertEqual(snapshot_metrics["load_failed"], 1)

    def test_checkpoint_callback_failure_stops_before_durable_completion_claim(
        self,
    ):
        callback_error = RuntimeError("checkpoint unavailable")
        calls = []

        def fail_checkpoint(*args):
            calls.append(copy.deepcopy(args))
            raise callback_error

        with self.assertRaises(RuntimeError) as caught:
            self.invoke(
                items=[{"id": "a"}, {"id": "b"}],
                outcomes={"a": RuntimeError("details")},
                checkpoint_callback=fail_checkpoint,
            )

        self.assertIs(caught.exception, callback_error)
        self.assertEqual(self.details_calls, ["a"])
        self.assertEqual(len(calls), 1)

        callback_metrics = calls[0][1]

        self.assertIn(
            GUARD_KEY,
            callback_metrics,
            "checkpoint callback did not receive details failure guard state",
        )

        callback_guard = callback_metrics[GUARD_KEY]

        self.assertIsInstance(
            callback_guard,
            dict,
            "details failure guard state must be a dictionary",
        )

        self.assertIn(
            "a",
            callback_guard.get("completed_resume_ids", []),
            "checkpoint callback did not receive the completed resume ID",
        )


class FailureGuardRehydrationTests(FailureGuardHarness, unittest.TestCase):
    # Handler-level state rehydration only.
    # Production V3 routing remains disabled and is not asserted here.
    def test_rehydrated_handler_state_restores_streak_and_next_failure_triggers(self):
        state = _state(
            recent_outcomes=["generic_error"] * 4,
            consecutive_failures=4,
            attempts_total=4,
            failures_total=4,
            generic_failures=4,
            completed_resume_ids=["a", "b", "c", "d"],
        )
        metrics = {GUARD_KEY: state}
        expect_guard_error(
            self,
            lambda: self.invoke(
                items=[{"id": "e"}],
                outcomes={"e": RuntimeError("x")},
                metrics=metrics,
            ),
        )
        self.assertEqual(_guard(self.checkpoints[-1][1])["consecutive_failures"], 5)

    # Handler-level state rehydration only.
    # Production V3 routing remains disabled and is not asserted here.
    def test_handler_level_rehydration_skips_completed_failed_id(self):
        metrics = {
            "scanned_new_candidates": 1,
            GUARD_KEY: _state(
                recent_outcomes=["generic_error"],
                consecutive_failures=1,
                attempts_total=1,
                failures_total=1,
                generic_failures=1,
                completed_resume_ids=["failed"],
            ),
        }
        self.invoke(
            items=[{"id": "failed"}, {"id": "new"}],
            metrics=metrics,
            target_pool=10,
        )
        state = _guard(metrics)
        self.assertEqual(self.details_calls, ["new"])
        self.assertEqual(metrics["scanned_new_candidates"], 2)
        self.assertEqual(state["attempts_total"], 2)
        self.assertEqual(state["recent_outcomes"], ["generic_error", "success"])

    # Handler-level state rehydration only.
    # Production V3 routing remains disabled and is not asserted here.
    def test_rehydrated_handler_state_keeps_existing_candidate_and_bounded_window(self):
        existing = [_candidate("accepted")]
        metrics = {
            GUARD_KEY: _state(
                recent_outcomes=["success"] * 20,
                attempts_total=20,
                completed_resume_ids=[f"old{i}" for i in range(19)] + ["accepted"],
            )
        }
        result = self.invoke(
            items=[{"id": "accepted"}, {"id": "new"}],
            existing_pool=existing,
            metrics=metrics,
            window=20,
            target_pool=10,
        )
        self.assertEqual(_pool_ids(result["pool"]), ["accepted", "new"])
        self.assertEqual(self.details_calls, ["new"])
        self.assertEqual(len(_guard(metrics)["recent_outcomes"]), 20)


class FailureGuardPrecedenceTests(FailureGuardHarness, unittest.TestCase):
    def test_guard_beats_scan_limit_on_same_failure(self):
        state = _state(
            recent_outcomes=["generic_error"] * 4,
            consecutive_failures=4,
            attempts_total=4,
            failures_total=4,
            generic_failures=4,
            completed_resume_ids=["a", "b", "c", "d"],
        )
        metrics = {"scanned_new_candidates": 4, GUARD_KEY: state}
        exc = expect_guard_error(
            self,
            lambda: self.invoke(
                items=[{"id": "e"}],
                outcomes={"e": RuntimeError("x")},
                metrics=metrics,
                scan_limit=5,
            ),
        )
        self.assertIsInstance(exc, UserFacingError)
        snapshot_metrics = self.checkpoints[-1][1]
        self.assertEqual(snapshot_metrics[GUARD_KEY]["trigger"]["kind"], "consecutive")
        self.assertNotEqual(snapshot_metrics.get("stop_reason"), "scan_limit_reached")

    def test_target_pool_beats_rate_guard_after_success(self):
        state = _state(
            recent_outcomes=["generic_error", "success"] * 4 + ["generic_error"],
            consecutive_failures=1,
            attempts_total=9,
            failures_total=5,
            generic_failures=5,
            completed_resume_ids=[f"old{i}" for i in range(9)],
        )
        metrics = {GUARD_KEY: state}
        result = self.invoke(
            items=[{"id": "winner"}],
            metrics=metrics,
            target_pool=1,
            consecutive_limit=99,
        )
        self.assertEqual(result["stop_reason"], "target_pool_reached")
        self.assertIsNone(_guard(metrics)["trigger"])
        self.assertIn("winner", _pool_ids(result["pool"]))

    def test_guard_does_not_return_terminal_result_or_terminal_reason(self):
        state = _state(
            recent_outcomes=["generic_error"] * 4,
            consecutive_failures=4,
            attempts_total=4,
            failures_total=4,
            generic_failures=4,
            completed_resume_ids=["a", "b", "c", "d"],
        )
        metrics = {GUARD_KEY: state}
        result_box = {}
        expect_guard_error(
            self,
            lambda: result_box.setdefault(
                "result",
                self.invoke(
                    items=[{"id": "e"}],
                    outcomes={"e": RuntimeError("x")},
                    metrics=metrics,
                ),
            ),
        )
        self.assertNotIn("result", result_box)
        snapshot_metrics = self.checkpoints[-1][1]
        self.assertNotIn("stop_reason", snapshot_metrics)
        for reason in ("search_exhausted", "page_limit_reached", "scan_limit_reached"):
            self.assertNotEqual(snapshot_metrics.get("stop_reason"), reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)

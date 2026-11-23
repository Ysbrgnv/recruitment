"""RED-2: isolated Candidate Pool V3 traversal contracts.

Offline only. All HH, history, progress and checkpoint boundaries are mocked.
The production dispatcher remains fail-closed and the V3 handler is invoked directly.
"""
from __future__ import annotations

import copy
import unittest
from unittest.mock import Mock, call, patch

from app.core import settings
from app.core.errors import HHUnavailableError, UserFacingError
from app.persistence.tracking_service import normalize
from app.services import recruitment_service as service


V3_METRIC_KEYS = (
    "search_items_seen",
    "unique_resume_ids_seen",
    "scanned_new_candidates",
    "duplicates_skipped",
    "blocked_by_ai_history",
    "invalid_resume_id",
    "load_failed",
    "pages_fetched",
)


def _item(resume_id):
    return {} if resume_id is None else {"id": resume_id}


def _resume(resume_id, **extra):
    value = {
        "id": normalize(resume_id),
        "title": f"Title {normalize(resume_id)}",
        "alternate_url": f"https://example.invalid/{normalize(resume_id)}",
    }
    value.update(extra)
    return value


def _candidate(resume_id):
    rid = normalize(resume_id)
    return (rid, _resume(rid), [])


def _plan():
    return {
        "strategies": [
            {"id": "balanced", "query": '"balanced"'},
            {"id": "title_recall", "query": '"titles"'},
            {"id": "functional_recall", "query": '"functions"'},
            {"id": "precision", "query": '"precision"'},
        ]
    }


def _pool(result):
    return result.get("pool") if isinstance(result, dict) else None


def _metrics(result, fallback):
    if isinstance(result, dict) and isinstance(result.get("metrics"), dict):
        return result["metrics"]
    return fallback


def _ids(result):
    pool = _pool(result)
    if not isinstance(pool, list):
        return None
    return [candidate[0] for candidate in pool]


def _assert_terminal(test, result, reason):
    test.assertIsInstance(result, dict)
    test.assertIs(result.get("pool_complete"), True)
    test.assertEqual(result.get("stop_reason"), reason)
    test.assertIsInstance(result.get("pool"), list)
    test.assertIsInstance(result.get("metrics"), dict)
    test.assertIsInstance(result.get("pool_exclusions"), dict)


def _comparable(value):
    if isinstance(value, BaseException):
        return ("exception", type(value), copy.deepcopy(value.args))
    if isinstance(value, dict):
        return {
            copy.deepcopy(key): _comparable(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_comparable(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_comparable(item) for item in value)
    return copy.deepcopy(value)


class Harness:
    def invoke(
        self,
        *,
        pages=None,
        details=None,
        target_pool=20,
        requested_count=5,
        metrics=None,
        existing_pool=None,
        pool_exclusions=None,
        blocked=None,
        scan_limit=100,
        max_pages=10,
        checkpoint=None,
        progress=None,
        query='"approved query"',
        plan=None,
    ):
        pages = copy.deepcopy(pages or {0: {"items": [], "pages": 1}})
        details = details or {}
        blocked = {normalize(value) for value in (blocked or set())}
        metrics = metrics if metrics is not None else {}
        existing_pool = existing_pool if existing_pool is not None else []
        pool_exclusions = pool_exclusions if pool_exclusions is not None else {}
        checkpoints = []
        progress_calls = []

        inputs_before = _comparable(
            (pages, existing_pool, pool_exclusions, plan, details)
        )

        def search(params):
            return copy.deepcopy(
                pages.get(params["page"], {"items": [], "pages": params["page"] + 1})
            )

        def load(resume_id):
            value = details.get(resume_id, _resume(resume_id))
            if isinstance(value, BaseException):
                raise value
            return copy.deepcopy(value)

        def save(candidates, current_metrics, exclusions):
            checkpoints.append(
                (
                    copy.deepcopy(candidates),
                    copy.deepcopy(current_metrics),
                    copy.deepcopy(exclusions),
                )
            )
            if checkpoint:
                checkpoint(candidates, current_metrics, exclusions)

        def report(*args):
            progress_calls.append(copy.deepcopy(args))
            if progress:
                progress(*args)

        with patch.object(
            settings, "CANDIDATE_POOL_V3_SCAN_LIMIT", scan_limit, create=True
        ), patch.object(
            settings, "CANDIDATE_POOL_V3_MAX_PAGES", max_pages, create=True
        ), patch.object(
            service, "search_resumes", side_effect=search
        ) as search_mock, patch.object(
            service, "fetch_resume_details", side_effect=load
        ) as fetch_mock, patch.object(
            service,
            "was_sent_to_ai",
            side_effect=lambda resume_id, vacancy_id: normalize(resume_id) in blocked,
        ) as history_mock, patch.object(service, "reset_history_cache"):
            result = service._search_candidate_pool_v3(
                "vacancy-1",
                {"id": "vacancy-1", "name": "Synthetic vacancy"},
                {"area_id": "1", "minimum_experience_months": 36},
                query,
                target_pool,
                requested_count,
                metrics,
                report,
                profile={"leadership_intent": {"level": "individual_contributor"}},
                pool_exclusions=pool_exclusions,
                existing_pool=existing_pool,
                checkpoint=save,
                plan=plan if plan is not None else _plan(),
            )

        self.assertEqual(
            _comparable(
                (pages, existing_pool, pool_exclusions, plan, details)
            ),
            inputs_before,
            "V3 traversal mutated an input fixture",
        )
        return {
            "result": result,
            "metrics_input": metrics,
            "search": search_mock,
            "fetch": fetch_mock,
            "history": history_mock,
            "checkpoints": checkpoints,
            "progress": progress_calls,
            "pool_exclusions_input": pool_exclusions,
        }


class CandidatePoolV3PoolSizeTests(Harness, unittest.TestCase):
    def test_target_pool_formula_matches_current_defaults(self):
        cases = {1: 5, 2: 8, 5: 17, 10: 32}
        for requested, expected in cases.items():
            with self.subTest(requested=requested):
                actual = min(
                    service.MAX_CANDIDATE_POOL,
                    requested * service.POOL_MULTIPLIER + service.POOL_EXTRA,
                )
                self.assertEqual(actual, expected)

    def test_target_pool_formula_applies_max_candidate_pool_cap(self):
        for requested in (20, 100):
            with self.subTest(requested=requested):
                actual = min(
                    service.MAX_CANDIDATE_POOL,
                    requested * service.POOL_MULTIPLIER + service.POOL_EXTRA,
                )
                self.assertEqual(actual, 50)


class CandidatePoolV3ApprovedQueryTests(Harness, unittest.TestCase):
    def test_every_page_uses_same_approved_query_and_runtime_per_page(self):
        pages = {
            0: {"items": [], "pages": 3},
            1: {"items": [], "pages": 3},
            2: {"items": [], "pages": 3},
        }
        outcome = self.invoke(pages=pages)
        params = [entry.args[0] for entry in outcome["search"].call_args_list]
        self.assertEqual([value["page"] for value in params], [0, 1, 2])
        self.assertTrue(params)
        self.assertTrue(all(value["text"] == '"approved query"' for value in params))
        self.assertTrue(all(value["per_page"] == service.SEARCH_PER_PAGE for value in params))

    def test_vacancy_search_plan_strategies_are_not_executed_or_mutated(self):
        plan = _plan()
        before = copy.deepcopy(plan)
        outcome = self.invoke(
            pages={0: {"items": [], "pages": 1}},
            plan=plan,
            query='"approved only"',
        )
        queries = [entry.args[0]["text"] for entry in outcome["search"].call_args_list]
        self.assertEqual(queries, ['"approved only"'])
        self.assertEqual(plan, before)
        for forbidden in ('"balanced"', '"titles"', '"functions"', '"precision"'):
            self.assertNotIn(forbidden, queries)


class CandidatePoolV3PagingAndStopTests(Harness, unittest.TestCase):
    def test_pages_are_requested_sequentially_and_declared_exhaustion_stops(self):
        pages = {
            0: {"items": [], "pages": 3},
            1: {"items": [], "pages": 3},
            2: {"items": [], "pages": 3},
        }
        outcome = self.invoke(pages=pages, max_pages=10)
        self.assertEqual(
            [entry.args[0]["page"] for entry in outcome["search"].call_args_list],
            [0, 1, 2],
        )
        _assert_terminal(self, outcome["result"], "search_exhausted")

    def test_page_limit_stops_without_requesting_page_after_limit(self):
        pages = {
            0: {"items": [], "pages": 20},
            1: {"items": [], "pages": 20},
            2: {"items": [], "pages": 20},
        }
        outcome = self.invoke(pages=pages, max_pages=2)
        self.assertEqual(
            [entry.args[0]["page"] for entry in outcome["search"].call_args_list],
            [0, 1],
        )
        _assert_terminal(self, outcome["result"], "page_limit_reached")

    def test_target_pool_stops_mid_page_before_accounting_next_item(self):
        pages = {0: {"items": [_item("a"), _item("b"), _item("c")], "pages": 2}}
        outcome = self.invoke(pages=pages, target_pool=2)
        metrics = _metrics(outcome["result"], outcome["metrics_input"])
        self.assertEqual(_ids(outcome["result"]), ["a", "b"])
        self.assertEqual(outcome["fetch"].call_args_list, [call("a"), call("b")])
        self.assertEqual(metrics.get("search_items_seen"), 2)
        self.assertEqual(metrics.get("unique_resume_ids_seen"), 2)
        self.assertEqual(metrics.get("scanned_new_candidates"), 2)
        _assert_terminal(self, outcome["result"], "target_pool_reached")

    def test_target_pool_reason_wins_over_scan_limit_on_same_candidate(self):
        pages = {0: {"items": [_item("a"), _item("b")], "pages": 2}}
        outcome = self.invoke(pages=pages, target_pool=1, scan_limit=1)
        _assert_terminal(self, outcome["result"], "target_pool_reached")
        self.assertEqual(outcome["fetch"].call_args_list, [call("a")])
        metrics = _metrics(outcome["result"], outcome["metrics_input"])
        self.assertEqual(metrics.get("search_items_seen"), 1)

    def test_target_pool_reason_wins_over_search_exhaustion_on_same_candidate(self):
        outcome = self.invoke(
            pages={0: {"items": [_item("a")], "pages": 1}},
            target_pool=1,
        )
        _assert_terminal(self, outcome["result"], "target_pool_reached")
        self.assertEqual(_ids(outcome["result"]), ["a"])

    def test_search_exhaustion_wins_when_page_limit_and_metadata_end_together(self):
        outcome = self.invoke(
            pages={0: {"items": [], "pages": 1}},
            max_pages=1,
        )
        self.assertEqual(outcome["search"].call_count, 1)
        _assert_terminal(self, outcome["result"], "search_exhausted")


class CandidatePoolV3OrderingTests(Harness, unittest.TestCase):
    def test_headhunter_page_and_item_order_is_preserved_without_sorting(self):
        pages = {
            0: {"items": [_item("z-last-title"), _item("a-first-title")], "pages": 2},
            1: {"items": [_item("m-middle-title")], "pages": 2},
        }
        details = {
            "z-last-title": _resume("z-last-title", score=1, updated_at="2020-01-01"),
            "a-first-title": _resume("a-first-title", score=999, updated_at="2030-01-01"),
            "m-middle-title": _resume("m-middle-title", score=500, updated_at="2025-01-01"),
        }
        outcome = self.invoke(pages=pages, details=details)
        expected = ["z-last-title", "a-first-title", "m-middle-title"]
        self.assertEqual(_ids(outcome["result"]), expected)
        self.assertEqual([entry.args[0] for entry in outcome["fetch"].call_args_list], expected)

    def test_existing_pool_keeps_order_and_new_candidates_are_appended(self):
        existing = [_candidate("existing-2"), _candidate("existing-1")]
        outcome = self.invoke(
            pages={0: {"items": [_item("new-2"), _item("new-1")], "pages": 1}},
            existing_pool=existing,
        )
        self.assertEqual(
            _ids(outcome["result"]),
            ["existing-2", "existing-1", "new-2", "new-1"],
        )
        self.assertEqual(
            [entry.args[0] for entry in outcome["fetch"].call_args_list],
            ["new-2", "new-1"],
        )


class CandidatePoolV3DeduplicationTests(Harness, unittest.TestCase):
    def test_normalized_duplicates_are_added_and_fetched_once(self):
        pages = {
            0: {"items": [_item(" 123.0 "), _item("123"), _item(123.0)], "pages": 1}
        }
        outcome = self.invoke(pages=pages)
        self.assertEqual(_ids(outcome["result"]), ["123"])
        self.assertEqual(outcome["fetch"].call_args_list, [call("123")])
        metrics = _metrics(outcome["result"], outcome["metrics_input"])
        self.assertEqual(metrics.get("duplicates_skipped"), 2)
        self.assertEqual(metrics.get("scanned_new_candidates"), 1)

    def test_duplicate_on_later_page_preserves_first_occurrence(self):
        pages = {
            0: {"items": [_item("first"), _item("same")], "pages": 2},
            1: {"items": [_item(" same "), _item("last")], "pages": 2},
        }
        outcome = self.invoke(pages=pages)
        self.assertEqual(_ids(outcome["result"]), ["first", "same", "last"])
        self.assertEqual(
            [entry.args[0] for entry in outcome["fetch"].call_args_list],
            ["first", "same", "last"],
        )

    def test_existing_pool_candidate_is_not_refetched_or_scanned(self):
        existing = [_candidate(" 123.0 ")]
        outcome = self.invoke(
            pages={0: {"items": [_item("123"), _item("new")], "pages": 1}},
            existing_pool=existing,
        )
        self.assertEqual(_ids(outcome["result"]), ["123", "new"])
        self.assertEqual(outcome["fetch"].call_args_list, [call("new")])
        metrics = _metrics(outcome["result"], outcome["metrics_input"])
        self.assertEqual(metrics.get("scanned_new_candidates"), 1)

    def test_existing_pool_exclusion_is_not_refetched_or_scanned(self):
        exclusions = {" 7.0 ": {"reason": "existing"}}
        before = copy.deepcopy(exclusions)
        outcome = self.invoke(
            pages={0: {"items": [_item("7"), _item("8")], "pages": 1}},
            pool_exclusions=exclusions,
        )
        self.assertEqual(_ids(outcome["result"]), ["8"])
        self.assertEqual(outcome["fetch"].call_args_list, [call("8")])
        metrics = _metrics(outcome["result"], outcome["metrics_input"])
        self.assertEqual(metrics.get("scanned_new_candidates"), 1)
        self.assertEqual(exclusions, before)
        self.assertEqual(outcome["result"].get("pool_exclusions"), before)


class CandidatePoolV3ScanHistoryAndFailureTests(Harness, unittest.TestCase):
    def test_missing_id_duplicate_and_ai_history_do_not_consume_scan_limit(self):
        pages = {
            0: {
                "items": [_item(None), _item("blocked"), _item("blocked"), _item("good")],
                "pages": 1,
            }
        }
        outcome = self.invoke(pages=pages, blocked={"blocked"}, scan_limit=1)
        metrics = _metrics(outcome["result"], outcome["metrics_input"])
        self.assertEqual(_ids(outcome["result"]), ["good"])
        self.assertEqual(metrics.get("search_items_seen"), 4)
        self.assertEqual(metrics.get("unique_resume_ids_seen"), 2)
        self.assertEqual(metrics.get("blocked_by_ai_history"), 1)
        self.assertEqual(metrics.get("duplicates_skipped"), 1)
        self.assertEqual(metrics.get("invalid_resume_id"), 1)
        self.assertEqual(metrics.get("scanned_new_candidates"), 1)
        self.assertEqual(outcome["fetch"].call_args_list, [call("good")])

    def test_scan_limit_is_checked_before_accounting_item_after_limit(self):
        pages = {0: {"items": [_item("a"), _item("b"), _item("c")], "pages": 2}}
        outcome = self.invoke(pages=pages, scan_limit=2, target_pool=10)
        metrics = _metrics(outcome["result"], outcome["metrics_input"])
        self.assertEqual(_ids(outcome["result"]), ["a", "b"])
        self.assertEqual(outcome["fetch"].call_args_list, [call("a"), call("b")])
        self.assertEqual(metrics.get("search_items_seen"), 2)
        self.assertEqual(metrics.get("unique_resume_ids_seen"), 2)
        self.assertEqual(metrics.get("scanned_new_candidates"), 2)
        _assert_terminal(self, outcome["result"], "scan_limit_reached")

    def test_generic_details_failure_is_counted_and_traversal_continues(self):
        outcome = self.invoke(
            pages={0: {"items": [_item("broken"), _item("good")], "pages": 1}},
            details={"broken": RuntimeError("synthetic failure"), "good": _resume("good")},
        )
        metrics = _metrics(outcome["result"], outcome["metrics_input"])
        self.assertEqual(_ids(outcome["result"]), ["good"])
        self.assertEqual(metrics.get("load_failed"), 1)
        self.assertEqual(metrics.get("scanned_new_candidates"), 2)
        self.assertEqual(outcome["fetch"].call_args_list, [call("broken"), call("good")])
        failure_snapshots = [snap for snap in outcome["checkpoints"] if snap[1].get("load_failed") == 1]
        self.assertTrue(failure_snapshots)
        self.assertNotIn("broken", [candidate[0] for candidate in failure_snapshots[0][0]])

    def test_hh_unavailable_during_details_is_propagated_after_saved_checkpoint(self):
        checkpoints = []
        with self.assertRaises(HHUnavailableError):
            self.invoke(
                pages={0: {"items": [_item("saved"), _item("unavailable")], "pages": 2}},
                details={
                    "saved": _resume("saved"),
                    "unavailable": HHUnavailableError("synthetic HH outage"),
                },
                checkpoint=lambda pool, metrics, exclusions: checkpoints.append(
                    (copy.deepcopy(pool), copy.deepcopy(metrics), copy.deepcopy(exclusions))
                ),
            )
        self.assertTrue(checkpoints)
        self.assertEqual([candidate[0] for candidate in checkpoints[-1][0]], ["saved"])
        self.assertEqual(checkpoints[-1][1].get("scanned_new_candidates"), 1)


class CandidatePoolV3ResultAndMetricsTests(Harness, unittest.TestCase):
    def test_empty_metrics_are_initialized_by_handler(self):
        outcome = self.invoke(metrics={}, pages={0: {"items": [], "pages": 1}})
        metrics = _metrics(outcome["result"], outcome["metrics_input"])
        self.assertIsInstance(metrics, dict)
        for key in V3_METRIC_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, metrics)
                self.assertEqual(metrics[key], 0 if key != "pages_fetched" else 1)

    def test_legacy_metrics_are_preserved_while_v3_metrics_are_initialized(self):
        legacy = {"scanned": 4, "search_items": 7, "already_sent": 2, "load_failed": 1}
        outcome = self.invoke(
            metrics=legacy,
            pages={0: {"items": [_item("new")], "pages": 1}},
        )
        metrics = _metrics(outcome["result"], outcome["metrics_input"])
        self.assertEqual(metrics.get("scanned"), 4)
        self.assertEqual(metrics.get("search_items"), 7)
        self.assertEqual(metrics.get("already_sent"), 2)
        self.assertEqual(metrics.get("load_failed"), 1)
        for key in V3_METRIC_KEYS:
            self.assertIn(key, metrics)

    def test_terminal_result_contains_required_fields_and_stop_reason_metric(self):
        outcome = self.invoke(
            pages={0: {"items": [_item("a")], "pages": 2}},
            target_pool=1,
        )
        result = outcome["result"]
        _assert_terminal(self, result, "target_pool_reached")
        self.assertEqual(result["metrics"].get("stop_reason"), "target_pool_reached")
        self.assertEqual(result["pool_exclusions"], {})


class CandidatePoolV3ProgressAndCheckpointTests(Harness, unittest.TestCase):
    def test_progress_is_reported_after_each_fetched_page(self):
        pages = {
            0: {"items": [], "pages": 3},
            1: {"items": [], "pages": 3},
            2: {"items": [], "pages": 3},
        }
        outcome = self.invoke(pages=pages, target_pool=17, requested_count=5)
        calls = outcome["progress"]
        self.assertEqual(len(calls), 3)
        payloads = [entry[2] for entry in calls]
        self.assertEqual([payload["pages_fetched"] for payload in payloads], [1, 2, 3])
        self.assertTrue(all(payload["target_pool"] == 17 for payload in payloads))
        self.assertTrue(all(payload["requested_count"] == 5 for payload in payloads))
        self.assertEqual(
            [entry.args[0]["page"] for entry in outcome["search"].call_args_list],
            [0, 1, 2],
        )

    def test_checkpoint_batches_skips_and_contains_final_counters(self):
        pages = {
            0: {
                "items": [_item(None), _item("dup"), _item("dup"), _item("blocked")],
                "pages": 1,
            }
        }
        outcome = self.invoke(pages=pages, blocked={"blocked"})
        self.assertTrue(outcome["checkpoints"])
        final_pool, final_metrics, final_exclusions = outcome["checkpoints"][-1]
        self.assertEqual([candidate[0] for candidate in final_pool], ["dup"])
        self.assertEqual(final_metrics.get("invalid_resume_id"), 1)
        self.assertEqual(final_metrics.get("duplicates_skipped"), 1)
        self.assertEqual(final_metrics.get("blocked_by_ai_history"), 1)
        self.assertEqual(final_exclusions, {})
        self.assertLessEqual(len(outcome["checkpoints"]), 5)


class CandidatePoolV3IsolationCompatibilityTests(Harness, unittest.TestCase):
    def test_production_dispatcher_remains_fail_closed_for_v3(self):
        metrics = {}
        with patch.object(settings, "CANDIDATE_POOL_V3_ENABLED", True), patch.object(
            service, "_search_candidate_pool_v3"
        ) as v3, patch.object(service, "_search_candidate_pool_v2") as v2, patch.object(
            service, "_search_candidate_pool_v1"
        ) as v1:
            with self.assertRaises(UserFacingError) as caught:
                service._search_candidate_pool(
                    "v", {}, {}, '"approved"', 5, 1, metrics, lambda *args: None
                )
        self.assertNotIsInstance(caught.exception, HHUnavailableError)
        v3.assert_not_called()
        v2.assert_not_called()
        v1.assert_not_called()

    def test_v1_and_v2_selection_remain_unchanged_when_v3_disabled(self):
        with patch.object(settings, "CANDIDATE_POOL_V3_ENABLED", False), patch.object(
            settings, "CANDIDATE_POOL_V2_ENABLED", True
        ), patch.object(service, "_search_candidate_pool_v2", return_value=["v2"]) as v2, patch.object(
            service, "_search_candidate_pool_v1", return_value=["v1"]
        ) as v1:
            result = service._search_candidate_pool(
                "v", {}, {}, '"approved"', 5, 1, {}, lambda *args: None, plan=_plan()
            )
        self.assertEqual(result, ["v2"])
        v2.assert_called_once()
        v1.assert_not_called()

        with patch.object(settings, "CANDIDATE_POOL_V3_ENABLED", False), patch.object(
            settings, "CANDIDATE_POOL_V2_ENABLED", False
        ), patch.object(service, "_search_candidate_pool_v2", return_value=["v2"]) as v2, patch.object(
            service, "_search_candidate_pool_v1", return_value=["v1"]
        ) as v1:
            result = service._search_candidate_pool(
                "v", {}, {}, '"approved"', 5, 1, {}, lambda *args: None, plan=_plan()
            )
        self.assertEqual(result, ["v1"])
        v1.assert_called_once()
        v2.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)

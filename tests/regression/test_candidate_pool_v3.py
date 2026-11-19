"""RED-1A: Candidate Pool V3 configuration and version-routing contracts."""
from __future__ import annotations

import copy
import importlib
import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from app.core import settings
from app.services import recruitment_service as service


V3_DEFAULTS = {
    "CANDIDATE_POOL_V3_ENABLED": False,
    "CANDIDATE_POOL_V3_EXPERIENCE_FILTER_ENABLED": False,
    "CANDIDATE_POOL_V3_MAX_PAGES": 10,
    "CANDIDATE_POOL_V3_SCAN_LIMIT": 100,
    "CANDIDATE_POOL_V3_EXPERIENCE_TOLERANCE_MONTHS": 2,
    "CANDIDATE_POOL_V3_PAGE_SNAPSHOT_TTL_HOURS": 24,
    "CANDIDATE_POOL_V3_RECOVERY_SUCCESS_THRESHOLD": 10,
    "CANDIDATE_POOL_V3_FAILURE_MIN_SAMPLE": 10,
    "CANDIDATE_POOL_V3_FAILURE_WINDOW": 20,
    "CANDIDATE_POOL_V3_FAILURE_RATE_THRESHOLD": 0.50,
    "CANDIDATE_POOL_V3_CONSECUTIVE_FAILURE_LIMIT": 5,
    "HH_MAX_RETRY_AFTER_SECONDS": 30,
}

V3_CHECKPOINT_MARKERS = {
    "page_snapshot": {
        "snapshot_schema_version": 1,
        "page_number": 0,
        "fetched_at": "2026-11-19T00:00:00Z",
        "items": [],
        "next_item_index": 0,
    },
    "scanned_new_candidates": 0,
}


def _require_callable(module, name):
    value = getattr(module, name, None)
    if not callable(value):
        raise AssertionError(f"Required Candidate Pool V3 API is missing: {name}")
    return value


def _selector():
    return _require_callable(service, "_select_candidate_pool_version")


def _v3_handler():
    return _require_callable(service, "_search_candidate_pool_v3")


def _version(result):
    if isinstance(result, int) and not isinstance(result, bool):
        return result
    if isinstance(result, dict):
        return result.get("pool_version")
    return None


def _manual_resolution(result):
    if not isinstance(result, dict):
        raise AssertionError("Version selector must return a structured result")
    return result


def _strategy(strategy_id, query):
    return {
        "id": strategy_id,
        "kind": "contract_strategy",
        "query": query,
        "executable_now": True,
        "blocked_by": None,
    }


def _valid_v2_plan():
    return {
        "schema_version": 3,
        "generation_basis": "deterministic_renderer_v3",
        "strategies": [
            _strategy("balanced", '"balanced"'),
            _strategy("title_recall", '"titles"'),
        ],
        "selected_strategy": "balanced",
    }


def _metrics():
    return {
        "search_items": 0,
        "scanned": 0,
        "already_sent": 0,
        "load_failed": 0,
        "excluded_city": 0,
        "excluded_experience": 0,
        "excluded_leadership": 0,
        "excluded_role_mismatch": 0,
        "excluded_professional_mismatch": 0,
        "excluded_leadership_mismatch": 0,
        "excluded_level_mismatch": 0,
        "level_unknown_kept": 0,
        "pool": 0,
    }


def _legacy_v1_checkpoint():
    return {
        "vacancy": {"id": "v1"},
        "pool": [("r1", {"id": "r1"}, ["legacy note"])],
        "pool_complete": False,
        "metrics": {"scanned": 1, "excluded_role_mismatch": 0},
        "pool_exclusions": {},
    }


def _legacy_v2_checkpoint():
    return {
        "vacancy": {"id": "v1"},
        "pool": [
            (
                "r1",
                {"id": "r1"},
                [
                    {
                        "pool_version": 2,
                        "matched_strategies": ["balanced"],
                        "strategy_matches": [
                            {"strategy_id": "balanced", "page": 0, "rank": 1}
                        ],
                        "signals": {},
                        "leadership": {},
                    }
                ],
            )
        ],
        "pool_complete": False,
        "metrics": {
            "scanned": 1,
            "pool_v2": {
                "strategy_order": [
                    "balanced",
                    "title_recall",
                    "functional_recall",
                    "precision",
                ],
                "completed_strategies": ["balanced"],
                "current_strategy": None,
                "next_page_by_strategy": {"balanced": 1},
                "matched_strategy_counts": {"balanced": 1},
            },
        },
        "pool_exclusions": {},
    }


@contextmanager
def _settings_defaults_loaded():
    names = tuple(V3_DEFAULTS)
    before = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ.pop(name, None)
        reloaded = importlib.reload(settings)
        yield reloaded
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        importlib.reload(settings)


class CandidatePoolV3ConfigurationTests(unittest.TestCase):
    def test_v3_settings_exist_and_have_source_defaults(self):
        with _settings_defaults_loaded() as reloaded:
            for name, expected in V3_DEFAULTS.items():
                with self.subTest(name=name):
                    self.assertTrue(
                        hasattr(reloaded, name),
                        f"Missing configuration: {name}",
                    )
                    self.assertEqual(getattr(reloaded, name), expected)

    def test_default_check_restores_environment(self):
        sentinel = {
            "CANDIDATE_POOL_V3_ENABLED": "true",
            "CANDIDATE_POOL_V3_EXPERIENCE_FILTER_ENABLED": "true",
            "CANDIDATE_POOL_V3_MAX_PAGES": "12",
            "CANDIDATE_POOL_V3_SCAN_LIMIT": "120",
            "CANDIDATE_POOL_V3_EXPERIENCE_TOLERANCE_MONTHS": "3",
            "CANDIDATE_POOL_V3_PAGE_SNAPSHOT_TTL_HOURS": "48",
            "CANDIDATE_POOL_V3_RECOVERY_SUCCESS_THRESHOLD": "11",
            "CANDIDATE_POOL_V3_FAILURE_MIN_SAMPLE": "12",
            "CANDIDATE_POOL_V3_FAILURE_WINDOW": "24",
            "CANDIDATE_POOL_V3_FAILURE_RATE_THRESHOLD": "0.25",
            "CANDIDATE_POOL_V3_CONSECUTIVE_FAILURE_LIMIT": "6",
            "HH_MAX_RETRY_AFTER_SECONDS": "25",
        }
        with patch.dict(os.environ, sentinel, clear=False):
            with _settings_defaults_loaded():
                for name in sentinel:
                    self.assertNotIn(name, os.environ)
            for name, expected in sentinel.items():
                self.assertEqual(os.environ.get(name), expected)

    def test_separate_v3_handler_exists(self):
        self.assertTrue(callable(_v3_handler()))


class CandidatePoolV3RoutingTests(unittest.TestCase):
    def test_new_operation_prefers_v3_when_enabled(self):
        with patch.object(settings, "CANDIDATE_POOL_V3_ENABLED", True, create=True), patch.object(
            settings, "CANDIDATE_POOL_V2_ENABLED", True
        ):
            self.assertEqual(_version(_selector()({})), 3)

    def test_explicit_pool_version_three_ignores_disabled_v3_flag(self):
        with patch.object(settings, "CANDIDATE_POOL_V3_ENABLED", False, create=True):
            self.assertEqual(_version(_selector()({"pool_version": 3})), 3)

    def test_explicit_pool_version_two_wins_over_enabled_v3(self):
        checkpoint = {"pool_version": 2, **_legacy_v1_checkpoint()}
        with patch.object(settings, "CANDIDATE_POOL_V3_ENABLED", True, create=True):
            self.assertEqual(_version(_selector()(checkpoint)), 2)

    def test_explicit_pool_version_one_wins_over_all_flags(self):
        checkpoint = {"pool_version": 1, **_legacy_v2_checkpoint()}
        with patch.object(settings, "CANDIDATE_POOL_V3_ENABLED", True, create=True), patch.object(
            settings, "CANDIDATE_POOL_V2_ENABLED", True
        ):
            self.assertEqual(_version(_selector()(checkpoint)), 1)

    def test_unambiguous_legacy_v1_checkpoint_selects_v1(self):
        self.assertEqual(_version(_selector()(_legacy_v1_checkpoint())), 1)

    def test_unambiguous_legacy_v2_checkpoint_selects_v2(self):
        self.assertEqual(_version(_selector()(_legacy_v2_checkpoint())), 2)

    def test_conflicting_legacy_markers_require_manual_resolution(self):
        checkpoint = _legacy_v1_checkpoint()
        checkpoint["metrics"]["pool_v2"] = copy.deepcopy(
            _legacy_v2_checkpoint()["metrics"]["pool_v2"]
        )
        result = _manual_resolution(_selector()(checkpoint))
        self.assertEqual(result.get("resolution"), "manual_resolution")
        self.assertEqual(result.get("reason"), "unknown_legacy_pool_version")

    def test_unknown_nonempty_checkpoint_requires_manual_resolution(self):
        result = _manual_resolution(_selector()({"unexpected": "marker"}))
        self.assertEqual(result.get("resolution"), "manual_resolution")
        self.assertEqual(result.get("reason"), "unknown_legacy_pool_version")

    def test_empty_checkpoint_is_new_operation(self):
        with patch.object(settings, "CANDIDATE_POOL_V3_ENABLED", False, create=True), patch.object(
            settings, "CANDIDATE_POOL_V2_ENABLED", False
        ):
            self.assertEqual(_version(_selector()({})), 1)

    def test_explicit_pool_version_three_wins_over_v2_markers(self):
        checkpoint = {
            **_legacy_v2_checkpoint(),
            "pool_version": 3,
        }
        for enabled in (False, True):
            with self.subTest(v3_enabled=enabled):
                with patch.object(
                    settings,
                    "CANDIDATE_POOL_V3_ENABLED",
                    enabled,
                    create=True,
                ):
                    self.assertEqual(
                        _version(_selector()(checkpoint)),
                        3,
                    )

    def test_versionless_v2_and_v3_markers_require_manual_resolution(self):
        checkpoint = {
            **_legacy_v2_checkpoint(),
            **copy.deepcopy(V3_CHECKPOINT_MARKERS),
        }
        checkpoint.pop("pool_version", None)
        self.assertIn("pool_v2", checkpoint["metrics"])
        for marker in V3_CHECKPOINT_MARKERS:
            self.assertIn(marker, checkpoint)
        self.assertNotIn("pool_version", checkpoint)
        result = _manual_resolution(_selector()(checkpoint))
        self.assertEqual(
            result.get("resolution"),
            "manual_resolution",
        )
        self.assertEqual(
            result.get("reason"),
            "unknown_legacy_pool_version",
        )


class CandidatePoolV3SingleQueryTests(unittest.TestCase):
    def test_v3_uses_only_approved_query_and_ignores_plan_strategies(self):
        approved_query = '"approved query"'
        plan = _valid_v2_plan()
        handler = _v3_handler()
        with patch.object(
            service,
            "search_resumes",
            return_value={"items": [], "pages": 1},
        ) as search, patch.object(service, "reset_history_cache"):
            handler(
                "vacancy-1",
                {"id": "vacancy-1", "name": "Solution Architect"},
                {"area_id": "1", "minimum_experience_months": 36},
                approved_query,
                8,
                2,
                _metrics(),
                lambda *args: None,
                profile={"leadership_intent": {"level": "individual_contributor"}},
                pool_exclusions={},
                existing_pool=[],
                checkpoint=None,
                plan=plan,
            )
        searched_queries = [item.args[0]["text"] for item in search.call_args_list]
        self.assertTrue(searched_queries)
        self.assertEqual(set(searched_queries), {approved_query})
        self.assertNotIn('"balanced"', searched_queries)
        self.assertNotIn('"titles"', searched_queries)


class CandidatePoolV3CompatibilityCharacterisationTests(unittest.TestCase):
    def test_v2_remains_selected_when_v3_disabled_and_v2_plan_valid(self):
        plan = _valid_v2_plan()
        with patch.object(settings, "CANDIDATE_POOL_V3_ENABLED", False, create=True), patch.object(
            settings, "CANDIDATE_POOL_V2_ENABLED", True
        ), patch.object(service, "_search_candidate_pool_v2", return_value=["v2"]) as v2, patch.object(
            service, "_search_candidate_pool_v1", return_value=["v1"]
        ) as v1, patch.object(service, "_search_candidate_pool_v3", return_value=["v3"], create=True) as v3:
            result = service._search_candidate_pool(
                "v", {}, {}, '"legacy"', 1, 1, _metrics(), lambda *args: None, plan=plan
            )
        self.assertEqual(result, ["v2"])
        v2.assert_called_once()
        v1.assert_not_called()
        v3.assert_not_called()

    def test_v1_remains_selected_when_v2_and_v3_disabled(self):
        with patch.object(settings, "CANDIDATE_POOL_V3_ENABLED", False, create=True), patch.object(
            settings, "CANDIDATE_POOL_V2_ENABLED", False
        ), patch.object(service, "_search_candidate_pool_v1", return_value=["v1"]) as v1, patch.object(
            service, "_search_candidate_pool_v2", return_value=["v2"]
        ) as v2, patch.object(service, "_search_candidate_pool_v3", return_value=["v3"], create=True) as v3:
            result = service._search_candidate_pool(
                "v", {}, {}, '"legacy"', 1, 1, _metrics(), lambda *args: None, plan=_valid_v2_plan()
            )
        self.assertEqual(result, ["v1"])
        v1.assert_called_once()
        v2.assert_not_called()
        v3.assert_not_called()

"""RED-1: Candidate Pool V3 versioned checkpoint-contract regression tests.

These tests define a future pure state-contract seam only. Fake durable values are
synthetic deep copies. RED-1 does not prove production durable persistence, crash
atomicity, exactly-once semantics, transaction boundaries, or real update_task
read-after-failure behavior.
"""
from __future__ import annotations

import copy
import importlib
import json
import unittest

MODULE_NAME = "app.services.candidate_pool_v3_state"
REQUIRED_SECTIONS = (
    "pool",
    "metrics",
    "pool_exclusions",
    "technical",
    "professional",
    "admission",
    "budgets",
    "traversal",
    "completion",
    "guard",
)
RESULT_KEYS = {
    "validation": {"status", "checkpoint", "reason"},
    "recovery": {"status", "state", "reason"},
    "reservation": {"status", "state", "reservation_id", "reason"},
    "commit": {"status", "state", "reason"},
    "resolution": {
        "status",
        "reservation_id",
        "processing_allowed",
        "reason",
    },
}

try:
    seam = importlib.import_module(MODULE_NAME)
except ModuleNotFoundError as exc:
    if exc.name != MODULE_NAME:
        raise
    seam = None


def synthetic_checkpoint() -> dict:
    return {
        "pool_version": 3,
        "checkpoint_schema_version": 1,
        "filter_policy_version": "synthetic-filter-policy-v1",
        "vacancy_profile_version": "synthetic-vacancy-profile-v1",
        "pool": [
            {
                "resume_id": "resume-pool-0001",
                "discovery_sequence": 7,
                "synthetic_payload": {"label": "fixture-only"},
            }
        ],
        "metrics": {"synthetic_counter": 1},
        "pool_exclusions": {},
        "technical": {
            "attempted_resume_ids": [],
            "detail_attempt_history": [],
            "details_failed_resume_ids": [],
            "terminal_technical_failure_ids": [],
        },
        "professional": {
            "completed_resume_ids": [],
            "decisions": [],
        },
        "admission": {
            "admitted_strong_ids": [],
            "admitted_uncertain_ids": [],
            "eligible_not_admitted_ids": [],
        },
        "budgets": {
            "limits": {
                "search_item_budget": 100,
                "unique_resume_budget": 80,
                "details_load_budget": 60,
                "page_budget": 10,
            },
            "consumed": {
                "search_items_consumed": 0,
                "unique_resumes_consumed": 0,
                "details_operations_consumed": 0,
                "page_operations_consumed": 0,
            },
            "reservations": {
                "search_item_reservations": [],
                "unique_resume_reservations": [],
                "details_operation_reservations": [],
                "page_operation_reservations": [],
            },
        },
        "traversal": {
            "current_page": 0,
            "next_item_index": 0,
            "discovery_sequence_next": 8,
            "page_snapshot": {
                "page_identity": "synthetic-page-0",
                "ordered_normalized_resume_ids": [],
            },
        },
        "completion": {
            "target_reached": False,
            "strong_target_reached": False,
            "collection_finished": False,
            "post_target_started": False,
            "post_target_remaining": 0,
            "stop_flags": {},
            "stop_reason": None,
        },
        "guard": {"synthetic_guard_state": "CLEAR"},
    }


def synthetic_legacy_v1_checkpoint() -> dict:
    return {
        "vacancy": {"id": "synthetic-vacancy"},
        "pool": [],
        "pool_complete": False,
        "metrics": {},
        "pool_exclusions": {},
    }


def synthetic_legacy_v2_checkpoint() -> dict:
    provenance_note = {
        "pool_version": 2,
        "matched_strategies": ["balanced"],
        "strategy_matches": [],
        "signals": {},
        "leadership": {},
    }
    checkpoint = synthetic_legacy_v1_checkpoint()
    checkpoint["metrics"]["pool_v2"] = {}
    checkpoint["pool"] = [
        [
            "resume-legacy-v2",
            {"id": "resume-legacy-v2"},
            [provenance_note],
        ]
    ]
    return checkpoint


def pool_ids(checkpoint: dict) -> list[str]:
    return [entry["resume_id"] for entry in checkpoint["pool"]]


def active_search_reservations(state: dict) -> list[dict]:
    return state["budgets"]["reservations"]["search_item_reservations"]


def assert_exact_result(test: unittest.TestCase, result: object, keys: set[str]) -> dict:
    test.assertIsInstance(result, dict)
    test.assertEqual(set(result), keys)
    return result


class CandidatePoolV3CheckpointContractAvailability(unittest.TestCase):
    def test_red1_state_contract_seam_exists(self):
        self.assertIsNotNone(
            seam,
            "RED-1 requires app.services.candidate_pool_v3_state",
        )


@unittest.skipIf(seam is None, "RED-1 state contract seam is absent")
class CandidatePoolV3CheckpointContractTests(unittest.TestCase):
    def validate(self, checkpoint: dict) -> dict:
        return assert_exact_result(
            self,
            seam.validate_checkpoint(checkpoint),
            RESULT_KEYS["validation"],
        )

    def recover(self, checkpoint: dict, *, derived_pool_ids=None) -> dict:
        return assert_exact_result(
            self,
            seam.recover_checkpoint(checkpoint, pool_ids=derived_pool_ids),
            RESULT_KEYS["recovery"],
        )

    def reserve(self, state: dict, page_identity="synthetic-page-0", item_index=0) -> dict:
        return assert_exact_result(
            self,
            seam.reserve_search_item(
                state,
                page_identity=page_identity,
                item_index=item_index,
            ),
            RESULT_KEYS["reservation"],
        )

    def committed_once(self):
        checkpoint = synthetic_checkpoint()
        state = self.recover(checkpoint, derived_pool_ids=pool_ids(checkpoint))["state"]
        reserved = self.reserve(state)
        self.assertEqual(reserved["status"], "RESERVED")
        result = assert_exact_result(
            self,
            seam.commit_item_outcome(
                reserved["state"],
                reservation_id=reserved["reservation_id"],
                technical_outcome="DETAILS_LOADED",
                resume_id="resume-synthetic-outcome",
                professional_decision="INCLUDE_WITH_NOTE",
                admission="NOT_ADMITTED",
            ),
            RESULT_KEYS["commit"],
        )
        return checkpoint, state, reserved, result

    def test_valid_versioned_checkpoint_is_accepted(self):
        checkpoint = synthetic_checkpoint()
        before = copy.deepcopy(checkpoint)
        result = self.validate(checkpoint)
        self.assertEqual(result["status"], "VALID")
        self.assertEqual(result["checkpoint"], checkpoint)
        self.assertIsNone(result["reason"])
        self.assertEqual(checkpoint, before)

    def test_unknown_schema_is_manual_resolution(self):
        checkpoint = synthetic_checkpoint()
        checkpoint["checkpoint_schema_version"] = 999
        result = self.validate(checkpoint)
        self.assertEqual(result["status"], "MANUAL_RESOLUTION")
        self.assertIsNone(result["checkpoint"])
        self.assertIsInstance(result["reason"], str)
        self.assertTrue(result["reason"])

    def test_versionless_v3_checkpoint_is_manual_resolution(self):
        checkpoint = synthetic_checkpoint()
        checkpoint.pop("checkpoint_schema_version")
        result = self.validate(checkpoint)
        self.assertEqual(result["status"], "MANUAL_RESOLUTION")
        self.assertIsNone(result["checkpoint"])
        self.assertTrue(result["reason"])

    def test_non_v3_pool_versions_require_manual_resolution(self):
        for pool_version in (1, 2):
            with self.subTest(pool_version=pool_version):
                checkpoint = synthetic_checkpoint()
                checkpoint["pool_version"] = pool_version
                result = self.validate(checkpoint)
                self.assertEqual(result["status"], "MANUAL_RESOLUTION")
                self.assertIsNone(result["checkpoint"])
                self.assertTrue(result["reason"])

    def test_missing_pool_version_requires_manual_resolution(self):
        checkpoint = synthetic_checkpoint()
        checkpoint.pop("pool_version")
        result = self.validate(checkpoint)
        self.assertEqual(result["status"], "MANUAL_RESOLUTION")
        self.assertIsNone(result["checkpoint"])
        self.assertTrue(result["reason"])

    def test_boolean_pool_version_is_not_valid_v3(self):
        checkpoint = synthetic_checkpoint()
        checkpoint["pool_version"] = True
        result = self.validate(checkpoint)
        self.assertNotEqual(result["status"], "VALID")
        self.assertIn(result["status"], {"INVALID", "MANUAL_RESOLUTION"})
        self.assertIsNone(result["checkpoint"])
        self.assertTrue(result["reason"])

    def test_legacy_v1_checkpoint_requires_manual_resolution(self):
        result = self.validate(synthetic_legacy_v1_checkpoint())
        self.assertEqual(result["status"], "MANUAL_RESOLUTION")
        self.assertIsNone(result["checkpoint"])
        self.assertTrue(result["reason"])

    def test_legacy_v2_checkpoint_requires_manual_resolution(self):
        result = self.validate(synthetic_legacy_v2_checkpoint())
        self.assertEqual(result["status"], "MANUAL_RESOLUTION")
        self.assertIsNone(result["checkpoint"])
        self.assertTrue(result["reason"])

    def test_conflicting_legacy_markers_require_manual_resolution(self):
        checkpoint = synthetic_legacy_v2_checkpoint()
        checkpoint["page_snapshot"] = {
            "snapshot_schema_version": 1,
            "page_number": 0,
            "items": [],
            "next_item_index": 0,
        }
        result = self.validate(checkpoint)
        self.assertEqual(result["status"], "MANUAL_RESOLUTION")
        self.assertIsNone(result["checkpoint"])
        self.assertTrue(result["reason"])

    def test_missing_required_section_is_invalid(self):
        for section in REQUIRED_SECTIONS:
            with self.subTest(section=section):
                checkpoint = synthetic_checkpoint()
                checkpoint.pop(section)
                result = self.validate(checkpoint)
                self.assertEqual(result["status"], "INVALID")
                self.assertIsNone(result["checkpoint"])
                self.assertTrue(result["reason"])

    def test_state_sections_are_independent_from_metrics(self):
        forbidden = {
            "technical",
            "professional",
            "admission",
            "budgets",
            "traversal",
            "completion",
        }
        checkpoint = synthetic_checkpoint()
        self.assertTrue(forbidden.isdisjoint(checkpoint["metrics"]))
        result = self.validate(checkpoint)
        self.assertEqual(result["status"], "VALID")
        for section in forbidden:
            self.assertIn(section, result["checkpoint"])
            self.assertIsNot(result["checkpoint"][section], result["checkpoint"]["metrics"])

    def test_execution_state_inside_metrics_is_invalid(self):
        execution_sections = (
            "technical",
            "professional",
            "admission",
            "budgets",
            "traversal",
            "completion",
        )
        for section in execution_sections:
            with self.subTest(section=section):
                checkpoint = synthetic_checkpoint()
                checkpoint["metrics"][section] = copy.deepcopy(checkpoint[section])
                result = self.validate(checkpoint)
                self.assertEqual(result["status"], "INVALID")
                self.assertIsNone(result["checkpoint"])
                self.assertTrue(result["reason"])

    def test_round_trip_preserves_checkpoint_without_aliasing(self):
        checkpoint = synthetic_checkpoint()
        before = copy.deepcopy(checkpoint)
        recovered = self.recover(checkpoint, derived_pool_ids=pool_ids(checkpoint))
        self.assertEqual(recovered["status"], "RECOVERED")
        state = recovered["state"]
        for section in REQUIRED_SECTIONS:
            self.assertEqual(state[section], checkpoint[section])
        self.assertEqual(state["pool"][0]["discovery_sequence"], 7)
        state["pool"][0]["synthetic_payload"]["label"] = "mutated"
        state["technical"]["attempted_resume_ids"].append("mutated")
        self.assertEqual(checkpoint, before)

    def test_search_item_reservation_does_not_advance_index(self):
        checkpoint = synthetic_checkpoint()
        state = self.recover(checkpoint, derived_pool_ids=pool_ids(checkpoint))["state"]
        before = copy.deepcopy(state)
        result = self.reserve(state)
        self.assertEqual(result["status"], "RESERVED")
        self.assertTrue(result["reservation_id"])
        self.assertIsNone(result["reason"])
        self.assertEqual(result["state"]["traversal"]["next_item_index"], 0)
        self.assertEqual(
            result["state"]["budgets"]["consumed"]["search_items_consumed"],
            0,
        )
        reservations = active_search_reservations(result["state"])
        self.assertEqual(len(reservations), 1)
        self.assertEqual(reservations[0]["page_identity"], "synthetic-page-0")
        self.assertEqual(reservations[0]["item_index"], 0)
        self.assertEqual(state, before)

    def test_same_inflight_item_does_not_create_second_budget_unit(self):
        checkpoint = synthetic_checkpoint()
        state = self.recover(checkpoint, derived_pool_ids=pool_ids(checkpoint))["state"]
        first = self.reserve(state)
        second = self.reserve(first["state"])
        self.assertEqual(second["status"], "RESERVED")
        self.assertEqual(second["reservation_id"], first["reservation_id"])
        self.assertEqual(len(active_search_reservations(second["state"])), 1)
        self.assertEqual(
            second["state"]["budgets"]["consumed"]["search_items_consumed"],
            0,
        )

    def test_commit_outcome_closes_reservation_and_advances_index(self):
        _, state, reserved, result = self.committed_once()
        self.assertEqual(result["status"], "COMMITTED")
        self.assertIsNone(result["reason"])
        committed = result["state"]
        self.assertEqual(active_search_reservations(committed), [])
        self.assertEqual(
            committed["budgets"]["consumed"]["search_items_consumed"],
            1,
        )
        self.assertEqual(committed["traversal"]["next_item_index"], 1)
        serialized = json.dumps(committed, sort_keys=True)
        self.assertIn("DETAILS_LOADED", serialized)
        self.assertEqual(state["traversal"]["next_item_index"], 0)
        self.assertEqual(len(active_search_reservations(reserved["state"])), 1)

    def test_reservation_cannot_be_committed_twice(self):
        _, _, reserved, first = self.committed_once()
        second = assert_exact_result(
            self,
            seam.commit_item_outcome(
                first["state"],
                reservation_id=reserved["reservation_id"],
                technical_outcome="DETAILS_LOADED",
            ),
            RESULT_KEYS["commit"],
        )
        self.assertEqual(second["status"], "INVALID_RESERVATION")
        self.assertTrue(second["reason"])
        self.assertEqual(second["state"], first["state"])

    def test_unknown_reservation_id_is_rejected(self):
        checkpoint = synthetic_checkpoint()
        state = self.recover(checkpoint, derived_pool_ids=pool_ids(checkpoint))["state"]
        before = copy.deepcopy(state)
        result = assert_exact_result(
            self,
            seam.commit_item_outcome(
                state,
                reservation_id="unknown-reservation-id",
                technical_outcome="DETAILS_LOADED",
            ),
            RESULT_KEYS["commit"],
        )
        self.assertEqual(result["status"], "INVALID_RESERVATION")
        self.assertEqual(result["state"], before)
        self.assertTrue(result["reason"])
        self.assertEqual(
            result["state"]["budgets"]["consumed"]["search_items_consumed"], 0
        )
        self.assertEqual(result["state"]["traversal"]["next_item_index"], 0)

    def test_existing_reservation_is_rejected_after_page_context_changes(self):
        checkpoint = synthetic_checkpoint()
        state = self.recover(checkpoint, derived_pool_ids=pool_ids(checkpoint))["state"]
        reserved = self.reserve(state)
        self.assertEqual(len(active_search_reservations(reserved["state"])), 1)
        reserved_before = copy.deepcopy(reserved["state"])
        changed = copy.deepcopy(reserved["state"])
        changed["traversal"]["page_snapshot"]["page_identity"] = "synthetic-page-1"
        result = assert_exact_result(
            self,
            seam.commit_item_outcome(
                changed,
                reservation_id=reserved["reservation_id"],
                technical_outcome="DETAILS_LOADED",
            ),
            RESULT_KEYS["commit"],
        )
        self.assertEqual(result["status"], "INVALID_RESERVATION")
        self.assertTrue(result["reason"])
        self.assertEqual(result["state"]["traversal"]["next_item_index"], 0)
        self.assertEqual(
            result["state"]["budgets"]["consumed"]["search_items_consumed"], 0
        )
        self.assertNotIn("DETAILS_LOADED", json.dumps(result["state"], sort_keys=True))
        self.assertEqual(reserved["state"], reserved_before)

    def test_existing_reservation_is_rejected_after_item_context_changes(self):
        checkpoint = synthetic_checkpoint()
        state = self.recover(checkpoint, derived_pool_ids=pool_ids(checkpoint))["state"]
        reserved = self.reserve(state, item_index=0)
        reserved_before = copy.deepcopy(reserved["state"])
        changed = copy.deepcopy(reserved["state"])
        changed["traversal"]["next_item_index"] = 1
        result = assert_exact_result(
            self,
            seam.commit_item_outcome(
                changed,
                reservation_id=reserved["reservation_id"],
                technical_outcome="DETAILS_LOADED",
            ),
            RESULT_KEYS["commit"],
        )
        self.assertEqual(result["status"], "INVALID_RESERVATION")
        self.assertTrue(result["reason"])
        self.assertEqual(
            result["state"]["budgets"]["consumed"]["search_items_consumed"], 0
        )
        self.assertEqual(result["state"]["traversal"]["next_item_index"], 1)
        self.assertEqual(len(active_search_reservations(result["state"])), 1)
        self.assertNotIn("DETAILS_LOADED", json.dumps(result["state"], sort_keys=True))
        self.assertEqual(reserved["state"], reserved_before)

    def assert_resolution(self, durable, reservation_id, read_status, expected_status, allowed):
        result = assert_exact_result(
            self,
            seam.resolve_reservation_after_failure(
                durable,
                reservation_id=reservation_id,
                read_status=read_status,
            ),
            RESULT_KEYS["resolution"],
        )
        self.assertEqual(result["status"], expected_status)
        self.assertEqual(result["reservation_id"], reservation_id)
        self.assertIs(result["processing_allowed"], allowed)
        return result

    def test_confirmed_reservation_allows_processing(self):
        checkpoint = synthetic_checkpoint()
        state = self.recover(checkpoint, derived_pool_ids=pool_ids(checkpoint))["state"]
        reserved = self.reserve(state)
        result = self.assert_resolution(
            reserved["state"],
            reserved["reservation_id"],
            "CONFIRMED",
            "CONFIRMED",
            True,
        )
        self.assertIsNone(result["reason"])

    def test_confirmed_read_without_matching_reservation_is_fail_closed(self):
        checkpoint = synthetic_checkpoint()
        state = self.recover(checkpoint, derived_pool_ids=pool_ids(checkpoint))["state"]
        result = self.assert_resolution(
            state,
            "unknown-reservation-id",
            "CONFIRMED",
            "NOT_PERSISTED",
            False,
        )
        self.assertTrue(result["reason"])

    def test_not_persisted_reservation_does_not_allow_processing(self):
        result = self.assert_resolution(
            None,
            "synthetic-reservation-1",
            "NOT_PERSISTED",
            "NOT_PERSISTED",
            False,
        )
        self.assertIsInstance(result["reason"], (str, type(None)))

    def test_unknown_persistence_state_is_fail_closed(self):
        result = self.assert_resolution(
            None,
            "synthetic-reservation-1",
            "UNKNOWN",
            "PERSISTENCE_UNKNOWN",
            False,
        )
        self.assertIsInstance(result["reason"], (str, type(None)))

    def test_pool_ids_are_derived_from_checkpoint_pool(self):
        checkpoint = synthetic_checkpoint()
        expected = pool_ids(checkpoint)
        result = self.recover(checkpoint, derived_pool_ids=None)
        self.assertEqual(result["status"], "RECOVERED")
        self.assertEqual(pool_ids(result["state"]), expected)

    def test_pool_ids_mismatch_requires_manual_resolution(self):
        checkpoint = synthetic_checkpoint()
        result = self.recover(checkpoint, derived_pool_ids=["contradictory-id"])
        self.assertEqual(result["status"], "MANUAL_RESOLUTION")
        self.assertIsNone(result["state"])
        self.assertTrue(result["reason"])

    def test_discovery_sequence_survives_round_trip(self):
        checkpoint = synthetic_checkpoint()
        checkpoint["pool"].append(
            {"resume_id": "resume-pool-0002", "discovery_sequence": 19}
        )
        result = self.recover(checkpoint, derived_pool_ids=pool_ids(checkpoint))
        self.assertEqual(result["status"], "RECOVERED")
        self.assertEqual(
            [entry["discovery_sequence"] for entry in result["state"]["pool"]],
            [7, 19],
        )

    def test_ambiguous_page_fetch_state_survives_round_trip(self):
        checkpoint = synthetic_checkpoint()
        checkpoint["traversal"]["page_snapshot"]["recovery"] = {
            "recovery_state": "AMBIGUOUS_PAGE_FETCH"
        }
        result = self.recover(checkpoint, derived_pool_ids=pool_ids(checkpoint))
        self.assertEqual(result["status"], "RECOVERED")
        self.assertEqual(
            result["state"]["traversal"]["page_snapshot"]["recovery"],
            {"recovery_state": "AMBIGUOUS_PAGE_FETCH"},
        )

    def test_synthetic_page_snapshot_of_100_ids_is_json_serializable(self):
        checkpoint = synthetic_checkpoint()
        identifiers = [f"resume-{index:04d}" for index in range(1, 101)]
        checkpoint["traversal"]["page_snapshot"][
            "ordered_normalized_resume_ids"
        ] = identifiers
        payload = json.dumps(checkpoint, ensure_ascii=False, sort_keys=True)
        size_bytes = len(payload.encode("utf-8"))
        self.assertGreater(size_bytes, 0, f"serialized_size_bytes={size_bytes}")
        self.assertEqual(len(identifiers), 100)
        self.assertEqual(identifiers[0], "resume-0001")
        self.assertEqual(identifiers[-1], "resume-0100")


if __name__ == "__main__":
    unittest.main(verbosity=2)

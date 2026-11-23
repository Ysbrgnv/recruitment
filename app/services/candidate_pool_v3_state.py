"""Pure-state Candidate Pool V3 checkpoint contract.

This module contains no storage, workflow, network, clock, random, or routing
integration. It only validates and transforms JSON-oriented state copies.
"""
from __future__ import annotations

import copy
import hashlib
import json


_REQUIRED_KEYS = {
    "pool_version",
    "checkpoint_schema_version",
    "filter_policy_version",
    "vacancy_profile_version",
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
}
_EXECUTION_SECTIONS = {
    "technical",
    "professional",
    "admission",
    "budgets",
    "traversal",
    "completion",
}
_SUPPORTED_SCHEMA_VERSION = 1


def _validation_result(status, checkpoint=None, reason=None):
    return {"status": status, "checkpoint": checkpoint, "reason": reason}


def _recovery_result(status, state=None, reason=None):
    return {"status": status, "state": state, "reason": reason}


def _reservation_result(status, state, reservation_id=None, reason=None):
    return {
        "status": status,
        "state": state,
        "reservation_id": reservation_id,
        "reason": reason,
    }


def _commit_result(status, state, reason=None):
    return {"status": status, "state": state, "reason": reason}


def _resolution_result(status, reservation_id, processing_allowed, reason=None):
    return {
        "status": status,
        "reservation_id": reservation_id,
        "processing_allowed": processing_allowed,
        "reason": reason,
    }


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _active_search_reservations(state):
    try:
        reservations = state["budgets"]["reservations"]["search_item_reservations"]
    except (KeyError, TypeError):
        return None
    if not isinstance(reservations, list):
        return None
    return [
        item
        for item in reservations
        if isinstance(item, dict) and item.get("state") == "RESERVED"
    ]


def _ordered_pool_ids(checkpoint):
    pool = checkpoint.get("pool")
    if not isinstance(pool, list):
        return None
    result = []
    for entry in pool:
        if not isinstance(entry, dict):
            return None
        resume_id = entry.get("resume_id")
        if not isinstance(resume_id, str) or not resume_id:
            return None
        result.append(resume_id)
    return result


def _valid_v3_structure(checkpoint):
    if not _REQUIRED_KEYS.issubset(checkpoint):
        return False, "Required V3 checkpoint section is missing"
    if not isinstance(checkpoint.get("metrics"), dict):
        return False, "Checkpoint metrics must be an object"
    duplicate_sections = _EXECUTION_SECTIONS.intersection(checkpoint["metrics"])
    if duplicate_sections:
        return False, "Execution state must not be stored inside metrics"
    for section in (
        "pool_exclusions",
        "technical",
        "professional",
        "admission",
        "budgets",
        "traversal",
        "completion",
        "guard",
    ):
        if not isinstance(checkpoint.get(section), dict):
            return False, f"Checkpoint section {section} must be an object"
    if _ordered_pool_ids(checkpoint) is None:
        return False, "V3 pool entries must be objects with non-empty resume_id"
    try:
        json.dumps(checkpoint)
    except (TypeError, ValueError):
        return False, "Checkpoint must be JSON serializable"
    return True, None


def validate_checkpoint(checkpoint: dict) -> dict:
    """Validate a versioned V3 checkpoint without mutating it."""
    if not isinstance(checkpoint, dict):
        return _validation_result(
            "MANUAL_RESOLUTION", reason="Checkpoint format requires manual resolution"
        )

    pool_version = checkpoint.get("pool_version")
    if not _is_int(pool_version) or pool_version != 3:
        return _validation_result(
            "MANUAL_RESOLUTION", reason="Checkpoint pool version is legacy or unknown"
        )

    if "checkpoint_schema_version" not in checkpoint:
        return _validation_result(
            "MANUAL_RESOLUTION", reason="Versioned checkpoint schema is missing"
        )
    schema_version = checkpoint.get("checkpoint_schema_version")
    if not _is_int(schema_version) or schema_version != _SUPPORTED_SCHEMA_VERSION:
        return _validation_result(
            "MANUAL_RESOLUTION", reason="Checkpoint schema version is unknown"
        )

    valid, reason = _valid_v3_structure(checkpoint)
    if not valid:
        return _validation_result("INVALID", reason=reason)
    return _validation_result("VALID", checkpoint=copy.deepcopy(checkpoint))


def recover_checkpoint(
    checkpoint: dict,
    *,
    pool_ids: list[str] | None = None,
) -> dict:
    """Recover an independent V3 state and verify its derived ordered IDs."""
    validated = validate_checkpoint(checkpoint)
    if validated["status"] != "VALID":
        return _recovery_result(
            "MANUAL_RESOLUTION",
            reason=validated["reason"] or "Checkpoint cannot be recovered safely",
        )

    recovered = validated["checkpoint"]
    derived_ids = _ordered_pool_ids(recovered)
    if derived_ids is None:
        return _recovery_result(
            "MANUAL_RESOLUTION", reason="Checkpoint pool entries are incompatible"
        )
    if pool_ids is not None:
        if not isinstance(pool_ids, list) or pool_ids != derived_ids:
            return _recovery_result(
                "MANUAL_RESOLUTION",
                reason="Derived pool_ids do not match checkpoint.pool",
            )
        if any(not isinstance(value, str) for value in pool_ids):
            return _recovery_result(
                "MANUAL_RESOLUTION", reason="Derived pool_ids are malformed"
            )
    return _recovery_result("RECOVERED", state=copy.deepcopy(recovered))


def reserve_search_item(
    state: dict,
    *,
    page_identity: str,
    item_index: int,
) -> dict:
    """Create or return a deterministic active search-item reservation."""
    result_state = copy.deepcopy(state)
    active = _active_search_reservations(result_state)
    if active is None:
        return _reservation_result(
            "PERSISTENCE_UNKNOWN",
            result_state,
            reason="Search-item reservation state is unavailable",
        )

    for reservation in active:
        if (
            reservation.get("operation_type") == "SEARCH_ITEM"
            and reservation.get("page_identity") == page_identity
            and reservation.get("item_index") == item_index
        ):
            return _reservation_result(
                "RESERVED", result_state, reservation.get("reservation_id")
            )

    try:
        consumed = result_state["budgets"]["consumed"]["search_items_consumed"]
        limit = result_state["budgets"]["limits"]["search_item_budget"]
    except (KeyError, TypeError):
        return _reservation_result(
            "PERSISTENCE_UNKNOWN",
            result_state,
            reason="Search-item budget state is unavailable",
        )
    if not _is_int(consumed) or not _is_int(limit):
        return _reservation_result(
            "PERSISTENCE_UNKNOWN",
            result_state,
            reason="Search-item budget state is malformed",
        )
    if consumed + len(active) >= limit:
        return _reservation_result(
            "BUDGET_EXHAUSTED",
            result_state,
            reason="Search-item budget is exhausted",
        )

    identity_payload = json.dumps(
        [page_identity, item_index], ensure_ascii=False, separators=(",", ":")
    )
    reservation_id = "search-item-" + hashlib.sha256(
        identity_payload.encode("utf-8")
    ).hexdigest()
    reservation = {
        "reservation_id": reservation_id,
        "operation_type": "SEARCH_ITEM",
        "page_identity": page_identity,
        "item_index": item_index,
        "state": "RESERVED",
    }
    result_state["budgets"]["reservations"][
        "search_item_reservations"
    ].append(reservation)
    return _reservation_result("RESERVED", result_state, reservation_id)


def commit_item_outcome(
    state: dict,
    *,
    reservation_id: str,
    technical_outcome: str,
    resume_id: str | None = None,
    professional_decision: str | None = None,
    admission: str | None = None,
) -> dict:
    """Commit one reserved item outcome when traversal context still matches."""
    result_state = copy.deepcopy(state)
    active = _active_search_reservations(result_state)
    if active is None:
        return _commit_result(
            "INVALID_RESERVATION",
            result_state,
            "Search-item reservation state is unavailable",
        )

    reservation = next(
        (
            item
            for item in active
            if item.get("reservation_id") == reservation_id
            and item.get("operation_type") == "SEARCH_ITEM"
        ),
        None,
    )
    if reservation is None:
        return _commit_result(
            "INVALID_RESERVATION", result_state, "Reservation is unknown or inactive"
        )

    try:
        page_identity = result_state["traversal"]["page_snapshot"]["page_identity"]
        next_item_index = result_state["traversal"]["next_item_index"]
        consumed = result_state["budgets"]["consumed"]["search_items_consumed"]
    except (KeyError, TypeError):
        return _commit_result(
            "INVALID_RESERVATION", result_state, "Traversal context is unavailable"
        )
    if (
        reservation.get("page_identity") != page_identity
        or reservation.get("item_index") != next_item_index
    ):
        return _commit_result(
            "INVALID_RESERVATION",
            result_state,
            "Reservation does not match current traversal context",
        )
    if not _is_int(consumed) or not _is_int(next_item_index):
        return _commit_result(
            "INVALID_RESERVATION", result_state, "Traversal counters are malformed"
        )

    reservations = result_state["budgets"]["reservations"][
        "search_item_reservations"
    ]
    reservations.remove(reservation)
    outcome = {
        "reservation_id": reservation_id,
        "technical_outcome": technical_outcome,
        "resume_id": resume_id,
        "professional_decision": professional_decision,
        "admission": admission,
    }
    result_state["technical"].setdefault("item_outcomes", []).append(outcome)
    result_state["budgets"]["consumed"]["search_items_consumed"] = consumed + 1
    result_state["traversal"]["next_item_index"] = next_item_index + 1
    return _commit_result("COMMITTED", result_state)


def resolve_reservation_after_failure(
    durable_checkpoint: dict | None,
    *,
    reservation_id: str,
    read_status: str,
) -> dict:
    """Interpret a caller-supplied pure-state read-after-failure result."""
    if read_status == "UNKNOWN":
        return _resolution_result(
            "PERSISTENCE_UNKNOWN",
            reservation_id,
            False,
            "Persistence result is unknown",
        )
    if read_status == "NOT_PERSISTED":
        return _resolution_result(
            "NOT_PERSISTED",
            reservation_id,
            False,
            "Reservation was not persisted",
        )
    if read_status == "CONFIRMED":
        active = (
            _active_search_reservations(durable_checkpoint)
            if isinstance(durable_checkpoint, dict)
            else None
        )
        found = bool(
            active
            and any(
                item.get("reservation_id") == reservation_id
                and item.get("operation_type") == "SEARCH_ITEM"
                for item in active
            )
        )
        if found:
            return _resolution_result("CONFIRMED", reservation_id, True)
        return _resolution_result(
            "NOT_PERSISTED",
            reservation_id,
            False,
            "Confirmed durable state does not contain the active reservation",
        )
    return _resolution_result(
        "PERSISTENCE_UNKNOWN",
        reservation_id,
        False,
        "Read status is unsupported",
    )

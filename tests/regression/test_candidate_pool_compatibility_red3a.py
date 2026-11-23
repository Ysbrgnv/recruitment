"""RED-3A: Candidate Pool V1/V2 compatibility characterisation."""

import copy
import unittest
from unittest.mock import patch

import requests

from app.core import settings
from app.core.errors import HHUnavailableError
from app.services import recruitment_service as service


def metrics():
    return {
        "search_items": 0,
        "scanned": 0,
        "already_sent": 0,
        "load_failed": 0,
        "excluded_city": 0,
        "excluded_experience": 0,
        "excluded_leadership": 0,
        "excluded_role_mismatch": 0,
        "pool": 0,
    }


def hh404():
    error = HHUnavailableError("404")
    error.error_kind = "not_found"
    error.request_scope = "details"
    error.status_code = 404
    return error


def valid_resume():
    return {
        "id": "a",
        "title": "Solution Architect",
        "area": {
            "id": "1",
            "name": "Алматы",
        },
        "total_experience": {
            "months": 96,
        },
        "professional_roles": [],
        "skill_set": [],
        "experience": [
            {
                "position": "Solution Architect",
                "start": "2024-01",
                "end": None,
                "description": "Architecture",
            }
        ],
    }


class CandidatePoolCompatibilityRed3A(unittest.TestCase):
    def v1(
        self,
        detail=None,
        *,
        items=None,
        pages=1,
        target_pool=1,
    ):
        items = [{"id": "a"}] if items is None else items
        detail = valid_resume() if detail is None else detail

        def load(_resume_id):
            if isinstance(detail, BaseException):
                raise detail
            return copy.deepcopy(detail)

        with (
            patch.object(
                settings,
                "CANDIDATE_POOL_V3_ENABLED",
                False,
            ),
            patch.object(
                settings,
                "CANDIDATE_POOL_V2_ENABLED",
                False,
            ),
            patch.object(
                service,
                "search_resumes",
                return_value={
                    "items": items,
                    "pages": pages,
                },
            ),
            patch.object(
                service,
                "fetch_resume_details",
                side_effect=load,
            ),
            patch.object(
                service,
                "was_sent_to_ai",
                return_value=False,
            ),
            patch.object(
                service,
                "reset_history_cache",
            ),
            patch.object(
                service,
                "resume_filter_result",
                return_value=(True, [], None),
            ),
            patch.object(
                service,
                "_resume_role_matches_query",
                return_value=True,
            ),
        ):
            return service._search_candidate_pool(
                "v",
                {},
                {},
                "q",
                target_pool,
                1,
                metrics(),
                lambda *args: None,
            )

    def v2(
        self,
        detail=None,
        *,
        items=None,
        pages=1,
        target_pool=1,
    ):
        items = [{"id": "a"}] if items is None else items
        detail = valid_resume() if detail is None else detail

        def load(_resume_id):
            if isinstance(detail, BaseException):
                raise detail
            return copy.deepcopy(detail)

        plan = {
            "strategies": [
                {
                    "id": "balanced",
                    "query": "q",
                    "executable_now": True,
                    "blocked_by": None,
                }
            ]
        }

        leadership = {
            "level": "unknown",
            "confidence": 0.2,
            "evidence_categories": [],
            "evidence": [],
            "review_hint": True,
        }

        with (
            patch.object(
                settings,
                "CANDIDATE_POOL_V3_ENABLED",
                False,
            ),
            patch.object(
                settings,
                "CANDIDATE_POOL_V2_ENABLED",
                True,
            ),
            patch.object(
                service,
                "search_resumes",
                return_value={
                    "items": items,
                    "pages": pages,
                },
            ),
            patch.object(
                service,
                "fetch_resume_details",
                side_effect=load,
            ),
            patch.object(
                service,
                "was_sent_to_ai",
                return_value=False,
            ),
            patch.object(
                service,
                "reset_history_cache",
            ),
            patch.object(
                service,
                "assess_candidate_leadership_v2",
                return_value=leadership,
            ),
        ):
            return service._search_candidate_pool(
                "v",
                {},
                {
                    "area_id": "1",
                    "minimum_experience_months": 0,
                },
                "q",
                target_pool,
                1,
                metrics(),
                lambda *args: None,
                profile={
                    "leadership_intent": {
                        "level": "unknown",
                    }
                },
                plan=plan,
            )

    def test_01_v1_hh_404_propagates(self):
        with self.assertRaises(HHUnavailableError):
            self.v1(hh404())

    def test_02_v2_hh_404_propagates(self):
        with self.assertRaises(HHUnavailableError):
            self.v2(hh404())

    def test_03_v1_details_json_is_generic_load_failed(self):
        result = self.v1(
            requests.exceptions.JSONDecodeError(
                "x",
                "x",
                0,
            )
        )
        self.assertEqual(result, [])

    def test_04_v2_details_json_is_generic_load_failed(self):
        result = self.v2(
            requests.exceptions.JSONDecodeError(
                "x",
                "x",
                0,
            )
        )
        self.assertEqual(result, [])

    def test_05_v1_v2_pages_none_keep_type_error(self):
        for invoke in (self.v1, self.v2):
            with self.subTest(invoke=invoke.__name__):
                with self.assertRaises(TypeError):
                    invoke(
                        items=[],
                        pages=None,
                        target_pool=1,
                    )

    def test_06_v1_v2_keep_list_result_format(self):
        for invoke in (self.v1, self.v2):
            with self.subTest(invoke=invoke.__name__):
                result = invoke()

                self.assertIsInstance(result, list)
                self.assertTrue(result)
                self.assertEqual(result[0][0], "a")

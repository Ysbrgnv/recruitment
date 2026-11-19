"""RED-1B: unified HH retry budget and effective-timeout contracts."""
from __future__ import annotations

import unittest
from unittest.mock import Mock, call, patch

import requests

from app.core import settings
from app.core.errors import HHUnavailableError
from app.integrations.hh import hh_client
from app.services import recruitment_service as service


RETRYABLE_HTTP_STATUSES = (500, 502, 503, 504)


def _response(status, *, retry_after=None):
    response = Mock()
    response.status_code = status
    response.headers = {}
    if retry_after is not None:
        response.headers["Retry-After"] = str(retry_after)
    response.json.return_value = {}
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
    return response


def _setting(name):
    if not hasattr(settings, name):
        raise AssertionError(f"Missing configuration: {name}")
    return getattr(settings, name)


class HHRetryContractV3BudgetTests(unittest.TestCase):
    def test_mixed_failures_share_one_four_request_budget(self):
        sequence = [
            requests.Timeout("attempt-1"),
            _response(503),
            _response(429, retry_after=60),
            requests.Timeout("attempt-4"),
        ]
        with patch.object(hh_client, "reload_saved_tokens"), patch.object(
            hh_client.requests, "get", side_effect=sequence
        ) as get, patch.object(hh_client.time, "sleep") as sleep:
            with self.assertRaises(HHUnavailableError):
                hh_client.hh_get("/resumes", retries=3)
        self.assertEqual(get.call_count, 4)
        self.assertEqual(sleep.call_args_list, [call(2), call(4), call(30)])

    def test_retry_after_replaces_current_attempt_delay(self):
        success = _response(200)
        with patch.object(hh_client, "reload_saved_tokens"), patch.object(
            hh_client.requests,
            "get",
            side_effect=[_response(429, retry_after=7), success],
        ), patch.object(hh_client.time, "sleep") as sleep:
            self.assertIs(hh_client.hh_get("/resumes", retries=3), success)
        sleep.assert_called_once_with(7)

    def test_retry_after_six_hundred_is_capped_at_thirty(self):
        success = _response(200)
        with patch.object(hh_client, "reload_saved_tokens"), patch.object(
            hh_client.requests,
            "get",
            side_effect=[_response(429, retry_after=600), success],
        ), patch.object(hh_client.time, "sleep") as sleep:
            hh_client.hh_get("/resumes", retries=3)
        sleep.assert_called_once_with(30)

    def test_changing_failure_type_does_not_reset_attempt_number(self):
        sequence = [
            requests.ConnectionError("a"),
            _response(500),
            requests.Timeout("c"),
            _response(502),
        ]
        with patch.object(hh_client, "reload_saved_tokens"), patch.object(
            hh_client.requests, "get", side_effect=sequence
        ) as get, patch.object(hh_client.time, "sleep") as sleep:
            with self.assertRaises(HHUnavailableError):
                hh_client.hh_get("/resumes", retries=3)
        self.assertEqual(get.call_count, 4)
        self.assertEqual(sleep.call_args_list, [call(2), call(4), call(8)])


class HHRetryContractV3ServerErrorTests(unittest.TestCase):
    def test_each_transient_server_error_is_retried_inside_hh_get(self):
        for status in RETRYABLE_HTTP_STATUSES:
            with self.subTest(status=status):
                success = _response(200)
                with patch.object(hh_client, "reload_saved_tokens"), patch.object(
                    hh_client.requests,
                    "get",
                    side_effect=[_response(status), success],
                ) as get, patch.object(hh_client.time, "sleep") as sleep:
                    self.assertIs(hh_client.hh_get("/resumes", retries=3), success)
                self.assertEqual(get.call_count, 2)
                sleep.assert_called_once_with(2)

    def test_server_error_budget_never_exceeds_four_requests(self):
        with patch.object(hh_client, "reload_saved_tokens"), patch.object(
            hh_client.requests,
            "get",
            return_value=_response(503),
        ) as get, patch.object(hh_client.time, "sleep") as sleep:
            with self.assertRaises(HHUnavailableError):
                hh_client.hh_get("/resumes", retries=3)
        self.assertEqual(get.call_count, 4)
        self.assertEqual(sleep.call_args_list, [call(2), call(4), call(8)])


class HHRetryContractV3PoolBoundaryTests(unittest.TestCase):
    def test_candidate_pool_does_not_create_second_retry_loop(self):
        unavailable = HHUnavailableError("HH temporarily unavailable")
        with patch.object(settings, "CANDIDATE_POOL_V2_ENABLED", False), patch.object(
            service, "reset_history_cache"
        ), patch.object(service, "search_resumes", side_effect=unavailable) as search, patch.object(
            service.time, "sleep"
        ) as sleep:
            with self.assertRaises(HHUnavailableError):
                service._search_candidate_pool(
                    "vacancy-1",
                    {},
                    {"area_id": ""},
                    '"approved"',
                    8,
                    2,
                    {
                        "search_items": 0,
                        "scanned": 0,
                        "already_sent": 0,
                        "load_failed": 0,
                        "excluded_city": 0,
                        "excluded_experience": 0,
                        "excluded_leadership": 0,
                        "excluded_role_mismatch": 0,
                        "pool": 0,
                    },
                    lambda *args: None,
                )
        search.assert_called_once()
        sleep.assert_not_called()


class HHRetryContractV3EffectiveConfigurationTests(unittest.TestCase):
    def test_effective_timeout_configuration_is_safe_for_retry_envelope(self):
        connect_timeout = _setting("HH_CONNECT_TIMEOUT_SECONDS")
        read_timeout = _setting("REQUEST_TIMEOUT")
        max_attempts = _setting("HH_MAX_HTTP_ATTEMPTS")
        retry_after_cap = _setting("HH_MAX_RETRY_AFTER_SECONDS")
        checkpoint_margin = _setting("CANDIDATE_POOL_V3_CHECKPOINT_MARGIN_SECONDS")
        worker_timeout = _setting("HH_WORKER_TIMEOUT_SECONDS")
        operation_lock_ttl = _setting("OPERATION_LOCK_TTL_SECONDS")
        queue_visibility_timeout = _setting("QUEUE_VISIBILITY_TIMEOUT_SECONDS")

        self.assertEqual(max_attempts, 4)
        self.assertEqual(retry_after_cap, 30)
        max_single_attempt = connect_timeout + read_timeout
        max_retry_delays = retry_after_cap * (max_attempts - 1)
        max_step_duration = (
            max_attempts * max_single_attempt
            + max_retry_delays
            + checkpoint_margin
        )
        self.assertGreater(worker_timeout, max_step_duration)
        self.assertGreater(operation_lock_ttl, worker_timeout)
        self.assertGreater(queue_visibility_timeout, worker_timeout)

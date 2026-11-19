"""RED-1C: HTTP 401 recovery must not reset or multiply the HH retry budget.

Target path after review: tests/regression/test_hh_auth_retry_contract_v3.py

All HTTP, authorization recovery and sleep calls are mocked.
No real network access and no real waiting.

Approved contract (PLANNING, GREEN-1):
  * transient errors share one budget: 1 request + at most 3 retries;
  * the request that returned HTTP 401 is a separate authorization attempt;
    therefore one 401 followed by only transient failures may use at most
    1 + 4 = 5 HTTP requests;
  * authorization recovery runs at most once per logical hh_get() call;
  * HTTP 401 itself is never delayed by the 2/4/8 transient backoff.
"""
from __future__ import annotations

import unittest
from unittest.mock import Mock, call, patch

import requests

from app.core.errors import HHUnavailableError
from app.integrations.hh import hh_client


MAX_TRANSIENT_REQUESTS = 4
MAX_HTTP_REQUESTS_WITH_ONE_AUTH_RECOVERY = MAX_TRANSIENT_REQUESTS + 1


def _response(status):
    response = Mock()
    response.status_code = status
    response.headers = {}
    response.json.return_value = {}
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
    return response


class HHAuthRetryContractV3Tests(unittest.TestCase):
    def test_401_recovery_does_not_reset_transient_retry_budget(self):
        sequence = [
            _response(401),
            _response(503),
            _response(503),
            _response(503),
            _response(503),
            # Must never be consumed: a fifth transient request is forbidden.
            _response(200),
        ]
        with patch.object(hh_client, "reload_saved_tokens"), patch.object(
            hh_client, "recover_auth", return_value=True
        ) as recover, patch.object(
            hh_client.requests, "get", side_effect=sequence
        ) as get, patch.object(hh_client.time, "sleep") as sleep:
            with self.assertRaises(HHUnavailableError):
                hh_client.hh_get("/resumes", retries=3)
        self.assertEqual(recover.call_count, 1)
        self.assertEqual(
            get.call_count,
            MAX_HTTP_REQUESTS_WITH_ONE_AUTH_RECOVERY,
            "401 + four transient attempts = exactly five HTTP requests",
        )
        self.assertLessEqual(get.call_count, MAX_HTTP_REQUESTS_WITH_ONE_AUTH_RECOVERY)
        self.assertEqual(sleep.call_args_list, [call(2), call(4), call(8)])

    def test_second_401_does_not_start_another_authorization_cycle(self):
        sequence = [
            _response(401),
            _response(401),
            # Must never be consumed: no third request and no second cycle.
            _response(200),
        ]
        with patch.object(hh_client, "reload_saved_tokens"), patch.object(
            hh_client, "recover_auth", return_value=True
        ) as recover, patch.object(
            hh_client.requests, "get", side_effect=sequence
        ) as get, patch.object(hh_client.time, "sleep") as sleep:
            with self.assertRaises(HHUnavailableError):
                hh_client.hh_get("/resumes", retries=3)
        self.assertEqual(recover.call_count, 1)
        self.assertEqual(get.call_count, 2)
        sleep.assert_not_called()

    def test_auth_failure_does_not_sleep_as_transient_server_error(self):
        with self.subTest(case="recovery succeeds"):
            success = _response(200)
            with patch.object(hh_client, "reload_saved_tokens"), patch.object(
                hh_client, "recover_auth", return_value=True
            ) as recover, patch.object(
                hh_client.requests, "get", side_effect=[_response(401), success]
            ) as get, patch.object(hh_client.time, "sleep") as sleep:
                self.assertIs(hh_client.hh_get("/resumes", retries=3), success)
            self.assertEqual(recover.call_count, 1)
            self.assertEqual(get.call_count, 2)
            sleep.assert_not_called()

        with self.subTest(case="recovery fails"):
            with patch.object(hh_client, "reload_saved_tokens"), patch.object(
                hh_client, "recover_auth", return_value=False
            ) as recover, patch.object(
                hh_client.requests, "get", side_effect=[_response(401), _response(200)]
            ) as get, patch.object(hh_client.time, "sleep") as sleep:
                with self.assertRaises(HHUnavailableError):
                    hh_client.hh_get("/resumes", retries=3)
            self.assertEqual(recover.call_count, 1)
            self.assertEqual(get.call_count, 1)
            sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()

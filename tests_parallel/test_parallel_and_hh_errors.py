from pathlib import Path
ROOT=Path(__file__).parents[1]
def test_parallel_limits():
    worker=(ROOT/'durable_queue.py').read_text()
    compose=(ROOT/'compose.yaml').read_text()
    assert "GENERAL_CONSUMERS','16'" in worker
    assert "XRAY_CONSUMERS','8'" in worker
    assert "GENERAL_CONSUMERS: '16'" in compose
    assert "XRAY_CONSUMERS: '8'" in compose
    assert "BROWSER_MAX_SESSIONS: '8'" in compose
def test_parallel_claim_is_safe():
    source=(ROOT/'durable_queue.py').read_text()
    assert 'FOR UPDATE OF j SKIP LOCKED LIMIT 1' in source
    assert 'task_jobs_one_running' in source
def test_hh_startup_single_flight_and_expected_error_without_traceback():
    source=(ROOT/'web_server.py').read_text()
    assert 'STATE_LOCK.acquire(blocking=False)' in source
    assert 'except UserFacingError as exc:' in source
    assert 'logger.warning("Вакансии HH недоступны: %s", exc)' in source
def test_refresh_guard_survives_restart():
    source=(ROOT/'hh_client.py').read_text()
    assert ".hh-refresh-state.json" in source
    assert "state.get('blocked')" in source


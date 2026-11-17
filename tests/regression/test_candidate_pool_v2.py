"""Offline RED contracts for feature-flagged Candidate Pool V2."""
from __future__ import annotations
import copy
import inspect
import unittest
from unittest.mock import patch
from app.core import settings
from app.services import recruitment_service as service
from app.services import workflow
STRATEGY_ORDER = ['balanced', 'title_recall', 'functional_recall', 'precision']

def _strategy(strategy_id, query, executable_now=True, blocked_by=None):
    return {'id': strategy_id, 'kind': 'contract_strategy', 'query': query, 'executable_now': executable_now, 'blocked_by': blocked_by}

def _plan(strategies=None):
    return {'schema_version': 3, 'generation_basis': 'deterministic_renderer_v3', 'strategies': strategies if strategies is not None else [_strategy('precision', '"precision"'), _strategy('functional_recall', '"functional"', False, 'pool_v1_requires_leading_title_group'), _strategy('balanced', '"balanced"'), _strategy('title_recall', '"titles"')], 'selected_strategy': 'balanced'}

def _vacancy(leadership='individual_contributor'):
    return {'id': 'vacancy-1', 'name': 'Solution Architect', 'area': {'id': '1', 'name': 'Алматы'}, 'leadership': leadership}

def _requirements(leadership='individual_contributor'):
    return {'area_id': '1', 'area_name': 'алматы', 'minimum_experience_months': 72, 'is_leadership_vacancy': leadership == 'leadership'}

def _profile(leadership='individual_contributor'):
    return {'role_family': 'solution_architecture', 'leadership_intent': {'level': leadership, 'evidence': ['Vacancy evidence']}, 'target_functions': ['solution architecture'], 'accepted_role_titles': ['Solution Architect'], 'mandatory_search_concepts': [], 'domain_context': [], 'preferred_concepts': [], 'excluded_role_families': [], 'excluded_titles': [], 'rationale': {'role_family': 'Architecture', 'accepted_role_titles': {}, 'excluded_titles': {}}}

def _resume(resume_id='r1', title='Solution Architect', description='', area_id='1', months=96):
    return {'id': resume_id, 'title': title, 'area': {'id': area_id, 'name': 'Алматы' if area_id == '1' else 'Астана'}, 'total_experience': {'months': months}, 'professional_roles': [], 'skill_set': [], 'experience': [{'position': title, 'start': '2024-01', 'end': None, 'description': description}], 'alternate_url': f'https://example.invalid/{resume_id}'}

def _metrics():
    return {'search_items': 0, 'scanned': 0, 'already_sent': 0, 'load_failed': 0, 'excluded_city': 0, 'excluded_experience': 0, 'excluded_leadership': 0, 'excluded_role_mismatch': 0, 'excluded_professional_mismatch': 0, 'excluded_leadership_mismatch': 0, 'excluded_level_mismatch': 0, 'level_unknown_kept': 0, 'pool': 0}

def _v2_note(candidate):
    for note in candidate[2]:
        if isinstance(note, dict) and note.get('pool_version') == 2:
            return note
    raise AssertionError('Candidate Pool V2 provenance note is missing')

def _resume_inputs_snapshot(values):
    result = {}
    for key, value in values.items():
        if isinstance(value, BaseException):
            result[key] = {'kind': 'exception', 'type': type(value), 'args': copy.deepcopy(value.args)}
        else:
            result[key] = {'kind': 'resume', 'value': copy.deepcopy(value)}
    return result

def _call_pool(*, plan=None, search_pages=None, resumes=None, target_pool=20, existing_pool=None, checkpoint=None, profile=None, requirements=None, metrics=None, pool_exclusions=None, vacancy=None):
    search_pages = search_pages or {}
    resumes = resumes or {}
    metrics = metrics or _metrics()
    pool_exclusions = pool_exclusions if pool_exclusions is not None else {}

    def search(params):
        return copy.deepcopy(search_pages.get((params.get('text'), params.get('page', 0)), {'items': [], 'pages': 1}))

    def load(resume_id):
        value = resumes[resume_id]
        if isinstance(value, BaseException):
            raise value
        return value
    plan = plan if plan is not None else _plan()
    vacancy = vacancy if vacancy is not None else _vacancy((profile or {}).get('leadership_intent', {}).get('level', 'individual_contributor'))
    requirements = requirements if requirements is not None else _requirements()
    existing_pool = existing_pool if existing_pool is not None else []
    plan_before = copy.deepcopy(plan)
    vacancy_before = copy.deepcopy(vacancy)
    requirements_before = copy.deepcopy(requirements)
    existing_pool_before = copy.deepcopy(existing_pool)
    resumes_before = _resume_inputs_snapshot(resumes)
    with patch.object(settings, 'CANDIDATE_POOL_V2_ENABLED', True, create=True), patch.object(service, 'search_resumes', side_effect=search) as search_mock, patch.object(service, 'fetch_resume_details', side_effect=load) as fetch_mock, patch.object(service, 'was_sent_to_ai', return_value=False), patch.object(service, 'reset_history_cache'), patch('app.integrations.ai.nano_client.ask', side_effect=AssertionError('AI called')) as ask_mock, patch('app.integrations.ai.nano_client.evaluate_one_resume', side_effect=AssertionError('AI evaluation called')) as evaluate_mock, patch.object(workflow, 'send_candidate_email', side_effect=AssertionError('email called')) as candidate_email, patch.object(workflow, 'send_xray_email', side_effect=AssertionError('email called')) as xray_email, patch('requests.get', side_effect=AssertionError('network called')) as get_mock, patch('requests.post', side_effect=AssertionError('network called')) as post_mock:
        arguments = {'profile': profile or _profile(), 'pool_exclusions': pool_exclusions, 'existing_pool': existing_pool, 'checkpoint': checkpoint}
        if 'plan' in inspect.signature(service._search_candidate_pool).parameters:
            arguments['plan'] = plan
        pool = service._search_candidate_pool('vacancy-1', vacancy, requirements, '"legacy query"', target_pool, 5, metrics, lambda *args: None, **arguments)
    for blocked in (ask_mock, evaluate_mock, candidate_email, xray_email, get_mock, post_mock):
        blocked.assert_not_called()
    if plan != plan_before:
        raise AssertionError('production mutated query plan')
    if vacancy != vacancy_before:
        raise AssertionError('production mutated vacancy')
    if requirements != requirements_before:
        raise AssertionError('production mutated requirements')
    if existing_pool != existing_pool_before:
        raise AssertionError('production mutated existing_pool')
    if _resume_inputs_snapshot(resumes) != resumes_before:
        raise AssertionError('production mutated fetched resume')
    return (pool, metrics, pool_exclusions, search_mock, fetch_mock)

class CandidatePoolV2FeatureFlagTests(unittest.TestCase):

    def test_pool_interface_accepts_optional_plan(self):
        signature = inspect.signature(service._search_candidate_pool)
        self.assertIn('plan', signature.parameters)
        self.assertIsNone(signature.parameters['plan'].default)

    def test_feature_flag_exists_and_defaults_false(self):
        self.assertTrue(hasattr(settings, 'CANDIDATE_POOL_V2_ENABLED'))
        self.assertIs(settings.CANDIDATE_POOL_V2_ENABLED, False)

    def test_pool_v1_path_ignores_plan_when_flag_false(self):
        with patch.object(settings, 'CANDIDATE_POOL_V2_ENABLED', False, create=True), patch.object(service, 'search_resumes', return_value={'items': [], 'pages': 1}) as search, patch.object(service, 'reset_history_cache'):
            service._search_candidate_pool('v', _vacancy(), _requirements(), '"legacy query"', 1, 1, _metrics(), lambda *args: None)
        self.assertEqual(search.call_args.args[0]['text'], '"legacy query"')

    def test_existing_positional_call_remains_valid(self):
        with patch.object(service, 'search_resumes', return_value={'items': [], 'pages': 1}), patch.object(service, 'reset_history_cache'):
            result = service._search_candidate_pool('v', _vacancy(), _requirements(), '"legacy query"', 1, 1, _metrics(), lambda *args: None)
        self.assertEqual(result, [])

class CandidatePoolV2StrategyTests(unittest.TestCase):

    def test_saved_strategies_execute_in_required_order(self):
        pages = {(f'''"{(name if name != 'title_recall' else 'titles')}"''', 0): {'items': [], 'pages': 1} for name in STRATEGY_ORDER}
        _, _, _, search, _ = _call_pool(plan=_plan(), search_pages=pages)
        self.assertEqual([call.args[0]['text'] for call in search.call_args_list], ['"balanced"', '"titles"', '"functional"', '"precision"'])

    def test_missing_optional_strategy_is_skipped(self):
        plan = _plan([_strategy('balanced', '"balanced"'), _strategy('precision', '"precision"')])
        _, _, _, search, _ = _call_pool(plan=plan)
        self.assertEqual([call.args[0]['text'] for call in search.call_args_list], ['"balanced"', '"precision"'])

    def test_duplicate_strategy_id_is_not_executed_twice(self):
        plan = _plan([_strategy('balanced', '"first"'), _strategy('balanced', '"second"')])
        _, _, _, search, _ = _call_pool(plan=plan)
        search.assert_called_once()
        self.assertEqual(search.call_args.args[0]['text'], '"first"')

    def test_functional_recall_executes_under_known_blocker(self):
        plan = _plan([_strategy('functional_recall', '"functional"', False, 'pool_v1_requires_leading_title_group')])
        _, _, _, search, _ = _call_pool(plan=plan)
        self.assertEqual(search.call_args.args[0]['text'], '"functional"')

    def test_unknown_blocker_does_not_execute(self):
        plan = _plan([_strategy('balanced', '"blocked"', False, 'manual_approval_required')])
        _, _, _, search, _ = _call_pool(plan=plan)
        search.assert_not_called()

    def test_saved_plan_and_strategies_are_immutable(self):
        plan = _plan()
        before = copy.deepcopy(plan)
        _call_pool(plan=plan)
        self.assertEqual(plan, before)

    def test_vacancy_requirements_resume_and_plan_are_immutable(self):
        vacancy = _vacancy()
        requirements = _requirements()
        resume = _resume()
        plan = _plan([_strategy('balanced', '"balanced"')])
        pages = {('"balanced"', 0): {'items': [{'id': 'r1', 'area': {'id': '1'}}], 'pages': 1}}
        before = copy.deepcopy((vacancy, requirements, resume, plan))
        _call_pool(plan=plan, search_pages=pages, resumes={'r1': resume}, vacancy=vacancy, requirements=requirements)
        self.assertEqual((vacancy, requirements, resume, plan), before)

    def test_strategy_dictionaries_and_existing_pool_are_immutable(self):
        strategies = [_strategy('balanced', '"balanced"')]
        plan = _plan(strategies)
        note = {'pool_version': 2, 'matched_strategies': ['balanced'], 'strategy_matches': [], 'signals': {}, 'leadership': {}}
        existing_pool = [('existing', _resume('existing'), [note])]
        before_strategies = copy.deepcopy(strategies)
        before_pool = copy.deepcopy(existing_pool)
        result, _, _, _, _ = _call_pool(plan=plan, existing_pool=existing_pool)
        self.assertEqual(strategies, before_strategies)
        self.assertEqual(existing_pool, before_pool)
        self.assertIsNot(result, existing_pool)
        self.assertEqual(result, existing_pool)

    def test_pool_v2_never_generates_queries_or_calls_ai(self):
        source = inspect.getsource(service._search_candidate_pool)
        self.assertNotIn('generate_vacancy_search_plan', source)
        self.assertNotIn('query_interpreter', source)
        _call_pool(plan=_plan())

class CandidatePoolV2DeduplicationTests(unittest.TestCase):

    def duplicate_result(self):
        pages = {('"balanced"', 0): {'items': [{'id': 'r1', 'area': {'id': '1'}}], 'pages': 1}, ('"titles"', 0): {'items': [{'id': 'r1', 'area': {'id': '1'}}], 'pages': 1}}
        plan = _plan([_strategy('balanced', '"balanced"'), _strategy('title_recall', '"titles"')])
        return _call_pool(plan=plan, search_pages=pages, resumes={'r1': _resume()})

    def test_duplicate_resume_appears_once_and_is_fetched_once(self):
        pool, _, _, _, fetch = self.duplicate_result()
        self.assertEqual([candidate[0] for candidate in pool], ['r1'])
        fetch.assert_called_once_with('r1')

    def test_all_matched_strategies_are_preserved(self):
        pool, _, _, _, _ = self.duplicate_result()
        self.assertEqual(_v2_note(pool[0])['matched_strategies'], ['balanced', 'title_recall'])

    def test_strategy_page_and_rank_provenance_is_preserved(self):
        pool, _, _, _, _ = self.duplicate_result()
        self.assertEqual(_v2_note(pool[0])['strategy_matches'], [{'strategy_id': 'balanced', 'page': 0, 'rank': 1}, {'strategy_id': 'title_recall', 'page': 0, 'rank': 1}])

    def test_existing_pool_candidate_is_not_fetched_again_and_provenance_survives(self):
        note = {'pool_version': 2, 'matched_strategies': ['balanced'], 'strategy_matches': [{'strategy_id': 'balanced', 'page': 0, 'rank': 1}], 'signals': {}, 'leadership': {}}
        existing_resume = _resume()
        existing = [('r1', existing_resume, [note])]
        pages = {('"titles"', 0): {'items': [{'id': 'r1', 'area': {'id': '1'}}], 'pages': 1}}
        plan = _plan([_strategy('title_recall', '"titles"')])
        pool, _, _, _, fetch = _call_pool(plan=plan, search_pages=pages, resumes={'r1': existing_resume}, existing_pool=existing)
        fetch.assert_not_called()
        self.assertIsNot(pool, existing)
        provenance = _v2_note(pool[0])
        self.assertEqual(provenance['matched_strategies'], ['balanced', 'title_recall'])

class CandidatePoolV2BroadRecallTests(unittest.TestCase):

    def assert_retained(self, resume, requirements=None, profile=None):
        pages = {('"balanced"', 0): {'items': [{'id': resume['id'], 'area': {'id': resume['area']['id']}}], 'pages': 1}}
        plan = _plan([_strategy('balanced', '"balanced"')])
        pool, _, exclusions, _, _ = _call_pool(plan=plan, search_pages=pages, resumes={resume['id']: resume}, requirements=requirements, profile=profile)
        self.assertEqual([candidate[0] for candidate in pool], [resume['id']])
        self.assertNotIn(resume['id'], exclusions)

    def test_title_and_adjacent_role_mismatch_are_retained(self):
        self.assert_retained(_resume(title='Business Analyst'))
        self.assert_retained(_resume(resume_id='r2', title='Integration Consultant'))

    def test_professional_mismatch_is_retained(self):
        self.assert_retained(_resume(title='Head of Analytics', description='BI reporting'))

    def test_experience_shortfall_is_retained(self):
        self.assert_retained(_resume(months=12))

    def test_city_mismatch_after_loading_is_retained(self):
        self.assert_retained(_resume(area_id='2'))

    def test_technical_lead_and_ambiguous_leadership_are_retained(self):
        for index, title in enumerate(('Technical Lead', 'Team Lead', 'Ведущий специалист', 'Project Manager'), start=1):
            with self.subTest(title=title):
                self.assert_retained(_resume(resume_id=f'r{index}', title=title))

class CandidatePoolV2LeadershipTests(unittest.TestCase):

    def test_one_management_evidence_category_is_insufficient(self):
        resume = _resume(title='Head of Engineering', description='10 direct reports and 12 direct reports')
        CandidatePoolV2BroadRecallTests.assert_retained(self, resume)

    def test_two_independent_management_categories_may_exclude_people_manager(self):
        resume = _resume(title='Head of Engineering', description='10 direct reports. Responsible for hiring and dismissal decisions.')
        pages = {('"balanced"', 0): {'items': [{'id': 'r1', 'area': {'id': '1'}}], 'pages': 1}}
        plan = _plan([_strategy('balanced', '"balanced"')])
        pool, _, exclusions, _, _ = _call_pool(plan=plan, search_pages=pages, resumes={'r1': resume})
        self.assertEqual(pool, [])
        self.assertEqual(exclusions['r1']['candidate_level'], 'people_manager')
        self.assertEqual(exclusions['r1']['confidence'], 0.8)
        self.assertEqual(set(exclusions['r1']['evidence_categories']), {'direct_reports', 'hiring_or_dismissal'})

    def test_two_categories_without_leadership_title_may_exclude_people_manager(self):
        resume = _resume(title='Software Engineer', description='10 direct reports. Responsible for hiring and dismissal decisions.')
        pages = {('"balanced"', 0): {'items': [{'id': 'r1', 'area': {'id': '1'}}], 'pages': 1}}
        pool, _, exclusions, _, _ = _call_pool(plan=_plan([_strategy('balanced', '"balanced"')]), search_pages=pages, resumes={'r1': resume})
        self.assertEqual(pool, [])
        self.assertEqual(exclusions['r1']['candidate_level'], 'people_manager')

    def test_three_independent_management_categories_have_point_nine_confidence(self):
        resume = _resume(title='Engineering Manager', description='10 direct reports. Responsible for hiring and dismissal decisions. Conducted annual performance reviews.')
        pages = {('"balanced"', 0): {'items': [{'id': 'r1', 'area': {'id': '1'}}], 'pages': 1}}
        pool, _, exclusions, _, _ = _call_pool(plan=_plan([_strategy('balanced', '"balanced"')]), search_pages=pages, resumes={'r1': resume})
        self.assertEqual(pool, [])
        self.assertEqual(exclusions['r1']['candidate_level'], 'people_manager')
        self.assertEqual(exclusions['r1']['confidence'], 0.9)
        self.assertEqual(set(exclusions['r1']['evidence_categories']), {'direct_reports', 'hiring_or_dismissal', 'performance_review'})

    def test_unknown_leadership_is_retained_with_review_hint(self):
        resume = _resume(title='Lead Engineer', description='Координировал подрядчиков и менторил сотрудников')
        pages = {('"balanced"', 0): {'items': [{'id': 'r1', 'area': {'id': '1'}}], 'pages': 1}}
        pool, _, _, _, _ = _call_pool(plan=_plan([_strategy('balanced', '"balanced"')]), search_pages=pages, resumes={'r1': resume})
        note = _v2_note(pool[0])
        self.assertEqual(note['leadership']['level'], 'unknown')
        self.assertTrue(note['leadership'].get('review_hint'))

    def test_leadership_vacancy_retains_individual_contributor(self):
        profile = _profile('leadership')
        CandidatePoolV2BroadRecallTests.assert_retained(self, _resume(title='Solution Architect'), requirements=_requirements('leadership'), profile=profile)

class CandidatePoolV2FailureCheckpointAndBoundsTests(unittest.TestCase):

    def test_generic_load_failure_is_metric_not_rejection_and_discovery_continues(self):
        items = [{'id': 'broken', 'area': {'id': '1'}}, {'id': 'good', 'area': {'id': '1'}}]
        pages = {('"balanced"', 0): {'items': items, 'pages': 1}}
        resumes = {'broken': RuntimeError('load failed'), 'good': _resume('good')}
        pool, metrics, exclusions, _, _ = _call_pool(plan=_plan([_strategy('balanced', '"balanced"')]), search_pages=pages, resumes=resumes)
        self.assertEqual([candidate[0] for candidate in pool], ['good'])
        self.assertEqual(metrics['load_failed'], 1)
        self.assertNotIn('broken', exclusions)

    def test_search_execution_is_bounded_by_strategies_and_page_limit(self):
        plan = _plan()
        _, metrics, _, search, _ = _call_pool(plan=plan)
        self.assertLessEqual(search.call_count, len(STRATEGY_ORDER) * service.SEARCH_MAX_PAGES)
        self.assertLessEqual(metrics['scanned'], service.SEARCH_SCAN_LIMIT)

    def test_target_pool_stops_discovery(self):
        items = [{'id': f'r{index}', 'area': {'id': '1'}} for index in range(1, 6)]
        pages = {('"balanced"', 0): {'items': items, 'pages': 1}}
        resumes = {item['id']: _resume(item['id']) for item in items}
        pool, _, _, _, fetch = _call_pool(plan=_plan([_strategy('balanced', '"balanced"')]), search_pages=pages, resumes=resumes, target_pool=2)
        self.assertEqual(len(pool), 2)
        self.assertEqual(fetch.call_count, 2)

    def test_checkpoint_receives_strategy_progress_and_resume_skips_completed_work(self):
        snapshots = []

        def checkpoint(candidates, metrics, exclusions):
            snapshots.append(copy.deepcopy(metrics))
        first_plan = _plan([_strategy('balanced', '"balanced"')])
        first_pages = {('"balanced"', 0): {'items': [{'id': 'r1', 'area': {'id': '1'}}], 'pages': 1}}
        first_plan_before = copy.deepcopy(first_plan)
        pool, metrics, _, _, _ = _call_pool(plan=first_plan, search_pages=first_pages, resumes={'r1': _resume()}, checkpoint=checkpoint)
        self.assertEqual([candidate[0] for candidate in pool], ['r1'])
        self.assertTrue(snapshots)
        state = snapshots[-1]['pool_v2']
        self.assertEqual(state['strategy_order'], STRATEGY_ORDER)
        self.assertIn('balanced', state['completed_strategies'])
        self.assertIn('next_page_by_strategy', state)
        self.assertEqual(_v2_note(pool[0])['matched_strategies'], ['balanced'])
        self.assertEqual(first_plan, first_plan_before)
        resumed_plan = _plan([_strategy('balanced', '"balanced"'), _strategy('title_recall', '"titles"')])
        resumed_pages = {('"titles"', 0): {'items': [{'id': 'r1', 'area': {'id': '1'}}], 'pages': 1}}
        resumed_plan_before = copy.deepcopy(resumed_plan)
        existing_pool_before = copy.deepcopy(pool)
        resumed_metrics = copy.deepcopy(metrics)
        resumed_metrics_before = copy.deepcopy(resumed_metrics)
        self.assertEqual(resumed_metrics, resumed_metrics_before)
        resumed, _, _, search, fetch = _call_pool(plan=resumed_plan, search_pages=resumed_pages, resumes={'r1': _resume()}, existing_pool=pool, metrics=resumed_metrics)
        searched_queries = [call.args[0]['text'] for call in search.call_args_list]
        self.assertEqual(searched_queries, ['"titles"'])
        fetch.assert_not_called()
        self.assertEqual([candidate[0] for candidate in resumed], ['r1'])
        provenance = _v2_note(resumed[0])
        self.assertEqual(provenance['matched_strategies'], ['balanced', 'title_recall'])
        strategy_matches = provenance['strategy_matches']
        self.assertEqual(sum((item['strategy_id'] == 'balanced' for item in strategy_matches)), 1)
        self.assertEqual(sum((item['strategy_id'] == 'title_recall' for item in strategy_matches)), 1)
        self.assertEqual(first_plan, first_plan_before)
        self.assertEqual(resumed_plan, resumed_plan_before)
        self.assertEqual(pool, existing_pool_before)
        self.assertIsNot(resumed, pool)

"""RED contracts for Query Generation V3. No external I/O at import time."""
from __future__ import annotations
import ast
import copy
import importlib
import inspect
import json
import re
import unittest
from unittest.mock import Mock, patch

def _optional_import(module_name):
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing_name = exc.name or ''
        expected_missing = missing_name == module_name or module_name.startswith(missing_name + '.')
        if expected_missing:
            return None
        raise

def _require_callable(module, module_name, name):
    if module is None:
        raise AssertionError(f'Required V3 module is missing: {module_name}')
    value = getattr(module, name, None)
    if not callable(value):
        raise AssertionError(f'Required V3 API is missing: {name}')
    return value

def _require_value(module, module_name, name):
    if module is None:
        raise AssertionError(f'Required V3 module is missing: {module_name}')
    if not hasattr(module, name):
        raise AssertionError(f'Required V3 constant is missing: {name}')
    return getattr(module, name)
DOMAIN_MODULE = 'app.domain.query_generation_v3'
VALIDATOR_MODULE = 'app.services.query_interpretation_validator'
RENDERER_MODULE = 'app.services.query_renderer'
INTERPRETER_MODULE = 'app.integrations.ai.query_interpreter'
v3_domain = _optional_import(DOMAIN_MODULE)
v3_validator = _optional_import(VALIDATOR_MODULE)
v3_renderer = _optional_import(RENDERER_MODULE)
v3_interpreter = _optional_import(INTERPRETER_MODULE)
nano = _optional_import('app.integrations.ai.nano_client')
errors = _optional_import('app.core.errors')
recruitment_data = _optional_import('app.services.recruitment_data')
V3_TOP_LEVEL_FIELDS = {'schema_version', 'generation_basis', 'vacancy_understanding', 'functions', 'role_titles', 'leadership_intent', 'unsupported_terms', 'warnings'}
V3_UNDERSTANDING_FIELDS = {'role_family', 'specialization', 'summary', 'confidence'}
V3_FUNCTION_FIELDS_EXPECTED = {'function_id', 'normalized_function', 'importance', 'market_terms', 'evidence'}
V3_MARKET_TERM_FIELDS_EXPECTED = {'value', 'language', 'derivation'}
V3_ROLE_TITLE_FIELDS_EXPECTED = {'canonical_id', 'title', 'language', 'priority', 'derived_from_functions', 'evidence'}
V3_RENDERED_PLAN_FIELDS = {'schema_version', 'generation_basis', 'strategies', 'selected_strategy'}
V3_STRATEGY_FIELDS_EXPECTED = {'id', 'kind', 'query', 'executable_now', 'blocked_by'}
V1_PROFILE_KEYS = {'role_family', 'target_functions', 'accepted_role_titles', 'mandatory_search_concepts', 'domain_context', 'preferred_concepts', 'excluded_role_families', 'excluded_titles', 'leadership_intent', 'rationale'}
ACCEPTANCE_VACANCY = {'name': 'Solution архитектор', 'area': {'name': 'Алматы'}, 'description': 'Проработка вариантов архитектурных решений, оценка и выбор итогового решения, документирование архитектурного дизайна. Impact analysis на инфраструктуру и информационные системы. Функциональные и нефункциональные требования. Проектирование систем с большим количеством интеграций. UML, C4, BPMN 2.0, СУБД.', 'key_skills': [{'name': value} for value in ('UML', 'C4', 'BPMN 2.0', 'СУБД')]}
EXPECTED_BALANCED_QUERY = '("Solution Architect" OR "Архитектор решений" OR "Integration Architect" OR "Архитектор интеграций") AND ("solution architecture" OR "архитектура решений" OR "проектирование решений" OR "integration architecture" OR "интеграционная архитектура" OR "impact analysis" OR "анализ влияния")'
EXPECTED_PRECISION_QUERY = '("Solution Architect" OR "Архитектор решений") AND ("solution architecture" OR "архитектура решений" OR "проектирование решений") AND ("integration architecture" OR "интеграционная архитектура" OR "impact analysis" OR "анализ влияния")'

def _term(value, language, derivation):
    return {'value': value, 'language': language, 'derivation': derivation}

def _function(function_id, normalized_function, terms, evidence, importance='primary'):
    return {'function_id': function_id, 'normalized_function': normalized_function, 'importance': importance, 'market_terms': terms, 'evidence': evidence}

def _role(canonical_id, title, language, priority, function_ids, evidence):
    return {'canonical_id': canonical_id, 'title': title, 'language': language, 'priority': priority, 'derived_from_functions': function_ids, 'evidence': evidence}

def _acceptance_interpretation():
    return {'schema_version': 3, 'generation_basis': 'semantic_functions_first', 'vacancy_understanding': {'role_family': 'solution_architecture', 'specialization': 'integration_architecture', 'summary': 'Проектирование архитектурных решений, интеграций и анализ влияния', 'confidence': 0.95}, 'functions': [_function('solution_design', 'проектирование архитектуры решений', [_term('solution architecture', 'en', 'market_equivalent'), _term('архитектура решений', 'ru', 'semantic_normalization'), _term('проектирование решений', 'ru', 'semantic_normalization')], ['Проработка вариантов архитектурных решений', 'Документирование архитектурного дизайна']), _function('integration_design', 'проектирование интеграций', [_term('integration architecture', 'en', 'market_equivalent'), _term('интеграционная архитектура', 'ru', 'semantic_normalization')], ['Проектирование систем с большим количеством интеграций']), _function('impact_analysis', 'анализ влияния', [_term('impact analysis', 'en', 'translation_equivalent'), _term('анализ влияния', 'ru', 'semantic_normalization')], ['Impact analysis на инфраструктуру и информационные системы'])], 'role_titles': [_role('solution_architect_en', 'Solution Architect', 'en', 'strong', ['solution_design'], ['Проработка архитектурных решений']), _role('solution_architect_ru', 'Архитектор решений', 'ru', 'strong', ['solution_design'], ['Проработка архитектурных решений']), _role('integration_architect_en', 'Integration Architect', 'en', 'conditional', ['integration_design'], ['Проектирование интеграций']), _role('integration_architect_ru', 'Архитектор интеграций', 'ru', 'conditional', ['integration_design'], ['Проектирование интеграций'])], 'leadership_intent': {'level': 'individual_contributor', 'evidence': ['Управление людьми не указано']}, 'unsupported_terms': [{'term': 'Kubernetes', 'reason': 'Не подтверждено вакансией'}], 'warnings': []}

def _validator():
    return _require_callable(v3_validator, VALIDATOR_MODULE, 'validate_search_interpretation_v3')

def _renderer():
    return _require_callable(v3_renderer, RENDERER_MODULE, 'render_search_strategies_v3')

def _projector():
    return _require_callable(v3_renderer, RENDERER_MODULE, 'build_pool_profile_v1_from_v3')

def _generator():
    return _require_callable(v3_interpreter, INTERPRETER_MODULE, 'generate_vacancy_search_plan_v3')

def _rendered_plan():
    return _renderer()(_acceptance_interpretation())

def _strategy(plan, strategy_id):
    return next((item for item in plan['strategies'] if item['id'] == strategy_id))

def _representative_v2_plan():
    balanced = '("Solution Architect" OR "Архитектор решений") AND ("solution architecture" OR "integration architecture")'
    return {'schema_version': 2, 'generation_basis': 'functions_first', 'vacancy_understanding': {'role_family': 'solution_architecture', 'specialization': 'integration_architecture', 'summary': 'Архитектура решений', 'confidence': 0.9}, 'functional_core': [{'function_id': 'solution_design', 'normalized_function': 'проектирование решений', 'search_terms': ['solution architecture'], 'evidence': ['Архитектурные решения']}, {'function_id': 'integration_design', 'normalized_function': 'проектирование интеграций', 'search_terms': ['integration architecture'], 'evidence': ['Интеграции']}], 'role_titles': {'strong': [{'title': 'Solution Architect', 'derived_from_functions': ['solution_design'], 'evidence': ['Архитектурные решения']}], 'conditional': [{'title': 'Архитектор решений', 'derived_from_functions': ['solution_design'], 'evidence': ['Архитектурные решения']}]}, 'leadership_intent': {'level': 'individual_contributor', 'evidence': ['Нет управления']}, 'strategies': [{'id': 'balanced', 'kind': 'title_and_function', 'query': balanced, 'executable_now': True, 'blocked_by': None}, {'id': 'precision', 'kind': 'title_and_multiple_functions', 'query': '("Solution Architect") AND ("solution architecture") AND ("integration architecture")', 'executable_now': True, 'blocked_by': None}, {'id': 'title_recall', 'kind': 'title_only', 'query': '("Solution Architect" OR "Архитектор решений")', 'executable_now': True, 'blocked_by': None}, {'id': 'functional_recall', 'kind': 'function_only', 'query': '("solution architecture" OR "integration architecture")', 'executable_now': False, 'blocked_by': 'pool_v1_requires_leading_title_group'}], 'selected_strategy': 'balanced', 'unsupported_terms': [{'term': 'Kubernetes', 'reason': 'Не подтверждено'}], 'warnings': []}

def _assert_invalid_cases(testcase, api, cases):
    if not callable(api):
        raise AssertionError('Required V3 API is not callable')
    for label, value, vacancy in cases:
        with testcase.assertRaises((ValueError, TypeError), msg=label):
            api(value, vacancy)

def _quoted_phrases(query):
    return re.findall('"([^"\\r\\n]+)"', query)

def _assert_rendered_plan_contract(testcase, plan):
    testcase.assertEqual(set(plan), V3_RENDERED_PLAN_FIELDS)
    testcase.assertEqual(plan['schema_version'], 3)
    testcase.assertEqual(plan['generation_basis'], 'deterministic_renderer_v3')
    testcase.assertEqual(plan['selected_strategy'], 'balanced')
    metadata = {'balanced': ('title_and_function', True, None), 'precision': ('title_and_multiple_functions', True, None), 'title_recall': ('title_only', True, None), 'functional_recall': ('function_only', False, 'pool_v1_requires_leading_title_group')}
    testcase.assertEqual({item['id'] for item in plan['strategies']}, set(metadata))
    testcase.assertEqual(len(plan['strategies']), 4)
    for strategy in plan['strategies']:
        testcase.assertEqual(set(strategy), V3_STRATEGY_FIELDS_EXPECTED)
        testcase.assertEqual((strategy['kind'], strategy['executable_now'], strategy['blocked_by']), metadata[strategy['id']])
        testcase.assertNotIn('\n', strategy['query'])
        testcase.assertNotIn('\r', strategy['query'])
        testcase.assertNotRegex(strategy['query'], '(?<!\\w)NOT(?!\\w)')
        for forbidden in ('Алматы', 'Example Employer', '500000'):
            testcase.assertNotIn(forbidden, strategy['query'])
    testcase.assertEqual(_strategy(plan, 'balanced')['query'].count(' AND '), 1)

class QueryGenerationV3ApiTests(unittest.TestCase):

    def test_v3_validator_api_is_required(self):
        self.assertTrue(callable(_validator()))
        self.assertEqual(_require_value(v3_domain, DOMAIN_MODULE, 'V3_SCHEMA_VERSION'), 3)
        self.assertEqual(_require_value(v3_domain, DOMAIN_MODULE, 'V3_GENERATION_BASIS'), 'semantic_functions_first')

    def test_v3_renderer_api_is_required(self):
        self.assertTrue(callable(_renderer()))
        self.assertEqual(set(_require_value(v3_domain, DOMAIN_MODULE, 'V3_STRATEGY_FIELDS')), V3_STRATEGY_FIELDS_EXPECTED)

    def test_v3_generator_api_is_required(self):
        self.assertTrue(callable(_generator()))
        self.assertEqual(set(_require_value(v3_domain, DOMAIN_MODULE, 'V3_INTERPRETATION_FIELDS')), V3_TOP_LEVEL_FIELDS)

    def test_v3_profile_projection_api_is_required(self):
        self.assertTrue(callable(_projector()))
        self.assertEqual(set(_require_value(v3_domain, DOMAIN_MODULE, 'V3_FUNCTION_FIELDS')), V3_FUNCTION_FIELDS_EXPECTED)
        self.assertEqual(set(_require_value(v3_domain, DOMAIN_MODULE, 'V3_MARKET_TERM_FIELDS')), V3_MARKET_TERM_FIELDS_EXPECTED)
        self.assertEqual(set(_require_value(v3_domain, DOMAIN_MODULE, 'V3_ROLE_TITLE_FIELDS')), V3_ROLE_TITLE_FIELDS_EXPECTED)

class QueryGenerationV3SchemaTests(unittest.TestCase):

    def test_valid_v3_interpretation_is_accepted(self):
        original = _acceptance_interpretation()
        vacancy = copy.deepcopy(ACCEPTANCE_VACANCY)
        before = copy.deepcopy(original)
        vacancy_before = copy.deepcopy(vacancy)
        validated = _validator()(original, vacancy)
        self.assertEqual(validated['schema_version'], 3)
        self.assertEqual(original, before)
        self.assertEqual(vacancy, vacancy_before)

    def test_v3_requires_exact_top_level_fields(self):
        validator = _validator()
        cases = []
        mutations = [('root missing', lambda plan: plan.pop('warnings')), ('root extra', lambda plan: plan.update(extra=True)), ('understanding missing', lambda plan: plan['vacancy_understanding'].pop('summary')), ('understanding extra', lambda plan: plan['vacancy_understanding'].update(extra=True)), ('function missing', lambda plan: plan['functions'][0].pop('importance')), ('function extra', lambda plan: plan['functions'][0].update(extra=True)), ('term missing', lambda plan: plan['functions'][0]['market_terms'][0].pop('language')), ('term extra', lambda plan: plan['functions'][0]['market_terms'][0].update(extra=True)), ('title missing', lambda plan: plan['role_titles'][0].pop('priority')), ('title extra', lambda plan: plan['role_titles'][0].update(extra=True)), ('leadership missing', lambda plan: plan['leadership_intent'].pop('evidence')), ('leadership extra', lambda plan: plan['leadership_intent'].update(extra=True)), ('unsupported missing', lambda plan: plan['unsupported_terms'][0].pop('reason')), ('unsupported extra', lambda plan: plan['unsupported_terms'][0].update(extra=True))]
        for label, mutation in mutations:
            plan = _acceptance_interpretation()
            mutation(plan)
            cases.append((label, plan, copy.deepcopy(ACCEPTANCE_VACANCY)))
        _assert_invalid_cases(self, validator, cases)

    def test_v3_rejects_wrong_schema_version(self):
        validator = _validator()
        cases = []
        for label, version, confidence in (('wrong version', 2, 0.95), ('boolean version', True, 0.95), ('boolean confidence', 3, True)):
            plan = _acceptance_interpretation()
            plan['schema_version'] = version
            plan['vacancy_understanding']['confidence'] = confidence
            cases.append((label, plan, copy.deepcopy(ACCEPTANCE_VACANCY)))
        _assert_invalid_cases(self, validator, cases)

    def test_v3_rejects_wrong_generation_basis(self):
        plan = _acceptance_interpretation()
        plan['generation_basis'] = 'functions_first'
        with self.assertRaises((ValueError, TypeError)):
            _validator()(plan, ACCEPTANCE_VACANCY)

    def test_v3_rejects_duplicate_function_id(self):
        plan = _acceptance_interpretation()
        plan['functions'].append(copy.deepcopy(plan['functions'][0]))
        with self.assertRaises((ValueError, TypeError)):
            _validator()(plan, ACCEPTANCE_VACANCY)

    def test_v3_rejects_unknown_function_reference(self):
        plan = _acceptance_interpretation()
        plan['role_titles'][0]['derived_from_functions'] = ['unknown_function']
        with self.assertRaises((ValueError, TypeError)):
            _validator()(plan, ACCEPTANCE_VACANCY)

    def test_v3_rejects_empty_evidence(self):
        plan = _acceptance_interpretation()
        plan['functions'][0]['evidence'] = []
        with self.assertRaises((ValueError, TypeError)):
            _validator()(plan, ACCEPTANCE_VACANCY)

    def test_v3_rejects_unknown_derivation(self):
        plan = _acceptance_interpretation()
        plan['functions'][0]['market_terms'][0]['derivation'] = 'invented'
        with self.assertRaises((ValueError, TypeError)):
            _validator()(plan, ACCEPTANCE_VACANCY)

class QueryGenerationV3GroundedInferenceTests(unittest.TestCase):

    def test_market_term_may_differ_from_vacancy_evidence(self):
        validated = _validator()(_acceptance_interpretation(), ACCEPTANCE_VACANCY)
        term = validated['functions'][2]['market_terms'][0]
        self.assertEqual(term['value'], 'impact analysis')
        self.assertEqual(term['derivation'], 'translation_equivalent')

    def test_inferred_term_requires_function_evidence(self):
        plan = _acceptance_interpretation()
        plan['functions'][2]['evidence'] = []
        with self.assertRaises((ValueError, TypeError)):
            _validator()(plan, ACCEPTANCE_VACANCY)

    def test_unsupported_technology_is_rejected(self):
        for target in ('market_terms', 'role_titles'):
            plan = _acceptance_interpretation()
            if target == 'market_terms':
                plan['functions'][0]['market_terms'].append(_term('Kubernetes', 'en', 'literal'))
            else:
                plan['role_titles'].append(_role('kubernetes_architect', 'Kubernetes', 'en', 'conditional', ['solution_design'], ['Архитектурные решения']))
            with self.assertRaises((ValueError, TypeError), msg=target):
                _validator()(plan, ACCEPTANCE_VACANCY)

    def test_long_vacancy_sentence_is_rejected_as_market_term(self):
        plan = _acceptance_interpretation()
        plan['functions'][0]['market_terms'].append(_term('Проектирование систем с большим количеством интеграций и документирование итогового архитектурного решения.', 'ru', 'literal'))
        with self.assertRaises((ValueError, TypeError)):
            _validator()(plan, ACCEPTANCE_VACANCY)

    def test_generic_unspecialized_market_term_is_rejected(self):
        for value in ('infrastructure', 'information technology', 'business', 'customer', 'system'):
            plan = _acceptance_interpretation()
            plan['functions'][0]['market_terms'].append(_term(value, 'en', 'market_equivalent'))
            with self.assertRaises((ValueError, TypeError), msg=value):
                _validator()(plan, ACCEPTANCE_VACANCY)

class QueryGenerationV3RendererTests(unittest.TestCase):

    def test_balanced_query_is_rendered_exactly(self):
        plan = _rendered_plan()
        _assert_rendered_plan_contract(self, plan)
        self.assertEqual(_strategy(plan, 'balanced')['query'], EXPECTED_BALANCED_QUERY)

    def test_balanced_uses_all_primary_function_terms(self):
        query = _strategy(_rendered_plan(), 'balanced')['query']
        for value in ('solution architecture', 'архитектура решений', 'проектирование решений', 'integration architecture', 'интеграционная архитектура', 'impact analysis', 'анализ влияния'):
            self.assertIn(f'"{value}"', query)

    def test_balanced_includes_conditional_title_for_primary_function(self):
        query = _strategy(_rendered_plan(), 'balanced')['query']
        self.assertIn('"Integration Architect"', query)
        self.assertIn('"Архитектор интеграций"', query)

    def test_balanced_excludes_conditional_title_for_secondary_function(self):
        plan = _acceptance_interpretation()
        plan['functions'][1]['importance'] = 'secondary'
        query = _strategy(_renderer()(plan), 'balanced')['query']
        self.assertNotIn('"Integration Architect"', query)
        self.assertNotIn('"Архитектор интеграций"', query)

    def test_title_recall_contains_approved_titles_only(self):
        query = _strategy(_rendered_plan(), 'title_recall')['query']
        self.assertNotIn(' AND ', query)
        for title in ('Solution Architect', 'Архитектор решений', 'Integration Architect', 'Архитектор интеграций'):
            self.assertIn(f'"{title}"', query)
        self.assertNotIn('"solution architecture"', query)

    def test_functional_recall_contains_no_role_titles(self):
        strategy = _strategy(_rendered_plan(), 'functional_recall')
        query = strategy['query']
        for title in ('Solution Architect', 'Архитектор решений', 'Integration Architect', 'Архитектор интеграций'):
            self.assertNotIn(f'"{title}"', query)
        self.assertNotIn(' AND ', query)
        self.assertFalse(strategy['executable_now'])
        self.assertEqual(strategy['blocked_by'], 'pool_v1_requires_leading_title_group')

    def test_renderer_is_deterministic(self):
        renderer = _renderer()
        first = renderer(_acceptance_interpretation())
        second = renderer(copy.deepcopy(_acceptance_interpretation()))
        self.assertEqual(first, second)
        self.assertEqual(_strategy(first, 'precision')['query'], EXPECTED_PRECISION_QUERY)

    def test_renderer_does_not_mutate_input(self):
        interpretation = _acceptance_interpretation()
        before = copy.deepcopy(interpretation)
        rendered = _renderer()(interpretation)
        self.assertEqual(interpretation, before)
        rendered['strategies'][0]['query'] = 'changed'
        self.assertEqual(interpretation, before)

    def test_renderer_deduplicates_canonical_variants(self):
        interpretation = _acceptance_interpretation()
        interpretation['functions'][0]['market_terms'].extend([_term('solution–design', 'en', 'market_equivalent'), _term('solution_design', 'en', 'market_equivalent'), _term('solution/design', 'en', 'market_equivalent')])
        interpretation['role_titles'].extend([_role('integration_architect_dash', 'Интеграционный–архитектор', 'ru', 'conditional', ['integration_design'], ['Проектирование интеграций']), _role('integration_architect_hyphen', 'Интеграционный-архитектор', 'ru', 'conditional', ['integration_design'], ['Проектирование интеграций'])])
        first = _renderer()(interpretation)
        second = _renderer()(copy.deepcopy(interpretation))
        self.assertEqual(first, second)
        balanced = _quoted_phrases(_strategy(first, 'balanced')['query'])
        self.assertIn('solution–design', balanced)
        self.assertNotIn('solution_design', balanced)
        self.assertNotIn('solution/design', balanced)
        self.assertEqual(balanced.count('solution–design'), 1)
        self.assertIn('Интеграционный–архитектор', balanced)
        self.assertNotIn('Интеграционный-архитектор', balanced)
        self.assertLess(balanced.index('solution architecture'), balanced.index('solution–design'))

    def test_renderer_does_not_call_ai_or_network(self):
        renderer = _renderer()
        with patch('socket.create_connection', side_effect=AssertionError('network called')), patch('socket.socket.connect', side_effect=AssertionError('network called')):
            if v3_interpreter is not None and hasattr(v3_interpreter, 'ask'):
                with patch.object(v3_interpreter, 'ask', side_effect=AssertionError('AI called')):
                    rendered = renderer(_acceptance_interpretation())
            else:
                rendered = renderer(_acceptance_interpretation())
        self.assertEqual(rendered['selected_strategy'], 'balanced')

class QueryGenerationV3GeneratorTests(unittest.TestCase):

    def test_v3_generator_uses_one_ai_call(self):
        generator = _generator()
        module = v3_interpreter
        fake_response = json.dumps(_acceptance_interpretation(), ensure_ascii=False)
        boundaries = ('requests', 'search_resumes', 'set_review', 'run_hh', 'send_candidate_email')
        blockers = [patch.object(module, name, side_effect=AssertionError(f'forbidden side effect: {name}'), create=True) for name in boundaries]
        active = [item.start() for item in blockers]
        try:
            with patch.object(module, 'ask', return_value=fake_response, create=True) as ask, patch.object(module, 'prepare_vacancy', return_value=copy.deepcopy(ACCEPTANCE_VACANCY), create=True), patch.object(module, 'validate_search_interpretation_v3', return_value=_acceptance_interpretation(), create=True), patch.object(module, 'render_search_strategies_v3', return_value={'strategies': [{'id': 'balanced', 'query': EXPECTED_BALANCED_QUERY}], 'selected_strategy': 'balanced'}, create=True), patch.object(module, 'build_pool_profile_v1_from_v3', return_value={key: [] for key in V1_PROFILE_KEYS}, create=True):
                generator(copy.deepcopy(ACCEPTANCE_VACANCY))
        finally:
            for item in reversed(blockers):
                item.stop()
        for blocker in active:
            blocker.assert_not_called()
        ask.assert_called_once()
        source = inspect.getsource(generator)
        tree = ast.parse(source)
        direct = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and (node.func.id == 'ask')]
        self.assertEqual(len(direct), 1)

    def test_v3_generator_validates_before_rendering(self):
        generator = _generator()
        module = v3_interpreter
        events = []

        def validate(value, vacancy):
            events.append('validate')
            return value

        def render(value):
            events.append('render')
            return {'strategies': [{'id': 'balanced', 'query': EXPECTED_BALANCED_QUERY}], 'selected_strategy': 'balanced'}
        with patch.object(module, 'ask', return_value=json.dumps(_acceptance_interpretation(), ensure_ascii=False), create=True), patch.object(module, 'prepare_vacancy', return_value=copy.deepcopy(ACCEPTANCE_VACANCY), create=True), patch.object(module, 'validate_search_interpretation_v3', side_effect=validate, create=True), patch.object(module, 'render_search_strategies_v3', side_effect=render, create=True), patch.object(module, 'build_pool_profile_v1_from_v3', return_value={key: [] for key in V1_PROFILE_KEYS}, create=True):
            generator(copy.deepcopy(ACCEPTANCE_VACANCY))
        self.assertEqual(events, ['validate', 'render'])
        error_type = _require_value(errors, 'app.core.errors', 'AIUnavailableError')
        forbidden_payloads = (('query', {'query': 'forbidden'}), ('strategies', {'strategies': []}), ('selected_strategy', {'selected_strategy': 'balanced'}))
        for label, extra in forbidden_payloads:
            payload = _acceptance_interpretation()
            payload.update(extra)
            vacancy = copy.deepcopy(ACCEPTANCE_VACANCY)
            vacancy_before = copy.deepcopy(vacancy)
            with patch.object(module, 'ask', return_value=json.dumps(payload, ensure_ascii=False), create=True) as ask, patch.object(module, 'prepare_vacancy', return_value=copy.deepcopy(vacancy), create=True), patch.object(module, 'validate_search_interpretation_v3', side_effect=ValueError(label), create=True), patch.object(module, 'render_search_strategies_v3', create=True) as renderer, patch.object(module, 'build_pool_profile_v1_from_v3', create=True) as projector:
                with self.assertRaises(error_type, msg=label) as caught:
                    generator(vacancy)
            self.assertTrue(caught.exception.request_sent)
            self.assertEqual(vacancy, vacancy_before)
            ask.assert_called_once()
            renderer.assert_not_called()
            projector.assert_not_called()

    def test_v3_generator_returns_interpretation_plan_query_profile(self):
        generator = _generator()
        vacancy = copy.deepcopy(ACCEPTANCE_VACANCY)
        before = copy.deepcopy(vacancy)
        plan = {'schema_version': 3, 'generation_basis': 'deterministic_renderer_v3', 'strategies': [{'id': 'balanced', 'kind': 'title_and_function', 'query': EXPECTED_BALANCED_QUERY, 'executable_now': True, 'blocked_by': None}], 'selected_strategy': 'balanced'}
        profile = {key: [] for key in V1_PROFILE_KEYS}
        with patch.object(v3_interpreter, 'ask', return_value=json.dumps(_acceptance_interpretation(), ensure_ascii=False), create=True) as ask, patch.object(v3_interpreter, 'prepare_vacancy', return_value=copy.deepcopy(vacancy), create=True), patch.object(v3_interpreter, 'validate_search_interpretation_v3', return_value=_acceptance_interpretation(), create=True), patch.object(v3_interpreter, 'render_search_strategies_v3', return_value=plan, create=True) as render, patch.object(v3_interpreter, 'build_pool_profile_v1_from_v3', return_value=profile, create=True) as project:
            result = generator(vacancy)
        self.assertEqual(set(result), {'interpretation', 'plan', 'query', 'profile'})
        self.assertEqual(result['query'], EXPECTED_BALANCED_QUERY)
        self.assertEqual(vacancy, before)
        ask.assert_called_once()
        render.assert_called_once()
        project.assert_called_once()

class QueryGenerationV3CompatibilityTests(unittest.TestCase):

    def test_v2_public_apis_remain_available(self):
        if nano is None:
            raise AssertionError('Required compatibility module is missing: app.integrations.ai.nano_client')
        names = ('validate_vacancy_search_plan_v2', 'validate_search_strategy_v2', 'build_pool_profile_v1_from_v2', 'generate_vacancy_search_plan', 'generate_resume_language_query')
        for name in names:
            self.assertTrue(callable(getattr(nano, name, None)), name)
        representative = _representative_v2_plan()
        vacancy = {'name': 'Solution Architect', 'area': {'name': 'Алматы'}}
        validated = nano.validate_vacancy_search_plan_v2(copy.deepcopy(representative), vacancy)
        self.assertEqual(validated['schema_version'], 2)
        if v3_interpreter is None:
            raise AssertionError(f'Required V3 module is missing: {INTERPRETER_MODULE}')
        delegate = getattr(v3_interpreter, 'generate_vacancy_search_plan_v3', None)
        if not callable(delegate):
            raise AssertionError('Required V3 delegate is missing: generate_vacancy_search_plan_v3')
        v3_result = {'interpretation': _acceptance_interpretation(), 'plan': {'schema_version': 3}, 'query': EXPECTED_BALANCED_QUERY, 'profile': {key: [] for key in V1_PROFILE_KEYS}}
        with patch.object(v3_interpreter, 'generate_vacancy_search_plan_v3', return_value=copy.deepcopy(v3_result)) as delegated:
            compatibility = nano.generate_vacancy_search_plan(copy.deepcopy(ACCEPTANCE_VACANCY))
        delegated.assert_called_once()
        self.assertEqual(set(compatibility), {'plan', 'query', 'profile'})
        self.assertNotIn('interpretation', compatibility)
        with patch.object(nano, 'generate_vacancy_search_plan', return_value=copy.deepcopy(compatibility)) as generate:
            query = nano.generate_resume_language_query(copy.deepcopy(ACCEPTANCE_VACANCY))
        generate.assert_called_once()
        self.assertEqual(query, compatibility['query'])

    def test_v3_profile_projection_preserves_v1_keys(self):
        interpretation = _acceptance_interpretation()
        rendered = _rendered_plan()
        before_interpretation = copy.deepcopy(interpretation)
        before_rendered = copy.deepcopy(rendered)
        profile = _projector()(interpretation, rendered)
        self.assertEqual(set(profile), V1_PROFILE_KEYS)
        self.assertEqual(profile['target_functions'], [item['normalized_function'] for item in interpretation['functions']])
        self.assertEqual(interpretation, before_interpretation)
        self.assertEqual(rendered, before_rendered)
        profile['accepted_role_titles'].append('changed')
        profile['mandatory_search_concepts'].append({'concept_id': 'changed', 'alternatives': []})
        self.assertEqual(interpretation, before_interpretation)
        self.assertEqual(rendered, before_rendered)

"""RED contracts for Query Generation V2. No external I/O at import time."""
import copy
import json
import unittest
from unittest.mock import patch
from app.integrations.ai import nano_client as nano
from app.persistence import app_state as state
from app.core.errors import AIUnavailableError
from app.services import recruitment_service as service
from app.integrations.xray import xray_plan

GOLDEN_BALANCED_QUERY = '("Solution Architect" OR "Solutions Architect" OR "Архитектор решений" OR "Архитектор информационных систем" OR "IT Architect" OR "Integration Architect" OR "Интеграционный архитектор") AND ("проектирование архитектуры" OR "архитектура решения" OR "solution architecture" OR "solution design" OR "проектирование интеграций" OR "integration architecture")'
GOLDEN_VACANCY = {
    "name": "Solution архитектор",
    "area": {"name": "Алматы"},
    "description": "Проработка вариантов архитектурных решений, оценка и выбор итогового решения, документирование архитектурного дизайна. Impact analysis на инфраструктуру и информационные системы. Функциональные и нефункциональные требования. Проектирование систем с большим количеством интеграций. UML, C4, BPMN 2.0, СУБД.",
    "key_skills": [{"name": x} for x in ("UML", "C4", "BPMN 2.0", "СУБД")],
}

def require_api(name):
    value = getattr(nano, name, None)
    if not callable(value):
        raise AssertionError(f"Missing Query Generation V2 API: {name}")
    return value

def function_item(fid, normalized, terms, evidence):
    return {"function_id": fid, "normalized_function": normalized, "search_terms": terms, "evidence": evidence}

def title_item(title, functions):
    return {"title": title, "derived_from_functions": functions, "evidence": ["Проработка и документирование архитектурных решений", "Проектирование систем с большим количеством интеграций"]}

def golden_plan():
    functions = [
        function_item("solution_design", "проектирование архитектурных решений", ["solution design", "solution architecture", "проектирование архитектуры", "архитектура решения"], ["проработка вариантов архитектурных решений", "документирование архитектурного дизайна"]),
        function_item("integration_design", "проектирование интеграций", ["integration architecture", "проектирование интеграций"], ["проектирование систем с большим количеством интеграций"]),
        function_item("impact_analysis", "анализ влияния", ["impact analysis", "анализ влияния", "архитектура инфраструктуры"], ["impact analysis на инфраструктуру и информационные системы"]),
    ]
    strong = [title_item("Solution Architect", ["solution_design"]), title_item("Архитектор решений", ["solution_design"]), title_item("Архитектор информационных систем", ["solution_design", "integration_design"])]
    conditional = [
        title_item("Solutions Architect", ["solution_design"]),
        title_item("IT Architect", ["solution_design"]),
        title_item("Integration Architect", ["integration_design"]),
        title_item("Интеграционный архитектор", ["integration_design"]),
        title_item("Application Architect", ["solution_design", "integration_design"]),
    ]
    strategies = [
        {"id":"balanced","kind":"title_and_function","query":GOLDEN_BALANCED_QUERY,"executable_now":True,"blocked_by":None},
        {"id":"precision","kind":"title_and_multiple_functions","query":'(“Solution Architect” OR “Архитектор решений”) AND (“solution design” OR “архитектура решения”) AND (“integration architecture” OR “impact analysis”)'.replace('“','"').replace('”','"'),"executable_now":True,"blocked_by":None},
        {"id":"title_recall","kind":"title_only","query":'(“Solution Architect” OR “Solutions Architect” OR “Архитектор решений” OR “Архитектор информационных систем” OR “IT Architect” OR “Integration Architect” OR “Интеграционный архитектор” OR “Application Architect”)'.replace('“','"').replace('”','"'),"executable_now":True,"blocked_by":None},
        {"id":"functional_recall","kind":"function_only","query":'(“solution design” OR “solution architecture” OR “проектирование архитектуры”) AND (“integration architecture” OR “impact analysis”)'.replace('“','"').replace('”','"'),"executable_now":False,"blocked_by":"pool_v1_requires_leading_title_group"},
    ]
    return {"schema_version":2,"generation_basis":"functions_first","vacancy_understanding":{"role_family":"solution_architecture","specialization":"application_and_integration_architecture","summary":"Проектирование архитектуры решений и интеграций","confidence":0.95},"functional_core":functions,"role_titles":{"strong":strong,"conditional":conditional},"leadership_intent":{"level":"individual_contributor","evidence":["Функции проектирования без управления подразделением"]},"strategies":strategies,"selected_strategy":"balanced","unsupported_terms":[{"term":x,"reason":"Не подтверждено вакансией"} for x in ("AWS","Azure","Kubernetes","Kafka","TOGAF","Presales")],"warnings":[]}

def validate_plan(plan=None):
    return require_api("validate_vacancy_search_plan_v2")(plan or golden_plan(), GOLDEN_VACANCY)

def validate_strategy(strategy, plan=None):
    p = plan or golden_plan()
    return require_api("validate_search_strategy_v2")(strategy, p, GOLDEN_VACANCY)

def generator_result():
    plan = golden_plan()
    profile = require_api("build_pool_profile_v1_from_v2")(plan)
    return {"plan": plan, "query": GOLDEN_BALANCED_QUERY, "profile": profile}


def _extract_query_system_schema_example():
    prompt = nano.QUERY_SYSTEM
    begin = "QUERY_PLAN_SCHEMA_BEGIN"
    end = "QUERY_PLAN_SCHEMA_END"
    if prompt.count(begin) != 1 or prompt.count(end) != 1:
        raise AssertionError("QUERY_SYSTEM must contain exactly one schema marker pair")
    schema_text = prompt.split(begin, 1)[1].split(end, 1)[0].strip()
    if schema_text.startswith("```") or schema_text.endswith("```"):
        raise AssertionError("Schema example must be raw JSON without markdown fences")
    return json.loads(schema_text)

def _normalized_prompt_text():
    import re
    return re.sub(r"\s+", " ", nano.QUERY_SYSTEM.casefold().replace("ё", "е")).strip()

def _replace_quoted_phrase_in_strategies(plan, old, new):
    needle = f'"{old}"'
    replacement = f'"{new}"'
    changed = 0
    for strategy in plan["strategies"]:
        before = strategy["query"]
        strategy["query"] = before.replace(needle, replacement)
        changed += before.count(needle)
    if changed == 0:
        raise AssertionError(f"Quoted phrase not found: {old}")

def _query_groups(plan, strategy_id):
    strategy = next(item for item in plan["strategies"] if item["id"] == strategy_id)
    groups, _ = nano._v2_parse(strategy["query"])
    return groups

def _make_english_only_plan():
    import re
    plan = copy.deepcopy(golden_plan())
    has_cyrillic = lambda value: re.search(r"[А-Яа-яЁё]", value) is not None
    for bucket in ("strong", "conditional"):
        plan["role_titles"][bucket] = [item for item in plan["role_titles"][bucket] if not has_cyrillic(item["title"])]
    by_id = {item["id"]: item for item in plan["strategies"]}
    balanced_groups, _ = nano._v2_parse(by_id["balanced"]["query"])
    by_id["balanced"]["query"] = '("Solution Architect" OR "Integration Architect") AND (' + ' OR '.join(f'"{value}"' for value in balanced_groups[1]) + ')'
    precision_groups, precision_ands = nano._v2_parse(by_id["precision"]["query"])
    precision_titles = [value for value in precision_groups[0] if not has_cyrillic(value)]
    precision_parts = ['(' + ' OR '.join(f'"{value}"' for value in precision_titles) + ')'] + ['(' + ' OR '.join(f'"{value}"' for value in group) + ')' for group in precision_groups[1:]]
    by_id["precision"]["query"] = ' AND '.join(precision_parts)
    declared = [item["title"] for bucket in ("strong", "conditional") for item in plan["role_titles"][bucket]]
    required = ["Solution Architect", "Integration Architect"]
    recall = required + [value for value in declared if value not in required]
    by_id["title_recall"]["query"] = '(' + ' OR '.join(f'"{value}"' for value in recall) + ')'
    normalized = {nano._v2_norm(item["query"]) for item in plan["strategies"]}
    assert len(normalized) == 4
    groups, ands = nano._v2_parse(by_id["balanced"]["query"])
    declared_titles = {nano._v2_norm(value) for value in declared}
    declared_terms = {nano._v2_norm(term) for function in plan["functional_core"] for term in function["search_terms"]}
    assert ands == 1 and {nano._v2_norm(value) for value in groups[0]} <= declared_titles
    assert {nano._v2_norm(value) for value in groups[1]} <= declared_terms
    assert plan["selected_strategy"] == "balanced"
    assert not any(has_cyrillic(value) for value in declared)
    assert not any(has_cyrillic(value) for value in groups[0])
    return plan

class QueryGenerationV2BilingualCoverageTests(unittest.TestCase):
    def test_mixed_language_vacancy_rejects_english_only_balanced_titles(self):
        import re
        plan = _make_english_only_plan(); vacancy = copy.deepcopy(GOLDEN_VACANCY)
        groups = _query_groups(plan, "balanced")
        declared_titles = {nano._v2_norm(item["title"]) for bucket in plan["role_titles"].values() for item in bucket}
        declared_terms = {nano._v2_norm(term) for function in plan["functional_core"] for term in function["search_terms"]}
        self.assertRegex(vacancy["name"], r"[A-Za-z]"); self.assertRegex(vacancy["name"], r"[А-Яа-яЁё]")
        self.assertTrue(any(re.search(r"[A-Za-z]", value) for value in groups[0])); self.assertFalse(any(re.search(r"[А-Яа-яЁё]", value) for value in groups[0]))
        self.assertTrue({nano._v2_norm(value) for value in groups[0]} <= declared_titles)
        self.assertTrue({nano._v2_norm(value) for value in groups[1]} <= declared_terms)
        self.assertEqual({item["id"] for item in plan["strategies"]}, {"balanced", "precision", "title_recall", "functional_recall"})
        self.assertEqual(plan["selected_strategy"], "balanced")
        self.assertEqual(len({nano._v2_norm(item["query"]) for item in plan["strategies"]}), 4)
        with self.assertRaises((ValueError, TypeError)):
            nano.validate_vacancy_search_plan_v2(plan, vacancy)

    def test_mixed_language_vacancy_accepts_bilingual_balanced_titles(self):
        import re
        plan = golden_plan(); vacancy = copy.deepcopy(GOLDEN_VACANCY); groups = _query_groups(plan, "balanced")
        declared = {nano._v2_norm(item["title"]) for bucket in plan["role_titles"].values() for item in bucket}
        latin = [value for value in groups[0] if re.search(r"[A-Za-z]", value)]; cyrillic = [value for value in groups[0] if re.search(r"[А-Яа-яЁё]", value)]
        self.assertRegex(vacancy["name"], r"[A-Za-z]"); self.assertRegex(vacancy["name"], r"[А-Яа-яЁё]")
        self.assertTrue(latin and cyrillic); self.assertIn(nano._v2_norm(latin[0]), declared); self.assertIn(nano._v2_norm(cyrillic[0]), declared)
        self.assertEqual(nano.validate_vacancy_search_plan_v2(plan, vacancy)["schema_version"], 2)

    def test_single_script_vacancy_does_not_require_bilingual_titles(self):
        import re
        plan = _make_english_only_plan(); vacancy = copy.deepcopy(GOLDEN_VACANCY); vacancy["name"] = "Solution Architect"
        groups = _query_groups(plan, "balanced")
        self.assertRegex(vacancy["name"], r"[A-Za-z]"); self.assertNotRegex(vacancy["name"], r"[А-Яа-яЁё]")
        self.assertTrue(all(re.search(r"[A-Za-z]", value) and not re.search(r"[А-Яа-яЁё]", value) for value in groups[0]))
        self.assertEqual(nano.validate_vacancy_search_plan_v2(plan, vacancy)["schema_version"], 2)

    def test_query_system_documents_mixed_language_title_coverage(self):
        prose = _normalized_prompt_text().split("QUERY_PLAN_SCHEMA_BEGIN".casefold(), 1)[0]
        for phrase in ("mixed-language vacancy title", "cyrillic title", "latin title", "balanced title or-group", "role_titles", "hybrid vacancy title", "must not be copied automatically"):
            self.assertIn(phrase, prose)

class QueryGenerationV2ApiTests(unittest.TestCase):
    def test_future_validator_api_is_required(self): self.assertTrue(callable(require_api("validate_vacancy_search_plan_v2")))
    def test_future_strategy_validator_api_is_required(self): self.assertTrue(callable(require_api("validate_search_strategy_v2")))
    def test_future_projection_api_is_required(self): self.assertTrue(callable(require_api("build_pool_profile_v1_from_v2")))

class ContractCase(unittest.TestCase):
    def reject_plan(self, mutate):
        plan=golden_plan();mutate(plan)
        with self.assertRaises((ValueError, TypeError)): validate_plan(plan)
    def reject_strategy(self, mutate, sid="balanced"):
        plan=golden_plan();strategy=next(x for x in plan["strategies"] if x["id"]==sid);mutate(strategy)
        with self.assertRaises((ValueError, TypeError)): validate_strategy(strategy,plan)

class QueryGenerationV2SchemaTests(ContractCase):
    def test_valid_plan_is_accepted(self): self.assertEqual(validate_plan()["schema_version"],2)
    def test_missing_top_level_field_rejected(self): self.reject_plan(lambda p:p.pop("warnings"))
    def test_extra_top_level_field_rejected(self): self.reject_plan(lambda p:p.update(extra=True))
    def test_wrong_schema_version_rejected(self): self.reject_plan(lambda p:p.update(schema_version=1))
    def test_wrong_generation_basis_rejected(self): self.reject_plan(lambda p:p.update(generation_basis="title_first"))
    def test_confidence_below_zero_rejected(self): self.reject_plan(lambda p:p["vacancy_understanding"].update(confidence=-0.1))
    def test_confidence_above_one_rejected(self): self.reject_plan(lambda p:p["vacancy_understanding"].update(confidence=1.1))
    def test_boolean_confidence_rejected(self): self.reject_plan(lambda p:p["vacancy_understanding"].update(confidence=True))
    def test_empty_role_family_rejected(self): self.reject_plan(lambda p:p["vacancy_understanding"].update(role_family=""))
    def test_empty_summary_rejected(self): self.reject_plan(lambda p:p["vacancy_understanding"].update(summary=""))
    def test_empty_functional_core_rejected(self): self.reject_plan(lambda p:p.update(functional_core=[]))
    def test_duplicate_function_id_rejected(self): self.reject_plan(lambda p:p["functional_core"].append(copy.deepcopy(p["functional_core"][0])))
    def test_invalid_function_id_rejected(self): self.reject_plan(lambda p:p["functional_core"][0].update(function_id="Bad-ID"))
    def test_empty_function_evidence_rejected(self): self.reject_plan(lambda p:p["functional_core"][0].update(evidence=[]))
    def test_empty_search_terms_rejected(self): self.reject_plan(lambda p:p["functional_core"][0].update(search_terms=[]))

class QueryGenerationV2SearchTermTests(ContractCase):
    def test_term_over_80_characters_rejected(self): self.reject_plan(lambda p:p["functional_core"][0]["search_terms"].append("x"*81))
    def test_term_over_six_words_rejected(self): self.reject_plan(lambda p:p["functional_core"][0]["search_terms"].append("one two three four five six seven"))
    def test_multiline_term_rejected(self): self.reject_plan(lambda p:p["functional_core"][0]["search_terms"].append("solution\narchitecture"))
    def test_sentence_ending_period_rejected(self): self.reject_plan(lambda p:p["functional_core"][0]["search_terms"].append("solution architecture."))
    def test_copied_long_vacancy_sentence_rejected(self): self.reject_plan(lambda p:p["functional_core"][0]["search_terms"].append("Опыт проектирования систем с большим количеством интеграций"))
    def test_generic_information_technology_rejected(self): self.reject_plan(lambda p:p["functional_core"][0]["search_terms"].append("информационные технологии"))
    def test_generic_business_customer_rejected(self): self.reject_plan(lambda p:p["functional_core"][0]["search_terms"].append("бизнес-заказчики"))
    def test_bare_infrastructure_rejected(self): self.reject_plan(lambda p:p["functional_core"][0]["search_terms"].append("инфраструктура"))
    def test_specialized_infrastructure_may_pass(self): self.assertEqual(validate_plan()["functional_core"][2]["search_terms"][-1],"архитектура инфраструктуры")
    def test_integration_architecture_accepted(self): self.assertIn("integration architecture",validate_plan()["functional_core"][1]["search_terms"])

class QueryGenerationV2FunctionFirstTests(ContractCase):
    def test_unknown_derived_function_rejected(self): self.reject_plan(lambda p:p["role_titles"]["strong"][0]["derived_from_functions"].append("unknown"))
    def test_title_without_evidence_rejected(self): self.reject_plan(lambda p:p["role_titles"]["strong"][0].update(evidence=[]))
    def test_duplicate_title_across_buckets_rejected(self): self.reject_plan(lambda p:p["role_titles"]["conditional"].append(copy.deepcopy(p["role_titles"]["strong"][0])))
    def test_vacancy_title_as_sole_evidence_rejected(self): self.reject_plan(lambda p:[x.update(evidence=[GOLDEN_VACANCY["name"]]) for b in p["role_titles"].values() for x in b])
    def test_leadership_manager_word_alone_not_proof(self): self.reject_plan(lambda p:p.update(leadership_intent={"level":"leadership","evidence":["Senior Manager"]}))

class QueryGenerationV2StrategyTests(ContractCase):
    def test_missing_strategy_rejected(self): self.reject_plan(lambda p:p["strategies"].pop())
    def test_duplicate_strategy_id_rejected(self): self.reject_plan(lambda p:p["strategies"][1].update(id="balanced"))
    def test_wrong_selected_strategy_rejected(self): self.reject_plan(lambda p:p.update(selected_strategy="precision"))
    def test_balanced_contract_validated(self): self.assertEqual(validate_strategy(golden_plan()["strategies"][0])["id"],"balanced")
    def test_balanced_leading_title_group_validated(self): self.assertTrue(GOLDEN_BALANCED_QUERY.startswith("("))
    def test_balanced_functional_group_validated(self): self.assertEqual(GOLDEN_BALANCED_QUERY.count(" AND "),1)
    def test_precision_title_subset_validated(self): self.assertEqual(validate_strategy(golden_plan()["strategies"][1])["kind"],"title_and_multiple_functions")
    def test_precision_functional_narrowing_validated(self): self.assertLessEqual(golden_plan()["strategies"][1]["query"].count("Solution Architect"),GOLDEN_BALANCED_QUERY.count("Solution Architect"))
    def test_title_recall_has_no_top_level_and(self): self.assertNotIn(" AND ",golden_plan()["strategies"][2]["query"])
    def test_title_recall_superset_validated(self): self.assertIn("Application Architect",golden_plan()["strategies"][2]["query"])
    def test_functional_recall_contains_no_titles(self): self.assertNotIn("Solution Architect",golden_plan()["strategies"][3]["query"])
    def test_functional_recall_blocked_contract(self): self.assertEqual(validate_strategy(golden_plan()["strategies"][3])["blocked_by"],"pool_v1_requires_leading_title_group")
    def test_identical_normalized_queries_rejected(self): self.reject_plan(lambda p:p["strategies"][1].update(query=p["strategies"][0]["query"]))
    def test_not_operator_rejected(self): self.reject_strategy(lambda s:s.update(query=s["query"]+" NOT \"x\""))
    def test_city_rejected(self): self.reject_strategy(lambda s:s.update(query=s["query"]+" AND (\"Алматы\")"))
    def test_excessive_top_level_and_rejected(self): self.reject_strategy(lambda s:s.update(query=s["query"]+" AND (\"x\") AND (\"y\")"))
    def test_multiline_query_rejected(self): self.reject_strategy(lambda s:s.update(query=s["query"]+"\n"))
    def test_unbalanced_quotes_rejected(self): self.reject_strategy(lambda s:s.update(query=s["query"]+" \""))
    def test_unbalanced_parentheses_rejected(self): self.reject_strategy(lambda s:s.update(query=s["query"]+")"))
    def test_all_strategy_titles_are_declared_in_role_titles(self):
        plan = golden_plan()
        validated = require_api("validate_vacancy_search_plan_v2")(plan, GOLDEN_VACANCY)
        declared = {item["title"].casefold() for bucket in validated["role_titles"].values() for item in bucket}
        for strategy_id in ("balanced", "precision", "title_recall"):
            strategy = next(item for item in validated["strategies"] if item["id"] == strategy_id)
            leading = strategy["query"].split(" AND ", 1)[0]
            titles = {value.casefold() for value in __import__("re").findall(r'"([^"\r\n]+)"', leading)}
            self.assertTrue(titles <= declared)
    def test_undeclared_query_title_is_rejected(self):
        plan = golden_plan()
        strategy = next(item for item in plan["strategies"] if item["id"] == "title_recall")
        strategy["query"] = strategy["query"][:-1] + ' OR "Cloud Architect")'
        with self.assertRaises((ValueError, TypeError)):
            require_api("validate_vacancy_search_plan_v2")(plan, GOLDEN_VACANCY)
    def test_all_strategy_function_terms_are_declared_in_functional_core(self):
        plan = golden_plan()
        validated = require_api("validate_vacancy_search_plan_v2")(plan, GOLDEN_VACANCY)
        titles = {item["title"].casefold() for bucket in validated["role_titles"].values() for item in bucket}
        declared = {term.casefold() for function in validated["functional_core"] for term in function["search_terms"]}
        for strategy_id in ("balanced", "precision", "functional_recall"):
            strategy = next(item for item in validated["strategies"] if item["id"] == strategy_id)
            quoted = {value.casefold() for value in __import__("re").findall(r'"([^"\r\n]+)"', strategy["query"])}
            self.assertTrue((quoted - titles) <= declared)
    def test_undeclared_function_term_is_rejected(self):
        plan = golden_plan()
        strategy = next(item for item in plan["strategies"] if item["id"] == "balanced")
        strategy["query"] = strategy["query"][:-1] + ' OR "cloud migration")'
        with self.assertRaises((ValueError, TypeError)):
            require_api("validate_vacancy_search_plan_v2")(plan, GOLDEN_VACANCY)

class QueryGenerationV2SolutionArchitectTests(ContractCase):
    def test_golden_plan_accepted(self): self.assertEqual(validate_plan()["vacancy_understanding"]["role_family"],"solution_architecture")
    def test_literal_hybrid_title_absent(self): self.assertTrue(all("Solution архитектор" not in x["query"] for x in golden_plan()["strategies"]))
    def test_strong_market_titles_present(self): self.assertTrue({"Solution Architect","Архитектор решений","Архитектор информационных систем"}<={x["title"] for x in golden_plan()["role_titles"]["strong"]})
    def test_integration_architect_conditional(self): self.assertIn("Integration Architect",{x["title"] for x in golden_plan()["role_titles"]["conditional"]})
    def test_architecture_functions_present(self): self.assertIn("solution_design",{x["function_id"] for x in golden_plan()["functional_core"]})
    def test_integration_functions_present(self): self.assertIn("integration_design",{x["function_id"] for x in golden_plan()["functional_core"]})
    def test_impact_analysis_function_present(self): self.assertIn("impact_analysis",{x["function_id"] for x in golden_plan()["functional_core"]})
    def test_long_vacancy_prose_absent(self): self.assertNotIn("Опыт проектирования систем с большим количеством интеграций",GOLDEN_BALANCED_QUERY)
    def test_unsupported_technologies_absent(self): self.assertTrue(all(x not in " ".join(s["query"] for s in golden_plan()["strategies"]) for x in ("AWS","Azure","Kubernetes","Kafka","TOGAF","Presales")))
    def test_exact_balanced_query_accepted(self): self.assertEqual(validate_strategy(golden_plan()["strategies"][0])["query"],GOLDEN_BALANCED_QUERY)
    def test_balanced_differs_from_literal_title(self): self.assertNotIn(GOLDEN_VACANCY["name"],validate_strategy(golden_plan()["strategies"][0])["query"])

class QueryGenerationV2FailureTests(ContractCase):
    def test_invalid_ai_json_has_no_title_fallback(self):
        with patch.object(nano, "ask", return_value="{bad") as ask:
            with self.assertRaises(AIUnavailableError) as caught:
                nano.generate_vacancy_search_plan(GOLDEN_VACANCY)
        self.assertTrue(caught.exception.request_sent)
        ask.assert_called_once()
    def test_invalid_v2_plan_raises_before_search(self): self.reject_plan(lambda p:p.update(schema_version=1))
    def test_generator_performs_one_ai_call(self):
        with patch.object(nano, "ask", return_value=json.dumps(golden_plan(), ensure_ascii=False)) as ask:
            result = nano.generate_vacancy_search_plan(GOLDEN_VACANCY)
        ask.assert_called_once()
        self.assertEqual(set(result), {"plan", "query", "profile"})
    def test_no_second_paid_ai_call(self):
        with patch.object(nano, "ask", side_effect=[json.dumps(golden_plan(), ensure_ascii=False), AssertionError("second paid call")]) as ask:
            result = nano.generate_vacancy_search_plan(GOLDEN_VACANCY)
        ask.assert_called_once()
        self.assertEqual(set(result), {"plan", "query", "profile"})
    def test_compatibility_wrapper_returns_selected_query(self):
        result = generator_result()
        with patch.object(nano, "generate_vacancy_search_plan", return_value=result) as generate:
            query = nano.generate_resume_language_query(GOLDEN_VACANCY)
        generate.assert_called_once_with(GOLDEN_VACANCY)
        self.assertEqual(query, GOLDEN_BALANCED_QUERY)
    def test_xray_flow_does_not_invoke_hh_v2_generator(self):
        with patch.object(service, "validate_nano_settings", return_value=[]), patch.object(service, "fetch_vacancy_details", return_value=GOLDEN_VACANCY), patch.object(state, "get_task", return_value={"xray_sources": []}), patch.object(service, "generate_xray_query", return_value=["Solution Architect"]), patch.object(xray_plan, "build_plan", return_value="xray query") as build, patch.object(service, "set_review") as review, patch.object(service, "generate_vacancy_search_plan") as hh:
            service.prepare_query("1", "task", "xray")
        hh.assert_not_called()
        build.assert_called_once()
        review.assert_called_once()
        args = review.call_args.args
        kwargs = review.call_args.kwargs
        self.assertEqual(args[:2], ("task", "xray query"))
        self.assertIsNone(kwargs.get("vacancy_search_profile"))
        self.assertNotIn("vacancy_search_plan", kwargs)

class QueryGenerationV2PersistenceContractTests(ContractCase):
    def test_set_review_future_signature_accepts_plan(self):
        with patch.object(state,"update_task") as update: state.set_review("t",GOLDEN_BALANCED_QUERY,vacancy_search_plan=golden_plan())
        self.assertEqual(update.call_count,1)
    def test_selected_query_plan_profile_one_update(self):
        projection=require_api("build_pool_profile_v1_from_v2")(golden_plan())
        with patch.object(state,"update_task") as update: state.set_review("t",GOLDEN_BALANCED_QUERY,vacancy_search_plan=golden_plan(),vacancy_search_profile=projection)
        self.assertEqual(update.call_count,1)
    def test_failed_save_no_partial_review(self):
        plan = golden_plan()
        projection = require_api("build_pool_profile_v1_from_v2")(plan)
        with patch.object(state, "update_task", side_effect=RuntimeError("injected atomic save failure")) as update:
            with self.assertRaisesRegex(RuntimeError, "injected atomic save failure"):
                state.set_review("task-v2", GOLDEN_BALANCED_QUERY, vacancy_search_plan=plan, vacancy_search_profile=projection)
        update.assert_called_once()
        task_id, = update.call_args.args
        payload = update.call_args.kwargs
        self.assertEqual(task_id, "task-v2")
        self.assertEqual(payload["query"], GOLDEN_BALANCED_QUERY)
        self.assertEqual(payload["vacancy_search_plan"], plan)
        self.assertEqual(payload["vacancy_search_profile"], projection)
        self.assertEqual(payload["status"], "waiting_approval")
    def test_legacy_set_review_remains_compatible(self):
        with patch.object(state,"update_task") as update: state.set_review("t","query")
        update.assert_called_once()
    def test_old_tasks_without_plan_readable(self):
        row = {"id": "legacy-task", "status": "waiting_approval", "created": 1.0, "updated": 2.0, "expires": None, "data": json.dumps({"query": "q"})}
        task = state._read(row)
        self.assertEqual(task["query"], "q")
        self.assertNotIn("vacancy_search_plan", task)

class QueryGenerationV2ProjectionTests(ContractCase):
    def test_projection_has_v1_keys(self): self.assertEqual(set(require_api("build_pool_profile_v1_from_v2")(golden_plan())),{"role_family","target_functions","accepted_role_titles","mandatory_search_concepts","domain_context","preferred_concepts","excluded_role_families","excluded_titles","leadership_intent","rationale"})
    def test_projection_titles_from_v2(self): self.assertIn("Solution Architect",require_api("build_pool_profile_v1_from_v2")(golden_plan())["accepted_role_titles"])
    def test_projection_functions_from_core(self): self.assertIn("проектирование архитектурных решений",require_api("build_pool_profile_v1_from_v2")(golden_plan())["target_functions"])
    def test_projection_leadership_preserved(self): self.assertEqual(require_api("build_pool_profile_v1_from_v2")(golden_plan())["leadership_intent"],golden_plan()["leadership_intent"])
    def test_projection_deterministic(self):
        project = require_api("build_pool_profile_v1_from_v2")
        first = project(golden_plan())
        second = project(copy.deepcopy(golden_plan()))
        self.assertEqual(first, second)
    def test_projection_excludes_unsupported(self): self.assertTrue(all(x not in json.dumps(require_api("build_pool_profile_v1_from_v2")(golden_plan())) for x in ("AWS","Azure","Kafka")))
    def test_projection_rejects_malformed_direct_input_with_contract_error(self):
        mutations = (lambda p: p.pop("vacancy_understanding"), lambda p: p["functional_core"][0].pop("normalized_function"), lambda p: p.update(role_titles=[]), lambda p: p.pop("leadership_intent"))
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                plan = golden_plan(); mutate(plan)
                with self.assertRaises((ValueError, TypeError)): nano.build_pool_profile_v1_from_v2(plan)


class QueryGenerationV2PromptContractTests(unittest.TestCase):
    def test_query_system_documents_all_top_level_fields(self):
        example = _extract_query_system_schema_example()
        self.assertEqual(set(example), {"schema_version", "generation_basis", "vacancy_understanding", "functional_core", "role_titles", "leadership_intent", "strategies", "selected_strategy", "unsupported_terms", "warnings"})
        self.assertEqual((example["schema_version"], example["generation_basis"], example["selected_strategy"]), (2, "functions_first", "balanced"))
    def test_query_system_documents_vacancy_understanding_schema(self):
        self.assertEqual(set(_extract_query_system_schema_example()["vacancy_understanding"]), {"role_family", "specialization", "summary", "confidence"})
    def test_query_system_documents_functional_core_schema(self):
        items = _extract_query_system_schema_example()["functional_core"]
        self.assertGreater(len(items), 0)
        for item in items: self.assertEqual(set(item), {"function_id", "normalized_function", "search_terms", "evidence"})
    def test_query_system_documents_role_title_schema(self):
        roles = _extract_query_system_schema_example()["role_titles"]
        self.assertEqual(set(roles), {"strong", "conditional"}); self.assertTrue(roles["strong"]); self.assertTrue(roles["conditional"])
        for bucket in roles.values():
            for item in bucket: self.assertEqual(set(item), {"title", "derived_from_functions", "evidence"})
    def test_query_system_documents_leadership_schema(self):
        self.assertEqual(set(_extract_query_system_schema_example()["leadership_intent"]), {"level", "evidence"})
    def test_query_system_documents_strategy_schema(self):
        strategies = _extract_query_system_schema_example()["strategies"]
        self.assertEqual(len(strategies), 4)
        for item in strategies: self.assertEqual(set(item), {"id", "kind", "query", "executable_now", "blocked_by"})
    def test_query_system_documents_unsupported_term_schema(self):
        items = _extract_query_system_schema_example()["unsupported_terms"]
        self.assertGreater(len(items), 0)
        for item in items: self.assertEqual(set(item), {"term", "reason"})
    def test_query_system_documents_all_strategy_ids(self):
        self.assertEqual({x["id"] for x in _extract_query_system_schema_example()["strategies"]}, {"balanced", "precision", "title_recall", "functional_recall"})
    def test_query_system_documents_functional_recall_metadata(self):
        by_id = {x["id"]: x for x in _extract_query_system_schema_example()["strategies"]}
        self.assertEqual((by_id["balanced"]["kind"], by_id["balanced"]["executable_now"], by_id["balanced"]["blocked_by"]), ("title_and_function", True, None))
        self.assertEqual((by_id["precision"]["kind"], by_id["precision"]["executable_now"], by_id["precision"]["blocked_by"]), ("title_and_multiple_functions", True, None))
        self.assertEqual((by_id["title_recall"]["kind"], by_id["title_recall"]["executable_now"], by_id["title_recall"]["blocked_by"]), ("title_only", True, None))
        self.assertEqual((by_id["functional_recall"]["kind"], by_id["functional_recall"]["executable_now"], by_id["functional_recall"]["blocked_by"]), ("function_only", False, "pool_v1_requires_leading_title_group"))
    def test_query_system_documents_query_provenance_rules(self):
        prose = _normalized_prompt_text().split("QUERY_PLAN_SCHEMA_BEGIN".casefold(), 1)[0]
        for token in ("query title", "role_titles", "query functional term", "functional_core.search_terms", "undeclared title", "forbidden", "undeclared functional term"):
            self.assertIn(token, prose)
    def test_query_system_documents_strategy_query_shapes(self):
        prose = _normalized_prompt_text().split("QUERY_PLAN_SCHEMA_BEGIN".casefold(), 1)[0]
        for token in ("balanced", "title or-group", "functional or-group", "precision", "one or two functional or-groups", "title_recall", "without top-level and", "functional_recall", "no role titles"):
            self.assertIn(token, prose)

class QueryGenerationV2LeadershipEvidenceTests(unittest.TestCase):
    def test_weak_leadership_titles_do_not_prove_leadership(self):
        for evidence in ("Senior Manager with strategic responsibilities", "Lead stakeholder communication", "Principal expert", "Ведущий архитектор", "Старший менеджер проектов"):
            with self.subTest(evidence=evidence):
                plan = golden_plan(); plan["leadership_intent"] = {"level": "leadership", "evidence": [evidence]}
                with self.assertRaises((ValueError, TypeError)): nano.validate_vacancy_search_plan_v2(plan, GOLDEN_VACANCY)
    def test_explicit_people_management_proves_leadership(self):
        for evidence in ("Управление командой из 8 человек", "5 direct reports", "Hiring and performance management responsibility", "Руководство подразделением"):
            with self.subTest(evidence=evidence):
                plan = golden_plan(); plan["leadership_intent"] = {"level": "leadership", "evidence": [evidence]}
                validated = nano.validate_vacancy_search_plan_v2(plan, GOLDEN_VACANCY)
                self.assertEqual(validated["leadership_intent"]["level"], "leadership")

class QueryGenerationV2CanonicalNormalizationTests(unittest.TestCase):
    def test_title_provenance_uses_canonical_normalization(self):
        plan = golden_plan()
        item = next(x for x in plan["role_titles"]["conditional"] if x["title"] == "Интеграционный архитектор")
        item["title"] = "Интеграционный–архитектор"
        _replace_quoted_phrase_in_strategies(plan, "Интеграционный архитектор", "Интеграционный-архитектор")
        self.assertEqual(nano.validate_vacancy_search_plan_v2(plan, GOLDEN_VACANCY)["schema_version"], 2)
    def test_function_provenance_uses_canonical_normalization(self):
        cases = (("solution design", "solution–design", "solution-design"), ("solution architecture", "проектирование ёмких систем", "проектирование емких систем"))
        for old, declared, queried in cases:
            with self.subTest(declared=declared):
                plan = golden_plan(); function = next(x for x in plan["functional_core"] if old in x["search_terms"])
                function["search_terms"][function["search_terms"].index(old)] = declared
                _replace_quoted_phrase_in_strategies(plan, old, queried)
                self.assertEqual(nano.validate_vacancy_search_plan_v2(plan, GOLDEN_VACANCY)["schema_version"], 2)
    def test_canonical_duplicate_titles_are_rejected(self):
        plan = golden_plan(); item = next(x for x in plan["role_titles"]["conditional"] if x["title"] == "Интеграционный архитектор")
        item["title"] = "Интеграционный–архитектор"; duplicate = copy.deepcopy(item); duplicate["title"] = "Интеграционный-архитектор"; plan["role_titles"]["conditional"].append(duplicate)
        with self.assertRaises((ValueError, TypeError)): nano.validate_vacancy_search_plan_v2(plan, GOLDEN_VACANCY)
    def test_canonical_duplicate_function_terms_are_rejected(self):
        plan = golden_plan(); function = next(x for x in plan["functional_core"] if "solution design" in x["search_terms"])
        function["search_terms"][function["search_terms"].index("solution design")] = "solution–design"; function["search_terms"].append("solution-design")
        with self.assertRaises((ValueError, TypeError)): nano.validate_vacancy_search_plan_v2(plan, GOLDEN_VACANCY)


class QueryGenerationV2FixtureSourceContractTests(unittest.TestCase):
    def test_golden_vacancy_has_required_title(self):
        self.assertEqual(GOLDEN_VACANCY["name"], "Solution архитектор")
    def test_golden_vacancy_has_almaty_area(self):
        self.assertEqual(GOLDEN_VACANCY["area"]["name"], "Алматы")
    def test_golden_vacancy_contains_architecture_design_evidence(self):
        self.assertIn("архитектурных решений", GOLDEN_VACANCY["description"])
    def test_golden_vacancy_contains_impact_analysis_evidence(self):
        self.assertIn("Impact analysis", GOLDEN_VACANCY["description"])
    def test_golden_vacancy_contains_integration_evidence(self):
        self.assertIn("большим количеством интеграций", GOLDEN_VACANCY["description"])
    def test_golden_vacancy_skills_are_grounded(self):
        self.assertEqual([item["name"] for item in GOLDEN_VACANCY["key_skills"]], ["UML", "C4", "BPMN 2.0", "СУБД"])
    def test_golden_vacancy_excludes_unsupported_technology_requirements(self):
        source = json.dumps(GOLDEN_VACANCY, ensure_ascii=False)
        self.assertFalse(any(term in source for term in ("AWS", "Azure", "Kubernetes", "Kafka", "TOGAF", "Presales")))
    def test_exact_balanced_query_is_one_line(self):
        self.assertNotIn("\n", GOLDEN_BALANCED_QUERY)
    def test_exact_balanced_query_has_one_top_level_and(self):
        self.assertEqual(GOLDEN_BALANCED_QUERY.count(" AND "), 1)
    def test_exact_balanced_query_omits_literal_hybrid_title(self):
        self.assertNotIn(GOLDEN_VACANCY["name"], GOLDEN_BALANCED_QUERY)

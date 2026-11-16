"""RED refinement contracts for confirmed Query Generation V3 live gaps."""
from __future__ import annotations

import copy
import re
import unittest

from app.integrations.ai import query_interpreter
from app.services.query_interpretation_validator import (
    validate_search_interpretation_v3,
)


MIXED_LANGUAGE_VACANCY = {
    "name": "Solution архитектор",
    "area": {"name": "Алматы"},
    "description": (
        "Проработка архитектурных решений, проектирование систем с большим "
        "количеством интеграций и impact анализ."
    ),
}


def _term(value, language, derivation="semantic_normalization"):
    return {
        "value": value,
        "language": language,
        "derivation": derivation,
    }


def _function(
    function_id,
    normalized_function,
    importance,
    market_terms,
    evidence,
):
    return {
        "function_id": function_id,
        "normalized_function": normalized_function,
        "importance": importance,
        "market_terms": market_terms,
        "evidence": evidence,
    }


def _title(
    canonical_id,
    title,
    language,
    priority,
    derived_from_functions,
    evidence,
):
    return {
        "canonical_id": canonical_id,
        "title": title,
        "language": language,
        "priority": priority,
        "derived_from_functions": derived_from_functions,
        "evidence": evidence,
    }


def _valid_mixed_language_interpretation():
    return {
        "schema_version": 3,
        "generation_basis": "semantic_functions_first",
        "vacancy_understanding": {
            "role_family": "solution_architecture",
            "specialization": "integration_architecture",
            "summary": "Проектирование решений, интеграций и анализ влияния",
            "confidence": 0.95,
        },
        "functions": [
            _function(
                "solution_design",
                "проектирование архитектуры решений",
                "primary",
                [
                    _term("solution architecture", "en", "market_equivalent"),
                    _term("архитектура решений", "ru"),
                ],
                ["Проработка архитектурных решений"],
            ),
            _function(
                "integration_design",
                "проектирование интеграций",
                "primary",
                [
                    _term("integration architecture", "en", "market_equivalent"),
                    _term("интеграционная архитектура", "ru"),
                ],
                ["Проектирование систем с большим количеством интеграций"],
            ),
            _function(
                "impact_analysis",
                "анализ влияния",
                "primary",
                [
                    _term("impact analysis", "en", "translation_equivalent"),
                    _term("анализ влияния", "ru"),
                ],
                ["impact анализ"],
            ),
            _function(
                "architecture_documentation",
                "документирование архитектуры",
                "secondary",
                [_term("architecture documentation", "en", "market_equivalent")],
                ["Документирование архитектурного дизайна"],
            ),
        ],
        "role_titles": [
            _title(
                "solution_architect_en",
                "Solution Architect",
                "en",
                "strong",
                ["solution_design"],
                ["Проработка архитектурных решений"],
            ),
            _title(
                "solution_architect_ru",
                "Архитектор решений",
                "ru",
                "strong",
                ["solution_design"],
                ["Проработка архитектурных решений"],
            ),
            _title(
                "integration_architect_en",
                "Integration Architect",
                "en",
                "conditional",
                ["integration_design"],
                ["Проектирование интеграций"],
            ),
            _title(
                "integration_architect_ru",
                "Архитектор интеграций",
                "ru",
                "conditional",
                ["integration_design"],
                ["Проектирование интеграций"],
            ),
        ],
        "leadership_intent": {
            "level": "individual_contributor",
            "evidence": ["Управление людьми не указано"],
        },
        "unsupported_terms": [],
        "warnings": [],
    }


def _english_only_titles_interpretation():
    interpretation = copy.deepcopy(_valid_mixed_language_interpretation())
    interpretation["role_titles"] = [
        item
        for item in interpretation["role_titles"]
        if item["language"] != "ru"
    ]
    return interpretation


def _hybrid_title_only_interpretation():
    interpretation = copy.deepcopy(_valid_mixed_language_interpretation())
    interpretation["role_titles"] = [
        _title(
            "hybrid_solution_architect",
            "Solution архитектор",
            "en",
            "strong",
            ["solution_design"],
            ["Solution архитектор"],
        )
    ]
    return interpretation


def _primary_missing_language_interpretation(language):
    interpretation = copy.deepcopy(_valid_mixed_language_interpretation())
    function = interpretation["functions"][1]
    function["market_terms"] = [
        term
        for term in function["market_terms"]
        if term["language"] != language
    ]
    return interpretation


def _secondary_one_language_interpretation():
    return copy.deepcopy(_valid_mixed_language_interpretation())


def _moderate_priority_interpretation():
    interpretation = copy.deepcopy(_valid_mixed_language_interpretation())
    interpretation["role_titles"][2]["priority"] = "moderate"
    return interpretation


def _monolingual_english_fixture():
    vacancy = copy.deepcopy(MIXED_LANGUAGE_VACANCY)
    vacancy["name"] = "Solution Architect"
    interpretation = _english_only_titles_interpretation()
    for function in interpretation["functions"]:
        if function["importance"] == "primary":
            function["market_terms"] = [
                term
                for term in function["market_terms"]
                if term["language"] == "en"
            ]
    return vacancy, interpretation


def _normalized_prompt():
    prompt = getattr(query_interpreter, "V3_INTERPRETATION_SYSTEM", "")
    if not isinstance(prompt, str) or not prompt.strip():
        raise AssertionError("V3_INTERPRETATION_SYSTEM is missing or empty")
    return re.sub(r"\s+", " ", prompt.casefold()).strip()


def _assert_contains_all(testcase, text, anchors):
    for anchor in anchors:
        testcase.assertIn(anchor, text)


class QueryGenerationV3LiveGapPromptTests(unittest.TestCase):
    def test_prompt_declares_exact_closed_enums(self):
        prompt = _normalized_prompt()
        _assert_contains_all(
            self,
            prompt,
            (
                "importance",
                "primary",
                "secondary",
                "language",
                "en",
                "ru",
                "other",
                "derivation",
                "literal",
                "semantic_normalization",
                "market_equivalent",
                "translation_equivalent",
                "priority",
                "strong",
                "conditional",
                "leadership",
                "individual_contributor",
                "unknown",
            ),
        )
        self.assertRegex(
            prompt,
            r"priority.{0,180}(strong.{0,80}conditional|conditional.{0,80}strong)",
        )
        self.assertRegex(
            prompt,
            r"priority.{0,260}moderate.{0,120}(never|forbidden|must not|no other value)",
        )
        self.assertRegex(
            prompt,
            r"priority.{0,320}(medium|primary|secondary)",
        )

    def test_prompt_requires_mixed_language_title_coverage(self):
        prompt = _normalized_prompt()
        self.assertRegex(prompt, r"vacancy (title|name).{0,140}latin.{0,100}cyrillic")
        self.assertRegex(prompt, r"mixed-language.{0,220}(english|language=en).{0,160}(russian|language=ru)")
        self.assertRegex(prompt, r"hybrid.{0,180}(not|must not|does not).{0,120}(only|coverage)")
        _assert_contains_all(self, prompt, ("role title", "strong"))

    def test_prompt_requires_bilingual_primary_market_terms(self):
        prompt = _normalized_prompt()
        self.assertRegex(prompt, r"every primary function.{0,220}(english|language=en)")
        self.assertRegex(prompt, r"every primary function.{0,320}(russian|language=ru)")
        self.assertRegex(prompt, r"secondary.{0,180}(exempt|not required|optional)")
        self.assertRegex(
            prompt,
            r"russian.{0,220}(semantic|market|translation).{0,180}(not need|need not|literal)",
        )

    def test_prompt_defines_compact_primary_search_core_and_self_check(self):
        prompt = _normalized_prompt()
        _assert_contains_all(
            self,
            prompt,
            (
                "compact",
                "primary search core",
                "do not mark every responsibility as primary",
                "do not mark every requirement as primary",
                "solution_design",
                "integration_design",
                "impact_analysis",
                "architecture_documentation",
                "architecture_modeling",
                "requirements_analysis",
                "database_architecture",
                "network_architecture",
                "self-check",
                "schema_version",
                "semantic_functions_first",
                "no query",
                "no strategies",
                "no selected_strategy",
                "moderate",
                "evidence",
                "canonical market term",
            ),
        )
        self.assertRegex(prompt, r"documentation.{0,180}secondary")
        self.assertRegex(prompt, r"modeling.{0,180}secondary")
        self.assertRegex(prompt, r"impact analysis.{0,200}(must not|do not).{0,100}downgrad")


class QueryGenerationV3LiveGapValidatorTests(unittest.TestCase):
    def test_mixed_language_vacancy_accepts_complete_bilingual_coverage(self):
        interpretation = _valid_mixed_language_interpretation()
        vacancy = copy.deepcopy(MIXED_LANGUAGE_VACANCY)
        before_interpretation = copy.deepcopy(interpretation)
        before_vacancy = copy.deepcopy(vacancy)
        validated = validate_search_interpretation_v3(interpretation, vacancy)
        self.assertEqual(validated["schema_version"], 3)
        self.assertEqual(interpretation, before_interpretation)
        self.assertEqual(vacancy, before_vacancy)

    def test_mixed_language_vacancy_rejects_english_only_titles(self):
        interpretation = _english_only_titles_interpretation()
        with self.assertRaises((ValueError, TypeError)):
            validate_search_interpretation_v3(
                interpretation,
                copy.deepcopy(MIXED_LANGUAGE_VACANCY),
            )

    def test_mixed_language_vacancy_rejects_hybrid_title_only_coverage(self):
        interpretation = _hybrid_title_only_interpretation()
        with self.assertRaises((ValueError, TypeError)):
            validate_search_interpretation_v3(
                interpretation,
                copy.deepcopy(MIXED_LANGUAGE_VACANCY),
            )

    def test_mixed_language_vacancy_rejects_primary_function_without_russian_term(self):
        interpretation = _primary_missing_language_interpretation("ru")
        with self.assertRaises((ValueError, TypeError)):
            validate_search_interpretation_v3(
                interpretation,
                copy.deepcopy(MIXED_LANGUAGE_VACANCY),
            )

    def test_mixed_language_vacancy_rejects_primary_function_without_english_term(self):
        interpretation = _primary_missing_language_interpretation("en")
        with self.assertRaises((ValueError, TypeError)):
            validate_search_interpretation_v3(
                interpretation,
                copy.deepcopy(MIXED_LANGUAGE_VACANCY),
            )

    def test_secondary_function_is_not_required_to_be_bilingual(self):
        interpretation = _secondary_one_language_interpretation()
        validated = validate_search_interpretation_v3(
            interpretation,
            copy.deepcopy(MIXED_LANGUAGE_VACANCY),
        )
        documentation = next(
            item
            for item in validated["functions"]
            if item["function_id"] == "architecture_documentation"
        )
        self.assertEqual(documentation["importance"], "secondary")
        self.assertEqual(
            {term["language"] for term in documentation["market_terms"]},
            {"en"},
        )


class QueryGenerationV3LiveGapRegressionTests(unittest.TestCase):
    def test_validator_still_rejects_moderate_priority(self):
        interpretation = _moderate_priority_interpretation()
        with self.assertRaises((ValueError, TypeError)):
            validate_search_interpretation_v3(
                interpretation,
                copy.deepcopy(MIXED_LANGUAGE_VACANCY),
            )

    def test_monolingual_vacancy_does_not_require_bilingual_coverage(self):
        vacancy, interpretation = _monolingual_english_fixture()
        validated = validate_search_interpretation_v3(interpretation, vacancy)
        self.assertEqual(
            {item["language"] for item in validated["role_titles"]},
            {"en"},
        )
        for function in validated["functions"]:
            if function["importance"] == "primary":
                self.assertEqual(
                    {term["language"] for term in function["market_terms"]},
                    {"en"},
                )

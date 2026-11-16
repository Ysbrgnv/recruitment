import copy
import re

from app.domain.query_generation_v3 import (
    ALLOWED_DERIVATIONS,
    ALLOWED_IMPORTANCE,
    ALLOWED_LANGUAGES,
    ALLOWED_LEADERSHIP_LEVELS,
    ALLOWED_PRIORITIES,
    V3_FUNCTION_FIELDS,
    V3_GENERATION_BASIS,
    V3_INTERPRETATION_FIELDS,
    V3_MARKET_TERM_FIELDS,
    V3_ROLE_TITLE_FIELDS,
    V3_SCHEMA_VERSION,
    canonical_phrase,
)

UNDERSTANDING_FIELDS = {"role_family", "specialization", "summary", "confidence"}
LEADERSHIP_FIELDS = {"level", "evidence"}
UNSUPPORTED_FIELDS = {"term", "reason"}
GENERIC_TERMS = {"infrastructure", "information technology", "business", "customer", "system"}
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{1,63}")


def _exact(value, fields, label):
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"invalid {label} fields")


def _text(value, label, max_length=None, max_words=None):
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise ValueError(label)
    stripped = value.strip()
    if max_length is not None and len(stripped) > max_length:
        raise ValueError(label)
    if max_words is not None and len(stripped.split()) > max_words:
        raise ValueError(label)
    return stripped


def _evidence(value, label):
    if not isinstance(value, list) or not value:
        raise ValueError(label)
    for item in value:
        _text(item, label)



def _has_latin(value):
    return isinstance(value, str) and re.search(r"[A-Za-z]", value) is not None


def _has_cyrillic(value):
    return isinstance(value, str) and re.search(r"[А-Яа-яЁё]", value) is not None


def _is_mixed_language_vacancy(vacancy):
    if not isinstance(vacancy, dict):
        return False
    name = vacancy.get("name")
    return _has_latin(name) and _has_cyrillic(name)

def validate_search_interpretation_v3(interpretation, vacancy):
    _exact(interpretation, V3_INTERPRETATION_FIELDS, "interpretation")
    version = interpretation["schema_version"]
    if type(version) is not int or version != V3_SCHEMA_VERSION:
        raise ValueError("schema_version")
    if interpretation["generation_basis"] != V3_GENERATION_BASIS:
        raise ValueError("generation_basis")

    understanding = interpretation["vacancy_understanding"]
    _exact(understanding, UNDERSTANDING_FIELDS, "vacancy_understanding")
    _text(understanding["role_family"], "role_family")
    if understanding["specialization"] is not None:
        _text(understanding["specialization"], "specialization")
    _text(understanding["summary"], "summary")
    confidence = understanding["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("confidence")

    mixed_language = _is_mixed_language_vacancy(vacancy)
    functions = interpretation["functions"]
    if not isinstance(functions, list) or not functions:
        raise ValueError("functions")
    function_ids = set()
    market_owners = {}
    for function in functions:
        _exact(function, V3_FUNCTION_FIELDS, "function")
        function_id = _text(function["function_id"], "function_id")
        if not IDENTIFIER.fullmatch(function_id) or function_id.casefold() in function_ids:
            raise ValueError("function_id")
        function_ids.add(function_id)
        _text(function["normalized_function"], "normalized_function")
        if function["importance"] not in ALLOWED_IMPORTANCE:
            raise ValueError("importance")
        _evidence(function["evidence"], "function evidence")
        terms = function["market_terms"]
        if not isinstance(terms, list) or not terms:
            raise ValueError("market_terms")
        for term in terms:
            _exact(term, V3_MARKET_TERM_FIELDS, "market term")
            value = _text(term["value"], "market term", 80, 6)
            if '"' in value:
                raise ValueError("market term quote")
            if value.endswith(".") or ";" in value:
                raise ValueError("market term")
            key = canonical_phrase(value)
            if key in GENERIC_TERMS or key in market_owners:
                raise ValueError("market term duplicate or generic")
            market_owners[key] = function_id
            if term["language"] not in ALLOWED_LANGUAGES:
                raise ValueError("market term language")
            if term["derivation"] not in ALLOWED_DERIVATIONS:
                raise ValueError("market term derivation")
        if mixed_language and function["importance"] == "primary":
            languages = {term["language"] for term in terms}
            if not {"en", "ru"}.issubset(languages):
                raise ValueError("mixed-language primary market terms")

    primary_ids = {
        function["function_id"]
        for function in functions
        if function["importance"] == "primary"
    }
    if not primary_ids:
        raise ValueError("primary function required")

    unsupported = interpretation["unsupported_terms"]
    if not isinstance(unsupported, list):
        raise ValueError("unsupported_terms")
    unsupported_keys = set()
    for item in unsupported:
        _exact(item, UNSUPPORTED_FIELDS, "unsupported term")
        unsupported_keys.add(canonical_phrase(_text(item["term"], "unsupported term")))
        _text(item["reason"], "unsupported reason")
    if unsupported_keys & set(market_owners):
        raise ValueError("unsupported market term")

    titles = interpretation["role_titles"]
    if not isinstance(titles, list) or not titles:
        raise ValueError("role_titles")
    title_ids = set()
    title_values = set()
    for title in titles:
        _exact(title, V3_ROLE_TITLE_FIELDS, "role title")
        canonical_id = _text(title["canonical_id"], "canonical_id")
        if not IDENTIFIER.fullmatch(canonical_id):
            raise ValueError("canonical_id")
        canonical_id_key = canonical_id.casefold()
        if canonical_id_key in title_ids:
            raise ValueError("canonical_id")
        title_ids.add(canonical_id_key)
        value = _text(title["title"], "title", 80, 6)
        if '"' in value:
            raise ValueError("title quote")
        title_key = canonical_phrase(value)
        if title_key in title_values or title_key in unsupported_keys:
            raise ValueError("title duplicate or unsupported")
        title_values.add(title_key)
        if title["language"] not in ALLOWED_LANGUAGES or title["priority"] not in ALLOWED_PRIORITIES:
            raise ValueError("title metadata")
        refs = title["derived_from_functions"]
        if not isinstance(refs, list) or not refs:
            raise ValueError("title provenance")
        for ref in refs:
            if (
                not isinstance(ref, str)
                or not IDENTIFIER.fullmatch(ref)
                or ref not in function_ids
            ):
                raise ValueError("unknown function reference")
        _evidence(title["evidence"], "title evidence")

    if mixed_language:
        title_languages = {title["language"] for title in titles}
        if not {"en", "ru"}.issubset(title_languages):
            raise ValueError("mixed-language role title coverage")

    strong_titles = [
        title
        for title in titles
        if title["priority"] == "strong"
    ]
    if not strong_titles:
        raise ValueError("strong title required")

    renderable_titles = [
        title
        for title in titles
        if title["priority"] == "strong"
        or primary_ids.intersection(title["derived_from_functions"])
    ]
    if not renderable_titles:
        raise ValueError("renderable title required")

    leadership = interpretation["leadership_intent"]
    _exact(leadership, LEADERSHIP_FIELDS, "leadership_intent")
    if leadership["level"] not in ALLOWED_LEADERSHIP_LEVELS:
        raise ValueError("leadership level")
    _evidence(leadership["evidence"], "leadership evidence")

    warnings = interpretation["warnings"]
    if not isinstance(warnings, list):
        raise ValueError("warnings")
    for warning in warnings:
        _text(warning, "warning")
    return copy.deepcopy(interpretation)

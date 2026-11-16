import re

V3_SCHEMA_VERSION = 3
V3_GENERATION_BASIS = "semantic_functions_first"
V3_INTERPRETATION_FIELDS = {"schema_version", "generation_basis", "vacancy_understanding", "functions", "role_titles", "leadership_intent", "unsupported_terms", "warnings"}
V3_FUNCTION_FIELDS = {"function_id", "normalized_function", "importance", "market_terms", "evidence"}
V3_MARKET_TERM_FIELDS = {"value", "language", "derivation"}
V3_ROLE_TITLE_FIELDS = {"canonical_id", "title", "language", "priority", "derived_from_functions", "evidence"}
V3_STRATEGY_FIELDS = {"id", "kind", "query", "executable_now", "blocked_by"}
ALLOWED_IMPORTANCE = frozenset({"primary", "secondary"})
ALLOWED_LANGUAGES = frozenset({"en", "ru", "other"})
ALLOWED_DERIVATIONS = frozenset({"literal", "semantic_normalization", "market_equivalent", "translation_equivalent"})
ALLOWED_PRIORITIES = frozenset({"strong", "conditional"})
ALLOWED_LEADERSHIP_LEVELS = frozenset({"individual_contributor", "leadership", "unknown"})


def canonical_phrase(value):
    if not isinstance(value, str):
        raise TypeError("phrase must be a string")
    value = value.casefold().replace("ё", "е")
    value = re.sub(r"[‐‑‒–—−_/\\-]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()

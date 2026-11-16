import copy

from app.domain.query_generation_v3 import canonical_phrase

LANGUAGE_ORDER = {"en": 0, "ru": 1, "other": 2}


def _deduplicate(values):
    result = []
    seen = set()
    for value in values:
        key = canonical_phrase(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _quote_group(values):
    return "(" + " OR ".join(f'"{value}"' for value in values) + ")"


def _function_terms(function):
    indexed = list(enumerate(function["market_terms"]))
    indexed.sort(key=lambda item: (LANGUAGE_ORDER[item[1]["language"]], item[0]))
    return [item["value"] for _, item in indexed]


def _primary_functions(interpretation):
    return [item for item in interpretation["functions"] if item["importance"] == "primary"]


def _title_values(interpretation, include_conditional):
    primary_ids = {item["function_id"] for item in _primary_functions(interpretation)}
    strong = [item["title"] for item in interpretation["role_titles"] if item["priority"] == "strong"]
    conditional = []
    if include_conditional:
        conditional = [
            item["title"]
            for item in interpretation["role_titles"]
            if item["priority"] == "conditional"
            and primary_ids.intersection(item["derived_from_functions"])
        ]
    return _deduplicate(strong + conditional)


def _functional_values(functions):
    values = []
    for function in functions:
        values.extend(_function_terms(function))
    return _deduplicate(values)


def render_search_strategies_v3(interpretation):
    primary = _primary_functions(interpretation)
    balanced_titles = _title_values(interpretation, True)
    strong_titles = _title_values(interpretation, False)
    all_terms = _functional_values(primary)
    first_terms = _functional_values(primary[:1])
    remaining_terms = _functional_values(primary[1:])
    if not balanced_titles:
        raise ValueError("balanced title group is empty")
    if not all_terms or not first_terms:
        raise ValueError("primary functional group is empty")
    if not strong_titles:
        raise ValueError("precision strong-title group is empty")
    balanced_query = _quote_group(balanced_titles) + " AND " + _quote_group(all_terms)
    precision_parts = [_quote_group(strong_titles), _quote_group(first_terms)]
    if remaining_terms:
        precision_parts.append(_quote_group(remaining_terms))
    strategies = [
        {"id": "balanced", "kind": "title_and_function", "query": balanced_query, "executable_now": True, "blocked_by": None},
        {"id": "precision", "kind": "title_and_multiple_functions", "query": " AND ".join(precision_parts), "executable_now": True, "blocked_by": None},
        {"id": "title_recall", "kind": "title_only", "query": _quote_group(balanced_titles), "executable_now": True, "blocked_by": None},
        {"id": "functional_recall", "kind": "function_only", "query": _quote_group(all_terms), "executable_now": False, "blocked_by": "pool_v1_requires_leading_title_group"},
    ]
    return {"schema_version": 3, "generation_basis": "deterministic_renderer_v3", "strategies": strategies, "selected_strategy": "balanced"}


def build_pool_profile_v1_from_v3(interpretation, rendered_plan):
    titles = _deduplicate([item["title"] for item in interpretation["role_titles"]])
    functions = {item["function_id"]: item["normalized_function"] for item in interpretation["functions"]}
    rationale = {}
    for item in interpretation["role_titles"]:
        if item["title"] in titles and item["title"] not in rationale:
            rationale[item["title"]] = "; ".join(functions[ref] for ref in item["derived_from_functions"])
    result = {
        "role_family": interpretation["vacancy_understanding"]["role_family"],
        "target_functions": [item["normalized_function"] for item in interpretation["functions"]],
        "accepted_role_titles": titles,
        "mandatory_search_concepts": [
            {"concept_id": item["function_id"], "alternatives": _deduplicate(_function_terms(item))}
            for item in interpretation["functions"]
        ],
        "domain_context": [],
        "preferred_concepts": [],
        "excluded_role_families": [],
        "excluded_titles": [],
        "leadership_intent": copy.deepcopy(interpretation["leadership_intent"]),
        "rationale": {"role_family": interpretation["vacancy_understanding"]["summary"], "accepted_role_titles": rationale, "excluded_titles": {}},
    }
    unsupported = {canonical_phrase(item["term"]) for item in interpretation["unsupported_terms"]}
    if any(canonical_phrase(value) in unsupported for value in titles):
        raise ValueError("unsupported title")
    if any(canonical_phrase(value) in unsupported for concept in result["mandatory_search_concepts"] for value in concept["alternatives"]):
        raise ValueError("unsupported market term")
    return result

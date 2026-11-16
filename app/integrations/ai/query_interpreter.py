import copy
import json
import re

from app.core.errors import AIUnavailableError
from app.services.recruitment_data import prepare_vacancy
from app.services.query_interpretation_validator import validate_search_interpretation_v3
from app.services.query_renderer import build_pool_profile_v1_from_v3, render_search_strategies_v3

V3_INTERPRETATION_SYSTEM = r'''Return JSON only with no markdown, code fences, comments, or prose outside JSON. Return schema_version 3 and generation_basis semantic_functions_first. Return interpretation only: never return query, strategies, or selected_strategy. Extract grounded professional functions from vacancy responsibilities and requirements. Market terms may be semantic normalization, market equivalents, or translation equivalents and need not literally equal vacancy wording. Each market term belongs to exactly one declared function; canonical duplicates across functions are forbidden. Every function and role title requires evidence. Every role title references declared functions. Do not invent unsupported technologies. Do not use city, employer, or salary as market terms. Do not copy long vacancy sentences. Role titles must be professional market roles. The hybrid vacancy title must not be copied automatically.
{"schema_version":3,"generation_basis":"semantic_functions_first","vacancy_understanding":{"role_family":"solution_architecture","specialization":"integration_architecture","summary":"Architecture design and integration","confidence":0.9},"functions":[{"function_id":"solution_design","normalized_function":"solution design","importance":"primary","market_terms":[{"value":"solution architecture","language":"en","derivation":"market_equivalent"}],"evidence":["Architecture design evidence"]}],"role_titles":[{"canonical_id":"solution_architect_en","title":"Solution Architect","language":"en","priority":"strong","derived_from_functions":["solution_design"],"evidence":["Architecture design evidence"]}],"leadership_intent":{"level":"individual_contributor","evidence":["No people management evidence"]},"unsupported_terms":[{"term":"UnconfirmedTechnology","reason":"Not grounded"}],"warnings":[]}'''


def ask(prompt, system_prompt):
    from app.integrations.ai import nano_client

    return nano_client.ask(prompt, system_prompt)


def _parse_response(raw):
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I | re.S).strip()
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise TypeError("root")
    return parsed


def generate_vacancy_search_plan_v3(vacancy):
    prepared = prepare_vacancy(copy.deepcopy(vacancy))
    prompt = "Interpret vacancy as Query Generation V3 JSON: " + json.dumps(prepared, ensure_ascii=False, default=str)
    raw = ask(prompt, V3_INTERPRETATION_SYSTEM)
    try:
        parsed = _parse_response(raw)
        version = parsed.get("schema_version")
        if type(version) is not int:
            raise ValueError("schema_version")
        if version == 3:
            interpretation = validate_search_interpretation_v3(parsed, prepared)
            plan = render_search_strategies_v3(interpretation)
            profile = build_pool_profile_v1_from_v3(interpretation, plan)
            selected = next(item for item in plan["strategies"] if item["id"] == plan["selected_strategy"])
            return {"interpretation": interpretation, "plan": plan, "query": selected["query"], "profile": profile}
        if version == 2:
            from app.integrations.ai import nano_client

            validated = nano_client.validate_vacancy_search_plan_v2(parsed, prepared)
            if validated["selected_strategy"] != "balanced":
                raise ValueError("selected_strategy")
            selected = next(item for item in validated["strategies"] if item["id"] == "balanced")
            profile = nano_client.build_pool_profile_v1_from_v2(validated)
            return {"interpretation": copy.deepcopy(validated), "plan": validated, "query": selected["query"], "profile": profile}
        raise ValueError("schema_version")
    except (json.JSONDecodeError, ValueError, TypeError, KeyError, StopIteration) as exc:
        raise AIUnavailableError("ИИ вернул некорректную интерпретацию поиска.", request_sent=True) from exc

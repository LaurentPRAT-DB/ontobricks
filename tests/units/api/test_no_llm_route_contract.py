"""Source-order contract for domain-level LLM route guards."""

import inspect

from api.routers.internal import mapping, ontology


ROUTES = [
    ontology.generate_business_rules_async,
    ontology.generate_ontology_async,
    ontology.auto_assign_icons,
    ontology.ontology_assistant_chat,
    ontology.ontology_assistant_invoke,
    mapping.generate_sql_from_prompt,
    mapping.start_auto_assign,
    mapping.single_auto_assign,
]


def test_every_ontology_and_mapping_llm_route_uses_domain_guard():
    for route in ROUTES:
        source = inspect.getsource(route)
        assert "require_domain_llm(" in source, route.__name__
        if "create_task(" in source:
            assert source.index("require_domain_llm(") < source.index("create_task(")

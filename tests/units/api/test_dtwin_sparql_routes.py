"""SPARQL route safety is enforced before session or warehouse access."""

import pytest

from api.routers.internal import dtwin
from back.core.errors import ValidationError


class JsonRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [dtwin.execute_sparql, dtwin.translate_sparql])
async def test_mutating_sparql_is_rejected_before_domain_lookup(route):
    request = JsonRequest({"query": "DELETE WHERE { ?s ?p ?o }"})

    with pytest.raises(ValidationError, match="read-only"):
        if route is dtwin.execute_sparql:
            await route(request, session_mgr=None, settings=None)
        else:
            await route(request, session_mgr=None)

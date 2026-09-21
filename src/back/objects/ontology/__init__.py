"""Ontology domain: OWL/RDFS/SHACL and industry import."""

from back.objects.ontology.json_views import (
    get_ontology_classes,
    get_ontology_info,
    get_ontology_properties,
)
from back.objects.ontology.Ontology import IndustryKind, Ontology, QUALITY_CATEGORIES
from back.objects.ontology.GenerateDraft import (
    GenerateDraft,
    GenerateDraftStore,
    GenerateEntity,
    compute_source_fingerprint,
)

__all__ = [
    "IndustryKind",
    "Ontology",
    "QUALITY_CATEGORIES",
    "GenerateDraft",
    "GenerateDraftStore",
    "GenerateEntity",
    "compute_source_fingerprint",
    "get_ontology_classes",
    "get_ontology_info",
    "get_ontology_properties",
]

``back.objects.ontology`` -- Ontology management (domain)
=========================================================

Ontology class
--------------

.. automodule:: back.objects.ontology.Ontology
   :members:
   :undoc-members:
   :show-inheritance:

Generate draft/contract layer (staged Generate)
------------------------------------------------

Durable draft/entity model, source fingerprinting, and the validation
primitives consumed by the staged agent entry points
(``agents.agent_owl_generator.staged``) and the async workflow below.

.. automodule:: back.objects.ontology.GenerateDraft
   :members:
   :undoc-members:
   :show-inheritance:

Generate workflow (staged Generate)
-------------------------------------

Orchestrates detect -> human review -> checkpointed relations/attributes/
axioms completion -> append-only merge, on top of ``GenerateDraft`` and the
staged agent entry points; exposed via the ``POST /ontology/wizard/
generate/detect``, ``GET/POST /ontology/wizard/generate/draft(/update|
/discard)``, and ``POST /ontology/wizard/generate/complete`` routes.

.. automodule:: back.objects.ontology.GenerateWorkflow
   :members:
   :undoc-members:
   :show-inheritance:

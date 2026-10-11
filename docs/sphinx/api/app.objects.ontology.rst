``back.objects.ontology`` -- Ontology management (domain)
=========================================================

Ontology facade
---------------

.. automodule:: back.objects.ontology.Ontology
   :members:
   :undoc-members:
   :show-inheritance:

OntologyClassModel class
------------------------

.. automodule:: back.objects.ontology.OntologyClassModel
   :members:
   :undoc-members:
   :show-inheritance:

OntologyEditor class
--------------------

.. automodule:: back.objects.ontology.OntologyEditor
   :members:
   :undoc-members:
   :show-inheritance:

OntologyEntityImport class
--------------------------

.. automodule:: back.objects.ontology.OntologyEntityImport
   :members:
   :undoc-members:
   :show-inheritance:

OntologyImport class
--------------------

.. automodule:: back.objects.ontology.OntologyImport
   :members:
   :undoc-members:
   :show-inheritance:

OntologyOwl class
-----------------

.. automodule:: back.objects.ontology.OntologyOwl
   :members:
   :undoc-members:
   :show-inheritance:

OntologyRules class
-------------------

.. automodule:: back.objects.ontology.OntologyRules
   :members:
   :undoc-members:
   :show-inheritance:

OntologyGroups class
--------------------

.. automodule:: back.objects.ontology.OntologyGroups
   :members:
   :undoc-members:
   :show-inheritance:

OntologyJsonViews class
-----------------------

.. automodule:: back.objects.ontology.json_views
   :members:
   :undoc-members:
   :show-inheritance:

Generate draft/contract layer (staged Generate)
------------------------------------------------

Durable draft/entity model, source fingerprinting, and the validation
primitives consumed by the staged agent entry points
(``agents.agent_owl_generator.staged``) and the async workflow below.

.. automodule:: back.objects.ontology.generate.GenerateDraft
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

.. automodule:: back.objects.ontology.generate.GenerateWorkflow
   :members:
   :undoc-members:
   :show-inheritance:

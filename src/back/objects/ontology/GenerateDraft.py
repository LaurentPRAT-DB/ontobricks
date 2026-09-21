"""Durable Generate draft models for the three-stage ontology Generate workflow.

See ``docs/superpowers/specs/2026-09-20-three-stage-ontology-generate-design.md``
for the full target contract. This module implements the **draft/contract
layer only** (plan task 2 of ``staged-ontology-generate``): stable entity
identity, the durable versioned draft, source fingerprinting/invalidation,
optimistic-concurrency persistence through :class:`DomainSession`, and the
validation primitives Stage 3 completion (task 3) and the async workflow
routes (task 4) will call. No agent, API, or UI code lives here.

Field names mirror the design doc's structured schemas exactly, except for
one documented adjustment: the design's illustrative ``ready parsed-document
corpus manifest`` uses ``name`` — the *actual* parsed-corpus manifest
contract (``docs/superpowers/specs/2026-09-18-parsed-document-corpus-design.md``)
uses ``filename`` and ``source_hash``. :func:`compute_source_fingerprint`
reads the real manifest field names so it can be fed manifests directly
without a translation layer.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field, replace
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
    TYPE_CHECKING,
)

from back.core.errors import ConflictError, ValidationError
from back.core.logging import get_logger

if TYPE_CHECKING:
    from back.objects.session.DomainSession import DomainSession

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Enumerations (kept as plain string constants — the persisted shape is a
# plain JSON-safe dict, matching the rest of DomainSession's session state).
# ---------------------------------------------------------------------------

DETECTING = "detecting"
REVIEWING = "reviewing"
COMPLETING = "completing"
DONE = "done"
_VALID_STAGES = frozenset({DETECTING, REVIEWING, COMPLETING, DONE})

ORIGIN_EXISTING = "existing"
ORIGIN_DETECTED = "detected"
ORIGIN_MANUAL = "manual"
_VALID_ORIGINS = frozenset({ORIGIN_EXISTING, ORIGIN_DETECTED, ORIGIN_MANUAL})

TYPE_CLASS = "class"
TYPE_OBJECT_PROPERTY = "object_property"
TYPE_DATA_PROPERTY = "data_property"
_VALID_TYPE_HINTS = frozenset({TYPE_CLASS, TYPE_OBJECT_PROPERTY, TYPE_DATA_PROPERTY})

SUBSTAGE_RELATIONS = "relations"
SUBSTAGE_ATTRIBUTES = "attributes"
SUBSTAGE_AXIOMS = "axioms"
_SUBSTAGE_ORDER = (SUBSTAGE_RELATIONS, SUBSTAGE_ATTRIBUTES, SUBSTAGE_AXIOMS)

CHECKPOINT_PENDING = "pending"
CHECKPOINT_RUNNING = "running"
CHECKPOINT_DONE = "done"
CHECKPOINT_FAILED = "failed"
_VALID_CHECKPOINT_STATUSES = frozenset(
    {CHECKPOINT_PENDING, CHECKPOINT_RUNNING, CHECKPOINT_DONE, CHECKPOINT_FAILED}
)


class DraftValidationError(ValidationError):
    """A draft/entity violates a structural or business invariant (400)."""


class DraftRevisionConflict(ConflictError):
    """A write supplied a stale ``draft_revision`` (409, optimistic concurrency)."""


class DraftStaleError(ConflictError):
    """A draft's ``source_fingerprint`` no longer matches the current source (409)."""


def _normalize_label(label: str) -> str:
    """Case/space-normalized form of a label, used for uniqueness checks."""
    return re.sub(r"\s+", " ", (label or "").strip()).casefold()


def new_candidate_id() -> str:
    """A fresh detection-time id for a new candidate entity (``cand-<hex12>``)."""
    return f"cand-{uuid.uuid4().hex[:12]}"


def _default_checkpoints() -> Dict[str, Dict[str, Any]]:
    return {s: {"status": CHECKPOINT_PENDING, "result": None} for s in _SUBSTAGE_ORDER}


def _normalize_checkpoints(raw: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Self-healing normalization used on the deserialize path.

    Unknown/missing substage entries or invalid statuses are coerced back to
    a safe ``pending`` default rather than raising — this is the boundary
    that must tolerate an older persisted schema (migration), unlike
    :meth:`GenerateDraft.with_checkpoint`, which is strict.

    Also repairs an **inconsistent chain**: a substage can only legitimately
    be ``done``/``running``/``failed`` if every predecessor is ``done`` —
    ``with_checkpoint()`` enforces this on every live transition, so a
    persisted draft where e.g. ``relations`` is ``pending``/``failed`` while
    ``attributes`` is ``done`` could never have been produced by the normal
    flow. Once a predecessor is found not ``done``, every later substage is
    forced back to ``pending`` (result cleared) regardless of what was
    stored — the break cascades forward, since none of those later
    substages could legitimately have started either.
    """
    raw = raw or {}
    normalized: Dict[str, Dict[str, Any]] = {}
    predecessor_done = True
    for substage in _SUBSTAGE_ORDER:
        entry = raw.get(substage) or {}
        status = entry.get("status")
        if status not in _VALID_CHECKPOINT_STATUSES:
            status = CHECKPOINT_PENDING

        if predecessor_done:
            result = entry.get("result")
        else:
            status = CHECKPOINT_PENDING
            result = None

        normalized[substage] = {"status": status, "result": result}
        predecessor_done = status == CHECKPOINT_DONE
    return normalized


@dataclass
class GenerateEntity:
    """One entity in a Generate draft: a locked existing anchor or a candidate.

    Both shapes (design doc's ``locked_anchor`` and ``candidate_entity``)
    share this single class — they differ only in ``origin``/``locked``/
    ``included`` values, not in structure. ``id`` is assigned once and never
    changes: renaming ``canonical_label`` or editing any other field never
    mints a new id (see design's Stable Identity section).
    """

    id: str
    canonical_label: str
    description: str = ""
    type_hint: str = TYPE_CLASS
    evidence: List[Dict[str, str]] = field(default_factory=list)
    alternate_labels: List[str] = field(default_factory=list)
    origin: str = ORIGIN_DETECTED
    included: bool = True
    locked: bool = False

    def __post_init__(self) -> None:
        if not self.id:
            raise DraftValidationError("Entity id is required.")
        if not self.canonical_label or not self.canonical_label.strip():
            raise DraftValidationError("Entity canonical_label is required.")
        if self.type_hint not in _VALID_TYPE_HINTS:
            raise DraftValidationError(f"Invalid type_hint: {self.type_hint!r}")
        if self.origin not in _VALID_ORIGINS:
            raise DraftValidationError(f"Invalid origin: {self.origin!r}")
        if self.locked and self.origin != ORIGIN_EXISTING:
            raise DraftValidationError("Only existing anchors may be locked.")
        if self.locked and not self.included:
            raise DraftValidationError("Locked anchors are always included.")
        self.evidence = [dict(e) for e in self.evidence]
        self.alternate_labels = [str(a) for a in self.alternate_labels]

    def normalized_labels(self) -> Set[str]:
        """Case/space-normalized canonical + alternate labels (dedup key set)."""
        labels = {_normalize_label(self.canonical_label)}
        labels.update(
            _normalize_label(a) for a in self.alternate_labels if a and a.strip()
        )
        labels.discard("")
        return labels

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "canonical_label": self.canonical_label,
            "description": self.description,
            "type_hint": self.type_hint,
            "evidence": [dict(e) for e in self.evidence],
            "alternate_labels": list(self.alternate_labels),
            "origin": self.origin,
            "included": self.included,
            "locked": self.locked,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GenerateEntity":
        """Deserialize, self-healing unknown enum values (migration boundary)."""
        data = data or {}
        locked = bool(data.get("locked", False))

        origin = str(data.get("origin", "") or "")
        if origin not in _VALID_ORIGINS:
            origin = ORIGIN_EXISTING if locked else ORIGIN_DETECTED

        type_hint = str(data.get("type_hint", "") or "")
        if type_hint not in _VALID_TYPE_HINTS:
            type_hint = TYPE_CLASS

        included = bool(data.get("included", True))
        if locked:
            included = True  # anchors are always counted (design invariant)

        return cls(
            id=str(data.get("id", "") or ""),
            canonical_label=str(data.get("canonical_label", "") or ""),
            description=str(data.get("description", "") or ""),
            type_hint=type_hint,
            evidence=[dict(e) for e in (data.get("evidence") or [])],
            alternate_labels=[str(a) for a in (data.get("alternate_labels") or [])],
            origin=origin,
            included=included,
            locked=locked,
        )

    @classmethod
    def new_candidate(
        cls,
        canonical_label: str,
        *,
        description: str = "",
        type_hint: str = TYPE_CLASS,
        evidence: Optional[List[Dict[str, str]]] = None,
        alternate_labels: Optional[List[str]] = None,
        origin: str = ORIGIN_DETECTED,
        included: bool = True,
        entity_id: Optional[str] = None,
    ) -> "GenerateEntity":
        """Build a new (non-locked) candidate — detected by Stage 1 or manual."""
        if origin not in (ORIGIN_DETECTED, ORIGIN_MANUAL):
            raise DraftValidationError(
                "New candidates must originate as 'detected' or 'manual'."
            )
        return cls(
            id=entity_id or new_candidate_id(),
            canonical_label=canonical_label,
            description=description,
            type_hint=type_hint,
            evidence=list(evidence or []),
            alternate_labels=list(alternate_labels or []),
            origin=origin,
            included=included,
            locked=False,
        )

    @classmethod
    def locked_anchor(
        cls,
        entity_id: str,
        canonical_label: str,
        *,
        alternate_labels: Optional[List[str]] = None,
        type_hint: str = TYPE_CLASS,
    ) -> "GenerateEntity":
        """Build a locked snapshot of an existing ontology entity."""
        return cls(
            id=entity_id,
            canonical_label=canonical_label,
            type_hint=type_hint,
            alternate_labels=list(alternate_labels or []),
            origin=ORIGIN_EXISTING,
            included=True,
            locked=True,
        )

    def with_updates(self, **kwargs: Any) -> "GenerateEntity":
        """Return a new entity with editable fields updated.

        ``id``, ``locked``, and ``origin`` never change here — identity is
        stable across rename (Stable Identity). A locked existing anchor
        rejects any edit outright: per the design's Stage 2 contract, its
        id/canonical label/alternate labels "cannot be changed from this
        screen" — only Stage 3 (out of scope here) may later enrich it.

        This is **strict construction**, not the ``from_dict`` migration
        boundary: an invalid enum value (e.g. an unknown ``type_hint``) is a
        live human-review edit, not an older persisted schema, so it raises
        :class:`DraftValidationError` immediately rather than being
        self-healed back to a default. Only ``from_dict`` (deserializing
        potentially-older persisted data) heals unknown enum values.
        """
        if self.locked and kwargs:
            raise DraftValidationError(
                f"Entity {self.id!r} is a locked existing anchor and cannot "
                "be edited from the review screen."
            )
        for immutable_field in ("id", "locked", "origin"):
            if immutable_field in kwargs and kwargs[immutable_field] != getattr(
                self, immutable_field
            ):
                raise DraftValidationError(f"{immutable_field!r} is immutable.")
        merged = {**self.to_dict(), **kwargs}
        return GenerateEntity(**merged)


def build_locked_anchors_from_classes(
    classes: Sequence[Dict[str, Any]],
) -> List[GenerateEntity]:
    """Snapshot current ontology classes as locked anchors.

    Existing ontology classes have no assigned UUID: ``name`` is already
    the stable identity used everywhere else in the ontology (domain/range,
    ``parent``, ``dataProperties``), so it doubles as the anchor's ``id``
    here — append mode never mints a competing id for an existing entity.
    """
    anchors: List[GenerateEntity] = []
    for cls in classes or []:
        name = cls.get("name") or ""
        if not name:
            continue
        anchors.append(
            GenerateEntity.locked_anchor(
                name,
                cls.get("label") or name,
                alternate_labels=list(cls.get("alternate_labels") or []),
            )
        )
    return anchors


def _sorted_source_config(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Order-independent copy of a source config (list values sorted)."""
    result: Dict[str, Any] = {}
    for key, value in (cfg or {}).items():
        if isinstance(value, list):
            result[key] = sorted(str(v) for v in value)
        else:
            result[key] = value
    return result


def compute_source_fingerprint(
    *,
    selected_source_config: Optional[Dict[str, Any]],
    ready_documents: Sequence[Dict[str, Any]] = (),
    existing_anchors: Sequence[GenerateEntity] = (),
) -> str:
    """Deterministic ``sha256:`` fingerprint over the detection-time source.

    Hashes only **identity** fields, never document content:

    - ``selected_source_config`` — the metadata/document selection (e.g.
      ``{"tables": [...], "documents": [...]}``), list-order independent;
    - ``ready_documents`` — parsed-corpus manifest identity only
      (``filename`` + ``source_hash``, per the manifest contract in
      ``docs/superpowers/specs/2026-09-18-parsed-document-corpus-design.md``);
      never manifest content;
    - ``existing_anchors`` — current ontology entity identity (``id`` +
      ``canonical_label``).

    Recompute with the *current* state and compare to a draft's
    ``source_fingerprint`` (or call :meth:`GenerateDraft.is_stale`) to detect
    a changed selection, a newly-ready document, or a changed ontology —
    all without re-reading any document body.
    """
    payload = {
        "selected_source_config": _sorted_source_config(selected_source_config),
        "ready_documents": sorted(
            (
                {
                    "filename": str(d.get("filename", d.get("name", ""))),
                    "source_hash": str(d.get("source_hash", "")),
                }
                for d in ready_documents
            ),
            key=lambda d: d["filename"],
        ),
        "existing_anchors": sorted(
            (
                {"id": a.id, "canonical_label": a.canonical_label}
                for a in existing_anchors
            ),
            key=lambda a: a["id"],
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class GenerateDraft:
    """The durable Generate draft persisted through ``DomainSession``.

    See the design doc's ``generate_draft`` structured schema. Persisted as
    a plain JSON-safe dict (:meth:`to_dict`/:meth:`from_dict`) under
    ``DomainSession.data["generate_draft"]`` — see :class:`GenerateDraftStore`
    for the persistence boundary with optimistic-concurrency revision checks.
    """

    draft_revision: int
    stage: str
    source_fingerprint: str
    selected_source_config: Dict[str, Any] = field(default_factory=dict)
    existing_anchors: List[GenerateEntity] = field(default_factory=list)
    candidate_entities: List[GenerateEntity] = field(default_factory=list)
    completion_checkpoints: Dict[str, Dict[str, Any]] = field(
        default_factory=_default_checkpoints
    )

    def __post_init__(self) -> None:
        if self.draft_revision < 0:
            raise DraftValidationError("draft_revision must be >= 0.")
        if self.stage not in _VALID_STAGES:
            raise DraftValidationError(f"Invalid stage: {self.stage!r}")
        for anchor in self.existing_anchors:
            if not anchor.locked:
                raise DraftValidationError(
                    f"existing_anchors must all be locked (got {anchor.id!r})."
                )
        for candidate in self.candidate_entities:
            if candidate.locked:
                raise DraftValidationError(
                    f"candidate_entities must not be locked (got {candidate.id!r})."
                )
        self.selected_source_config = copy.deepcopy(self.selected_source_config or {})
        self.completion_checkpoints = _normalize_checkpoints(self.completion_checkpoints)
        self._check_unique_labels()
        self._check_unique_ids()

    def _check_unique_ids(self) -> None:
        seen: Set[str] = set()
        for entity in (*self.existing_anchors, *self.candidate_entities):
            if entity.id in seen:
                raise DraftValidationError(f"Duplicate entity id: {entity.id!r}")
            seen.add(entity.id)

    def _check_unique_labels(self) -> None:
        seen: Dict[str, str] = {}
        for entity in (*self.existing_anchors, *self.candidate_entities):
            for label in entity.normalized_labels():
                owner = seen.get(label)
                if owner is not None and owner != entity.id:
                    raise DraftValidationError(
                        f"Duplicate normalized label {label!r} used by both "
                        f"{owner!r} and {entity.id!r}."
                    )
                seen[label] = entity.id

    # -- serialization -----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "draft_revision": self.draft_revision,
            "stage": self.stage,
            "source_fingerprint": self.source_fingerprint,
            "selected_source_config": copy.deepcopy(self.selected_source_config),
            "existing_anchors": [a.to_dict() for a in self.existing_anchors],
            "candidate_entities": [c.to_dict() for c in self.candidate_entities],
            "completion_checkpoints": {
                k: dict(v) for k, v in self.completion_checkpoints.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GenerateDraft":
        """Deserialize, self-healing an unknown/missing ``stage`` (migration)."""
        data = data or {}
        stage = str(data.get("stage", "") or "")
        if stage not in _VALID_STAGES:
            stage = DETECTING
        return cls(
            draft_revision=int(data.get("draft_revision", 0) or 0),
            stage=stage,
            source_fingerprint=str(data.get("source_fingerprint", "") or ""),
            selected_source_config=copy.deepcopy(
                data.get("selected_source_config") or {}
            ),
            existing_anchors=[
                GenerateEntity.from_dict(a) for a in (data.get("existing_anchors") or [])
            ],
            candidate_entities=[
                GenerateEntity.from_dict(c)
                for c in (data.get("candidate_entities") or [])
            ],
            completion_checkpoints=data.get("completion_checkpoints") or {},
        )

    @classmethod
    def new(
        cls,
        *,
        source_fingerprint: str,
        selected_source_config: Optional[Dict[str, Any]] = None,
        existing_anchors: Sequence[GenerateEntity] = (),
        candidate_entities: Sequence[GenerateEntity] = (),
        stage: str = REVIEWING,
    ) -> "GenerateDraft":
        """Build a fresh, not-yet-persisted draft (``draft_revision=0``).

        Pass to :meth:`GenerateDraftStore.save` to persist it as revision 1.
        """
        return cls(
            draft_revision=0,
            stage=stage,
            source_fingerprint=source_fingerprint,
            selected_source_config=dict(selected_source_config or {}),
            existing_anchors=list(existing_anchors),
            candidate_entities=list(candidate_entities),
            completion_checkpoints=_default_checkpoints(),
        )

    # -- staleness -----------------------------------------------------------

    def is_stale(self, current_fingerprint: str) -> bool:
        """``True`` iff *current_fingerprint* no longer matches this draft's."""
        return self.source_fingerprint != current_fingerprint

    def ensure_not_stale(self, current_fingerprint: str) -> None:
        """Raise :class:`DraftStaleError` unless the source is still current.

        Call before resuming or updating a draft (Review/Complete screens,
        Stage 3 substage start). Never silently reuses stale candidates or
        reparses to "refresh" the fingerprint — the only way past this is
        an explicit re-run of Stage 1 detection.
        """
        if self.is_stale(current_fingerprint):
            raise DraftStaleError(
                "Source fingerprint changed since detection; re-run "
                "detection before resuming or updating this draft."
            )

    # -- Stage 2 review mutations --------------------------------------------

    def with_candidate_added(self, entity: GenerateEntity) -> "GenerateDraft":
        """Add a brand-new (manual or detected) candidate."""
        if entity.locked:
            raise DraftValidationError("Cannot add a locked entity as a candidate.")
        if any(c.id == entity.id for c in self.candidate_entities):
            raise DraftValidationError(f"Candidate id {entity.id!r} already exists.")
        return replace(self, candidate_entities=[*self.candidate_entities, entity])

    def with_candidate_removed(self, entity_id: str) -> "GenerateDraft":
        """Delete a candidate row entirely (distinct from excluding it — see
        design's Stage 2: exclude keeps the row for later re-inclusion,
        remove deletes it and requires re-detection to bring it back)."""
        remaining = [c for c in self.candidate_entities if c.id != entity_id]
        if len(remaining) == len(self.candidate_entities):
            raise DraftValidationError(f"No candidate with id {entity_id!r} to remove.")
        return replace(self, candidate_entities=remaining)

    def with_candidate_updated(self, entity_id: str, **field_updates: Any) -> "GenerateDraft":
        """Edit one candidate's editable fields (canonical_label, description,
        type_hint, evidence, alternate_labels, included). ``id`` is stable."""
        updated: List[GenerateEntity] = []
        found = False
        for candidate in self.candidate_entities:
            if candidate.id == entity_id:
                found = True
                updated.append(candidate.with_updates(**field_updates))
            else:
                updated.append(candidate)
        if not found:
            raise DraftValidationError(f"No candidate with id {entity_id!r} to update.")
        return replace(self, candidate_entities=updated)

    def ensure_ready_for_completion(self) -> None:
        """Raise unless at least one entity is part of the validated set.

        Existing anchors always count (locked entities are always
        ``included``); this can only fail on a brand-new ontology with zero
        anchors and every candidate removed or excluded.
        """
        if not self.existing_anchors and not any(
            c.included for c in self.candidate_entities
        ):
            raise DraftValidationError(
                "At least one entity (existing anchor or included candidate) "
                "is required before Stage 3 can start."
            )

    # -- Stage 3 completion primitives (consumed by tasks 3/4) --------------

    def closed_entity_ids(self) -> Set[str]:
        """Ids referenceable by completion: locked anchors ∪ included candidates.

        Excluded candidates are deliberately absent — "excluded entities
        unavailable to completion" is enforced by simply never including
        their id in this set.
        """
        ids = {a.id for a in self.existing_anchors}
        ids.update(c.id for c in self.candidate_entities if c.included)
        return ids

    def validate_references(self, referenced_ids: Iterable[str], *, context: str = "") -> None:
        """Raise if any id in *referenced_ids* is outside the closed entity set.

        This is the entity-closure check a completion substage's output
        must pass before it is checkpointed (design's Entity Closure
        invariant) — reject-only, never a silent rewrite.
        """
        closed = self.closed_entity_ids()
        unknown = sorted({str(rid) for rid in referenced_ids} - closed)
        if unknown:
            prefix = f"{context}: " if context else ""
            raise DraftValidationError(
                f"{prefix}references unknown or excluded entity id(s): "
                + ", ".join(unknown)
            )

    def with_checkpoint(
        self, substage: str, status: str, *, result: Any = None
    ) -> "GenerateDraft":
        """Return a copy with one substage's checkpoint updated.

        Strict — unlike the deserialize path, this is a live state
        transition and enforces the design's ordering invariants directly:
        a substage's checkpoint cannot be touched before its predecessor is
        ``done``, and a substage already ``done`` cannot be reopened.
        """
        if substage not in _SUBSTAGE_ORDER:
            raise DraftValidationError(f"Unknown substage: {substage!r}")
        if status not in _VALID_CHECKPOINT_STATUSES:
            raise DraftValidationError(f"Invalid checkpoint status: {status!r}")

        idx = _SUBSTAGE_ORDER.index(substage)
        for predecessor in _SUBSTAGE_ORDER[:idx]:
            if self.completion_checkpoints[predecessor]["status"] != CHECKPOINT_DONE:
                raise DraftValidationError(
                    f"Cannot update {substage!r} before {predecessor!r} is done."
                )
        if self.completion_checkpoints[substage]["status"] == CHECKPOINT_DONE:
            raise DraftValidationError(
                f"{substage!r} checkpoint is already done and cannot be reopened."
            )

        new_checkpoints = {k: dict(v) for k, v in self.completion_checkpoints.items()}
        new_checkpoints[substage] = {"status": status, "result": result}
        return replace(self, completion_checkpoints=new_checkpoints)

    def next_pending_substage(self) -> Optional[str]:
        """First substage (in strict order) not yet checkpointed ``done``."""
        for substage in _SUBSTAGE_ORDER:
            if self.completion_checkpoints[substage]["status"] != CHECKPOINT_DONE:
                return substage
        return None


class GenerateDraftStore:
    """Persists/loads the durable Generate draft through a ``DomainSession``.

    Thin persistence boundary: converts between the typed
    :class:`GenerateDraft` and the raw dict ``DomainSession`` stores under
    ``data["generate_draft"]``, and enforces optimistic-concurrency revision
    checks on every write so a stale read-modify-write (e.g. from a reloaded
    tab) cannot silently clobber a newer draft. Callers still own calling
    the session's own ``save()`` (already done here for convenience, mirroring
    every other ``DomainSession`` mutation helper).
    """

    def __init__(self, session: "DomainSession") -> None:
        self._session = session

    def load(self) -> Optional[GenerateDraft]:
        """Return the persisted draft, or ``None`` if none exists.

        ``GenerateDraft.from_dict`` already self-heals unknown/missing
        per-field enum values (the ordinary schema-migration path), but a
        handful of invariants can only be checked once the whole draft is
        assembled (unique entity ids, unique normalized labels, a required
        ``id``/``canonical_label`` on every entity). A persisted draft that
        still fails one of *those* is corrupted beyond safe per-field
        repair — rather than raise and take down whatever screen is trying
        to resume, treat it exactly like "no draft": force re-detection.
        """
        raw = self._session.data.get("generate_draft")
        if not raw:
            return None
        try:
            return GenerateDraft.from_dict(raw)
        except DraftValidationError:
            logger.warning(
                "Discarding structurally invalid persisted Generate draft "
                "(forcing re-detection).",
                exc_info=True,
            )
            return None

    def save(self, draft: GenerateDraft) -> GenerateDraft:
        """Persist *draft*, enforcing optimistic concurrency.

        ``draft.draft_revision`` must equal the currently-persisted
        revision (``0`` when no draft is persisted yet — i.e. this is the
        first save). On success the persisted revision is bumped by 1 and
        the bumped copy is both stored and returned.
        """
        current = self.load()
        current_revision = current.draft_revision if current else 0
        if draft.draft_revision != current_revision:
            raise DraftRevisionConflict(
                f"Draft revision conflict: submitted revision "
                f"{draft.draft_revision!r} does not match current revision "
                f"{current_revision!r}. Reload the draft and retry."
            )
        saved = replace(draft, draft_revision=current_revision + 1)
        self._session.data["generate_draft"] = saved.to_dict()
        self._session.save()
        return saved

    def reset(self) -> None:
        """Discard the persisted draft (explicit discard, or re-detection)."""
        self._session.data["generate_draft"] = None
        self._session.save()

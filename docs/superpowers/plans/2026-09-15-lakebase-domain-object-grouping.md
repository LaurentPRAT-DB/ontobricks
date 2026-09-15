# Lakebase Domain Object Grouping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Group Lakebase adjacency and entity-search tables under their owning domain/version card and include them in grouped deletion.

**Architecture:** Keep the Lakebase objects API unchanged. Extend the existing pure name-normalization helper in `settings.js`; all existing card rendering, count, individual deletion, and grouped deletion behavior will then consume the corrected base key automatically.

**Tech Stack:** JavaScript, Jinja-rendered Settings UI, Python `pytest` source contracts.

## Global Constraints

- Recognized table suffixes are exactly `_sync`, `__app`, `_adj_in`, `_adj_out`, and `_entity_search`.
- Views and unrelated table names retain their complete names as the group base.
- Existing view-first grouped deletion order remains unchanged.
- Do not change the Lakebase API, physical table names, Lakehouse grouping, or Neo4j behavior.
- Run all Python commands through `uv run --frozen`.

---

### Task 1: Normalize Lakebase graph-index table names

**Files:**
- Modify: `src/front/static/config/js/settings.js:2934-2977`
- Create: `tests/units/front/test_lakebase_object_grouping.py`
- Modify: `docs/user-guide.md`
- Modify: `changelogs/v0.9.0/benoitcayladbx_2026-09-15.log`

**Interfaces:**
- Consumes: Lakebase object rows shaped as `{kind: "table"|"view", schemaName: string, name: string}`.
- Produces: `objectBase(name: string, kind: string) -> string`, used as the `_lkDomainRegistry` key.

- [x] **Step 1: Write the failing frontend contract**

Create `tests/units/front/test_lakebase_object_grouping.py`:

```python
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SETTINGS_JS = Path("src/front/static/config/js/settings.js")


def _object_base_block() -> str:
    source = SETTINGS_JS.read_text(encoding="utf-8")
    start = source.index("function objectBase(name, kind)")
    end = source.index("\n            function kindBadge", start)
    return source[start:end]


def test_lakebase_domain_grouping_recognizes_all_owned_table_suffixes():
    block = _object_base_block()
    for suffix in ("_sync", "__app", "_adj_in", "_adj_out", "_entity_search"):
        assert f"'{suffix}'" in block


def test_lakebase_domain_grouping_keeps_unrecognized_names_unchanged():
    assert "return name;" in _object_base_block()


def test_lakebase_group_count_and_delete_registry_use_normalized_base():
    source = SETTINGS_JS.read_text(encoding="utf-8")
    assert "const base = objectBase(o.name, o.kind);" in source
    assert "_lkDomainRegistry[base].items.push(o);" in source
    assert "+ grp.items.length +" in source
    assert "grp.sortedItems = sorted;" in source
```

- [x] **Step 2: Run the test to verify it fails**

Run:

```bash
uv run --frozen pytest -q tests/units/front/test_lakebase_object_grouping.py
```

Expected: the suffix contract fails because `objectBase` only recognizes `_sync` and `__app`.

- [x] **Step 3: Extend the normalization helper**

Replace the table branch in `objectBase` with:

```javascript
function objectBase(name, kind) {
    if (kind === 'table') {
        const domainSuffixes = [
            '_entity_search',
            '_adj_out',
            '_adj_in',
            '__app',
            '_sync',
        ];
        const suffix = domainSuffixes.find(candidate => name.endsWith(candidate));
        if (suffix) return name.slice(0, -suffix.length);
    }
    return name;
}
```

This keeps normalization explicit and avoids grouping unrelated suffixes.

- [x] **Step 4: Run focused tests**

Run:

```bash
uv run --frozen pytest -q \
  tests/units/front/test_lakebase_object_grouping.py \
  tests/units/settings/test_lakebase_provision.py
```

Expected: all tests pass.

- [x] **Step 5: Update user documentation and changelog**

In `docs/user-guide.md`, document that Settings → Lakebase → Objects groups the
reader view, `_sync`, `__app`, `_adj_in`, `_adj_out`, and `_entity_search` under
one domain/version card, and that grouped deletion removes all listed objects.

Append an English section to
`changelogs/v0.9.0/benoitcayladbx_2026-09-15.log` with context, numbered file
changes, modified files, and test results.

- [x] **Step 6: Run full verification**

Run:

```bash
uv run --frozen pytest -q -m "not scenario"
```

Expected: the non-scenario suite passes.

- [ ] **Step 7: Commit**

```bash
git add \
  src/front/static/config/js/settings.js \
  tests/units/front/test_lakebase_object_grouping.py \
  docs/user-guide.md \
  changelogs/v0.9.0/benoitcayladbx_2026-09-15.log \
  docs/superpowers/plans/2026-09-15-lakebase-domain-object-grouping.md
git commit -m "fix(settings): group Lakebase graph indexes by domain"
```

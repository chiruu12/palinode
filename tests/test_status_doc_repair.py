"""Status-doc frontmatter integrity + the one-time repair pass.

The report behind the status-doc YAML repair filed the symptom — ``entities:`` holding
freeform ``[2026-05-24] …`` status sentences, which strict YAML reads as a flow-sequence
opener — and hypothesised a serialization bug in "whatever writes the status doc". The
actual mechanism is different and is what these tests pin:

1. ``fact_ids.add_fact_ids_to_file`` tagged **every** ``- item`` line in the
   file, YAML frontmatter included, so ``- project/infrastructure`` under
   ``entities:`` acquired a ``<!-- fact:… -->`` marker and became a distinct
   entity reference;
2. the executor's fact regexes are whole-file + MULTILINE, so a later UPDATE /
   SUPERSEDE rewrote that frontmatter line with LLM-supplied status prose, and
   ARCHIVE deleted it outright (leaving ``entities: null``).

Every frontmatter writer in the codebase already goes through ``yaml.dump``,
which quotes ``[``-leading scalars — so no writer could have produced the
reported YAML. The corruption was post-hoc. The fix is a frontmatter boundary,
not a serializer change.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from palinode.cli.repair_status import repair_status
from palinode.consolidation import fact_ids as fact_ids_mod
from palinode.consolidation.executor import apply_operations
from palinode.consolidation.status_doc import (
    FRONTMATTER_RE,
    desired_frontmatter,
    fact_ids,
    repair_status_doc,
    split_frontmatter,
    strip_frontmatter_fact_markers,
)
from palinode.core.config import config
from tests._store_helpers import upsert_entities


@pytest.fixture(autouse=True)
def _default_write_choke_point_memory_dir(tmp_path, monkeypatch):
    # write_memory_file (fact_ids' and the executor's write primitive)
    # validates its target resolves inside config.memory_dir — every
    # fixture/test in this module writes its memory file under tmp_path, so
    # point memory_dir there by default. Named distinctly from the
    # `_memory_dir` plain helper below (not a fixture) so this autouse
    # fixture isn't shadowed by that later same-name module-level def.
    # Fixtures further down that already set it explicitly (e.g.
    # _isolate_store) just re-set the same value.
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))


#: Undamaged, but *not* canonical: its frontmatter does not yet carry the
#: derived ``memory_count`` / ``date_range``, so a repair legitimately adds
#: them. Deliberately not named "clean" — a "leaves a clean document alone"
#: test written against this fixture can never assert byte identity, which is
#: how the always-dirty report went unnoticed.
WELL_FORMED_DOC = (
    "---\n"
    "id: project-infra-status\n"
    "category: project\n"
    "entities:\n"
    "- project/infrastructure\n"
    "---\n\n"
    "# Infra Status\n\n"
    "- [2026-05-01] A genuine fact. <!-- fact:infra-a1b2c3 -->\n"
)

#: Genuinely clean: derived keys already describe the body, and the YAML is
#: byte-for-byte what ``render`` emits. ``repair_status_doc`` must hand this
#: back unchanged — including ``last_updated``.
CANONICAL_DOC = (
    "---\n"
    "id: project-infra-status\n"
    "category: project\n"
    "entities:\n"
    "- project/infrastructure\n"
    "memory_count: 1\n"
    "date_range: 2026-05-01 to 2026-05-01\n"
    "last_updated: '2026-05-01T00:00:00+00:00'\n"
    "---\n\n"
    "# Infra Status\n\n"
    "- [2026-05-01] A genuine fact. <!-- fact:infra-a1b2c3 -->\n"
)

#: The substantive repair counters, with the status flags removed.
NO_REPAIRS = {
    "frontmatter_markers_stripped": 0,
    "entities_relocated": 0,
    "log_lines_elided": 0,
    "log_ids_unresolved": 0,
}


def _counters(report: dict) -> dict:
    """*report* minus ``changed`` / ``noncanonical`` / ``unparseable``."""
    return {
        k: v for k, v in report.items()
        if k not in ("changed", "noncanonical", "unparseable")
    }

# The reported corruption shape, reproduced: a frontmatter entity that was
# fact-tagged and then rewritten by the executor with status prose.
CORRUPTED_DOC = (
    "---\n"
    "id: project-infra-status\n"
    "category: project\n"
    "entities:\n"
    "- [2026-05-24] Infra is in active, daily evolution #status: active"
    " <!-- fact:infra-status-dbba20 -->\n"
    "- project/infrastructure\n"
    "---\n\n"
    "# Infra Status\n\n"
    "- [2026-05-01] A genuine fact. <!-- fact:infra-a1b2c3 -->\n\n"
    "## Consolidation Log\n\n"
    "### 2026-06-01\n"
    "- [KEEP] infra-a1b2c3: \n"
    "- [ARCHIVE] supersedes-supersedes-supersedes-infra-status-dbba20: \n"
    "- [SUPERSEDE] [the original, non-superseded status if unique]: \n"
    "- [UPDATE] infra-a1b2c3: rewrote the date prefix\n\n"
    "- [2026-06-01] Session: shipped the thing\n"
)


def _strict_parse(text: str) -> dict:
    match = FRONTMATTER_RE.match(text)
    assert match is not None
    return yaml.safe_load(match.group(1))


# ── prevention: the frontmatter boundary ─────────────────────────────────────


def test_fact_ids_never_tag_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setattr(config.git, "auto_commit", False)
    path = tmp_path / "infra-status.md"
    path.write_text(WELL_FORMED_DOC, encoding="utf-8")

    added = fact_ids_mod.add_fact_ids_to_file(str(path))

    text = path.read_text(encoding="utf-8")
    frontmatter_block, body = split_frontmatter(text)
    assert added == 0  # the one body bullet already carries an id
    assert "<!-- fact:" not in frontmatter_block
    assert _strict_parse(text)["entities"] == ["project/infrastructure"]
    assert "<!-- fact:infra-a1b2c3 -->" in body


def test_fact_ids_still_tag_body_items(tmp_path, monkeypatch):
    monkeypatch.setattr(config.git, "auto_commit", False)
    path = tmp_path / "infra-status.md"
    path.write_text(WELL_FORMED_DOC + "- [2026-05-02] An untagged fact.\n", encoding="utf-8")

    added = fact_ids_mod.add_fact_ids_to_file(str(path))

    text = path.read_text(encoding="utf-8")
    assert added == 1
    assert "An untagged fact. <!-- fact:" in text
    assert "<!-- fact:" not in split_frontmatter(text)[0]


def test_executor_never_rewrites_frontmatter(tmp_path, monkeypatch):
    """A legacy file whose frontmatter carries a fact marker: the executor must
    leave the frontmatter byte-for-byte alone."""
    monkeypatch.setattr(config.git, "auto_commit", False)
    path = tmp_path / "infra-status.md"
    path.write_text(CORRUPTED_DOC, encoding="utf-8")
    before = split_frontmatter(CORRUPTED_DOC)[0]

    stats = apply_operations(str(path), [
        {"op": "UPDATE", "id": "infra-status-dbba20",
         "new_text": "[2026-06-10] LLM prose that must not land in YAML"},
        {"op": "ARCHIVE", "id": "infra-status-dbba20"},
    ])

    after = split_frontmatter(path.read_text(encoding="utf-8"))[0]
    assert after == before
    assert stats["updated"] == 0 and stats["archived"] == 0


def test_executor_still_mutates_body_facts(tmp_path, monkeypatch):
    monkeypatch.setattr(config.git, "auto_commit", False)
    path = tmp_path / "infra-status.md"
    path.write_text(WELL_FORMED_DOC, encoding="utf-8")

    stats = apply_operations(str(path), [
        {"op": "UPDATE", "id": "infra-a1b2c3", "new_text": "[2026-05-01] Revised."},
    ])

    text = path.read_text(encoding="utf-8")
    assert stats["updated"] == 1
    assert "Revised. <!-- fact:infra-a1b2c3 -->" in text
    assert _strict_parse(text)["entities"] == ["project/infrastructure"]


# ── repair ───────────────────────────────────────────────────────────────────


def test_repair_makes_frontmatter_strict_parseable():
    # Precondition: the corrupted shape really does break strict YAML.
    raw = FRONTMATTER_RE.match(CORRUPTED_DOC)
    try:
        yaml.safe_load(raw.group(1))
        broke = False
    except yaml.YAMLError:
        broke = True
    assert broke, "fixture no longer reproduces the #470 parse failure"

    repaired, report = repair_status_doc(CORRUPTED_DOC)

    meta = _strict_parse(repaired)
    assert meta["entities"] == ["project/infrastructure"]
    assert report["frontmatter_markers_stripped"] == 1
    assert report["entities_relocated"] == 1


def test_repair_relocates_rather_than_drops_non_entity_values():
    repaired, _ = repair_status_doc(CORRUPTED_DOC)
    assert "## Recovered from frontmatter" in repaired
    assert "Infra is in active, daily evolution" in split_frontmatter(repaired)[1]


def test_repair_elides_uninformative_and_unresolvable_log_lines():
    repaired, report = repair_status_doc(CORRUPTED_DOC)
    body = split_frontmatter(repaired)[1]

    # Blank KEEP and the prose-as-id line are gone; the self-nesting chain is
    # replaced by the sentinel; the informative line survives verbatim.
    assert "- [KEEP] infra-a1b2c3:" not in body
    assert "the original, non-superseded status" not in body
    assert "supersedes-supersedes" not in body
    assert "- [ARCHIVE] (unresolved):" in body
    assert "- [UPDATE] infra-a1b2c3: rewrote the date prefix" in body
    assert report["log_lines_elided"] == 2
    assert report["log_ids_unresolved"] == 1


def test_repair_preserves_session_bullets_and_facts():
    repaired, _ = repair_status_doc(CORRUPTED_DOC)
    assert "- [2026-06-01] Session: shipped the thing" in repaired
    assert "<!-- fact:infra-a1b2c3 -->" in repaired


def test_repair_keeps_the_elision_bullets_own_fact_marker():
    """The repair pass re-renders the elision bullet too, and must not re-id it.

    Same defect, second caller: ``repair_status_doc`` parses the elision line
    through the same helper as the write path and rebuilds it from the counters.
    The bullet is a body bullet that ``bootstrap-ids`` has tagged, so dropping
    the marker hands the next bootstrap pass a fresh id and orphans every
    reference to the old one.
    """
    doc = CORRUPTED_DOC.replace(
        "## Consolidation Log\n\n",
        "## Consolidation Log\n\n"
        "- _[log elided] 7 operation line(s) across 2 date block(s) — "
        "2026-05-01 → 2026-05-30. Full detail in git history._"
        " <!-- fact:infra-status-e1d0c9 -->\n\n",
    )

    repaired, _ = repair_status_doc(doc)

    body = split_frontmatter(repaired)[1]
    elided = [ln for ln in body.splitlines() if ln.startswith("- _[log elided]")]
    assert len(elided) == 1
    # Two garbage log lines were elided into it, so the counters moved — which
    # is exactly when the marker used to fall off.
    assert "9 operation line(s)" in elided[0]
    assert elided[0].endswith("<!-- fact:infra-status-e1d0c9 -->")
    assert "infra-status-e1d0c9" in fact_ids(body)


def test_repair_reconciles_frontmatter_counts():
    repaired, _ = repair_status_doc(CORRUPTED_DOC)
    meta = _strict_parse(repaired)
    body = split_frontmatter(repaired)[1]
    assert meta["memory_count"] == len(fact_ids(body)) == 1
    assert meta["date_range"] == "2026-05-01 to 2026-06-01"


@pytest.mark.parametrize(
    "doc", [CANONICAL_DOC, WELL_FORMED_DOC, CORRUPTED_DOC],
    ids=["canonical", "well-formed", "corrupted"],
)
def test_repair_is_idempotent(doc):
    """A second pass must be a true no-op — *including* ``last_updated``.

    This assertion used to exempt the timestamp ("last_updated moves; everything else
    must be stable"), which is exactly the behaviour the repair-status cleanliness fix
    reports: the receipt moved on every pass, so `changed` was always true and a clean
    store could never report zero. """
    once, _ = repair_status_doc(doc)
    twice, report = repair_status_doc(once)
    assert twice == once  # byte-for-byte, timestamp included
    assert report["changed"] is False
    assert _counters(report) == NO_REPAIRS


def test_repair_resolves_ids_from_the_history_sibling():
    """A legitimately ARCHIVE'd fact lives on in ``-history.md`` — its log entry
    must keep its id rather than being flagged unresolved."""
    repaired, _ = repair_status_doc(
        CORRUPTED_DOC, extra_known_ids={"supersedes-supersedes-supersedes-infra-status-dbba20"}
    )
    assert "- [ARCHIVE] supersedes-supersedes-supersedes-infra-status-dbba20:" in repaired


def test_repair_leaves_a_clean_document_alone():
    """The whole document, not just the body — that gap is what hid the repair-status
cleanliness fix."""
    repaired, report = repair_status_doc(CANONICAL_DOC)
    assert repaired == CANONICAL_DOC
    assert report["changed"] is False
    assert report["noncanonical"] is False
    assert _counters(report) == NO_REPAIRS


def test_missing_derived_keys_is_reported_as_changed():
    """Undamaged but not canonical: the derived keys are absent, so adding
    them *is* a change and the report must say so rather than claim clean."""
    repaired, report = repair_status_doc(WELL_FORMED_DOC)
    assert report["changed"] is True
    assert _counters(report) == NO_REPAIRS
    meta = _strict_parse(repaired)
    assert meta["memory_count"] == 1
    assert meta["date_range"] == "2026-05-01 to 2026-05-01"


def test_noncanonical_yaml_is_reported_without_claiming_damage():
    """Semantically correct, non-canonical serialization: flagged separately so
    the headline repair count keeps meaning "something is wrong"."""
    doc = CANONICAL_DOC.replace(
        "date_range: 2026-05-01 to 2026-05-01",
        'date_range: "2026-05-01 to 2026-05-01"',  # same value, quoted
    )
    repaired, report = repair_status_doc(doc)
    assert report["changed"] is False
    assert report["noncanonical"] is True
    assert repaired == CANONICAL_DOC  # normalized, receipt untouched
    assert _strict_parse(repaired)["last_updated"] == "2026-05-01T00:00:00+00:00"


@pytest.mark.parametrize("content", [
    "no frontmatter at all\n",
    "---\nkey: [unclosed\n---\n\nbody\n",
    "---\n- just\n- a\n- list\n---\n\nbody\n",
], ids=["absent", "unparseable", "not-a-mapping"])
def test_desired_frontmatter_returns_none_when_it_cannot_decide(content):
    """The never-destroy-a-file invariant, in the type rather than a docstring."""
    assert desired_frontmatter(content) is None


def _isolate_store(tmp_path: Path, monkeypatch) -> None:
    """Point BOTH ``memory_dir`` and ``db_path`` at ``tmp_path``.

    ``db_path`` is resolved to an **absolute** path when config loads, so
    patching ``memory_dir`` alone does not move it — the store still opens at
    the process default, which on a developer machine is their real database.
    Every helper in this module that reaches code opening a store (the CLI
    paths, ``store.get_db()``) goes through here so the pair cannot drift apart
    again. ``tests/conftest.py`` fails any test that writes the default DB.

    ``PALINODE_ALLOW_FRESH_DB`` is required, not incidental: ``_ensure_db``
    refuses to auto-create a database when ``memory_dir`` already holds ``.md``
    files, because that combination usually means a misconfigured path rather
    than a first run. These fixtures write fixture documents and *then* open a
    store, which is exactly that shape. It never fired before because the store
    being opened was the real one, which already existed.
    """
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setenv("PALINODE_ALLOW_FRESH_DB", "1")

    # Create the schema too. Redirecting the path alone yields an empty file
    # with no tables, and the tests that query the index were previously
    # satisfied by the real database's schema — which is the clearest statement
    # of what this defect was: not a stray file, but tests reading and writing a
    # live store.
    from palinode.core import store as _store

    _store.init_db()


# ── CLI ──────────────────────────────────────────────────────────────────────


def _memory_dir(tmp_path: Path, monkeypatch) -> Path:
    projects = tmp_path / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    (projects / "infra-status.md").write_text(CORRUPTED_DOC, encoding="utf-8")
    _isolate_store(tmp_path, monkeypatch)
    return projects


def test_cli_dry_run_reports_without_writing(tmp_path, monkeypatch):
    projects = _memory_dir(tmp_path, monkeypatch)
    before = (projects / "infra-status.md").read_bytes()

    result = CliRunner().invoke(repair_status, [])

    assert result.exit_code == 0, result.output
    assert "would repair" in result.output
    assert (projects / "infra-status.md").read_bytes() == before


def test_cli_execute_writes_repaired_file(tmp_path, monkeypatch):
    projects = _memory_dir(tmp_path, monkeypatch)

    result = CliRunner().invoke(repair_status, ["--execute"])

    assert result.exit_code == 0, result.output
    text = (projects / "infra-status.md").read_text(encoding="utf-8")
    assert _strict_parse(text)["entities"] == ["project/infrastructure"]


# ── store-wide marker strip (--scope all) ────────────────────────────────────

# `bootstrap_all_fact_ids` walks people/ projects/ decisions/ insights/, so a
# curated non-status memory could be tagged too. A marker lands *inside* the
# entity ref's string value, which forks it into its own entity-graph node.
TAGGED_PERSON = (
    "---\n"
    "id: person-alice\n"
    "category: people\n"
    "entities:\n"
    "- person/alice <!-- fact:alice-bio-048fc0 -->\n"
    "source_agents:\n"
    "- claude <!-- fact:alice-bio-9a1b2c -->\n"
    "---\n\n"
    "# Alice\n\n"
    "- [2026-05-01] A curated biographical fact. <!-- fact:alice-bio-abc123 -->\n"
)


def test_strip_frontmatter_fact_markers_is_surgical():
    """Only the marker text changes — the parsed structure is otherwise equal."""
    before = _strict_parse(TAGGED_PERSON)
    stripped, count = strip_frontmatter_fact_markers(TAGGED_PERSON)
    after = _strict_parse(stripped)

    assert count == 2
    assert after == {
        "id": "person-alice",
        "category": "people",
        "entities": ["person/alice"],
        "source_agents": ["claude"],
    }
    assert set(before) == set(after)
    # The body — including its legitimate fact marker — is untouched.
    assert split_frontmatter(stripped)[1] == split_frontmatter(TAGGED_PERSON)[1]
    assert "<!-- fact:alice-bio-abc123 -->" in stripped


def test_strip_frontmatter_fact_markers_is_idempotent_and_inert_when_clean():
    once, first = strip_frontmatter_fact_markers(TAGGED_PERSON)
    twice, second = strip_frontmatter_fact_markers(once)
    assert second == 0 and twice == once
    assert strip_frontmatter_fact_markers(WELL_FORMED_DOC) == (WELL_FORMED_DOC, 0)


def test_strip_frontmatter_drops_a_list_entry_that_was_only_a_marker():
    """Stripping must not leave a `- ` line that parses as a null list member."""
    doc = (
        "---\nid: x\nentities:\n- person/alice <!-- fact:a1 -->\n"
        "- <!-- fact:a2 -->\n---\n\n# X\n"
    )
    stripped, count = strip_frontmatter_fact_markers(doc)
    assert count == 2
    assert _strict_parse(stripped)["entities"] == ["person/alice"]


def _mixed_store(tmp_path: Path, monkeypatch) -> Path:
    for directory in ("people", "projects", "decisions", "insights"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    (tmp_path / "projects" / "infra-status.md").write_text(
        CORRUPTED_DOC, encoding="utf-8")
    (tmp_path / "people" / "alice.md").write_text(TAGGED_PERSON, encoding="utf-8")
    (tmp_path / "decisions" / "d1.md").write_text(TAGGED_PERSON, encoding="utf-8")
    (tmp_path / "insights" / "i1.md").write_text(WELL_FORMED_DOC, encoding="utf-8")
    _isolate_store(tmp_path, monkeypatch)
    return tmp_path


def test_default_scope_leaves_non_status_files_alone(tmp_path, monkeypatch):
    """The narrow default must not touch curated memories."""
    store = _mixed_store(tmp_path, monkeypatch)
    before = (store / "people" / "alice.md").read_bytes()

    result = CliRunner().invoke(repair_status, ["--execute"])

    assert result.exit_code == 0, result.output
    assert (store / "people" / "alice.md").read_bytes() == before


def test_scope_all_strips_markers_across_every_memory_dir(tmp_path, monkeypatch):
    store = _mixed_store(tmp_path, monkeypatch)

    result = CliRunner().invoke(repair_status, ["--scope", "all", "--execute"])

    assert result.exit_code == 0, result.output
    for rel in ("people/alice.md", "decisions/d1.md"):
        text = (store / rel).read_text(encoding="utf-8")
        assert "<!-- fact:" not in split_frontmatter(text)[0]
        assert _strict_parse(text)["entities"] == ["person/alice"]
        # markers-only: the body keeps its own fact marker verbatim
        assert "<!-- fact:alice-bio-abc123 -->" in text
    assert "markers only" in result.output


def test_scope_all_does_not_rewrite_non_status_bodies(tmp_path, monkeypatch):
    """Non-status files get the marker strip and nothing else — no re-dump, no
    log rewrite, no entity relocation."""
    store = _mixed_store(tmp_path, monkeypatch)
    body_before = split_frontmatter(
        (store / "people" / "alice.md").read_text(encoding="utf-8"))[1]

    CliRunner().invoke(repair_status, ["--scope", "all", "--execute"])

    text = (store / "people" / "alice.md").read_text(encoding="utf-8")
    assert split_frontmatter(text)[1] == body_before
    # Key order preserved (no yaml re-dump reordering).
    assert list(_strict_parse(text)) == ["id", "category", "entities", "source_agents"]


def test_scope_all_dry_run_writes_nothing(tmp_path, monkeypatch):
    store = _mixed_store(tmp_path, monkeypatch)
    snapshot = {
        p: p.read_bytes() for p in store.rglob("*.md")
    }

    result = CliRunner().invoke(repair_status, ["--scope", "all"])

    assert result.exit_code == 0, result.output
    assert all(p.read_bytes() == blob for p, blob in snapshot.items())
    assert "would repair" in result.output


def test_set_entities_for_path_replaces_rather_than_accumulates(tmp_path, monkeypatch):
    """A corrected entity ref must remove the old row, not add a second one.

    `upsert_entities` only ever INSERTs, so a changed ref writes a new row and
    orphans the old one. The stale node then survives both the file
    repair and a full `palinode reindex`.
    """

    from palinode.core import store

    _isolate_store(tmp_path, monkeypatch)
    (tmp_path / "projects").mkdir(exist_ok=True)
    fp = str(tmp_path / "projects" / "thing.md")

    # Seed the index the way the old tagger left it: a polluted ref.
    upsert_entities(fp, {"category": "projects",
                               "entities": ["person/alice <!-- fact:thing-abc123 -->"]})
    db = store.get_db()
    before = [r[0] for r in db.execute(
        "SELECT entity_ref FROM entities WHERE file_path = ?", (fp,)).fetchall()]
    db.close()
    assert before == ["person/alice <!-- fact:thing-abc123 -->"]

    # Repair corrects the ref.
    store.set_entities_for_path(fp, ["person/alice"])

    db = store.get_db()
    after = [r[0] for r in db.execute(
        "SELECT entity_ref FROM entities WHERE file_path = ?", (fp,)).fetchall()]
    db.close()
    assert after == ["person/alice"], f"stale ref survived: {after}"


def test_repair_execute_repoints_the_index(tmp_path, monkeypatch):
    """`--execute` must leave the index agreeing with the file it just fixed."""
    from click.testing import CliRunner

    from palinode.cli.repair_status import repair_status
    from palinode.core import store

    _isolate_store(tmp_path, monkeypatch)
    d = tmp_path / "insights"
    d.mkdir()
    fp = d / "note.md"
    fp.write_text(
        "---\nid: insights-note\ncategory: insights\nentities:\n"
        "- person/alice <!-- fact:note-048fc0 -->\n---\n\n- A fact.\n",
        encoding="utf-8",
    )
    upsert_entities(str(fp), {"category": "insights",
                                    "entities": ["person/alice <!-- fact:note-048fc0 -->"]})

    res = CliRunner().invoke(repair_status, ["--scope", "all", "--execute"])
    assert res.exit_code == 0, res.output

    assert "fact:" not in fp.read_text(encoding="utf-8")
    # Scoped to this file: the entities table is shared across the suite, so a
    # table-wide assertion would read other tests' rows.
    db = store.get_db()
    refs = [r[0] for r in db.execute(
        "SELECT entity_ref FROM entities WHERE file_path = ?", (str(fp),)).fetchall()]
    db.close()
    assert refs == ["person/alice"], f"index not repointed: {refs}"

"""A fact id names exactly one line — minting, sweeping, and reporting.

A fact id is derived from the line's text, so two byte-identical status log
lines minted the same id: a repeated session summary, or the same Codex session
ending twice. An id on two lines is not an address. The first live age sweep
retired 236 lines and logged six ``ARCHIVE unmatched`` warnings for five ids —
the first ARCHIVE for an id had already taken every line that id named, so the
per-line ops behind it found nothing.

Three surfaces, pinned here:

1. **Minting** — both writers (``session-end``'s append and the ``bootstrap-ids``
   walk) mint against the ids the document already carries, so a repeated line
   gets ``…-2`` rather than a collision.
2. **The sweep** — one op per distinct id, and the executor counts *lines*, so a
   legacy document with duplicates retires everything and logs no ``unmatched``.
3. **Reporting** — ``palinode doctor`` names the duplicates already on disk.
   They are reported, not re-minted: the id in the file is what the history
   entries and Consolidation Log lines already reference.

Real files under ``tmp_path``, the real runner→executor path, real SQLite. The
only fake is the propose seam (``llm_fn``), so the deterministic sweep is what
is under test.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from palinode.consolidation import runner
from palinode.consolidation.executor import apply_operations
from palinode.consolidation.fact_ids import (
    add_fact_ids_to_file,
    document_fact_ids,
    generate_fact_id,
    stamp_fact_id,
)
from palinode.core.config import Config, config
from palinode.diagnostics.checks.duplicate_fact_ids import status_fact_ids_unique
from palinode.diagnostics.registry import all_checks
from palinode.diagnostics.types import DoctorContext

_MARKER_RE = re.compile(r"<!-- fact:(\S+) -->")


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")


def _status_doc(body: str) -> str:
    return (
        "---\n"
        "id: projects-proj-status\n"
        "category: project\n"
        "entities:\n"
        "- project/proj\n"
        "---\n\n"
        "# Proj Status\n\n"
        "## Current Work\n\n"
        f"{body}\n"
    )


def _log_line(days: int, index: int) -> str:
    """A rendered session line, marker and all — what session-end appends."""
    return (
        f"- [{_days_ago(days)}] Session {index}: shipped something. "
        f"(1 decision → daily/{_days_ago(days)}.md) <!-- fact:s{index:04d} -->"
    )


@pytest.fixture
def store(tmp_path, monkeypatch) -> Path:
    """A memory dir the weekly pass can run against, with today's daily note."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    for sub in ("projects", "daily", "specs/prompts"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "specs" / "prompts" / "compaction.md").write_text(
        "Return consolidation operations as a JSON array.\n", encoding="utf-8"
    )
    (tmp_path / "daily" / f"{_today()}.md").write_text(
        f"---\nid: daily-{_today()}\ncategory: daily\nentities:\n- project/proj\n---\n\n"
        "Worked on project/proj today.\n",
        encoding="utf-8",
    )
    return tmp_path


def _write_target(store: Path, body: str) -> Path:
    target = store / "projects" / "proj-status.md"
    target.write_text(_status_doc(body), encoding="utf-8")
    return target


def _no_ops(_system: str, _user: str) -> tuple[str, str]:
    """A model that proposes nothing — isolates the deterministic sweep."""
    return "[]", "fake-model"


def _ctx(memory_dir: Path) -> DoctorContext:
    cfg = Config(memory_dir=str(memory_dir), db_path=str(memory_dir / ".palinode.db"))
    cfg.git.auto_commit = False
    return DoctorContext(config=cfg)


# ── 1. Minting: an id is unique within the document it lands in ──────────────


def _run_session_end(tmp_path, monkeypatch, **kwargs) -> tuple[str, str]:
    """Drive the real session-end handler against a tmp store."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)

    projects_dir = os.path.join(str(tmp_path), "projects")
    os.makedirs(projects_dir, exist_ok=True)
    status_path = os.path.join(projects_dir, "palinode-status.md")
    if not os.path.exists(status_path):
        with open(status_path, "w", encoding="utf-8") as f:
            f.write("---\nid: projects-palinode-status\n---\n\n# palinode status\n")

    with mock.patch(
        "palinode.api.routers.session._check_session_end_dedup",
        return_value=(None, None),
    ):
        from palinode.api.server import SessionEndRequest, session_end_api

        kwargs.setdefault("project", "palinode")
        kwargs.setdefault("source", "test")
        session_end_api(SessionEndRequest(**kwargs))

    return status_path, open(status_path, encoding="utf-8").read()


def test_two_identical_session_ends_mint_two_distinct_ids(tmp_path, monkeypatch):
    """The defect at its source: the same summary twice on the same day."""
    _run_session_end(tmp_path, monkeypatch, summary="Shipped the thing.")
    status_path, text = _run_session_end(
        tmp_path, monkeypatch, summary="Shipped the thing."
    )

    lines = [line for line in text.splitlines() if line.startswith("- [")]
    ids = [_MARKER_RE.search(line).group(1) for line in lines]

    assert len(lines) == 2, "both lines are written; neither is swallowed"
    assert len(set(ids)) == 2, f"an id on two lines is not an address: {ids}"
    assert document_fact_ids(text) == set(ids)


def test_the_first_line_keeps_the_plain_derived_id(tmp_path, monkeypatch):
    """Disambiguation is a suffix on the repeat, never a re-mint of the first."""
    _run_session_end(tmp_path, monkeypatch, summary="Shipped the thing.")
    status_path, text = _run_session_end(
        tmp_path, monkeypatch, summary="Shipped the thing."
    )

    first, second = [line for line in text.splitlines() if line.startswith("- [")]
    body, _ = first.rsplit(" <!-- fact:", 1)

    assert _MARKER_RE.search(first).group(1) == generate_fact_id(status_path, body)
    assert _MARKER_RE.search(second).group(1) == f"{generate_fact_id(status_path, body)}-2"


def test_bootstrap_ids_gives_identical_bullets_distinct_ids(tmp_path, monkeypatch):
    """The other minting writer, on a document built by hand or by an importer."""
    monkeypatch.setattr(config.git, "auto_commit", False)
    path = tmp_path / "palinode-status.md"
    line = "- [2026-09-13] Shipped the thing. (0 decisions → daily/2026-09-13.md)"
    path.write_text(f"---\nid: s\n---\n\n{line}\n{line}\n{line}\n", encoding="utf-8")

    assert add_fact_ids_to_file(str(path)) == 3

    text = path.read_text(encoding="utf-8")
    ids = _MARKER_RE.findall(text)
    assert len(ids) == 3
    assert len(set(ids)) == 3, f"three lines, three addresses: {ids}"


def test_re_running_bootstrap_over_deduplicated_ids_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(config.git, "auto_commit", False)
    path = tmp_path / "palinode-status.md"
    line = "- [2026-09-13] Shipped the thing."
    path.write_text(f"---\nid: s\n---\n\n{line}\n{line}\n", encoding="utf-8")
    add_fact_ids_to_file(str(path))
    before = path.read_bytes()

    assert add_fact_ids_to_file(str(path)) == 0
    assert path.read_bytes() == before


def test_an_id_a_later_bullet_would_want_is_never_reused(tmp_path, monkeypatch):
    """A ``-2`` already in the document pushes the next collision to ``-3``."""
    monkeypatch.setattr(config.git, "auto_commit", False)
    path = tmp_path / "s.md"
    line = "- Repeated."
    base = generate_fact_id(str(path), line)
    path.write_text(
        f"---\nid: s\n---\n\n{line} <!-- fact:{base} -->\n"
        f"{line} <!-- fact:{base}-2 -->\n{line}\n",
        encoding="utf-8",
    )

    add_fact_ids_to_file(str(path))

    ids = _MARKER_RE.findall(path.read_text(encoding="utf-8"))
    assert ids == [base, f"{base}-2", f"{base}-3"]


def test_stamping_without_a_document_is_the_derived_id(tmp_path):
    """``taken_ids`` is opt-in; every existing caller's id is unchanged."""
    line = "- [2026-09-13] Shipped the thing."
    path = str(tmp_path / "projects" / "proj-status.md")

    assert stamp_fact_id(path, line).endswith(
        f"<!-- fact:{generate_fact_id(path, line)} -->"
    )


# ── 2. The sweep: no spurious `unmatched` on a legacy document ────────────────


def test_the_sweep_over_duplicated_ids_logs_no_unmatched(store, monkeypatch, caplog):
    """The regression, end to end: three lines, two of them sharing one id."""
    monkeypatch.setattr(config.consolidation, "status_log_retention_days", 90)
    repeated = _log_line(200, 1)
    target = _write_target(store, "\n".join([repeated, repeated, _log_line(200, 2)]))

    with caplog.at_level(logging.WARNING):
        result = runner.run_consolidation(llm_fn=_no_ops)

    assert "ARCHIVE unmatched" not in caplog.text, caplog.text
    assert result["age_retired"] == 3, "lines retired, not ops emitted"
    assert result.get("unmatched", 0) == 0
    text = target.read_text(encoding="utf-8")
    assert "<!-- fact:s0001 -->" not in text
    assert "<!-- fact:s0002 -->" not in text


def test_every_duplicated_line_reaches_the_history_sibling(store, monkeypatch):
    """Two lines removed, two history entries — a 6-hex id can collide."""
    monkeypatch.setattr(config.consolidation, "status_log_retention_days", 90)
    repeated = _log_line(200, 1)
    _write_target(store, "\n".join([repeated, repeated]))

    runner.run_consolidation(llm_fn=_no_ops)

    history = (store / "projects" / "proj-history.md").read_text(encoding="utf-8")
    assert history.count("Session 1: shipped something.") == 2


def test_the_range_op_counts_lines_not_ids(store, monkeypatch):
    """``ARCHIVE_BEFORE`` is the other path over the same lines."""
    monkeypatch.setattr(config.consolidation, "status_log_retention_days", 0)
    repeated = _log_line(200, 1)
    target = _write_target(store, "\n".join([repeated, repeated, _log_line(1, 2)]))

    stats = apply_operations(str(target), [
        {"op": "ARCHIVE_BEFORE", "before": _days_ago(90), "rationale": "stale"},
    ])

    assert stats["archived"] == 2
    assert stats["archived_by_range"] == 2
    assert stats["unmatched"] == 0
    assert "<!-- fact:s0002 -->" in target.read_text(encoding="utf-8")


def test_a_single_archive_of_a_duplicated_id_counts_both_lines(store, monkeypatch):
    monkeypatch.setattr(config.consolidation, "status_log_retention_days", 0)
    repeated = _log_line(200, 1)
    target = _write_target(store, "\n".join([repeated, repeated]))

    stats = apply_operations(str(target), [
        {"op": "ARCHIVE", "id": "s0001", "rationale": "stale"},
    ])

    assert stats["archived"] == 2
    assert stats["unmatched"] == 0
    assert "<!-- fact:s0001 -->" not in target.read_text(encoding="utf-8")


def test_an_archive_naming_nothing_is_still_unmatched(store, monkeypatch):
    """The warning is not suppressed — only the spurious one is gone."""
    monkeypatch.setattr(config.consolidation, "status_log_retention_days", 0)
    target = _write_target(store, _log_line(200, 1))

    stats = apply_operations(str(target), [
        {"op": "ARCHIVE", "id": "nothing-here", "rationale": "stale"},
    ])

    assert stats["archived"] == 0
    assert stats["unmatched"] == 1


# ── 3. Reporting: doctor names the duplicates already on disk ────────────────


def test_the_check_is_registered_as_fast():
    names = {fn.__name__: tags for fn, tags in all_checks()}

    assert "fast" in names["status_fact_ids_unique"]


def test_a_duplicated_id_warns(store):
    repeated = _log_line(1, 1)
    _write_target(store, "\n".join([repeated, repeated, _log_line(2, 2)]))

    result = status_fact_ids_unique(_ctx(store))

    assert result.passed is False
    assert result.severity == "warn"
    assert "projects/proj-status.md" in result.message
    assert "s0001" in result.message
    assert "s0002" not in result.message
    assert result.remediation


def test_a_document_whose_ids_are_unique_passes(store):
    _write_target(store, "\n".join([_log_line(1, 1), _log_line(2, 2)]))

    result = status_fact_ids_unique(_ctx(store))

    assert result.passed is True
    assert result.severity == "info"
    assert result.remediation is None


def test_a_store_with_no_status_documents_passes(tmp_path):
    result = status_fact_ids_unique(_ctx(tmp_path))

    assert result.passed is True


def test_a_frontmatter_marker_is_not_counted_as_a_duplicate_fact(store):
    """Marker residue in ``entities:`` is repair-status's business, not this one."""
    target = store / "projects" / "proj-status.md"
    target.write_text(
        "---\nid: projects-proj-status\nentities:\n"
        "- project/proj <!-- fact:s0001 -->\n---\n\n"
        f"{_log_line(1, 1)}\n",
        encoding="utf-8",
    )

    assert status_fact_ids_unique(_ctx(store)).passed is True


def test_the_check_survives_a_document_it_cannot_read(store):
    _write_target(store, _log_line(1, 1))
    (store / "projects" / "unreadable-status.md").mkdir()

    assert status_fact_ids_unique(_ctx(store)).passed is True

"""Resolution policy (``palinode.core.resolution``) — three outcomes, no guesses.

Real markdown under ``tmp_path``, real SQLite through the reconcile seam, real
git, and the real retirement path (``consolidation.archive.archive_memory``)
for every seeded explicit change — the executor writes the ``superseded_by``
frontmatter and the ``stale_backing`` flags this policy reads, so a test that
hand-wrote them would be testing the test. The embedder is the only double and
it is deterministic.
"""
from __future__ import annotations

import hashlib
import math
import re
import subprocess
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from palinode.consolidation.archive import archive_memory
from palinode.core import store
from palinode.core.config import config
from palinode.core.evidence import resolve_evidence
from palinode.core.resolution import (
    KIND_ACCEPTED_INTENT,
    KIND_OBSERVATION,
    KIND_PROPOSAL,
    KIND_UNKNOWN,
    OUTCOME_CONFLICT,
    OUTCOME_INSUFFICIENT,
    OUTCOME_SUPPORTED,
    REASONS,
    claim_facts,
    claim_kind,
    applicable,
    resolve,
)
from palinode.indexer import reconcile

_DIM = 1024
_NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)


def _bow_embed(text: str, backend: str = "local") -> list[float]:
    vec = [0.0] * _DIM
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        h = int(hashlib.md5(tok.encode(), usedforsecurity=False).hexdigest(), 16)
        vec[h % _DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@pytest.fixture()
def mem(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True)
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    with patch("palinode.core.embedder.embed", side_effect=_bow_embed):
        yield tmp_path


def _write(mem, rel: str, body: str, **meta) -> str:
    """Write ``rel`` with the given frontmatter and index it."""
    import yaml

    path = mem / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = yaml.safe_dump(meta, default_flow_style=False, sort_keys=False)
    content = f"---\n{fm}---\n\n{body}\n"
    path.write_text(content, encoding="utf-8")
    diff = reconcile.reconcile(str(path), content)
    assert diff.committed, diff
    return str(path)


def _reindex(mem, rel: str) -> None:
    """Re-index a file the retirement path rewrote underneath us."""
    path = mem / rel
    content = path.read_text(encoding="utf-8")
    reconcile.reconcile(str(path), content)


def _seed_row(mem, rel: str) -> dict:
    db = store.get_db()
    try:
        row = db.execute(
            "SELECT id, file_path, section_id, content_hash FROM chunks WHERE file_path = ?",
            (str(mem / rel),),
        ).fetchone()
    finally:
        db.close()
    assert row is not None, rel
    return {"id": row["id"], "file_path": row["file_path"],
            "section_id": row["section_id"], "content_hash": row["content_hash"]}


def _resolved(mem, rel: str, *, mode: str = "linked", now: datetime = _NOW):
    """Resolve one seed end to end: gather evidence, then decide."""
    ev = resolve_evidence([_seed_row(mem, rel)], mode=mode, now=now)
    seed = ev.seeds[0]
    res = resolve(seed.seed_meta, seed, now=now)
    assert res.outcome in (OUTCOME_SUPPORTED, OUTCOME_CONFLICT, OUTCOME_INSUFFICIENT)
    assert set(res.reasons) <= REASONS, sorted(set(res.reasons) - REASONS)
    return res


def _refs(sides) -> set[str]:
    return {s.ref for s in sides}


# ── claim kinds ──────────────────────────────────────────────────────────────


def test_claim_kinds_come_from_existing_vocabulary_only():
    kind = lambda **meta: claim_kind(claim_facts(meta, ref="x/y", now=_NOW))  # noqa: E731
    assert kind(type="Decision", status="active") == KIND_ACCEPTED_INTENT
    # An unsettled marker beats the type: a Decision nobody accepted yet is a
    # proposal, whichever way round the frontmatter is written.
    assert kind(type="Decision", epistemic="open_question") == KIND_PROPOSAL
    assert kind(type="Decision", epistemic="unverified") == KIND_PROPOSAL
    assert kind(epistemic="fact") == KIND_OBSERVATION
    assert kind(type="ProjectSnapshot") == KIND_OBSERVATION
    assert kind(epistemic="inference") == "inference"
    # Legacy untyped: no type, no marker, no date — and no kind is invented.
    facts = claim_facts({"entities": ["project/shop"]}, ref="notes/old", now=_NOW)
    assert claim_kind(facts) == KIND_UNKNOWN
    assert facts.effective_at is None and facts.declared_at is None


def test_fallback_disabled_constant_matches_the_evidence_layer():
    from palinode.core import evidence as ev
    from palinode.core import resolution as rs

    assert rs._FALLBACK_DISABLED == ev.FALLBACK_DISABLED


def test_applicability_separates_environments_entities_and_windows():
    prod = claim_facts({"entities": ["project/shop", "env/production"]}, ref="a", now=_NOW)
    stage = claim_facts({"entities": ["project/shop", "env/staging"]}, ref="b", now=_NOW)
    assert not applicable(prod, stage)

    # Same project, no env declared on one side — nothing separates them.
    plain = claim_facts({"entities": ["project/shop"]}, ref="c", now=_NOW)
    assert applicable(prod, plain)

    # Different subjects entirely.
    other = claim_facts({"entities": ["project/other"]}, ref="d", now=_NOW)
    assert not applicable(plain, other)

    # Bare (un-namespaced) entities are not facets: they are as often the
    # disputed value as the subject, so they never separate two records.
    assert applicable(
        claim_facts({"entities": ["postgres"]}, ref="e", now=_NOW),
        claim_facts({"entities": ["sqlite"]}, ref="f", now=_NOW),
    )

    # Non-overlapping time windows.
    past = claim_facts(
        {"date": "2026-01-01", "expires_at": "2026-02-01"}, ref="g", now=_NOW
    )
    later = claim_facts({"date": "2026-03-01"}, ref="h", now=_NOW)
    assert not applicable(past, later)


# ── kinds in conflict ────────────────────────────────────────────────────────


def test_newer_proposal_does_not_replace_an_accepted_decision(mem):
    _write(mem, "decisions/deploy-target.md", "# Deploy target\n\nDeploys go to the VPS.",
           type="Decision", status="active", date="2026-01-10", entities=["project/shop"])
    # The proposal is newer, names the decision, and is marked unsettled.
    _write(mem, "proposals/deploy-k8s.md", "# Move to Kubernetes\n\nDeploys should go to k8s.",
           type="Decision", epistemic="open_question", date="2026-09-01",
           entities=["project/shop"], contradicts=["decisions/deploy-target"])

    res = _resolved(mem, "decisions/deploy-target.md")
    assert res.outcome == OUTCOME_SUPPORTED
    assert res.current is not None and res.current.ref == "decisions/deploy-target"
    assert res.current.kind == KIND_ACCEPTED_INTENT
    assert "proposal_does_not_replace_decision" in res.reasons
    # The proposal stays visible rather than being silently dropped.
    assert "proposals/deploy-k8s" in _refs(res.sides)
    assert [s.kind for s in res.sides if s.ref == "proposals/deploy-k8s"] == [KIND_PROPOSAL]
    # Nothing about the newer date entered the decision.
    assert "explicit_replacement" not in res.reasons

    # Seeded from the proposal's side, the same decision stands.
    from_proposal = _resolved(mem, "proposals/deploy-k8s.md")
    assert from_proposal.outcome == OUTCOME_SUPPORTED
    assert from_proposal.current.ref == "decisions/deploy-target"


def test_production_and_staging_observations_coexist(mem):
    _write(mem, "observations/latency-prod.md",
           "# Latency in production\n\nCheckout latency is 120 ms.",
           epistemic="fact", status="active", date="2026-09-01",
           entities=["project/shop", "env/production"])
    _write(mem, "observations/latency-staging.md",
           "# Latency in staging\n\nCheckout latency is 640 ms.",
           epistemic="fact", status="active", date="2026-09-02",
           entities=["project/shop", "env/staging"])

    for rel, ref in (("observations/latency-prod.md", "observations/latency-prod"),
                     ("observations/latency-staging.md", "observations/latency-staging")):
        # mode="full" so each one is discovered from the other's subject entity:
        # they are found, and still do not conflict.
        res = _resolved(mem, rel, mode="full")
        assert res.outcome == OUTCOME_SUPPORTED, res
        assert res.current is not None and res.current.ref == ref
        assert "uncontested" in res.reasons
        assert res.sides == ()


def test_policy_implementation_mismatch_is_reported_not_resolved(mem):
    _write(mem, "decisions/db.md", "# Database\n\nThe primary database is Postgres.",
           type="Decision", status="active", date="2026-01-05", entities=["project/shop"])
    # What PROPOSE_CONTRADICTS records: a later observation that the world
    # disagrees with the decision. It retires nothing.
    _write(mem, "observations/db-deployed.md", "# Deployed database\n\nProduction runs SQLite.",
           epistemic="fact", status="active", date="2026-09-01",
           entities=["project/shop"], contradicts=["decisions/db"])

    for rel in ("decisions/db.md", "observations/db-deployed.md"):
        res = _resolved(mem, rel)
        assert res.outcome == OUTCOME_CONFLICT, res
        assert res.current is None
        assert "policy_implementation_mismatch" in res.reasons
        assert _refs(res.sides) == {"decisions/db", "observations/db-deployed"}
        kinds = {s.ref: s.kind for s in res.sides}
        assert kinds["decisions/db"] == KIND_ACCEPTED_INTENT
        assert kinds["observations/db-deployed"] == KIND_OBSERVATION


def test_two_incomparable_observations_stay_contested(mem):
    _write(mem, "observations/throughput-a.md", "# Throughput\n\nThroughput is 900 per hour.",
           epistemic="fact", status="active", date="2026-08-01", entities=["project/shop"])
    _write(mem, "observations/throughput-b.md", "# Throughput\n\nThroughput is 400 per hour.",
           epistemic="fact", status="active", date="2026-09-01",
           entities=["project/shop"], contradicts=["observations/throughput-a"])

    res = _resolved(mem, "observations/throughput-a.md")
    assert res.outcome == OUTCOME_CONFLICT
    assert "incomparable_observations" in res.reasons
    assert res.current is None
    assert _refs(res.sides) == {"observations/throughput-a", "observations/throughput-b"}


def test_a_conflict_two_hops_out_does_not_contest_the_seed(mem):
    """The evidence block is the closure around the seed; only an edge incident
    to the record now standing makes it contested."""
    _write(mem, "insights/seed.md", "# Seed\n\nThe queue drains in a minute.",
           epistemic="fact", status="active", date="2026-08-01", entities=["project/shop"])
    _write(mem, "insights/near.md", "# Near\n\nThe queue drains in an hour.",
           epistemic="fact", status="active", date="2026-08-02",
           entities=["project/shop"], contradicts=["insights/seed", "insights/far"])
    _write(mem, "insights/far.md", "# Far\n\nThere is no queue.",
           epistemic="fact", status="active", date="2026-08-03", entities=["project/shop"])

    res = _resolved(mem, "insights/seed.md")
    assert res.outcome == OUTCOME_CONFLICT
    # `insights/far` is reachable (it conflicts with the record that conflicts
    # with the seed) but it is not a side of *this* question.
    assert _refs(res.sides) == {"insights/seed", "insights/near"}


def test_a_contradiction_across_environments_is_not_a_conflict(mem):
    _write(mem, "observations/errors-prod.md", "# Errors\n\nError rate is 0.1%.",
           epistemic="fact", status="active", date="2026-09-01",
           entities=["project/shop", "env/production"])
    _write(mem, "observations/errors-staging.md", "# Errors\n\nError rate is 12%.",
           epistemic="fact", status="active", date="2026-09-02",
           entities=["project/shop", "env/staging"],
           contradicts=["observations/errors-prod"])

    res = _resolved(mem, "observations/errors-prod.md")
    assert res.outcome == OUTCOME_SUPPORTED
    assert "scope_disjoint" in res.reasons and "uncontested" in res.reasons
    assert res.current.ref == "observations/errors-prod"


# ── time ─────────────────────────────────────────────────────────────────────


def test_expired_evidence_is_insufficient_not_current(mem):
    _write(mem, "decisions/freeze.md", "# Release freeze\n\nNo releases this week.",
           type="Decision", status="active", date="2026-08-01",
           expires_at=(_NOW - timedelta(days=1)).isoformat(), entities=["project/shop"])

    res = _resolved(mem, "decisions/freeze.md")
    assert res.outcome == OUTCOME_INSUFFICIENT
    assert res.reasons == ("expired",)
    assert res.current is None
    assert _refs(res.sides) == {"decisions/freeze"}


def test_future_effective_evidence_does_not_take_effect_early(mem):
    _write(mem, "decisions/new-rate.md", "# New rate\n\nThe rate becomes 5% next quarter.",
           type="Decision", status="active", date="2027-01-01", entities=["project/shop"])

    res = _resolved(mem, "decisions/new-rate.md")
    assert res.outcome == OUTCOME_INSUFFICIENT
    assert "not_yet_effective" in res.reasons
    assert res.current is None


def test_a_save_stamp_never_makes_a_record_future_effective(mem):
    """Only a declared ``date`` can. ``created_at``/``last_updated`` are stamps."""
    _write(mem, "notes/backfilled.md", "# Backfilled\n\nAn old note imported today.",
           status="active", last_updated="2027-05-05", entities=["project/shop"])

    res = _resolved(mem, "notes/backfilled.md")
    assert res.outcome == OUTCOME_SUPPORTED
    assert "not_yet_effective" not in res.reasons


def test_undated_records_conflict_without_inventing_an_order(mem):
    _write(mem, "notes/rate-old.md", "# Rate\n\nThe rate is 3%.", entities=["project/shop"])
    _write(mem, "notes/rate-new.md", "# Rate\n\nThe rate is 7%.",
           entities=["project/shop"], contradicts=["notes/rate-old"])

    res = _resolved(mem, "notes/rate-old.md")
    assert res.outcome == OUTCOME_CONFLICT
    assert "undated" in res.reasons and "kind_unknown" in res.reasons
    assert res.current is None
    # Nothing claimed a date, a kind, or an authority these records never had.
    assert all(s.effective_at is None for s in res.sides)
    assert {s.kind for s in res.sides} == {KIND_UNKNOWN}
    assert all(s.epistemic is None for s in res.sides)


# ── explicit change, through the real retirement path ────────────────────────


def _seed_explicit_change(mem) -> None:
    _write(mem, "decisions/db.md", "# Database\n\nWe use Postgres as the primary database.",
           type="Decision", status="active", date="2026-01-05", entities=["project/shop"])
    _write(mem, "decisions/db-v2.md", "# Database v2\n\nThe primary database is SQLite now.",
           type="Decision", status="active", date="2026-09-01", entities=["project/shop"])
    archive_memory("decisions/db.md", reason="moved to SQLite",
                   superseded_by="decisions/db-v2")
    _reindex(mem, "decisions/db.md")


def test_explicit_change_resolves_to_the_successor(mem):
    _seed_explicit_change(mem)

    res = _resolved(mem, "decisions/db.md")
    assert res.outcome == OUTCOME_SUPPORTED
    assert res.current is not None and res.current.ref == "decisions/db-v2"
    assert "explicit_replacement" in res.reasons
    # The record that was replaced stays visible as history, never as current.
    assert _refs(res.sides) == {"decisions/db"}
    assert [s.currency for s in res.sides] == ["retired"]

    # Deterministic: same store state, same clock, same answer.
    assert _resolved(mem, "decisions/db.md").to_dict() == res.to_dict()


def test_replacement_withdrawn_does_not_resurrect_the_old_value(mem):
    _seed_explicit_change(mem)
    # The successor is itself retracted, and nothing replaces it.
    _write(mem, "decisions/db-v2.md", "# Database v2\n\nThe primary database is SQLite now.",
           type="Decision", status="retracted", date="2026-09-01", entities=["project/shop"])

    res = _resolved(mem, "decisions/db.md")
    assert res.outcome == OUTCOME_INSUFFICIENT
    assert "replacement_withdrawn" in res.reasons
    assert res.current is None
    # Postgres does not come back just because SQLite went away.
    assert _refs(res.sides) == {"decisions/db", "decisions/db-v2"}
    assert all(s.currency == "retired" for s in res.sides)


def test_an_unreachable_successor_is_unknown_not_the_old_value(mem):
    _write(mem, "decisions/db.md", "# Database\n\nWe use Postgres.",
           type="Decision", status="archived", superseded_by="decisions/db-v9",
           date="2026-01-05", entities=["project/shop"])

    res = _resolved(mem, "decisions/db.md")
    assert res.outcome == OUTCOME_INSUFFICIENT
    assert "replacement_unresolved" in res.reasons
    assert res.current is None


def _seed_scheduled_change(mem, *, predecessor: dict | None = None) -> None:
    """A → B through the real SUPERSEDE path, where B is declared effective later.

    The shape ``supersede_record`` leaves behind when the writer dates the
    successor forward: the executor tombstones A the moment B is written, and
    B's own ``date`` says the change does not happen until 2027-01-01.
    """
    _write(mem, "decisions/ratelimit.md",
           "# Rate limit\n\nThe API rate limit is 100 requests per minute.",
           type="Decision", status="active", date="2026-01-20",
           entities=["project/atlas"], **(predecessor or {}))
    _write(mem, "decisions/ratelimit-v2.md",
           "# Rate limit from January\n\nThe API rate limit is 500 requests per minute.",
           type="Decision", status="active", date="2027-01-01",
           entities=["project/atlas"])
    archive_memory("decisions/ratelimit.md", reason="raised from January",
                   superseded_by="decisions/ratelimit-v2")
    _reindex(mem, "decisions/ratelimit.md")


def test_a_scheduled_replacement_keeps_the_old_value_until_it_takes_effect(mem):
    """A dated change has not happened until its date; the old value holds."""
    _seed_scheduled_change(mem)

    res = _resolved(mem, "decisions/ratelimit.md")
    assert res.outcome == OUTCOME_SUPPORTED
    assert res.current is not None and res.current.ref == "decisions/ratelimit"
    assert "replacement_scheduled" in res.reasons
    assert "not_yet_effective" not in res.reasons, "the seed itself is effective"
    # The record's own declaration is not overridden: it still says retired,
    # and the resolution says from when.
    assert res.current.currency == "retired"
    assert "superseded_from:2027-01-01" in res.current.qualifiers
    # The successor stays visible — the reader is told what is coming.
    assert "decisions/ratelimit-v2" in _refs(res.sides)

    # Seeded from the successor instead — the end a query actually reaches,
    # because the predecessor is retired and out of default recall — the same
    # answer. Which end was seeded is not allowed to decide what is current.
    from_successor = _resolved(mem, "decisions/ratelimit-v2.md")
    assert from_successor.outcome == OUTCOME_SUPPORTED
    assert from_successor.current.ref == "decisions/ratelimit"
    assert "replacement_scheduled" in from_successor.reasons
    assert "superseded_from:2027-01-01" in from_successor.current.qualifiers

    # On the far side of the transition the successor stands, unchanged.
    after = _resolved(mem, "decisions/ratelimit.md",
                      now=datetime(2027, 2, 1, tzinfo=UTC))
    assert after.outcome == OUTCOME_SUPPORTED
    assert after.current.ref == "decisions/ratelimit-v2"
    assert "explicit_replacement" in after.reasons
    assert "replacement_scheduled" not in after.reasons


def test_a_scheduled_replacement_does_not_revive_a_retracted_predecessor(mem):
    """A value withdrawn on its own account stays withdrawn.

    ``retracted`` is a retirement the writer declared about *this* record —
    known-incorrect — not one the pending supersession caused. A successor
    that has not arrived yet does not hand the floor back to it.
    """
    _seed_scheduled_change(mem)
    _write(mem, "decisions/ratelimit.md",
           "# Rate limit\n\nThe API rate limit is 100 requests per minute.",
           type="Decision", status="retracted", superseded_by="decisions/ratelimit-v2",
           date="2026-01-20", entities=["project/atlas"])

    for rel in ("decisions/ratelimit.md", "decisions/ratelimit-v2.md"):
        res = _resolved(mem, rel)
        assert res.outcome == OUTCOME_INSUFFICIENT, rel
        assert "not_yet_effective" in res.reasons
        assert "replacement_scheduled" not in res.reasons
        assert res.current is None


def test_a_scheduled_replacement_does_not_revive_a_lapsed_predecessor(mem):
    """Same rule for a record that ran out on its own clock."""
    _seed_scheduled_change(
        mem, predecessor={"expires_at": (_NOW - timedelta(days=1)).isoformat()}
    )

    res = _resolved(mem, "decisions/ratelimit.md")
    assert res.outcome == OUTCOME_INSUFFICIENT
    assert "not_yet_effective" in res.reasons
    assert res.current is None


def test_retired_with_no_successor_is_unknown(mem):
    _write(mem, "decisions/old.md", "# Old\n\nA decision nobody replaced.",
           type="Decision", status="archived", date="2026-01-05")

    res = _resolved(mem, "decisions/old.md")
    assert res.outcome == OUTCOME_INSUFFICIENT
    assert "retired_no_successor" in res.reasons


def test_withdrawn_support_makes_the_conclusion_unknown(mem):
    """The real propagation path writes ``stale_backing``; this reads it."""
    _write(mem, "research/benchmark.md", "# Benchmark\n\nThe benchmark measured 900 per hour.",
           epistemic="fact", status="active", date="2026-02-01")
    _write(mem, "insights/capacity.md", "# Capacity\n\nWe can serve 900 orders per hour.",
           epistemic="inference", status="active", date="2026-02-02",
           backed_by=["research/benchmark"])
    archive_memory("research/benchmark.md", reason="measurement was withdrawn")
    _reindex(mem, "research/benchmark.md")
    _reindex(mem, "insights/capacity.md")

    facts = claim_facts(
        {"backed_by": ["research/benchmark"],
         "stale_backing": [{"ref": "research/benchmark"}]}, ref="insights/capacity", now=_NOW)
    assert facts.stale_backing == ("research/benchmark",)

    res = _resolved(mem, "insights/capacity.md")
    assert res.outcome == OUTCOME_INSUFFICIENT
    assert "support_withdrawn" in res.reasons
    assert res.current is None
    assert "stale_backing:research/benchmark" in res.qualifiers


# ── lineage ──────────────────────────────────────────────────────────────────


def _anchor(ref: str) -> list[dict]:
    return [{"ref": ref, "quote": "sustained 900 orders per hour"}]


def test_derived_copies_group_by_origin_and_do_not_outvote_a_correction(mem):
    _write(mem, "research/bench.md", "# Bench\n\nsustained 900 orders per hour",
           epistemic="fact", status="active", date="2026-02-01")
    _write(mem, "observations/throughput.md", "# Throughput\n\nThroughput is 900 per hour.",
           epistemic="fact", status="active", date="2026-02-02",
           entities=["project/shop"], sources=_anchor("research/bench.md"))
    # Three derived copies of that one observation: a session summary, a
    # snapshot, and a decision rationale. Same source anchor, different paths.
    for rel, body, extra in (
        ("daily/2026-02-03.md", "# Session\n\nThroughput is 900 per hour.", {}),
        ("projects/shop-snapshot.md", "# Snapshot\n\nThroughput is 900 per hour.",
         {"type": "ProjectSnapshot"}),
        ("decisions/capacity.md", "# Capacity\n\nSized for 900 per hour.",
         {"type": "Decision", "epistemic": "unverified",
          "contradicts": ["observations/throughput-v2"]}),
    ):
        _write(mem, rel, body, status="active", date="2026-02-03",
               entities=["project/shop"], backed_by=["observations/throughput"],
               sources=_anchor("research/bench.md"), **extra)
    # One record with similar prose and no anchor at all: reachable only by
    # discovery, and its lineage is unknown — not independent, not derived.
    _write(mem, "notes/hearsay.md", "# Hearsay\n\nThroughput is 900 per hour.",
           status="active", date="2026-02-04", entities=["project/shop"])
    # …and one explicit correction, written by the real retirement path.
    _write(mem, "observations/throughput-v2.md", "# Throughput\n\nThroughput is 400 per hour.",
           epistemic="fact", status="active", date="2026-09-01", entities=["project/shop"])
    archive_memory("observations/throughput.md", reason="re-measured",
                   superseded_by="observations/throughput-v2")
    _reindex(mem, "observations/throughput.md")

    res = _resolved(mem, "observations/throughput.md", mode="full")

    # Three copies do not outvote one explicit correction.
    assert res.outcome == OUTCOME_SUPPORTED
    assert res.current.ref == "observations/throughput-v2"
    assert "explicit_replacement" in res.reasons

    groups = {g.origin: g for g in res.support}
    shared = groups["research/bench.md"]
    assert shared.origin_kind == "source"
    assert {m.ref for m in shared.members} == {
        "daily/2026-02-03", "projects/shop-snapshot", "decisions/capacity"}

    # Unknown lineage stays unknown: similar prose alone groups nothing.
    hearsay = groups["notes/hearsay"]
    assert hearsay.origin_kind == "unknown" and len(hearsay.members) == 1
    assert "lineage_unknown" in res.reasons

    # A copied claim keeps its own qualifications through the grouping.
    rationale = next(m for m in shared.members if m.ref == "decisions/capacity")
    assert "epistemic:unverified" in rationale.qualifiers
    assert "contradicts:observations/throughput-v2" in rationale.qualifiers
    assert rationale.currency == "contested"


# ── learned scores are not proof ─────────────────────────────────────────────


def test_rank_and_similarity_never_elect_the_retired_record(mem):
    # The replaced record still carries `status: active`, so it is still in
    # default recall — and it is the one the query matches best.
    _write(mem, "decisions/db.md", "# Database\n\nWe use Postgres as the primary database.",
           type="Decision", status="active", superseded_by="decisions/db-v2",
           date="2026-01-05", entities=["project/shop"])
    _write(mem, "decisions/db-v2.md", "# Database v2\n\nThe primary database is SQLite now.",
           type="Decision", status="active", date="2026-09-01", entities=["project/shop"])

    from palinode.core.embedder import embed

    query = "We use Postgres as the primary database"
    hits = store.search(embed(query), top_k=5, threshold=0.0, record_access=False)
    assert hits, "the corpus should match"
    top = hits[0]
    assert top["file_path"].endswith("decisions/db.md"), "the retired record ranks first"
    successor = next(h for h in hits if h["file_path"].endswith("decisions/db-v2.md"))
    assert successor["score"] < top["score"]

    ev = resolve_evidence([top], mode="linked", now=_NOW)
    res = resolve(ev.seeds[0].seed_meta, ev.seeds[0], now=_NOW)
    assert res.outcome == OUTCOME_SUPPORTED
    assert res.current.ref == "decisions/db-v2"

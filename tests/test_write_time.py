"""
Tests for palinode/consolidation/write_time.py (tier 2a, ADR-004).

Most of these tests mock the LLM call path entirely — they verify queue
mechanics, marker files, feature flag, and error handling without touching a
real embedder or consolidation runner. The "Sync path" tests below are the
exception: they run the real `_run_check_and_apply` end to end (with a fake
`llm_fn` injected at the propose seam and the embedder/vector-search calls it
makes faked at the infra boundary) — see that section's comment for why.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
from unittest.mock import patch

import pytest

from palinode.consolidation import write_time


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_palinode_dir(monkeypatch):
    """Point config at a temp directory and reset module state between tests."""
    with tempfile.TemporaryDirectory() as tmp:
        # Point config at the temp dir
        from palinode.core.config import config

        monkeypatch.setattr(config, "memory_dir", tmp)
        monkeypatch.setattr(config, "db_path", os.path.join(tmp, ".palinode.db"))
        # Enable the feature flag for tests that need it
        monkeypatch.setattr(config.consolidation.write_time, "enabled", True)
        monkeypatch.setattr(config.consolidation.write_time, "queue_max_size", 10)
        # Reset module-level queue state between tests
        monkeypatch.setattr(write_time, "_queue", None)
        monkeypatch.setattr(write_time, "_inflight", set())
        yield tmp


@pytest.fixture
def tmp_memory_file(tmp_palinode_dir):
    """Create a memory file in the temp palinode dir."""
    path = os.path.join(tmp_palinode_dir, "decisions", "test-decision.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("---\nid: decision-test\ncategory: decision\n---\n\n# Test Decision\n")
    return path


@pytest.fixture
def sample_item():
    """A sample item dict matching what save_api passes through."""
    return {
        "content": "We chose Postgres over MySQL",
        "category": "decisions",
        "type": "Decision",
        "entities": ["project/test"],
        "id": "decision-test",
    }


# ── Feature flag tests ─────────────────────────────────────────────────────


def test_feature_flag_disabled_returns_none(tmp_palinode_dir, tmp_memory_file, sample_item):
    """When config.consolidation.write_time.enabled is False, schedule is a no-op."""
    from palinode.core.config import config
    config.consolidation.write_time.enabled = False

    result = write_time.schedule_contradiction_check(
        tmp_memory_file, sample_item, sync=False
    )
    assert result is None

    result = write_time.schedule_contradiction_check(
        tmp_memory_file, sample_item, sync=True
    )
    assert result is None


# ── Disk marker tests ──────────────────────────────────────────────────────


def test_write_marker_atomic(tmp_palinode_dir, tmp_memory_file, sample_item):
    """Marker files are written atomically — no .tmp files left behind on success."""
    marker = write_time._write_marker(tmp_memory_file, sample_item)

    assert os.path.exists(marker)
    assert marker.endswith(".json")
    assert not marker.endswith(".tmp")

    # Load it and verify structure
    with open(marker) as f:
        job = json.load(f)
    assert job["file_path"] == tmp_memory_file
    assert job["item"] == sample_item
    assert "enqueued_at" in job


def test_write_marker_creates_pending_dir(tmp_palinode_dir, tmp_memory_file, sample_item):
    """_write_marker creates the pending directory if it doesn't exist."""
    pending_dir = write_time._pending_dir()
    # Directory should not exist yet
    assert not os.path.exists(pending_dir)

    write_time._write_marker(tmp_memory_file, sample_item)
    assert os.path.isdir(pending_dir)


def test_mark_failed_renames_to_failed_json(tmp_palinode_dir, tmp_memory_file, sample_item):
    """_mark_failed renames a marker to .failed.json for operator review."""
    marker = write_time._write_marker(tmp_memory_file, sample_item)
    assert os.path.exists(marker)

    write_time._mark_failed(marker)

    assert not os.path.exists(marker)
    failed_path = marker.replace(".json", ".failed.json")
    assert os.path.exists(failed_path)


# ── Sweeper tests ──────────────────────────────────────────────────────────


def test_sweep_empty_pending_dir_returns_zero(tmp_palinode_dir):
    """Sweeping a non-existent pending dir is a no-op."""
    recovered = write_time.sweep_pending_markers()
    assert recovered == 0


def test_sweep_recovers_markers_to_queue(tmp_palinode_dir, tmp_memory_file, sample_item):
    """Sweeper finds marker files and enqueues them — and leaves them on disk.

    Enqueue is not the point of no return (ADR-004): the marker is the job's
    durable record, so it survives the handoff and is only spent by a run that
    completes. The sweep records the handoff as an attempt.
    """

    async def run():
        # Pre-create a marker on disk (simulates a CLI save from before API startup)
        marker = write_time._write_marker(tmp_memory_file, sample_item)
        assert os.path.exists(marker)

        recovered = write_time.sweep_pending_markers()
        assert recovered == 1

        # Marker stays pending until a worker reports the job done
        assert os.path.exists(marker)
        assert _marker(marker)["attempts"] == 1

        # Queue should have the job, carrying the marker it came from
        queue = write_time._get_queue()
        assert queue.qsize() == 1
        job = queue.get_nowait()
        assert job["file_path"] == tmp_memory_file
        assert job["item"] == sample_item
        assert job["marker_path"] == marker
        assert job["attempt"] == 1

    asyncio.run(run())


def test_sweep_does_not_hand_out_a_marker_already_in_flight(
    tmp_palinode_dir, tmp_memory_file, sample_item
):
    """The marker outliving the enqueue must not mean the job runs twice.

    Deleting it at enqueue used to be the de-dup guard; the in-flight claim is
    what replaces it.
    """

    async def run():
        write_time._write_marker(tmp_memory_file, sample_item)

        assert write_time.sweep_pending_markers() == 1
        assert write_time.sweep_pending_markers() == 0
        assert write_time._get_queue().qsize() == 1

    asyncio.run(run())


def test_sweep_handles_corrupt_marker(tmp_palinode_dir):
    """Corrupt JSON in a marker file → renamed to .failed.json, not a crash."""

    async def run():
        pending_dir = write_time._pending_dir()
        os.makedirs(pending_dir, exist_ok=True)
        bad_marker = os.path.join(pending_dir, "20260410T000000-deadbeef.json")
        with open(bad_marker, "w") as f:
            f.write("{not valid json")

        recovered = write_time.sweep_pending_markers()
        assert recovered == 0
        assert not os.path.exists(bad_marker)
        assert os.path.exists(bad_marker.replace(".json", ".failed.json"))

    asyncio.run(run())


def test_sweep_handles_marker_missing_fields(tmp_palinode_dir):
    """Marker with missing file_path or item → renamed to .failed.json."""

    async def run():
        pending_dir = write_time._pending_dir()
        os.makedirs(pending_dir, exist_ok=True)
        bad_marker = os.path.join(pending_dir, "20260410T000000-cafebabe.json")
        with open(bad_marker, "w") as f:
            json.dump({"enqueued_at": "2026-04-10"}, f)

        recovered = write_time.sweep_pending_markers()
        assert recovered == 0
        assert os.path.exists(bad_marker.replace(".json", ".failed.json"))

    asyncio.run(run())


def test_sweep_processes_markers_in_timestamp_order(tmp_palinode_dir, tmp_memory_file, sample_item):
    """Markers are sorted by timestamp so older jobs run first."""

    async def run():
        pending_dir = write_time._pending_dir()
        os.makedirs(pending_dir, exist_ok=True)
        # Write three markers with different timestamps (sorted lexically = sorted by time)
        ts_order = ["20260410T100000", "20260410T100005", "20260410T100010"]
        for ts in ts_order:
            path = os.path.join(pending_dir, f"{ts}-abcd1234.json")
            with open(path, "w") as f:
                json.dump({"file_path": tmp_memory_file, "item": sample_item}, f)

        recovered = write_time.sweep_pending_markers()
        assert recovered == 3

        queue = write_time._get_queue()
        assert queue.qsize() == 3

    asyncio.run(run())


# ── Marker lifecycle: a job survives its worker ────────────────────────────
#
# ADR-004: a job whose worker times out leaves its marker in pending and is
# retried by the next sweep. The marker is therefore retired by exactly two
# events — a run that completed, or the attempt bound — and never by the
# handoff itself. These drive the real `_worker_loop` against a real marker
# file; only `_run_check_and_apply` (the LLM + executor payload) is stood in
# for, because the unit under test is the job's lifecycle, not the check.


def _marker(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _pending(tmp_dir: str) -> list[str]:
    d = write_time._pending_dir()
    if not os.path.isdir(d):
        return []
    return sorted(
        n for n in os.listdir(d)
        if n.endswith(".json") and not n.endswith(".failed.json")
    )


async def _drain(timeout: float = 10.0) -> None:
    """Run the real worker loop until every queued job has been accounted for."""
    queue = write_time._get_queue()
    task = asyncio.create_task(write_time._worker_loop(queue))
    try:
        await asyncio.wait_for(queue.join(), timeout=timeout)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _hangs(*_args, **_kwargs):
    """A payload that outlives the worker's check timeout."""
    time.sleep(0.3)
    return {"operations": [], "applied_stats": {}}


def test_a_timed_out_job_stays_pending_and_the_next_sweep_retries_it(
    tmp_palinode_dir, tmp_memory_file, sample_item, monkeypatch
):
    """Enqueue → worker timeout → the marker is still pending, and the next
    sweep hands it out again. Before this fix the sweep deleted the marker at
    enqueue, so the timeout consumed the job and nothing re-queued it."""
    from palinode.core.config import config

    monkeypatch.setattr(config.consolidation.write_time, "check_timeout_seconds", 0.05)
    marker = write_time._write_marker(tmp_memory_file, sample_item)

    async def run():
        assert write_time.sweep_pending_markers() == 1
        assert os.path.exists(marker), "enqueue must not consume the marker"

        with patch.object(write_time, "_run_check_and_apply", _hangs):
            await _drain()

        # The worker timed out. The job is untouched and still pending.
        assert os.path.exists(marker)
        assert _marker(marker)["attempts"] == 1
        assert not os.path.exists(marker.replace(".json", ".failed.json"))

        # The next sweep picks it up — this is the retry ADR-004 promises.
        assert write_time.sweep_pending_markers() == 1
        assert _marker(marker)["attempts"] == 2
        job = write_time._get_queue().get_nowait()
        assert job["file_path"] == tmp_memory_file
        assert job["item"] == sample_item

    asyncio.run(run())


def test_retries_stop_at_max_attempts_and_the_marker_is_retired(
    tmp_palinode_dir, tmp_memory_file, sample_item, monkeypatch, caplog
):
    """A job that never succeeds is retried a bounded number of times and then
    retired to .failed.json with the reason logged — a poison job must not
    loop forever."""
    from palinode.core.config import config

    monkeypatch.setattr(config.consolidation.write_time, "max_attempts", 2)
    marker = write_time._write_marker(tmp_memory_file, sample_item)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("check exploded")

    async def run():
        with patch.object(write_time, "_run_check_and_apply", _boom):
            for expected_attempts in (1, 2):
                assert write_time.sweep_pending_markers() == 1
                await _drain()
                assert os.path.exists(marker)
                assert _marker(marker)["attempts"] == expected_attempts

            # Bound reached: the third sweep retires it instead of retrying.
            with caplog.at_level(logging.ERROR):
                assert write_time.sweep_pending_markers() == 0

        assert not os.path.exists(marker)
        failed = marker.replace(".json", ".failed.json")
        assert os.path.exists(failed)
        assert _marker(failed)["attempts"] == 2
        assert _marker(failed)["item"] == sample_item  # preserved for review
        assert "no successful run after 2 attempt(s)" in caplog.text
        assert write_time._get_queue().qsize() == 0

        # And it stays retired — a .failed.json is never swept again.
        assert write_time.sweep_pending_markers() == 0

    asyncio.run(run())


def test_a_completed_job_clears_its_marker(
    tmp_palinode_dir, tmp_memory_file, sample_item
):
    """The no-regression half: a run that completes still consumes the marker,
    leaving nothing pending and nothing failed."""
    marker = write_time._write_marker(tmp_memory_file, sample_item)
    ran: list[str] = []

    def _ok(file_path, item):
        ran.append(file_path)
        return {"operations": [], "applied_stats": {"updated": 1}}

    async def run():
        assert write_time.sweep_pending_markers() == 1
        assert os.path.exists(marker)

        with patch.object(write_time, "_run_check_and_apply", _ok):
            await _drain()

        assert ran == [tmp_memory_file]
        assert not os.path.exists(marker)
        assert not os.path.exists(marker.replace(".json", ".failed.json"))
        assert _pending(tmp_palinode_dir) == []

    asyncio.run(run())


# ── Queue tests ────────────────────────────────────────────────────────────


def test_enqueue_when_no_event_loop_falls_to_marker(
    tmp_palinode_dir, tmp_memory_file, sample_item
):
    """Calling schedule from sync context (no event loop) uses disk markers."""
    result = write_time.schedule_contradiction_check(
        tmp_memory_file, sample_item, sync=False
    )
    assert result is None

    pending_dir = write_time._pending_dir()
    markers = [f for f in os.listdir(pending_dir) if f.endswith(".json")]
    assert len(markers) == 1


def test_queue_full_falls_to_marker(tmp_palinode_dir, tmp_memory_file, sample_item):
    """When the asyncio queue is full, new jobs land on disk instead of blocking."""

    async def run():
        # Fill the queue
        queue = write_time._get_queue()
        for _ in range(queue.maxsize):
            queue.put_nowait({"file_path": tmp_memory_file, "item": sample_item})

        assert queue.full()

        # Now schedule one more — should fall through to a marker
        result = write_time.schedule_contradiction_check(
            tmp_memory_file, sample_item, sync=False
        )
        assert result is None

        pending_dir = write_time._pending_dir()
        markers = [f for f in os.listdir(pending_dir) if f.endswith(".json")]
        assert len(markers) == 1, f"Expected 1 marker, found {markers}"

    asyncio.run(run())


# ── Sync path tests ────────────────────────────────────────────────────────
#
# These run the REAL `_run_check_and_apply` — the one function joining
# `_translate_ops` to `apply_operations` — rather than patching it out
# wholesale. Prior versions of this test patched `write_time._run_check_and_apply`
# itself, which meant the join was never exercised anywhere in the suite;
# see tests/test_proposer_seam.py's docstring for why wholesale-mocking the
# propose seam is inadequate. Only the LLM call (`llm_fn`) and the
# infra-boundary calls it makes (embedder, vector search) are faked — the real
# translate -> apply_operations -> file mutation pipeline runs end to end.


def _seed_contradiction_check_files(
    tmp_dir: str,
    *,
    fact_id: str = "f-old",
    fact_text: str = "Old fact needing update.",
    target_rel: str = os.path.join("decisions", "target.md"),
) -> str:
    """Seed the update.md prompt (required for _check_contradictions to reach
    its LLM call instead of short-circuiting to ADD) plus a target memory file
    with one tagged fact the executor can match against. Returns the target's
    absolute path."""
    prompts_dir = os.path.join(tmp_dir, "specs", "prompts")
    os.makedirs(prompts_dir, exist_ok=True)
    with open(os.path.join(prompts_dir, "update.md"), "w") as f:
        f.write("Return the operation as JSON.\n")

    target = os.path.join(tmp_dir, target_rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w") as f:
        f.write(
            "---\nid: decision-test\ncategory: decision\n---\n\n"
            f"- [2026-06-01] {fact_text} <!-- fact:{fact_id} -->\n"
        )
    return target


def _fake_llm(op_json: str):
    """A fake propose seam returning fixed op-JSON, regardless of the prompts."""
    def _fn(system_prompt: str, user_prompt: str) -> tuple[str, str]:
        return op_json, "fake-model"
    return _fn


def test_sync_path_applies_real_write_time_delete_end_to_end(
    tmp_palinode_dir, sample_item
):
    """A text-less write-time DELETE, driven through the real pipeline with a
    fake llm_fn, retires the target fact into history (ARCHIVE) and leaves an
    audit trail — it is neither silently dropped nor turned into a SUPERSEDE
    carrying the saved item's body. `embedder.embed` and
    `store.search_internal` are faked at the infra boundary (network
    embedder, vector DB) — everything downstream of the LLM call (parse,
    translate, apply_operations, file write) is real."""
    target = _seed_contradiction_check_files(tmp_palinode_dir)
    ops_json = json.dumps(
        {
            "operation": "DELETE",
            "target_id": "f-old",
            "reason": "contradicted by new save",
        }
    )

    with patch("palinode.consolidation.runner.embedder.embed", return_value=[0.1] * 8), \
            patch(
                "palinode.consolidation.runner.store.search_internal",
                return_value=[{"id": "f-old", "content": "Old fact needing update."}],
            ):
        result = write_time.schedule_contradiction_check(
            target, sample_item, sync=True, llm_fn=_fake_llm(ops_json)
        )

    assert result is not None
    assert result["applied_stats"]["archived"] == 1
    assert result["applied_stats"]["superseded"] == 0
    assert result["applied_stats"]["unmatched"] == 0
    body = open(target).read()
    assert "fact:f-old" not in body
    # The saved item's content is never written into the target as a
    # successor line.
    assert sample_item["content"] not in body
    history = open(target.replace(".md", "-history.md")).read()
    assert "Archived: [2026-06-01] Old fact needing update." in history
    assert "contradicted by new save" in history
    assert "<!-- fact:f-old -->" in history


def test_sync_path_swallows_check_errors(tmp_palinode_dir, sample_item):
    """Errors in the real pipeline never propagate to the save caller — the
    ADR-004 save-never-fails invariant — exercised via a genuine failure at
    the embedder boundary rather than a mock of _run_check_and_apply itself."""
    target = _seed_contradiction_check_files(tmp_palinode_dir)

    def _boom(text, backend="local"):
        raise RuntimeError("LLM exploded")

    with patch("palinode.consolidation.runner.embedder.embed", side_effect=_boom):
        # Must not raise
        result = write_time.schedule_contradiction_check(
            target, sample_item, sync=True
        )
        assert result is None  # error path returns None, save continues


# ── Op translation tests ───────────────────────────────────────────────────


def test_translate_ops_filters_ops_without_target_id():
    """Ops without a target_id can't be applied deterministically → filtered out."""
    ops = [
        {"operation": "UPDATE", "item": {"content": "new"}},  # no target_id
        {"operation": "UPDATE", "item": {"content": "new"}, "target_id": "f1", "new_text": "x"},
    ]
    translated = write_time._translate_ops(ops, "/tmp/fake.md")
    assert len(translated) == 1
    assert translated[0]["op"] == "UPDATE"
    assert translated[0]["id"] == "f1"


def test_translate_ops_update_without_text_is_skipped(caplog):
    """An UPDATE carrying no ``new_text`` is malformed: it produces no executor
    op, and the item's content is never used as the replacement. (Previously
    it became an UPDATE whose ``new_text`` was the whole saved body.) The
    warning names the target id."""
    ops = [
        {
            "operation": "UPDATE",
            "target_id": "f-old",
            "reason": "revised",
            "item": {"id": "decision-new", "content": "The new content."},
        }
    ]
    with caplog.at_level(logging.WARNING):
        translated = write_time._translate_ops(ops, "/tmp/fake.md")
    assert translated == []
    assert "write-time: UPDATE skipped — fact id='f-old'" in caplog.text


def test_translate_ops_update_with_text_is_unchanged():
    """An UPDATE carrying ``new_text`` maps to UPDATE with exactly that text;
    the item's content is not consulted."""
    ops = [
        {
            "operation": "UPDATE",
            "target_id": "f-old",
            "new_text": "explicit replacement text",
            "reason": "revised",
            "item": {"id": "decision-new", "content": "The new content."},
        }
    ]
    translated = write_time._translate_ops(ops, "/tmp/fake.md")
    assert translated == [
        {
            "op": "UPDATE",
            "id": "f-old",
            "new_text": "explicit replacement text",
            "reason": "revised",
        },
    ]


def test_translate_ops_delete_without_text_becomes_archive():
    """A DELETE carrying no ``new_text`` is a retirement with no replacement:
    it maps to ARCHIVE, and the item's content is never used as a successor
    line. (Previously it became a SUPERSEDE whose ``new_text`` was the whole
    saved body.)"""
    ops = [
        {
            "operation": "DELETE",
            "item": {"id": "decision-new", "content": "The new content that superseded it."},
            "target_id": "f-old",
            "reason": "contradicted by new",
        }
    ]
    translated = write_time._translate_ops(ops, "/tmp/fake.md")
    assert translated == [
        {"op": "ARCHIVE", "id": "f-old", "reason": "contradicted by new"},
    ]


def test_translate_ops_delete_with_text_becomes_supersede():
    """A DELETE carrying ``new_text`` maps to SUPERSEDE with exactly that text
    as the successor line; the item's content is not consulted."""
    ops = [
        {
            "operation": "DELETE",
            "item": {"id": "decision-new", "content": "item content, should be ignored"},
            "new_text": "explicit replacement text",
            "target_id": "f-old",
        }
    ]
    translated = write_time._translate_ops(ops, "/tmp/fake.md")
    assert len(translated) == 1
    op = translated[0]
    assert op["op"] == "SUPERSEDE"
    assert op["id"] == "f-old"
    assert op["superseded_by"] == "decision-new"
    assert op["new_text"] == "explicit replacement text"


# ── The `revalidate` job kind ──────────────────────────────────────────────
#
# The queue carries one other kind of job: deterministic backing revalidation
# (`palinode.core.revalidation`). It rides this marker path so deferred
# memory writes have one sweep, one recovery story and one failure surface —
# which means the sweep must carry it unchanged and the applier must be
# reachable from the same entry point the worker uses.


def _revalidate_item():
    return {
        "kind": write_time._REVALIDATE_KIND,
        "ref": "decisions/test-decision",
        "findings": [{"ref": "insights/gone", "hop": 1,
                      "reason": "support_withdrawn", "via": "decisions/test-decision",
                      "detail": "status:archived"}],
        "cleared": [],
    }


def test_the_marker_kind_matches_the_module_that_writes_it():
    """Two spellings, pinned: the enqueuer's and the router's."""
    from palinode.core import revalidation

    assert write_time._REVALIDATE_KIND == revalidation.MARKER_KIND


def test_sweep_carries_a_revalidate_marker_through_unchanged(
    tmp_palinode_dir, tmp_memory_file
):
    """A revalidate job survives the disk round trip with its item intact."""

    async def run():
        item = _revalidate_item()
        marker = write_time._write_marker(tmp_memory_file, item)

        assert write_time.sweep_pending_markers() == 1
        assert os.path.exists(marker)  # spent by a completed run, not by enqueue

        job = write_time._get_queue().get_nowait()
        assert job["file_path"] == tmp_memory_file
        assert job["item"] == item

    asyncio.run(run())


def test_a_revalidate_job_is_applied_without_calling_the_llm(
    tmp_palinode_dir, tmp_memory_file
):
    """The deterministic branch: no propose seam, no contradiction check."""
    source = os.path.join(tmp_palinode_dir, "insights", "gone.md")
    os.makedirs(os.path.dirname(source), exist_ok=True)
    with open(source, "w") as f:
        f.write("---\nid: insights-gone\nstatus: archived\n---\n\n# Gone\n")
    with open(tmp_memory_file, "w") as f:
        f.write("---\nid: decision-test\ncategory: decision\nstatus: active\n"
                "backed_by:\n  - insights/gone\n---\n\n# Test Decision\n")

    with patch("palinode.consolidation.runner._check_contradictions") as llm:
        result = write_time._run_check_and_apply(tmp_memory_file, _revalidate_item())

    llm.assert_not_called()
    assert result["operations"] == []
    assert result["applied_stats"]["flagged"] == 1

    import frontmatter

    entry = frontmatter.load(tmp_memory_file).metadata["stale_backing"][0]
    assert entry["ref"] == "insights/gone"
    assert entry["op"] == "revalidate-check"


def test_a_revalidate_job_for_a_vanished_target_reports_rather_than_raises(
    tmp_palinode_dir
):
    """A marker can outlive its target. That is a skip, counted and logged."""
    missing = os.path.join(tmp_palinode_dir, "decisions", "vanished.md")
    result = write_time._run_check_and_apply(missing, _revalidate_item())
    assert result["applied_stats"] == {"flagged": 0, "cleared": 0, "skipped": 1}


def test_a_corrupt_revalidate_marker_is_preserved_for_an_operator(tmp_palinode_dir):
    """Partial or failed application stays visible: `.failed.json`, not silence."""

    async def run():
        pending_dir = write_time._pending_dir()
        os.makedirs(pending_dir, exist_ok=True)
        marker = os.path.join(pending_dir, "20260912T110000-feedface.json")
        with open(marker, "w") as f:
            json.dump({"item": _revalidate_item()}, f)  # no file_path

        assert write_time.sweep_pending_markers() == 0
        assert not os.path.exists(marker)
        assert os.path.exists(marker.replace(".json", ".failed.json"))

    asyncio.run(run())

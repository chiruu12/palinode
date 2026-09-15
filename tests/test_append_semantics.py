"""``update_policy="append"`` appends; it does not silently replace the body.

The reported failure: a ~90-line insight was saved, then re-saved to the same
slug with ``update_policy: "append"`` and a short addendum. The file afterwards
held *only* the addendum. Every visible signal said the parameter had taken
effect — it validated, it landed in frontmatter, ``created_at`` was preserved —
and the receipt said ``(replaced)``, which reads as a status line rather than as
a report that 90 lines were gone.

``test_append_keeps_the_existing_body`` is that report, minimised: it fails on
the pre-fix code (the original paragraphs are absent) and passes after.

The rest pins the boundaries of the new semantic, which is deliberately narrow:
only an *explicit or file-inherited* ``append`` against an *explicitly slugged*
existing document appends. A save that declares no policy still overwrites, a
derived slug still disambiguates rather than appending onto an unrelated
memory, and ``replace`` still overwrites in place.

Real SQLite + tmp_path; the embedder is mocked so the test does not need Ollama
(same shape as ``test_update_policy_axis.py``).
"""
from __future__ import annotations

import importlib
from unittest.mock import patch

import frontmatter
import pytest
from fastapi.testclient import TestClient

from palinode.core.config import config

_FAKE_VECTOR = [0.01] * 1024

#: The body the reporter lost. Multi-paragraph on purpose: a single line would
#: pass a naive "is the old text in there somewhere" assertion by accident.
_ORIGINAL_BODY = """The resolver reads the projection, not the raw body.

That means an index lag shows up as a stale answer with a fresh timestamp,
which is worse than no answer at all — the caller has no way to tell.

- The projection is rebuilt by the watcher, not by the save path.
- A save returns before the rebuild, so a read immediately after can lag.
- The honest fix is to stamp the projection generation onto the answer.
"""

_ADDENDUM = "Filed as an issue against the point release."


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient on a fresh tmp memory_dir + real SQLite DB; git off."""
    db_path = tmp_path / ".palinode.db"
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    for _k in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE"):
        monkeypatch.delenv(_k, raising=False)
    import palinode.api.server as srv
    srv = importlib.reload(srv)
    srv._rate_counters.clear()
    with TestClient(srv.app, raise_server_exceptions=True) as c:
        yield c
    srv._rate_counters.clear()


def _save(client, **body):
    body.setdefault("type", "Insight")
    with (
        patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")),
        patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR),
    ):
        return client.post("/save", json=body)


def _body(file_path: str) -> str:
    return frontmatter.load(file_path).content


def _meta(file_path: str) -> dict:
    return frontmatter.load(file_path).metadata


# ── the regression ──────────────────────────────────────────────────────────

def test_append_keeps_the_existing_body(client):
    """The reported data loss, minimised. Fails before the fix."""
    first = _save(client, content=_ORIGINAL_BODY, slug="resolver-projection-lag")
    assert first.status_code == 200, first.text
    fp = first.json()["file_path"]

    second = _save(
        client,
        content=_ADDENDUM,
        slug="resolver-projection-lag",
        update_policy="append",
    )
    assert second.status_code == 200, second.text
    assert second.json()["file_path"] == fp  # same document, not a sibling

    body = _body(fp)
    # Every paragraph of the original survives, not merely some of it.
    for line in _ORIGINAL_BODY.strip().splitlines():
        if line.strip():
            assert line.strip() in body, f"lost original line: {line!r}"
    assert _ADDENDUM in body
    # And the original still comes first — an append adds beneath.
    assert body.index("The resolver reads the projection") < body.index(_ADDENDUM)


def test_append_receipt_says_appended(client):
    """The receipt has to name what happened; ``(replaced)`` was the reporter's
    strongest signal that the parameter had worked."""
    _save(client, content=_ORIGINAL_BODY, slug="receipt-doc")
    res = _save(client, content=_ADDENDUM, slug="receipt-doc",
                update_policy="append")
    assert res.json()["save_outcome"] == "appended"


def test_replace_receipt_still_says_replaced(client):
    _save(client, content=_ORIGINAL_BODY, slug="replace-doc")
    res = _save(client, content=_ADDENDUM, slug="replace-doc",
                update_policy="replace")
    assert res.json()["save_outcome"] == "replaced"
    assert _ADDENDUM in _body(res.json()["file_path"])
    assert "The resolver reads the projection" not in _body(res.json()["file_path"])


# ── the separator ───────────────────────────────────────────────────────────

def test_appended_block_carries_a_dated_heading_and_marker(client):
    from palinode.core.memory_write import APPEND_BLOCK_MARKER_PREFIX

    _save(client, content=_ORIGINAL_BODY, slug="sep-doc")
    res = _save(client, content=_ADDENDUM, slug="sep-doc",
                update_policy="append")
    post = frontmatter.load(res.json()["file_path"])
    body = post.content

    assert APPEND_BLOCK_MARKER_PREFIX in body
    # The heading is an H2 so the indexer gives the appended block its own
    # chunk instead of growing the tail chunk without bound.
    heading = f"## Update — {str(post.metadata['last_updated'])[:10]}"
    assert heading in body
    assert body.index(heading) < body.index(_ADDENDUM)


def test_appended_block_is_its_own_chunk(client):
    """The H2 separator is load-bearing for recall, not decoration."""
    from palinode.core.parser import parse_markdown

    long_body = _ORIGINAL_BODY * 8  # over parse_markdown's 2000-char floor
    _save(client, content=long_body, slug="chunk-doc")
    res = _save(client, content=_ADDENDUM, slug="chunk-doc",
                update_policy="append")
    with open(res.json()["file_path"], encoding="utf-8") as fh:
        _meta_out, sections = parse_markdown(fh.read())

    addendum_sections = [s for s in sections if _ADDENDUM in s["content"]]
    assert len(addendum_sections) == 1
    assert addendum_sections[0]["section_id"].startswith("update-")


def test_first_save_with_append_gets_no_separator(client):
    """Nothing to append to — a plain write, not a document born with a seam."""
    from palinode.core.memory_write import APPEND_BLOCK_MARKER_PREFIX

    res = _save(client, content=_ORIGINAL_BODY, slug="fresh-doc",
                update_policy="append")
    assert res.status_code == 200, res.text
    assert res.json()["save_outcome"] == "created"
    body = _body(res.json()["file_path"])
    assert APPEND_BLOCK_MARKER_PREFIX not in body
    assert "## Update" not in body


def test_repeated_appends_accumulate_in_order(client):
    _save(client, content="Block one.", slug="log-doc", update_policy="append")
    _save(client, content="Block two.", slug="log-doc", update_policy="append")
    res = _save(client, content="Block three.", slug="log-doc",
                update_policy="append")
    body = _body(res.json()["file_path"])
    assert body.index("Block one.") < body.index("Block two.") < body.index(
        "Block three."
    )


def test_identical_append_is_not_suppressed(client):
    """Deliberate: a repeated append leaves a visible, editable duplicate.
    Dropping it would silently discard caller content — the failure mode this
    change exists to end. If de-duplication is ever wanted it belongs at the
    caller, where the intent is known."""
    _save(client, content=_ORIGINAL_BODY, slug="dup-doc")
    _save(client, content=_ADDENDUM, slug="dup-doc", update_policy="append")
    res = _save(client, content=_ADDENDUM, slug="dup-doc",
                update_policy="append")
    assert _body(res.json()["file_path"]).count(_ADDENDUM) == 2


# ── what append must NOT do ─────────────────────────────────────────────────

def test_save_with_no_policy_still_overwrites(client):
    """The implicit default is not an append. Every ordinary re-save — session
    notes, snapshots, consolidation write-backs — depends on this."""
    _save(client, content=_ORIGINAL_BODY, slug="no-policy-doc")
    res = _save(client, content=_ADDENDUM, slug="no-policy-doc")
    body = _body(res.json()["file_path"])
    assert body.strip() == _ADDENDUM
    assert res.json()["save_outcome"] == "replaced"


def test_derived_slug_collision_still_disambiguates(client):
    """A derived slug that collides is an accident between two *unrelated*
    memories. Appending one onto the other would be worse than the sibling the
    disambiguator already mints."""
    first = _save(client, content="Shared opening line.\n\nFirst memory.",
                  update_policy="append")
    second = _save(client, content="Shared opening line.\n\nSecond memory.",
                   update_policy="append")
    assert first.json()["file_path"] != second.json()["file_path"]
    assert second.json()["save_outcome"] == "disambiguated"
    assert "First memory." not in _body(second.json()["file_path"])


def test_sticky_append_carries_forward(client):
    """``append`` is sticky exactly as ``replace`` is: a file that declares it
    keeps it when a later save omits the param, or the frontmatter would be
    lying about the file's regime."""
    _save(client, content=_ORIGINAL_BODY, slug="sticky-append",
          update_policy="append")
    res = _save(client, content=_ADDENDUM, slug="sticky-append")
    assert res.json()["save_outcome"] == "appended"
    body = _body(res.json()["file_path"])
    assert "The resolver reads the projection" in body
    assert _ADDENDUM in body


def test_flipping_to_replace_overrides_a_sticky_append(client):
    """An explicit param still wins over the file's declared regime — the
    escape hatch for rewriting an append-log wholesale."""
    _save(client, content=_ORIGINAL_BODY, slug="flip-doc",
          update_policy="append")
    res = _save(client, content=_ADDENDUM, slug="flip-doc",
                update_policy="replace")
    assert res.json()["save_outcome"] == "replaced"
    assert _body(res.json()["file_path"]).strip() == _ADDENDUM


# ── frontmatter around an append ────────────────────────────────────────────

def test_append_preserves_created_at_and_advances_last_updated(client):
    first = _save(client, content=_ORIGINAL_BODY, slug="stamp-doc",
                  update_policy="append")
    fp = first.json()["file_path"]
    born = _meta(fp)["created_at"]

    second = _save(client, content=_ADDENDUM, slug="stamp-doc",
                   update_policy="append")
    meta = _meta(second.json()["file_path"])
    assert str(meta["created_at"]) == str(born)
    assert str(meta["last_updated"]) >= str(born)


def test_append_content_hash_covers_the_whole_body(client):
    """``content_hash`` records what is on disk. After an append that is the
    composed document, not the addendum that triggered it."""
    import hashlib

    _save(client, content=_ORIGINAL_BODY, slug="hash-doc")
    res = _save(client, content=_ADDENDUM, slug="hash-doc",
                update_policy="append")
    fp = res.json()["file_path"]
    addendum_only = hashlib.sha256(_ADDENDUM.encode()).hexdigest()
    assert _meta(fp)["content_hash"] != addendum_only


def test_append_keeps_the_wiki_footer_at_the_end(client):
    """The auto-footer is re-emitted after the appended block rather than being
    stranded mid-document (PROGRAM.md wiki-maintenance contract)."""
    from palinode.core.memory_write import _WIKI_FOOTER_MARKER

    _save(client, content=_ORIGINAL_BODY, slug="footer-doc",
          entities=["project/palinode"], update_policy="append")
    res = _save(client, content=_ADDENDUM, slug="footer-doc",
                entities=["project/palinode"], update_policy="append")
    body = _body(res.json()["file_path"])
    assert body.count(_WIKI_FOOTER_MARKER) == 1
    assert body.index(_ADDENDUM) < body.index(_WIKI_FOOTER_MARKER)


# ── the append is searchable, not just on disk ──────────────────────────────

def test_appended_content_is_indexed(client):
    """Inline indexing runs over the composed body, so the addendum is
    keyword-searchable immediately after the save returns."""
    _save(client, content=_ORIGINAL_BODY, slug="indexed-doc")
    res = _save(client, content="Distinctive marmalade token.",
                slug="indexed-doc", update_policy="append")
    assert res.status_code == 200, res.text

    from palinode.core import store
    hits = store.search_fts("marmalade", top_k=5)
    assert any("indexed-doc" in h["file_path"] for h in hits), hits


# ── the composer, unit-level ────────────────────────────────────────────────

def test_compose_returns_new_content_when_there_is_no_prior_body():
    from palinode.core.memory_write import compose_appended_body

    out = compose_appended_body("   \n\n  ", "fresh", "2026-09-13T00:00:00+00:00")
    assert out == "fresh"


def test_compose_never_truncates_the_prior_body():
    from palinode.core.memory_write import compose_appended_body

    prior = "line one\nline two\n\nline three\n"
    out = compose_appended_body(prior, "added", "2026-09-13T01:02:03+00:00")
    assert out.startswith("line one\nline two\n\nline three")
    assert out.endswith("added")
    assert "## Update — 2026-09-13" in out
    assert "<!-- palinode-append 2026-09-13T01:02:03+00:00 -->" in out


def test_strip_wiki_footer_leaves_user_authored_sections_alone():
    from palinode.core.memory_write import _WIKI_FOOTER_MARKER, strip_wiki_footer

    user_authored = "body\n\n## See also\n- a hand-written pointer\n"
    assert strip_wiki_footer(user_authored) == user_authored.rstrip("\n")

    auto = f"body\n\n## See also\n{_WIKI_FOOTER_MARKER}\n- [[palinode]]\n"
    assert strip_wiki_footer(auto) == "body"


# ── the composed body survives the downstream readers ───────────────────────

def test_append_separator_survives_the_current_text_projection(client):
    """The projection (what FTS and the embedder actually see) removes only
    lifecycle tombstones. The append heading and marker are neither, so they
    must pass through untouched — and so must both blocks of prose."""
    from palinode.core.projection import project_current_text

    _save(client, content=_ORIGINAL_BODY, slug="projected-doc")
    res = _save(client, content=_ADDENDUM, slug="projected-doc",
                update_policy="append")
    with open(res.json()["file_path"], encoding="utf-8") as fh:
        projected = project_current_text(fh.read())

    assert "The resolver reads the projection" in projected.text
    assert _ADDENDUM in projected.text
    assert "## Update — " in projected.text


def test_append_does_not_disturb_fact_tagged_list_items(client):
    """The consolidation executor anchors every op on ``- … <!-- fact:ID -->``
    list items. The separator is a heading plus an HTML comment, so an append
    must leave existing fact ids addressable and mint none of its own."""
    import re as _re

    tagged = (
        "- the embedder moved to the GPU host <!-- fact:abc123 -->\n"
        "- the watcher debounce is 30s <!-- fact:def456 -->\n"
    )
    _save(client, content=tagged, slug="tagged-doc")
    res = _save(
        client,
        content="- and the index reconciles nightly <!-- fact:ghi789 -->",
        slug="tagged-doc",
        update_policy="append",
    )
    body = _body(res.json()["file_path"])

    assert set(_re.findall(r"<!-- fact:(\w+) -->", body)) == {
        "abc123", "def456", "ghi789"
    }
    # The separator itself carries no fact id and is not a list item.
    for line in body.splitlines():
        if line.startswith("## Update") or "palinode-append" in line:
            assert "fact:" not in line
            assert not line.lstrip().startswith(("-", "*"))

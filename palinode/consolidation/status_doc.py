"""Status-document integrity — the audit surface consolidation writes about itself.

``projects/<slug>-status.md`` carries a ``## Consolidation Log`` that is supposed
to be the blame-able record of what the deterministic executor did. It was none
of those things: rationales were dropped, operation kinds were
misreported, ``fact_id``s were never checked against the file, the log grew
without bound, and the frontmatter counts were never reconciled with the body.

This module holds the shared render/bound/reconcile primitives used by two
callers that must agree byte-for-byte:

* :func:`palinode.consolidation.runner._update_status_summary` — the write path.
* ``palinode repair-status`` — the one-time repair pass over existing files.

Design notes worth keeping:

* **The log stays in-file, capped, with a cumulative elision counter.** A
  sidecar history file was the alternative; it loses because the status doc is
  ``core: true`` and indexed, so a sidecar needs its own ``status: archived``
  frontmatter and indexing story, and it would sit next to the executor's
  existing ``-history.md`` as a *third* log surface, blurring the
  identity/status/history layer contract. Every write is git-committed, so the
  elided detail is recoverable from ``git log`` — the cap costs auditability
  nothing and removes the recall pollution that motivated the issue.
* **Operation fields are read through** :mod:`palinode.consolidation.op_parse`
  only. Hand-rolled ``item.get("reason", "")`` in the write path — while the
  dry-run preview used ``op_reason()`` — is precisely why the defect was
  invisible from ``--dry-run``.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

import yaml

from palinode.consolidation.op_parse import op_kind, op_reason
from palinode.core.parser import FRONTMATTER_RE, split_frontmatter

logger = logging.getLogger("palinode.consolidation.status_doc")

LOG_HEADING = "## Consolidation Log"

#: Date blocks retained verbatim in the log; older ones collapse into the
#: elision line. Overridable via ``consolidation.status_log_max_blocks``.
DEFAULT_MAX_LOG_BLOCKS = 10

#: Operation lines retained within a single date block. Blocks are per-day, so
#: this only binds when consolidation runs many times in one day and prevents
#: duplicate entries from growing without bound under one heading.
MAX_LINES_PER_BLOCK = 50

#: Rationale text longer than this is truncated so a model cannot place an
#: unbounded paragraph of deliberation in a single entry.
MAX_REASON_CHARS = 240

#: Rendered in place of a ``fact_id`` that matches no ``<!-- fact:… -->`` marker
#: in the target file. Free LLM text never reaches the file.
UNRESOLVED = "(unresolved)"

_FACT_MARKER_RE = re.compile(r"<!-- fact:(\S+) -->")
# Anchored at line start so a `### Consolidation Log` sub-heading can't be
# mistaken for the section, and tolerant of the legacy
# `## Consolidation Log (<timestamp>)` form. Nothing emits that form any more
# (its writer was removed as dead code), but memory files written before then
# still carry it, so the tolerance stays.
_LOG_HEADING_RE = re.compile(r"^## Consolidation Log.*$", re.MULTILINE)
_DATE_HEADING_RE = re.compile(r"^### (\d{4}-\d{2}-\d{2})\s*$")
_OP_LINE_RE = re.compile(r"^- \[([A-Z_]+)\] (.*?):[ \t]?(.*)$")
_BRACKET_DATE_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2})\]")
_SECTION_HEADING_RE = re.compile(r"^## ", re.MULTILINE)
_ELISION_RE = re.compile(
    r"^- _\[log elided\] (\d+) operation line\(s\)"
    r"(?: across (\d+) date block\(s\))?"
    r"(?: — (\d{4}-\d{2}-\d{2}) → (\d{4}-\d{2}-\d{2}))?"
)
#: A ``fact_id`` shaped like an identifier. Anything else (a bracketed pseudo-id,
#: a sentence of model deliberation) is unrecoverable garbage, not a stale id.
_PLAUSIBLE_ID_RE = re.compile(r"^[A-Za-z0-9][\w.-]*$")
#: A bare ``YYYY-MM-DD``, and the ``before <date>`` label a range op logs under.
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RANGE_LABEL_RE = re.compile(r"^before \d{4}-\d{2}-\d{2}$")
#: Canonical ``kind/slug`` entity reference (PROGRAM.md wiki-maintenance).
_ENTITY_REF_RE = re.compile(r"^[a-z][a-z0-9_-]*/[a-z0-9][a-z0-9._-]*$")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def fact_ids(text: str) -> set[str]:
    """Every ``<!-- fact:ID -->`` marker present in *text*."""
    return set(_FACT_MARKER_RE.findall(text))


# ── log rendering ────────────────────────────────────────────────────────────


def _op_fact_ids(op: dict) -> list[str]:
    """The fact ids an operation names, in ``id`` / ``fact_id`` / ``ids`` order."""
    ids: list[str] = []
    for key in ("fact_id", "id"):
        val = op.get(key)
        if val:
            ids.append(str(val).strip())
            break
    raw = op.get("ids")
    if isinstance(raw, list):
        for val in raw:
            token = str(val).strip()
            if token and token not in ids:
                ids.append(token)
    return ids


def _clean_reason(text: str) -> str:
    reason = " ".join(str(text).split())
    if len(reason) > MAX_REASON_CHARS:
        reason = reason[: MAX_REASON_CHARS - 1].rstrip() + "…"
    return reason


def _format_label(resolved: list[str], total: int) -> str:
    if not resolved:
        return UNRESOLVED
    label = ", ".join(resolved)
    dropped = total - len(resolved)
    if dropped:
        label += f" (+{dropped} unresolved)"
    return label


def _range_label(op: dict) -> str | None:
    """``before <date>`` for a well-formed range op, else ``None``.

    ``ARCHIVE_BEFORE`` is the one operation that names no fact id — its
    subject is a date, and the lines it retires are whichever ones fall before
    it. Rendering :data:`UNRESOLVED` in the id slot would report the executor's
    largest single retirement as unauditable, so the range takes the slot
    instead. Recognised by :func:`_repair_log_line` as canonical, so the
    repair pass leaves these lines alone.
    """
    if op_kind(op) != "ARCHIVE_BEFORE":
        return None
    before = str(op.get("before") or "").strip()
    return f"before {before}" if _DATE_ONLY_RE.match(before) else None


def render_log_lines(operations: list[Any], known_ids: set[str]) -> list[str]:
    """Render operations as ``- [KIND] id: rationale`` audit lines.

    Contract:

    * kind and rationale come from :func:`op_kind` / :func:`op_reason`, so an op
      carrying only ``rationale`` (ARCHIVE/RETRACT are rationale-first in the
      executor) logs its rationale instead of an empty string;
    * a missing kind defaults to ``KEEP`` — what the executor actually performs
      — not ``UPDATE``, so the log can no longer claim a mutation that never ran;
    * a ``KEEP`` with no rationale emits no line at all (it is a no-op with
      nothing to audit);
    * a ``fact_id`` absent from *known_ids* is replaced by :data:`UNRESOLVED` —
      LLM free text never lands in the id slot;
    * an ``ARCHIVE_BEFORE`` names a date rather than an id, so its ``before``
      date takes the id slot as ``before <date>`` (see :func:`_range_label`).
    """
    lines: list[str] = []
    for op in operations:
        if not isinstance(op, dict):
            logger.warning("Skipping malformed operation in status log: %r", op)
            continue
        kind = op_kind(op) or "KEEP"
        reason = _clean_reason(op_reason(op))
        if kind == "KEEP" and not reason:
            continue
        ids = _op_fact_ids(op)
        resolved = [i for i in ids if i in known_ids]
        label = _range_label(op) or _format_label(resolved, len(ids))
        lines.append(f"- [{kind}] {label}: {reason}".rstrip())
    return lines


# ── log section parsing / bounding ───────────────────────────────────────────


class _Elision:
    """Cumulative counter for log content collapsed out of the file.

    *fact_id* is the marker the bullet already carries, carried across the
    re-render. The elision line is an ordinary body bullet, so
    ``bootstrap_all_fact_ids`` tags it like any other; rebuilding the line from
    the counters alone dropped that marker every time the counts moved, leaving
    one untagged fact in an otherwise fully tagged document and inviting the
    next bootstrap pass to mint a *fresh* id — which would break any
    ``backed_by``/blame reference to the old one. Preserved, never minted here:
    a line that arrives untagged stays untagged.
    """

    def __init__(self, ops: int = 0, blocks: int = 0,
                 first: str | None = None, last: str | None = None,
                 fact_id: str | None = None) -> None:
        self.ops = ops
        self.blocks = blocks
        self.first = first
        self.last = last
        self.fact_id = fact_id

    def add(self, ops: int, blocks: int, dates: list[str]) -> None:
        self.ops += ops
        self.blocks += blocks
        for date in dates:
            if self.first is None or date < self.first:
                self.first = date
            if self.last is None or date > self.last:
                self.last = date

    def render(self) -> str:
        span = f" — {self.first} → {self.last}" if self.first and self.last else ""
        scope = f" across {self.blocks} date block(s)" if self.blocks else ""
        marker = f" <!-- fact:{self.fact_id} -->" if self.fact_id else ""
        return (
            f"- _[log elided] {self.ops} operation line(s)"
            f"{scope}{span}. Full detail in git history._{marker}"
        )

    def __bool__(self) -> bool:
        return bool(self.ops or self.blocks)


def _parse_elision(line: str) -> _Elision | None:
    match = _ELISION_RE.match(line)
    if not match:
        return None
    marker = _FACT_MARKER_RE.search(line)
    return _Elision(
        ops=int(match.group(1)),
        blocks=int(match.group(2) or 0),
        first=match.group(3),
        last=match.group(4),
        fact_id=marker.group(1) if marker else None,
    )


def _split_log_section(body: str) -> tuple[str, list[str], str]:
    """Return ``(before, section_lines, after)`` around the Consolidation Log.

    ``section_lines`` excludes the ``## Consolidation Log`` heading itself and
    stops at the next ``## `` heading, so a log that is not the last section is
    handled correctly. When the heading is absent ``section_lines`` is empty and
    ``before`` is the whole body.
    """
    heading = _LOG_HEADING_RE.search(body)
    if heading is None:
        return body, [], ""
    idx = heading.start()
    rest = body[heading.end():]
    if rest.startswith("\n"):
        rest = rest[1:]
    next_heading = _SECTION_HEADING_RE.search(rest)
    section = rest[: next_heading.start()] if next_heading else rest
    after = rest[next_heading.start():] if next_heading else ""
    return body[:idx], section.splitlines(), after


def _parse_items(lines: list[str]) -> list[tuple]:
    """Parse log-section lines into ``("block", date, lines)`` / ``("raw", line)``.

    A block owns its ``### <date>`` heading plus the following run of operation
    lines (blank lines between them included). Anything else — session-end
    bullets appended by ``/wrap``, prose, the elision line — stays a ``raw``
    item in place and is never rewritten.
    """
    items: list[tuple] = []
    i = 0
    total = len(lines)
    while i < total:
        heading = _DATE_HEADING_RE.match(lines[i])
        if not heading:
            items.append(("raw", lines[i]))
            i += 1
            continue
        block = [lines[i]]
        i += 1
        pending: list[str] = []
        while i < total:
            line = lines[i]
            if not line.strip():
                pending.append(line)
                i += 1
                continue
            if _OP_LINE_RE.match(line):
                block.extend(pending)
                pending = []
                block.append(line)
                i += 1
                continue
            break
        items.append(("block", heading.group(1), block))
        items.extend(("raw", blank) for blank in pending)
    return items


def _block_op_lines(block: list[str]) -> list[str]:
    return [line for line in block if _OP_LINE_RE.match(line)]


def _trim_block(block: list[str], date: str, elision: _Elision) -> list[str]:
    """Drop the oldest operation lines from an over-long single-day block."""
    op_positions = [i for i, line in enumerate(block) if _OP_LINE_RE.match(line)]
    excess = len(op_positions) - MAX_LINES_PER_BLOCK
    if excess <= 0:
        return block
    doomed = set(op_positions[:excess])
    elision.add(excess, 0, [date])
    return [line for i, line in enumerate(block) if i not in doomed]


def _bound_items(items: list[tuple], max_blocks: int,
                 elision: _Elision) -> list[tuple]:
    """Bound the log: cap the number of date blocks and each block's length.

    Everything dropped is accounted for in *elision* — the counter is
    cumulative, so a status doc's log never loses the *fact* that operations
    happened, only their per-line detail (which stays in git history).
    """
    block_positions = [i for i, item in enumerate(items) if item[0] == "block"]
    excess = len(block_positions) - max_blocks if max_blocks > 0 else 0
    doomed = set(block_positions[:excess]) if excess > 0 else set()

    kept: list[tuple] = []
    for i, item in enumerate(items):
        if i in doomed:
            elision.add(len(_block_op_lines(item[2])), 1, [item[1]])
            continue
        if item[0] == "block":
            kept.append(("block", item[1], _trim_block(item[2], item[1], elision)))
            continue
        kept.append(item)
    return kept


def _emit_items(items: list[tuple], elision: _Elision) -> list[str]:
    """Render items back to lines, normalising blank-line runs.

    Each date block is preceded by exactly one blank line; leading blanks and
    consecutive blank runs are collapsed. Normalising here is what stops the
    section from accreting whitespace across runs.
    """
    lines: list[str] = []
    if elision:
        lines.append(elision.render())
    for item in items:
        if item[0] == "block":
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend(item[2])
        elif item[1].strip() or (lines and lines[-1].strip()):
            lines.append(item[1])
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def merge_log_entry(body: str, date: str, new_lines: list[str],
                    max_blocks: int = DEFAULT_MAX_LOG_BLOCKS) -> str:
    """Merge *new_lines* into the ``## Consolidation Log`` for *date*, bounded.

    Same-date reruns extend the existing ``### <date>`` block instead of
    emitting a duplicate heading. Blocks older than
    *max_blocks* collapse into one cumulative elision line.
    """
    before, section_lines, after = _split_log_section(body)

    elision = _Elision()
    retained: list[str] = []
    for line in section_lines:
        parsed = _parse_elision(line)
        if parsed is not None:
            elision = parsed
            continue
        retained.append(line)

    items = _parse_items(retained)

    if new_lines:
        for i in range(len(items) - 1, -1, -1):
            if items[i][0] == "block" and items[i][1] == date:
                # Re-running consolidation on the same day is common and mostly
                # re-proposes the same operations; logging them twice is the
                # duplication this guard prevents.
                existing = set(items[i][2])
                items[i][2].extend(ln for ln in new_lines if ln not in existing)
                break
        else:
            items.append(("block", date, [f"### {date}", *new_lines]))

    items = _bound_items(items, max_blocks, elision)
    rendered = _emit_items(items, elision)

    section = "\n".join(rendered)
    prefix = before.rstrip("\n")
    parts = [prefix, "", LOG_HEADING, ""] if prefix else [LOG_HEADING, ""]
    parts.append(section)
    result = "\n".join(parts).rstrip("\n") + "\n"
    if after:
        result += "\n" + after.lstrip("\n")
    return result


# ── frontmatter reconciliation ───────────────────────────────────────────────


def _body_dates(body: str) -> list[str]:
    dates = set(_BRACKET_DATE_RE.findall(body))
    dates.update(re.findall(r"^### (\d{4}-\d{2}-\d{2})\s*$", body, re.MULTILINE))
    return sorted(dates)


def _lenient_frontmatter(raw: str) -> dict[str, Any] | None:
    """Best-effort recovery of frontmatter YAML that does not strict-parse.

    the status-doc YAML repair's whole point is that ``yaml.safe_load`` *throws* on these files, so
    the repair pass cannot get at ``entities:`` through the strict parser. This
    models the narrow subset the corruption produces — top-level ``key: value``
    scalars, ``key:`` followed by ``- item`` list entries taken as raw strings,
    and folded continuation lines — and returns ``None`` for anything richer
    (nested mappings, anchors) so an unmodelled document is left alone rather
    than silently rewritten wrong.
    """
    meta: dict[str, Any] = {}
    list_key: str | None = None
    scalar_key: str | None = None

    for line in raw.split("\n"):
        if not line.strip():
            continue
        stripped = line.strip()
        indented = line[0].isspace()

        if stripped.startswith("- ") or stripped == "-":
            if list_key is None:
                return None
            if meta.get(list_key) is None:
                meta[list_key] = []
            if not isinstance(meta[list_key], list):
                return None
            meta[list_key].append(stripped[1:].strip())
            continue

        if indented:
            if scalar_key is None or not isinstance(meta.get(scalar_key), str):
                return None
            meta[scalar_key] = f"{meta[scalar_key]} {stripped}"
            continue

        key, sep, value = line.partition(":")
        if not sep or not key.strip() or key.strip() != key:
            return None
        key = key.strip()
        value = value.strip()
        if not value:
            list_key, scalar_key = key, None
            meta.setdefault(key, None)
            continue
        try:
            parsed = yaml.safe_load(value)
        except yaml.YAMLError:
            parsed = value
        meta[key] = value if parsed is None else parsed
        list_key, scalar_key = None, key

    return meta


def _clean_entities(value: Any) -> list[str]:
    """Strip fact markers from ``entities`` entries and coerce ``None`` to ``[]``.

    Non-destructive: a value that is not an entity reference is kept (only its
    stray ``<!-- fact:… -->`` residue is removed). ``repair_status_doc`` is the
    surface that relocates non-references out of the field.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        return value
    cleaned: list[str] = []
    for entry in value:
        text = _FACT_MARKER_RE.sub("", str(entry)).strip()
        if text:
            cleaned.append(text)
    return cleaned


def desired_frontmatter(content: str) -> dict[str, Any] | None:
    """What *content*'s frontmatter should say, given its body. Pure.

    Sets ``memory_count`` (distinct ``<!-- fact:… -->`` markers in the body)
    and ``date_range`` (min → max ``[YYYY-MM-DD]`` / ``### YYYY-MM-DD`` in the
    body), and cleans fact-marker residue out of ``entities``.

    No clock, no I/O, no serialization — so a caller can ask *"is this document
    already correct?"* without changing it. That question is unanswerable
    against a function that stamps the time on its way past, which is what
    made ``repair-status`` report a clean store as 100% dirty.

    ``last_updated`` is passed through untouched. It is a write receipt, not
    derived state: only a caller that decides to write may set it, and it is
    excluded from the correctness comparison (see ``_without_receipt``).
    Leaving it in place preserves its position under ``sort_keys=False``.

    Returns ``None`` when there is no frontmatter, when it does not parse, or
    when it is not a mapping. The caller must then leave the file alone — a
    consolidation write must never be the thing that destroys a file.
    """
    block, body = split_frontmatter(content)
    if not block:
        return None
    match = FRONTMATTER_RE.match(block)
    if not match:
        return None
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        logger.warning(
            "status frontmatter does not parse — leaving it untouched "
            "(run `palinode repair-status`): %s", exc,
        )
        return None
    if not isinstance(meta, dict):
        return None

    meta = dict(meta)
    if "entities" in meta:
        meta["entities"] = _clean_entities(meta["entities"])
    meta["memory_count"] = len(fact_ids(body))
    dates = _body_dates(body)
    if dates:
        meta["date_range"] = f"{dates[0]} to {dates[-1]}"
    return meta


def render(meta: dict[str, Any], body: str) -> str:
    """Serialize *meta* and *body* into a document. Deterministic.

    ``safe_dump`` quotes any scalar that would otherwise re-read as YAML syntax — the
    ``- [2026-05-24] …`` breakage in the status-doc YAML repair. Key order is preserved.
    """
    dumped = yaml.safe_dump(
        meta, default_flow_style=False, allow_unicode=True, sort_keys=False
    )
    return f"---\n{dumped}---\n{body}"


def _without_receipt(meta: dict[str, Any] | None) -> dict[str, Any]:
    """*meta* minus ``last_updated`` — the view that decides correctness.

    ``last_updated`` records *when the content last meaningfully changed*, so
    comparing it would make every document differ from itself.
    """
    if meta is None:
        return {}
    trimmed = dict(meta)
    trimmed.pop("last_updated", None)
    return trimmed


def current_frontmatter(content: str) -> dict[str, Any] | None:
    """*content*'s frontmatter exactly as written. ``None`` if absent or bad."""
    block, _ = split_frontmatter(content)
    if not block:
        return None
    match = FRONTMATTER_RE.match(block)
    if not match:
        return None
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    return meta if isinstance(meta, dict) else None


# ── one-time repair ──────────────────────────────────────────────────────────


def _strip_block_markers(frontmatter_block: str) -> tuple[str, int]:
    """Remove ``<!-- fact:… -->`` markers from a raw frontmatter block."""
    stripped, count = _FACT_MARKER_RE.subn("", frontmatter_block)
    if not count:
        return frontmatter_block, 0
    # A tagged line now ends in the whitespace that preceded the marker.
    return "\n".join(line.rstrip() for line in stripped.split("\n")), count


def strip_frontmatter_fact_markers(content: str) -> tuple[str, int]:
    """Strip fact markers wrongly tagged into a memory file's frontmatter.

    This is the *store-wide* half of the repair, and it is deliberately the
    only transformation applied outside ``projects/*-status.md``.

    ``bootstrap_all_fact_ids`` walks ``people/``, ``projects/``, ``decisions/``
    and ``insights/``, so marker injection was never confined to status docs.
    The residue is not cosmetic: a marker lands *inside* the string value of a
    frontmatter list entry, so ``entities: [project/infra <!-- fact:x -->]``
    becomes a distinct node in the entity graph. In an affected store that can
    split one person entity into multiple nodes, one per tagged file.

    Structural safety is measured, not assumed: this changes exactly the marker
    text — no key set, list
    length, scalar type or value changes beyond marker removal, no value
    becomes empty, and nothing that parsed before stops parsing. A list entry
    that consists *only* of a marker would strip to an empty scalar, so that
    case is dropped rather than left as a ``null`` list member.

    Returns ``(content, markers_stripped)``; unchanged when there is no
    frontmatter or no markers in it.
    """
    frontmatter_block, body = split_frontmatter(content)
    if not frontmatter_block:
        return content, 0
    stripped_block, count = _strip_block_markers(frontmatter_block)
    if not count:
        return content, 0
    kept = [
        line for line in stripped_block.split("\n")
        if line.strip() not in ("-", "*")
    ]
    return "\n".join(kept) + body, count


def _repair_log_line(line: str, known_ids: set[str]) -> tuple[str | None, bool]:
    """Re-render one historical log line.

    Returns ``(text, newly_unresolved)`` where ``text`` is ``None`` when the
    line is unrecoverable and should be elided. Re-running over already-repaired
    output is a no-op (the :data:`UNRESOLVED` sentinel is recognised, not
    re-processed as a garbage id), so the repair is idempotent.
    """
    match = _OP_LINE_RE.match(line)
    if not match:
        return line, False
    kind, raw_id, reason = match.group(1), match.group(2).strip(), match.group(3)
    reason = _clean_reason(reason)
    if kind == "KEEP" and not reason:
        return None, False
    if raw_id == UNRESOLVED or _RANGE_LABEL_RE.match(raw_id) or raw_id in known_ids:
        return f"- [{kind}] {raw_id}: {reason}".rstrip(), False
    if not _PLAUSIBLE_ID_RE.match(raw_id):
        # Not a stale id — a bracketed pseudo-id or a paragraph of model
        # deliberation occupying the id slot. Nothing here is auditable.
        return None, False
    return f"- [{kind}] {UNRESOLVED}: {reason}".rstrip(), True


def repair_status_doc(content: str, *,
                      max_blocks: int = DEFAULT_MAX_LOG_BLOCKS,
                      extra_known_ids: set[str] | None = None,
                      now: datetime | None = None) -> tuple[str, dict[str, Any]]:
    """Repair one status document in memory. Returns ``(content, report)``.

    ``extra_known_ids`` widens the resolvable set beyond the markers still in
    the file — callers pass the sibling ``-history.md``'s ids so a *legitimately*
    archived fact keeps its id in the log instead of being flagged unresolved.

    Applies to existing files exactly the rules the writer now applies, plus the
    block cap:

    1. fact markers wrongly tagged into frontmatter list entries are stripped,
       and ``entities`` values that are not ``kind/slug`` references are moved
       into a ``## Recovered from frontmatter`` body section — relocated, never
       dropped;
    2. log lines whose id is unrecoverable garbage are elided, stale-but-plausible
       ids become ``(unresolved)``, empty ``KEEP`` lines are elided;
    3. the log is bounded to *max_blocks* date blocks with a cumulative elision
       line;
    4. frontmatter counts/dates are reconciled with the body.

    The report distinguishes two kinds of difference:

    ``changed``
        the document is semantically wrong — a counter moved, entities were
        relocated, log lines were repaired, or the body differs. This is what
        a caller should count when asking *"does anything need repair?"*.
    ``noncanonical``
        the document is semantically correct but its YAML is not what
        ``render`` would emit (quoting, spacing, comments). Reported so the status-doc YAML repair
        formatting drift is visible, but it does not mean damage.
    ``unparseable``
        the frontmatter could not be read; the original content is returned
        untouched.

    ``last_updated`` is stamped only when ``changed`` — normalizing formatting
    must not move a document up ``ranker``'s recency window.
    """
    report: dict[str, Any] = {
        "frontmatter_markers_stripped": 0,
        "entities_relocated": 0,
        "log_lines_elided": 0,
        "log_ids_unresolved": 0,
        "changed": False,
        "noncanonical": False,
        "unparseable": False,
    }

    frontmatter_block, body = split_frontmatter(content)
    recovered: list[str] = []

    if frontmatter_block:
        frontmatter_block, marker_count = _strip_block_markers(frontmatter_block)
        report["frontmatter_markers_stripped"] = marker_count
        match = FRONTMATTER_RE.match(frontmatter_block)
        if match:
            strict = True
            try:
                meta = yaml.safe_load(match.group(1))
            except yaml.YAMLError:
                # The corrupted case: `- [2026-05-24] …` reads as a flow-sequence
                # opener, so the strict parser can't reach `entities:` at all.
                meta = _lenient_frontmatter(match.group(1))
                strict = False
                if meta is None:
                    logger.warning(
                        "frontmatter is unparseable and not recoverable — "
                        "leaving it untouched for manual review",
                    )
            if isinstance(meta, dict):
                if isinstance(meta.get("entities"), list):
                    keep: list[str] = []
                    for entry in meta["entities"]:
                        text = _FACT_MARKER_RE.sub("", str(entry)).strip()
                        if _ENTITY_REF_RE.match(text):
                            keep.append(text)
                        elif text:
                            recovered.append(text)
                    if recovered:
                        report["entities_relocated"] = len(recovered)
                        meta["entities"] = keep
                if recovered or not strict:
                    # Re-dumping is what makes the document strict-parseable:
                    # safe_dump quotes any scalar that would otherwise re-read
                    # as YAML syntax.
                    dumped = yaml.safe_dump(
                        meta, default_flow_style=False, allow_unicode=True,
                        sort_keys=False,
                    )
                    frontmatter_block = f"---\n{dumped}---\n"

    if recovered:
        body = body.rstrip("\n") + "\n\n## Recovered from frontmatter\n\n"
        body += "".join(f"- {line}\n" for line in recovered)

    known_ids = fact_ids(body) | set(extra_known_ids or set())
    before, section_lines, after = _split_log_section(body)

    elision = _Elision()
    kept_lines: list[str] = []
    for line in section_lines:
        parsed = _parse_elision(line)
        if parsed is not None:
            elision = parsed
            continue
        repaired, newly_unresolved = _repair_log_line(line, known_ids)
        if repaired is None:
            report["log_lines_elided"] += 1
            elision.add(1, 0, [])
            continue
        if newly_unresolved:
            report["log_ids_unresolved"] += 1
        kept_lines.append(repaired)

    if section_lines:
        items = _bound_items(_parse_items(kept_lines), max_blocks, elision)
        rendered = _emit_items(items, elision)
        prefix = before.rstrip("\n")
        parts = [prefix, "", LOG_HEADING, ""] if prefix else [LOG_HEADING, ""]
        parts.append("\n".join(rendered))
        body = "\n".join(parts).rstrip("\n") + "\n"
        if after:
            body += "\n" + after.lstrip("\n")

    repaired = frontmatter_block + body
    desired = desired_frontmatter(repaired)
    if desired is None:
        report["unparseable"] = True
        return content, report

    repaired_body = split_frontmatter(repaired)[1]
    report["changed"] = (
        _without_receipt(desired) != _without_receipt(current_frontmatter(content))
        or repaired_body != split_frontmatter(content)[1]
    )
    if report["changed"]:
        desired["last_updated"] = (now or _utc_now()).isoformat()

    rendered = render(desired, repaired_body)
    report["noncanonical"] = not report["changed"] and rendered != content
    return rendered, report

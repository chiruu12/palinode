"""Resolution policy over gathered evidence — pure, and deliberately timid.

:mod:`palinode.core.evidence` gathers what the store holds around a search hit
and refuses to say which side wins. This module is the layer that decides, and
it decides one of exactly three things:

``supported_current``
    A record stands as the current answer, with the evidence that supports it.
``unresolved_conflict``
    Two or more sides cannot both hold and nothing mechanical settles it. Every
    visible side is reported; no winner is picked.
``insufficient_evidence``
    Unknown, as an explicit result — never a guess, and never the older value
    that a withdrawn replacement used to replace.

It is pure: frontmatter (already read by the evidence layer), a
:class:`~palinode.core.evidence.SeedEvidence`, and an injected clock in; a
frozen :class:`Resolution` out. No filesystem, no database, no network, and no
clock read other than ``now``. The MCP, REST and CLI surfaces render it; they
do not re-decide it, so the three realizations cannot disagree.

What resolves automatically, and what never does
------------------------------------------------

Only **mechanically explicit** changes retire a record or pick a winner:

* a ``superseded_by`` chain that ends at a visible, standing successor,
* a retirement the writer declared (``status``/``lifecycle`` archived,
  deprecated, superseded, retracted — the executor's own archive/retract
  paths write these), and
* ``expires_at`` in the past, on the injected clock.

A replacement the writer **dated forward** has not happened yet, and the
record it will replace is still the last value in force until it does
(``replacement_scheduled``). The executor tombstones a record the moment its
successor is written, so this is read off two declarations and a clock — the
supersession link, the successor's own ``date``, and ``now`` — and only when
that pending supersession is the *whole* of the predecessor's retirement. A
predecessor that also ran out (``expires_at``), was retracted or deprecated on
its own account, or sits under ``archive/`` keeps the answer it had: unknown.
The transition rides on the standing record as ``superseded_from:<date>``, so
nobody is handed a value that is about to change without being told when.

Everything else is *advisory*. A ``contradicts`` link (including one the
compaction model proposed through ``PROPOSE_CONTRADICTS``) makes a conflict
**visible** — it can put a record on the ``sides`` list — but it cannot retire
anything or elect anybody. Neither can a newer date, an ``epistemic: fact``
label, a self-declared status, a similarity score, a rank position, or a
recall count. None of those is an input to any branch below; the one place
time is consulted is the two mechanical gates (``expires_at``, and a declared
future ``date``), and the one place ordering could have crept in — "the newer
record wins" — does not exist.

Claim kinds
-----------

A conflict between two records means different things depending on what kind
of claim each one makes, so each record is classified from the **existing**
vocabulary only (:data:`CLAIM_KINDS`) — ``type``, ``epistemic``, ``status``:

``proposal``
    The record declares its own claim unsettled: ``epistemic: open_question``
    or ``epistemic: unverified``. That is the only way the current vocabulary
    says "proposed, not accepted".
``accepted_intent``
    ``type: Decision`` that is not marked unsettled — a decision in force
    (the same reading the consolidation runner's ``ACTIVE_DECISIONS`` takes).
``observation``
    ``epistemic: fact`` (directly observed / verified), or
    ``type: ProjectSnapshot`` (a report of observed state).
``inference``
    ``epistemic: inference`` — derived, not observed.
``unknown``
    No signal at all. Every legacy untyped record is here, and it *stays*
    here: absence is not promoted to any kind, and a record with no ``date``
    is reported ``undated`` rather than dated from a file stamp.

A newer proposal never replaces an accepted decision; a decision and an
observation that disagree are a ``policy_implementation_mismatch``, reported
as such — consistent with the compaction prompt's rule 9, where a fact
observed *later* than the decision it conflicts with records the conflict and
retires nothing.

Comparing subject and applicability
-----------------------------------

Two records conflict only if their claims apply to an overlapping situation.
:func:`applicable` is the test, and it is conservative in the direction of
"these coexist":

1. **Explicit scope** — two records that both declare ``scope:`` and declare
   *different* scopes do not overlap. The directory-inferred default is never
   consulted (it would make every category look like a separate scope).
2. **Namespaced entities, kind by kind** — for every entity *kind* (the part
   before ``/``) that both records declare, they must name at least one entity
   in common. A production observation (``env/production``) and a staging one
   (``env/staging``) share ``project/shop`` but no ``env/`` ref, so they
   coexist. Bare, un-namespaced entities are deliberately not facets: they are
   as often the disputed value as the subject.
3. **Time windows** — ``[effective moment, expires_at)``. Windows that do not
   overlap describe different periods and do not conflict.

A ``contradicts`` link whose two sides fail this test is reported through the
``scope_disjoint`` reason instead of as a conflict. The link is still visible
in the evidence block; it just does not make the seed contested.

Evidence lineage
----------------

Support is grouped by the origin it rests on, never counted by record. A
session summary, a project snapshot and a decision rationale that all cite the
same ``claims[].source_id`` + quote anchor (or the same ``sources[].ref``, or
the same ``backed_by`` ref) are three copies of one origin and appear as one
:class:`SupportGroup`. Lineage is only ever taken from those explicit anchors —
matching prose establishes neither dependence nor independence, so a record
with no anchor forms its own group with ``origin_kind: unknown`` and
contributes the ``lineage_unknown`` reason.

Grouping exists to make the copies legible, not to weigh them: no branch in
this module counts support at all, so N copies of one observation cannot
outvote one explicit correction — and neither could N independent ones.

Backing that stopped holding
----------------------------

A record that declares ``backed_by`` rests on something. Two inputs say that
support no longer holds: the ``stale_backing`` flag a retirement persisted,
and the read-time support check the evidence layer ran
(:mod:`palinode.core.revalidation`), which sees a source retired *since* the
last maintenance pass and one two hops out that propagation never walked.
How many withdrawals it takes is the record's own declaration:

* ``backing_policy: all-of`` — one withdrawn source is enough.
* ``backing_policy: any-of`` — every declared source must be gone.
* no declared policy (every legacy list) — advisory: findings are reported as
  qualifiers, and the outcome only changes when every declared source is
  flagged, which is the rule this module already applied.

The outcome is ``insufficient_evidence`` either way, with the reason naming
which kind of loss it was: ``support_withdrawn`` for a source that was
retired, ``support_disproven`` for one that was explicitly shown false. Both
are uncertainty. Neither ever elects the opposite claim, and neither brings
back the value a retired source used to carry — the same rule
``replacement_withdrawn`` applies one layer up.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Mapping

from palinode.core.claims import parse_claims
from palinode.core.lifecycle import RETIRED_STATUSES as _RETIRED_STATUSES
from palinode.core.lifecycle import Eligibility, eligibility, parse_moment
from palinode.core.parser import parse_sources
from palinode.core.typed_links import parse_link_refs

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import cycle
    from palinode.core.evidence import EvidenceRecord, EvidenceResult, SeedEvidence

# ── outcomes ─────────────────────────────────────────────────────────────────

OUTCOME_SUPPORTED = "supported_current"
OUTCOME_CONFLICT = "unresolved_conflict"
OUTCOME_INSUFFICIENT = "insufficient_evidence"

#: The three outcomes. Nothing else is ever returned.
OUTCOMES: tuple[str, ...] = (OUTCOME_SUPPORTED, OUTCOME_CONFLICT, OUTCOME_INSUFFICIENT)

# ── claim kinds ──────────────────────────────────────────────────────────────

KIND_PROPOSAL = "proposal"
KIND_ACCEPTED_INTENT = "accepted_intent"
KIND_OBSERVATION = "observation"
KIND_INFERENCE = "inference"
KIND_UNKNOWN = "unknown"

#: The closed set of claim kinds (see the module docstring for the signals).
CLAIM_KINDS: tuple[str, ...] = (
    KIND_PROPOSAL, KIND_ACCEPTED_INTENT, KIND_OBSERVATION, KIND_INFERENCE, KIND_UNKNOWN,
)

# ── reasons ──────────────────────────────────────────────────────────────────

# Deciding reasons — exactly one of these names why the outcome is what it is.
EXPLICIT_REPLACEMENT = "explicit_replacement"
UNCONTESTED = "uncontested"
PROPOSAL_NOT_DECISION = "proposal_does_not_replace_decision"
POLICY_IMPLEMENTATION_MISMATCH = "policy_implementation_mismatch"
INCOMPARABLE_OBSERVATIONS = "incomparable_observations"
NO_EXPLICIT_CHANGE = "no_explicit_change"
EXPIRED = "expired"
NOT_YET_EFFECTIVE = "not_yet_effective"
REPLACEMENT_SCHEDULED = "replacement_scheduled"
REPLACEMENT_WITHDRAWN = "replacement_withdrawn"
REPLACEMENT_UNRESOLVED = "replacement_unresolved"
RETIRED_NO_SUCCESSOR = "retired_no_successor"
SUPPORT_WITHDRAWN = "support_withdrawn"
SUPPORT_DISPROVEN = "support_disproven"
NO_ELIGIBLE_EVIDENCE = "no_eligible_evidence"

#: Qualifier prefix for a record whose supersession is dated in the future:
#: ``superseded_from:<YYYY-MM-DD>``. It rides on the record that still stands,
#: so a reader is never handed a value that is about to change without being
#: told when. Read by the bundle renderer; nothing else derives from it.
SUPERSEDED_FROM = "superseded_from"

# Qualifying reasons — they ride along and are never the whole story.
UNDATED = "undated"
KIND_UNKNOWN_REASON = "kind_unknown"
SCOPE_DISJOINT = "scope_disjoint"
COVERAGE_PARTIAL = "coverage_partial"
INDEX_STALE = "index_stale"
LINEAGE_UNKNOWN = "lineage_unknown"

#: The closed reason vocabulary. A consumer can match on these; nothing
#: outside the set is ever emitted, so no free-text reason can name a record
#: the requester may not see.
REASONS: frozenset[str] = frozenset({
    EXPLICIT_REPLACEMENT, UNCONTESTED, PROPOSAL_NOT_DECISION,
    POLICY_IMPLEMENTATION_MISMATCH, INCOMPARABLE_OBSERVATIONS, NO_EXPLICIT_CHANGE,
    EXPIRED, NOT_YET_EFFECTIVE, REPLACEMENT_SCHEDULED, REPLACEMENT_WITHDRAWN,
    REPLACEMENT_UNRESOLVED,
    RETIRED_NO_SUCCESSOR, SUPPORT_WITHDRAWN, SUPPORT_DISPROVEN, NO_ELIGIBLE_EVIDENCE,
    UNDATED, KIND_UNKNOWN_REASON, SCOPE_DISJOINT, COVERAGE_PARTIAL, INDEX_STALE,
    LINEAGE_UNKNOWN,
})

#: Backing policies a record may declare, spelled out here rather than
#: imported for the same reason ``_FALLBACK_DISABLED`` is: this module stays
#: free of the layers that read disk. Mirrors
#: :data:`palinode.core.revalidation.BACKING_POLICIES`, and
#: ``tests/test_revision_aware_backing.py`` pins the two together.
_POLICY_ALL_OF = "all-of"
_POLICY_ANY_OF = "any-of"
_POLICY_ADVISORY = "advisory"
BACKING_POLICIES: tuple[str, ...] = (_POLICY_ALL_OF, _POLICY_ANY_OF, _POLICY_ADVISORY)

#: The one coverage reason that says nothing about the evidence — it is the
#: caller's own ``resolve="linked"`` choice. Mirrors
#: ``palinode.core.evidence.FALLBACK_DISABLED``, spelled out rather than
#: imported so this module stays free of the gathering layer (the evidence
#: layer imports the store; the policy imports nothing that touches disk).
#: ``tests/test_resolution_policy.py`` pins the two together.
_FALLBACK_DISABLED = "fallback_disabled"

#: Roles a record plays in a resolution.
ROLE_SEED = "seed"
ROLE_REPLACEMENT = "replacement"
ROLE_CONTRADICTION = "contradiction"
ROLE_SUPPORT = "support"
ROLE_DISCOVERED = "discovered"


# ── the facts one record declares ────────────────────────────────────────────


@dataclass(frozen=True)
class ClaimFacts:
    """What one record's live frontmatter declares — no policy, no inference.

    Built by :func:`claim_facts` from the metadata the evidence layer already
    read. Every field is either present in the frontmatter or ``None``/empty;
    nothing is defaulted into existence, which is what lets a legacy untyped
    record resolve without being assigned a date, a kind or an authority.
    """

    ref: str | None
    type: str | None
    status: str | None
    epistemic: str | None
    scope: str | None
    entities: tuple[str, ...] = ()
    contradicts: tuple[str, ...] = ()
    backed_by: tuple[str, ...] = ()
    stale_backing: tuple[str, ...] = ()
    #: ``all-of`` / ``any-of`` when the record declared one; ``advisory``
    #: otherwise — a legacy list asserts no policy, so no conclusion is drawn
    #: from it beyond the one this module already drew (every source flagged).
    backing_policy: str = _POLICY_ADVISORY
    source_refs: tuple[str, ...] = ()
    claim_anchors: tuple[str, ...] = ()
    superseded_by: str | None = None
    expires_at: str | None = None
    #: The record's effective moment (``date`` → ``last_updated`` →
    #: ``created_at``), or ``None`` when the record is undated.
    effective_at: datetime | None = None
    #: The *declared* ``date`` only. A save stamp is not a declaration, so
    #: only this field can make a record future-effective.
    declared_at: datetime | None = None
    #: Lifecycle state and the signal that decided it (:mod:`.lifecycle`).
    state: str = "unmarked"
    state_reason: str = "unmarked"
    #: Retired by *location* — the record sits under ``archive/``. Carried
    #: separately because a declared retirement outranks the path rule and so
    #: hides it from :attr:`state_reason`
    #: (:attr:`palinode.core.lifecycle.Eligibility.by_path`).
    retired_by_path: bool = False

    @property
    def retired(self) -> bool:
        return self.state == "retired"

    @property
    def undated(self) -> bool:
        return self.effective_at is None


def _str(value: Any) -> str | None:
    if value is None or isinstance(value, (list, dict, bool)):
        return None
    text = str(value).strip()
    return text or None


def _entities(meta: Mapping[str, Any]) -> tuple[str, ...]:
    raw = meta.get("entities")
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for item in raw:
        ref = _str(item)
        if ref and ref not in out:
            out.append(ref)
    return tuple(out)


def _claim_anchors(meta: Mapping[str, Any]) -> tuple[str, ...]:
    """``<source_id>#<quote_hash>`` for every claim anchor the record carries."""
    out: list[str] = []
    for entry in parse_claims(dict(meta)):
        span = entry.get("span") or {}
        anchor = f"{entry['source_id']}#{str(span.get('quote_hash') or '').strip()}"
        if anchor not in out:
            out.append(anchor)
    return tuple(out)


def _source_refs(meta: Mapping[str, Any]) -> tuple[str, ...]:
    out: list[str] = []
    for entry in parse_sources(dict(meta)):
        ref = _str(entry.get("ref")) if isinstance(entry, dict) else None
        if ref and ref not in out:
            out.append(ref)
    return tuple(out)


def _stale_refs(elig: Eligibility) -> tuple[str, ...]:
    return tuple(dict.fromkeys(elig.stale_backing))


def _backing_policy(meta: Mapping[str, Any]) -> str:
    """The declared backing policy, or ``advisory``. Soft-fail: an unreadable
    policy must never make this module *more* confident than a legacy list."""
    raw = _str(meta.get("backing_policy"))
    value = (raw or "").lower().replace("_", "-")
    return value if value in (_POLICY_ALL_OF, _POLICY_ANY_OF) else _POLICY_ADVISORY


def _norm_ref(ref: str) -> str:
    """Comparison form of a ref: no ``.md``, no surrounding whitespace."""
    text = str(ref).strip().lstrip("/")
    return text[:-3] if text.endswith(".md") else text


def claim_facts(
    meta: Mapping[str, Any] | None,
    *,
    ref: str | None = None,
    now: datetime | None = None,
) -> ClaimFacts:
    """Project one record's frontmatter into :class:`ClaimFacts`. Pure.

    ``now`` is the clock :func:`palinode.core.lifecycle.eligibility` uses for
    ``expires_at``; it is the only clock input.
    """
    fm: dict[str, Any] = dict(meta) if isinstance(meta, Mapping) else {}
    elig = eligibility(fm, path=ref, now=now)
    return ClaimFacts(
        ref=ref,
        type=_str(fm.get("type")),
        status=_str(fm.get("status")) or _str(fm.get("lifecycle")),
        epistemic=elig.epistemic,
        scope=_str(fm.get("scope")),
        entities=_entities(fm),
        contradicts=tuple(dict.fromkeys(elig.contradicts)),
        backed_by=tuple(dict.fromkeys(parse_link_refs(fm, "backed_by"))),
        stale_backing=_stale_refs(elig),
        backing_policy=_backing_policy(fm),
        source_refs=_source_refs(fm),
        claim_anchors=_claim_anchors(fm),
        superseded_by=elig.superseded_by,
        expires_at=elig.expires_at,
        effective_at=elig.effective_at,
        declared_at=parse_moment(fm.get("date")),
        state=elig.state,
        state_reason=elig.reason,
        retired_by_path=elig.by_path,
    )


# ── claim kind ───────────────────────────────────────────────────────────────

#: ``epistemic`` values that declare the record's own claim unsettled.
_UNSETTLED_EPISTEMICS: frozenset[str] = frozenset({"open_question", "unverified"})


def claim_kind(facts: ClaimFacts) -> str:
    """Classify the kind of claim a record makes (see :data:`CLAIM_KINDS`).

    First match wins, and the order is the point: a ``Decision`` that declares
    itself unsettled is a *proposal*, not an accepted intent, and a record
    with no ``type`` and no ``epistemic`` is ``unknown`` rather than being
    read as a fact.
    """
    epistemic = (facts.epistemic or "").lower()
    if epistemic in _UNSETTLED_EPISTEMICS:
        return KIND_PROPOSAL
    if facts.type == "Decision":
        return KIND_ACCEPTED_INTENT
    if epistemic == "fact":
        return KIND_OBSERVATION
    if epistemic == "inference":
        return KIND_INFERENCE
    if facts.type == "ProjectSnapshot":
        return KIND_OBSERVATION
    return KIND_UNKNOWN


# ── applicability ────────────────────────────────────────────────────────────


def _by_kind(entities: tuple[str, ...]) -> dict[str, set[str]]:
    """Namespaced entity refs grouped by their kind; bare refs are dropped."""
    out: dict[str, set[str]] = {}
    for ref in entities:
        kind, sep, _name = ref.partition("/")
        if sep and kind:
            out.setdefault(kind, set()).add(ref)
    return out


def _expires_moment(facts: ClaimFacts) -> datetime | None:
    return parse_moment(facts.expires_at)


def applicable(a: ClaimFacts, b: ClaimFacts) -> bool:
    """Could these two records' claims apply to the same situation?

    ``True`` when nothing in the declared scope, entities or time windows
    separates them — which includes the case where neither declares anything,
    since absence is not a separation. See the module docstring for the three
    tests and why bare entities are not among them.
    """
    if a.scope and b.scope and a.scope != b.scope:
        return False

    kinds_a, kinds_b = _by_kind(a.entities), _by_kind(b.entities)
    for kind in kinds_a.keys() & kinds_b.keys():
        if not (kinds_a[kind] & kinds_b[kind]):
            return False

    a_end, b_end = _expires_moment(a), _expires_moment(b)
    if a_end is not None and b.effective_at is not None and a_end <= b.effective_at:
        return False
    if b_end is not None and a.effective_at is not None and b_end <= a.effective_at:
        return False
    return True


# ── the resolution ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Side:
    """One record as it appears in a resolution: the standing answer, a visible
    side of a conflict, or a member of a support group."""

    ref: str | None
    kind: str
    #: ``current`` | ``unmarked`` | ``retired`` | ``contested`` — the
    #: frontmatter half of ``store.currency_of``'s vocabulary.
    currency: str
    currency_reason: str
    role: str
    effective_at: str | None
    epistemic: str | None
    #: Qualifications the record carries and a reader must not lose:
    #: ``epistemic:…``, ``contradicts:…``, ``stale_backing:…``,
    #: ``expires_at:…``, ``undated``, ``index_stale``.
    qualifiers: tuple[str, ...] = ()
    #: The support origin this record rests on, when it names one.
    origin: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "kind": self.kind,
            "currency": self.currency,
            "currency_reason": self.currency_reason,
            "role": self.role,
            "effective_at": self.effective_at,
            "epistemic": self.epistemic,
            "qualifiers": list(self.qualifiers),
            "origin": self.origin,
        }


@dataclass(frozen=True)
class SupportGroup:
    """Support that rests on one origin. Every member counts as that origin once."""

    origin: str
    #: ``claim`` | ``source`` | ``backed_by`` | ``unknown`` — how the origin
    #: was established. ``unknown`` means the record named no anchor, not that
    #: it was shown to be independent.
    origin_kind: str
    members: tuple[Side, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "origin_kind": self.origin_kind,
            "members": [m.to_dict() for m in self.members],
        }


@dataclass(frozen=True)
class Resolution:
    """What :func:`resolve` decided, and why."""

    outcome: str
    #: The record that stands, or ``None`` for a conflict or for unknown.
    current: Side | None = None
    #: Every visible side considered — populated whenever more than one record
    #: was in play, so a conflict keeps both sides and a decision that stood
    #: still shows the proposal it stood against.
    sides: tuple[Side, ...] = ()
    #: Support and discovered records, grouped by the origin each one names.
    #: Grouping is reporting, not weighing: nothing here is counted.
    support: tuple[SupportGroup, ...] = ()
    reasons: tuple[str, ...] = ()
    qualifiers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "current": self.current.to_dict() if self.current else None,
            "sides": [s.to_dict() for s in self.sides],
            "support": [g.to_dict() for g in self.support],
            "reasons": list(self.reasons),
            "qualifiers": list(self.qualifiers),
        }


# ── building sides ───────────────────────────────────────────────────────────


def _currency(facts: ClaimFacts) -> tuple[str, str]:
    """``(currency, reason)`` from frontmatter alone.

    The frontmatter half of :func:`palinode.core.store.currency_of` — the text
    half (a retired fact tombstone inside otherwise-current text) is already
    projected out of everything the evidence layer carries.
    """
    if facts.retired:
        if facts.superseded_by:
            return "retired", f"superseded_by: {facts.superseded_by}"
        return "retired", facts.state_reason
    if facts.contradicts:
        return "contested", "contradicts: " + ", ".join(facts.contradicts)
    return facts.state, facts.state_reason


def _qualifiers(
    facts: ClaimFacts, *, freshness: str | None = None, check: Any | None = None
) -> tuple[str, ...]:
    """Qualifications a reader must not lose, persisted and read-time alike.

    ``check`` is this record's :class:`~palinode.core.revalidation.SupportCheck`
    when the evidence layer ran one: its findings arrive as
    ``stale_backing:<ref>@<hop>:<reason>``, beside — not instead of — the
    ``stale_backing:<ref>`` a persisted flag already produced. A record whose
    source was retired *since* the last maintenance pass carries the second
    without carrying the first, which is the gap being closed.
    """
    out: list[str] = []
    if facts.epistemic:
        out.append(f"epistemic:{facts.epistemic}")
    out.extend(f"contradicts:{ref}" for ref in facts.contradicts)
    out.extend(f"stale_backing:{ref}" for ref in facts.stale_backing)
    if check is not None:
        out.extend(check.qualifiers())
    if facts.expires_at:
        out.append(f"expires_at:{facts.expires_at}")
    if facts.undated:
        out.append(UNDATED)
    if freshness == "stale":
        out.append(INDEX_STALE)
    return tuple(dict.fromkeys(out))


def _origin(facts: ClaimFacts) -> tuple[str, str]:
    """The support origin this record rests on: ``(origin, origin_kind)``.

    Precedence is claim anchor → declared source → ``backed_by`` ref, each
    taken in sorted order so one record maps to exactly one origin and the
    grouping is deterministic. A record naming none of them is its own origin
    with kind ``unknown``: unnamed lineage stays unknown.
    """
    if facts.claim_anchors:
        return sorted(facts.claim_anchors)[0], "claim"
    if facts.source_refs:
        return sorted(facts.source_refs)[0], "source"
    if facts.backed_by:
        return sorted(facts.backed_by)[0], "backed_by"
    return facts.ref or "", "unknown"


def _side(
    facts: ClaimFacts,
    *,
    role: str,
    freshness: str | None = None,
    check: Any | None = None,
) -> Side:
    currency, reason = _currency(facts)
    origin, origin_kind = _origin(facts)
    return Side(
        ref=facts.ref,
        kind=claim_kind(facts),
        currency=currency,
        currency_reason=reason,
        role=role,
        effective_at=facts.effective_at.isoformat() if facts.effective_at else None,
        epistemic=facts.epistemic,
        qualifiers=_qualifiers(facts, freshness=freshness, check=check),
        origin=origin if origin_kind != "unknown" else None,
    )


@dataclass(frozen=True)
class _Candidate:
    """One evidence record with the facts and the side derived from it."""

    facts: ClaimFacts
    side: Side
    record: EvidenceRecord | None = None
    #: This record's support check, when the evidence layer ran one.
    check: Any | None = None


def _candidates(
    records: list[EvidenceRecord],
    *,
    role: str,
    now: datetime | None,
    checks: Mapping[str, Any] | None = None,
) -> list[_Candidate]:
    out: list[_Candidate] = []
    for rec in records:
        facts = claim_facts(rec.meta, ref=rec.ref, now=now)
        check = (checks or {}).get(rec.ref)
        out.append(_Candidate(
            facts, _side(facts, role=role, freshness=rec.freshness, check=check), rec, check
        ))
    return out


def _support_failure(facts: ClaimFacts, check: Any | None) -> str | None:
    """The deciding reason this record's declared backing no longer holds.

    ``None`` when it still does — including when nothing is known, which is
    the usual case for a legacy record. The policy the record declared is what
    decides how many withdrawals it takes:

    * ``all-of`` — one flagged source is enough. The record said it needs
      every one of them.
    * ``any-of`` and the legacy advisory default — every declared source must
      be flagged. One standing source still supports the record, and an
      advisory list draws no automatic conclusion short of losing all of them
      (the rule this module already applied to a persisted flag).

    A disproven source names itself: the outcome is still uncertainty, never
    the opposite claim, but a reader is told the difference between a source
    that was withdrawn and one that was shown false.
    """
    declared = {_norm_ref(r) for r in facts.backed_by}
    if not declared:
        return None
    withdrawn = {_norm_ref(r) for r in facts.stale_backing}
    disproven: set[str] = set()
    policy = facts.backing_policy
    if check is not None:
        withdrawn |= {_norm_ref(r) for r in check.decisive_refs()}
        disproven |= {_norm_ref(r) for r in check.disproven_refs()}
        policy = check.policy or policy
    flagged = declared & withdrawn
    if not flagged:
        return None
    if policy != _POLICY_ALL_OF and not declared <= withdrawn:
        return None
    return SUPPORT_DISPROVEN if (declared & disproven) else SUPPORT_WITHDRAWN


# ── the policy ───────────────────────────────────────────────────────────────


def _future(facts: ClaimFacts, now: datetime) -> bool:
    """Is this record's *declared* date still ahead of the clock?

    Only ``date`` counts. ``last_updated`` and ``created_at`` are save stamps —
    a backfilled import, a re-summarised file or a touched mtime must not be
    able to make a record effective, in either direction. There is no
    ``effective_from``/``valid_from`` field in the vocabulary today; if one is
    added, it belongs here beside ``date``.
    """
    return facts.declared_at is not None and facts.declared_at > now


#: Retiring ``status`` / ``lifecycle`` values that are the bookkeeping half of
#: a supersession rather than a statement about the record itself: the archive
#: op stamps ``archived`` beside the ``superseded_by`` it writes, and a
#: hand-written retirement uses the legacy ``superseded``. ``retracted`` (shown
#: false) and ``deprecated`` (do not use this) are claims about *this* record,
#: so neither is ever read as "retired only because something replaces it".
_SUPERSESSION_STATUSES: frozenset[str] = frozenset({"archived", "superseded"})


def _retired_only_by(facts: ClaimFacts, successor: ClaimFacts, now: datetime) -> bool:
    """Is the pending supersession by ``successor`` the *only* thing retiring ``facts``?

    The question a scheduled replacement turns on. The executor tombstones a
    record the moment its successor is written, so a successor dated forward
    leaves a store that says two things: this record is retired, and the thing
    replacing it does not apply until later. Reading the retirement as
    scheduled is only honest when the supersession is all there is to it —
    every other retiring signal is a statement the record makes on its own
    account and outlives any successor:

    * ``expires_at`` in the past — it ran out on its own clock,
    * ``retracted`` / ``deprecated`` — shown false, or withdrawn from use,
    * a path under ``archive/`` — moved out of the live corpus, whatever the
      frontmatter still says (:attr:`ClaimFacts.retired_by_path`).

    Any of those, and the answer stays what it was: unknown.
    """
    if not facts.superseded_by or not successor.ref:
        return False
    if _norm_ref(facts.superseded_by) != _norm_ref(successor.ref):
        return False
    if facts.retired_by_path:
        return False
    expires = _expires_moment(facts)
    if expires is not None and expires <= now:
        return False
    status = (facts.status or "").lower()
    return status not in (_RETIRED_STATUSES - _SUPERSESSION_STATUSES)


def _pending_predecessor(
    successor: ClaimFacts, cands: list[_Candidate], now: datetime
) -> _Candidate | None:
    """The record a not-yet-effective successor will replace, if exactly one is.

    The other end of the same link. A retired predecessor is out of default
    recall, so the record a query actually reaches is usually the successor —
    and it has to answer the same question from that side, or the answer would
    depend on which end of the link was seeded.

    ``None`` unless exactly one visible record names this successor and is
    retired by nothing else (:func:`_retired_only_by`): two predecessors
    merging into one successor do not say which of them still applies, and a
    guess there is the thing this module does not do.
    """
    found = [
        cand for cand in cands
        if cand.record is not None
        and cand.record.relation == "superseded_by"
        and cand.record.direction == "reverse"
        and _retired_only_by(cand.facts, successor, now)
        and not _future(cand.facts, now)
        and _support_failure(cand.facts, cand.check) is None
    ]
    return found[0] if len(found) == 1 else None


def _scheduled(side: Side, successor: ClaimFacts) -> Side:
    """``side`` with the date its supersession takes effect on it."""
    moment = successor.declared_at
    stamp = moment.date().isoformat() if moment else ""
    return replace(
        side,
        qualifiers=tuple(dict.fromkeys(
            (*side.qualifiers, f"{SUPERSEDED_FROM}:{stamp}")
        )),
    )


def _forward_chain(seed_ref: str | None, cands: list[_Candidate]) -> list[_Candidate]:
    """The ``superseded_by`` chain from the seed, in hop order."""
    by_via: dict[str, _Candidate] = {}
    for cand in cands:
        rec = cand.record
        if rec is not None and rec.relation == "superseded_by" and rec.direction == "forward":
            by_via.setdefault(rec.via, cand)
    chain: list[_Candidate] = []
    cur, seen = seed_ref, {seed_ref}
    while cur in by_via:
        nxt = by_via[cur]
        if nxt.facts.ref in seen:
            break  # a cycle is not a replacement
        chain.append(nxt)
        seen.add(nxt.facts.ref)
        cur = nxt.facts.ref
    return chain


def _support_groups(cands: list[_Candidate]) -> tuple[SupportGroup, ...]:
    """Group support and discovered records by the origin each one names."""
    groups: dict[tuple[str, str], list[Side]] = {}
    for cand in cands:
        key = _origin(cand.facts)
        groups.setdefault(key, []).append(cand.side)
    return tuple(
        SupportGroup(origin=origin, origin_kind=kind, members=tuple(members))
        for (origin, kind), members in sorted(groups.items())
    )


def _conflict_reason(kinds: set[str]) -> str:
    if kinds == {KIND_ACCEPTED_INTENT, KIND_OBSERVATION}:
        return POLICY_IMPLEMENTATION_MISMATCH
    if kinds == {KIND_OBSERVATION}:
        return INCOMPARABLE_OBSERVATIONS
    return NO_EXPLICIT_CHANGE


def _finish(
    outcome: str,
    *,
    current: Side | None,
    sides: tuple[Side, ...],
    support: tuple[SupportGroup, ...],
    reasons: set[str],
) -> Resolution:
    considered = ((current,) if current else ()) + sides
    qualifiers: list[str] = []
    for side in considered:
        qualifiers.extend(side.qualifiers)
    # Two qualifications are promoted to reasons because they are what a
    # reader would otherwise supply for themselves: an unknown kind (no
    # authority may be assumed) and a missing date (no order may be assumed).
    if any(s.kind == KIND_UNKNOWN for s in considered):
        reasons.add(KIND_UNKNOWN_REASON)
    if any(UNDATED in s.qualifiers for s in considered):
        reasons.add(UNDATED)
    if any(g.origin_kind == "unknown" for g in support):
        reasons.add(LINEAGE_UNKNOWN)
    return Resolution(
        outcome=outcome,
        current=current,
        sides=sides,
        support=support,
        reasons=tuple(sorted(reasons)),
        qualifiers=tuple(sorted(dict.fromkeys(qualifiers))),
    )


def resolve(
    seed: Mapping[str, Any] | ClaimFacts | None,
    evidence: SeedEvidence,
    *,
    now: datetime | None = None,
) -> Resolution:
    """Decide the seed's outcome from the evidence gathered around it.

    ``seed`` is the seed's live frontmatter (what
    :attr:`~palinode.core.evidence.SeedEvidence.seed_meta` holds), or a
    prepared :class:`ClaimFacts`; ``None`` means the seed itself could not be
    read. ``evidence`` is that seed's :class:`~palinode.core.evidence.SeedEvidence`.
    ``now`` is the only clock input — given the same store state and the same
    ``now``, the result is identical on every surface and every run.
    """
    clock = now or datetime.now(UTC)
    reasons: set[str] = set()
    # ``fallback_disabled`` is the caller's own choice of mode, not evidence
    # that was cut off, so it is the one coverage reason that does not qualify
    # the outcome. Every other one does: a budget, a hidden target, a missing
    # file or a lagging index all mean something could be out there.
    if evidence.reasons - {_FALLBACK_DISABLED}:
        reasons.add(COVERAGE_PARTIAL)
    if evidence.seed_freshness == "stale":
        reasons.add(INDEX_STALE)

    if seed is None:
        reasons.add(NO_ELIGIBLE_EVIDENCE)
        return _finish(OUTCOME_INSUFFICIENT, current=None, sides=(), support=(), reasons=reasons)

    facts = seed if isinstance(seed, ClaimFacts) else claim_facts(
        seed, ref=evidence.seed_ref, now=clock
    )
    # Read-time support checks, when the evidence layer ran them. Duck-typed:
    # this module stays free of the gathering layer, and a caller that hands
    # in a bare ``SeedEvidence`` (or a test double) simply has none.
    checks: Mapping[str, Any] = getattr(evidence, "support_checks", None) or {}
    seed_check = checks.get(facts.ref) if facts.ref else None
    seed_side = _side(
        facts, role=ROLE_SEED, freshness=evidence.seed_freshness, check=seed_check
    )

    replacements = _candidates(
        evidence.replacements, role=ROLE_REPLACEMENT, now=clock, checks=checks
    )
    conflicts = _candidates(evidence.conflicts, role=ROLE_CONTRADICTION, now=clock)
    support = _support_groups(
        _candidates(evidence.support, role=ROLE_SUPPORT, now=clock)
        + _candidates(evidence.discovered, role=ROLE_DISCOVERED, now=clock)
    )

    def _done(outcome: str, *, current: Side | None, sides: tuple[Side, ...] = ()) -> Resolution:
        return _finish(outcome, current=current, sides=sides, support=support, reasons=reasons)

    # 1 — Mechanically explicit replacement. The only path that retires the
    #     seed in favour of another record.
    chain = _forward_chain(facts.ref, replacements)
    if facts.superseded_by and not chain:
        # The record names a successor the requester cannot see or that is
        # gone. Not a licence to present the record it replaced.
        reasons.add(REPLACEMENT_UNRESOLVED)
        return _done(OUTCOME_INSUFFICIENT, current=None, sides=(seed_side,))
    if chain:
        terminal = chain[-1]
        sides = (seed_side, *[c.side for c in chain])
        if terminal.facts.superseded_by:
            reasons.add(REPLACEMENT_UNRESOLVED)  # chain cut short of its end
            return _done(OUTCOME_INSUFFICIENT, current=None, sides=sides)
        if terminal.facts.retired:
            # The replacement was itself withdrawn. The value it replaced does
            # not come back — unknown is the honest answer.
            reasons.add(REPLACEMENT_WITHDRAWN)
            return _done(OUTCOME_INSUFFICIENT, current=None, sides=sides)
        if _future(terminal.facts, clock):
            # The change is dated, and that date has not arrived. Until it
            # does the replacement has not happened, so the record it will
            # replace is still the last value in force — but only when that
            # pending supersession is the *only* thing retiring it, and only
            # when it is itself effective and still supported. Anything else,
            # and unknown stays the honest answer.
            prior = chain[-2] if len(chain) >= 2 else None
            prior_facts = prior.facts if prior else facts
            prior_side = prior.side if prior else seed_side
            prior_check = prior.check if prior else seed_check
            if (
                _retired_only_by(prior_facts, terminal.facts, clock)
                and not _future(prior_facts, clock)
                and _support_failure(prior_facts, prior_check) is None
            ):
                reasons.add(REPLACEMENT_SCHEDULED)
                return _resolve_standing(
                    prior_facts, _scheduled(prior_side, terminal.facts), conflicts,
                    support=support, reasons=reasons,
                    extra_sides=tuple(s for s in sides if s is not prior_side),
                    clock=clock,
                )
            reasons.add(NOT_YET_EFFECTIVE)
            return _done(OUTCOME_INSUFFICIENT, current=None, sides=sides)
        failure = _support_failure(terminal.facts, terminal.check)
        if failure is not None:
            # The replacement stands only while its own backing does. It does
            # not fall back to what it replaced — that value is no more
            # supported than it was when it was replaced.
            reasons.add(failure)
            return _done(OUTCOME_INSUFFICIENT, current=None, sides=sides)
        reasons.add(EXPLICIT_REPLACEMENT)
        return _resolve_standing(
            terminal.facts, terminal.side, conflicts,
            support=support, reasons=reasons, extra_sides=(seed_side, *[c.side for c in chain[:-1]]),
            clock=clock,
        )

    # 2 — The seed's own lifecycle, on the injected clock.
    if facts.retired:
        reasons.add(EXPIRED if facts.state_reason == "expired" else RETIRED_NO_SUCCESSOR)
        return _done(OUTCOME_INSUFFICIENT, current=None, sides=(seed_side,))
    if _future(facts, clock):
        # This record *is* the scheduled change. Until its date arrives the
        # record it will replace is still the last value in force — the same
        # rule as the chain branch above, reached from the other end of the
        # link, which is the end a query usually reaches: the predecessor is
        # retired and therefore out of default recall.
        prior = _pending_predecessor(facts, replacements, clock)
        if prior is not None:
            reasons.add(REPLACEMENT_SCHEDULED)
            return _resolve_standing(
                prior.facts, _scheduled(prior.side, facts), conflicts,
                support=support, reasons=reasons, extra_sides=(seed_side,),
                clock=clock,
            )
        reasons.add(NOT_YET_EFFECTIVE)
        return _done(OUTCOME_INSUFFICIENT, current=None, sides=(seed_side,))

    # 3 — Support that was withdrawn under the record: the flag a retirement
    #     persisted, and what the read-time check found since (a source
    #     retired after the last maintenance pass, or two hops out). The
    #     declared ``backing_policy`` decides how many it takes.
    failure = _support_failure(facts, seed_check)
    if failure is not None:
        reasons.add(failure)
        return _done(OUTCOME_INSUFFICIENT, current=None, sides=(seed_side,))

    # 4 — Conflicts, kinds, and the standing answer.
    return _resolve_standing(
        facts, seed_side, conflicts, support=support, reasons=reasons,
        extra_sides=(), clock=clock,
    )


def _resolve_standing(
    facts: ClaimFacts,
    side: Side,
    conflicts: list[_Candidate],
    *,
    support: tuple[SupportGroup, ...],
    reasons: set[str],
    extra_sides: tuple[Side, ...],
    clock: datetime,
) -> Resolution:
    """Would ``facts`` stand as the current answer, and against what?

    ``extra_sides`` are records already established as history (the seed and
    the middle of a replacement chain): reported so the trail stays visible,
    never candidates to stand.
    """
    live: list[_Candidate] = []
    for cand in conflicts:
        if cand.facts.ref == facts.ref:
            continue
        # Only an edge incident to *this* record makes it contested. The
        # evidence block is the closure around the seed, so it can hold a
        # conflict two hops out that has nothing to do with the record now
        # standing (``via`` is the other end of the edge that was followed).
        incident = (
            (cand.record is not None and cand.record.via == facts.ref)
            or cand.facts.ref in facts.contradicts
        )
        if not incident:
            continue
        if cand.facts.retired or _future(cand.facts, clock):
            continue  # a retired side no longer contests anything
        if not applicable(facts, cand.facts):
            reasons.add(SCOPE_DISJOINT)
            continue
        live.append(cand)

    # A ``contradicts`` ref naming something not in the evidence block (hidden,
    # missing, or past a budget) is coverage, not absence of a conflict.
    seen = {c.facts.ref for c in conflicts}
    if any(ref not in seen for ref in facts.contradicts):
        reasons.add(COVERAGE_PARTIAL)

    def _finish_with(outcome: str, *, current: Side | None, sides: tuple[Side, ...]) -> Resolution:
        return _finish(outcome, current=current, sides=sides, support=support, reasons=reasons)

    if not live:
        reasons.add(UNCONTESTED)
        return _finish_with(OUTCOME_SUPPORTED, current=side, sides=extra_sides)

    sides = (*extra_sides, side, *[c.side for c in live])
    kinds = {side.kind} | {c.side.kind for c in live}

    # An accepted decision is not displaced by a proposal, however new the
    # proposal is. The decision stands; the proposal stays visible as a side.
    if kinds == {KIND_ACCEPTED_INTENT, KIND_PROPOSAL}:
        decisions = [
            s for s in (side, *[c.side for c in live]) if s.kind == KIND_ACCEPTED_INTENT
        ]
        if len(decisions) == 1:
            reasons.add(PROPOSAL_NOT_DECISION)
            return _finish_with(OUTCOME_SUPPORTED, current=decisions[0], sides=sides)

    reasons.add(_conflict_reason(kinds))
    return _finish_with(OUTCOME_CONFLICT, current=None, sides=sides)


# ── attaching to results ─────────────────────────────────────────────────────


def attach_resolution(
    results: list[dict[str, Any]],
    evidence: EvidenceResult,
    *,
    now: datetime | None = None,
) -> list[Resolution]:
    """Set a ``resolution`` block on each result row, beside its ``evidence``.

    Additive and opt-in: called only when a request asked for evidence, so a
    request that did not ask stays byte-identical.
    """
    out: list[Resolution] = []
    for row, seed in zip(results, evidence.seeds, strict=True):
        resolution = resolve(seed.seed_meta, seed, now=now)
        row["resolution"] = resolution.to_dict()
        out.append(resolution)
    return out


__all__ = [
    "BACKING_POLICIES",
    "CLAIM_KINDS",
    "OUTCOMES",
    "OUTCOME_CONFLICT",
    "OUTCOME_INSUFFICIENT",
    "OUTCOME_SUPPORTED",
    "REASONS",
    "SUPERSEDED_FROM",
    "ClaimFacts",
    "Resolution",
    "Side",
    "SupportGroup",
    "applicable",
    "attach_resolution",
    "claim_facts",
    "claim_kind",
    "resolve",
]

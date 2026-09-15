# Delivery Receipts

A **delivery receipt** is the qualified record of one hand-off of context: which
records were supplied, at which exact source revision, how each was disposed, what
lineage is known behind them, under which policy and caller scope, evaluated on
which clock, and what the next known temporal boundary is.

It records **supplied context, not causal influence**. A receipt never claims that a
delivered record shaped what the agent did next; that edge is a separate, unbuilt
piece of provenance.

Implementation: [`palinode/core/receipt.py`](../palinode/core/receipt.py). A narrative
introduction is in [`HOW-MEMORY-WORKS.md` §10](HOW-MEMORY-WORKS.md#10-delivery-receipts-what-you-were-just-handed).

---

## Fields

### Delivery level

| Field | Type | Meaning |
| --- | --- | --- |
| `bundle_id` | string (16 hex) | Identity of this delivery: a digest of the normalized request, the supplied `(ref, revision)` pairs, and the evaluation time. The correlation key shared by the response, the retrieval log, and any later bundle. A second identical request is a second delivery and gets a different id. |
| `policy_version` | string | `palinode/<package>+projection/<n>+config/<fingerprint>` — see [Policy version](#policy-version). |
| `scope` | array of string | The caller scope chain **as the server resolved it**. `[]` = no scope identity (access control only). Never the caller's own claim. |
| `requested_time` | ISO-8601 | The clock the lifecycle policy evaluated against. |
| `evaluated_at` | ISO-8601 | When this result was evaluated. |
| `next_transition` | ISO-8601 or `null` | The nearest known transition **ahead of** `evaluated_at` among the delivered records: the earliest `expires_at`, or a declared future `date`. `null` means no boundary is known — it never means "never". A malformed or absent date contributes nothing. |
| `supplied` | array | One entry per delivered record (below). |
| `lineage` | array | Origin groups (below). |
| `coverage` | object | `{"status": "complete" \| "partial" \| "not_requested", "reasons": [...]}`. `partial` reasons come from the closed evidence vocabulary. `not_requested` means no evidence was gathered at all — distinct from `complete`, because nothing was looked for. |
| `dispositions` | object | Counts per disposition, over `supplied`. |

### `supplied[]` — one delivered record

| Field | Meaning |
| --- | --- |
| `ref` | The memory ref (path without `.md`). |
| `revision` | The exact source revision, or `null` when the delivery never computed one. |
| `revision_basis` | Which hash domain `revision` is in: `index_section_sha256` (the raw per-section hash `store.check_freshness` compares against — what a search hit carries), `file_sha256` (the whole file as read — what a `/context/prime` row and an evidence record carry, and what a `resolve` record carries whenever `freshness` is `stale`, because under index lag the delivered text came from the file and the indexed hash describes nothing that was supplied), or `unknown` (the delivery never computed one). **The domains are never compared across surfaces**, which is why the basis is named on every record. |
| `freshness` | Index/source agreement: `valid` / `stale` / `unknown`. `null` where the surface computes no index comparison. |
| `currency` | Whether the assertion is in force: `current` / `unmarked` / `retired` / `contested`. |
| `span_integrity` | Whether the record's cited quote anchors still match their sources. |
| `disposition` | See below. |
| `origin` / `origin_kind` | The support anchor this record rests on (`claim` / `source` / `backed_by`), or `null` / `unknown` — which means the record named no anchor, **not** that it was shown to be independent. |

### Dispositions

A closed vocabulary:

| Value | Meaning |
| --- | --- |
| `selected` | The record that stands as the answer (or, without resolution, a hit with nothing against it). |
| `replaced` | Retired — superseded, archived, retracted, or expired. |
| `conflict_side` | A visible side of a conflict nothing mechanical settles. |
| `insufficient` | The evidence around the hit does not settle it. |
| `evidence_only` | Supplied as evidence around a hit, not as an answer. |

### `lineage[]` — one origin group

| Field | Meaning |
| --- | --- |
| `origin` / `origin_kind` | The anchor the group's members cite, and how it was established. `null` / `unknown` for a record that anchors nothing. |
| `origin_revision` | The origin's revision **as this delivery supplied it** — set only when the origin is itself one of the supplied records. `null` otherwise: the origin was named, not delivered. |
| `members` | The records resting on that origin. A snapshot and a session summary citing one observation are **one group**, not two witnesses. |
| `status` | `known` or `unknown`. An `unknown` group is one record's un-established lineage, never a claim of independence. |

### Policy version

Three parts, deliberately the smallest honest set:

- **package** — the installed `palinode` version. Selection, lifecycle, visibility and
  resolution policy are code.
- **projection** — `PROJECTION_VERSION`, the version the current-text projection behind
  every delivered excerpt was produced under. It moves independently.
- **config** — an 8-hex fingerprint over the configured values that decide what is in
  scope and how far evidence may look (`scope.enabled`, `scope.prime_mode`,
  `search.exclude_status`, the `search.evidence.*` budgets). Configuration is edited
  without a release; without this the tuple would claim an unchanged policy across a
  real policy change.

Prompt versions are deliberately **not** in it: no prompt runs on the delivery path.

---

## Two views

| View | Carries | Returned by |
| --- | --- | --- |
| **public** | Every field above: refs, revisions, dispositions, lineage, coverage, scope, times. **No memory text, no titles, no excerpts, and not the caller's query.** | Every delivery surface. |
| **diagnostics** | The public view plus the request fingerprint (which includes the query prose), the surface, the resolve mode, the evaluation window and the reuse key. | Nothing. Internal — for operators reading their own store. |

A third, minimal shape — the **reference** (`bundle_id` + `evaluated_at`) — is what a
delivery that gathered no evidence returns.

---

## Surfaces

### MCP — `palinode_search`

The rendered result gains a receipt block. Without `resolve` that is **one line**
(`Receipt: <bundle_id> · evaluated <time>`); with `resolve` it is the full public view
— a line per supplied record with its revision and disposition, the lineage groups
where copies share an origin, and the coverage. No new tool parameter: an agent is
never asked whether it wants provenance for what it was just handed.

### REST — `POST /search`

`/search` returns a bare JSON array and that stays true. Set `receipt: true` on the
request and the response becomes:

```json
{
  "results": [ ... ],
  "receipt": { ... }
}
```

`results` is byte-identical to what the same request returns without the flag. The
receipt is the **public view** when `resolve` is on, and the two-field **reference**
when it is not. Omit the flag and the response is today's array, unchanged.

### REST — `POST /context/prime`

The digest response gains a top-level `receipt` (public view): every supplied ref at
its `file_sha256` revision, its disposition, the known lineage, the resolved scope and
policy, the evaluation time and the next known transition. A contested row is a
`conflict_side`, not a quiet `selected`.

No retrieval-log rows are written for a prime — a session-start injection ledger is a
separate contract, and this endpoint has never written to that log.

### REST / MCP / CLI — `resolve` (the bounded-resolution bundle)

The bundle carries its receipt in two places: `receipt_ref` (the `bundle_id`) and
`receipt` (the public view), and the rendered `text` ends with a
`Receipt: <bundle_id>` line, so the id is available to a reader that only ever sees
the injected string.

`supplied[]` covers every record the bundle **delivered**, which is more than the
records it printed in full: the sides of a conflict the budget omitted are delivered
by ref in the omission notice, and what a standing assertion replaced or rests on is
delivered as a pointer. Each one carries the disposition the bundle itself assigned —
`selected`, `replaced`, `conflict_side`, `insufficient`, `evidence_only` — rather than
a disposition re-derived from currency, which could disagree with the payload it is
the receipt for. `coverage` is the bundle's own coverage, packing reasons included.

Revisions come from the index (`index_section_sha256`) when the record is indexed and
from the whole file the evidence layer read (`file_sha256`) when it is not — the two
domains are named, never compared.

**No retrieval-log rows are written for a resolve.** The operation runs on every
prompt; it records no recall and no retrieval event, and having a receipt does not
change that. A receipt describes a delivery; it is not an event in one.

### CLI

`palinode search` and `palinode prime` print the bundle id in text mode. `--format
json` output is unchanged (the results array / the digest object), so existing scripts
keep parsing what they parsed.

---

## Where receipts are recorded

On the retrieval-event log Palinode already writes — `.audit/retrievals.jsonl` — not a
parallel ledger. Each row it wrote before now additionally carries `bundle_id`,
`policy_version`, `scope`, `revision`, `revision_basis`, `disposition`,
`lineage_group`, `coverage` and `next_transition`. Rows from one delivery join on
`bundle_id`.

- **Additive, no migration.** Every field is optional; a line written before receipts
  existed reads back exactly as it did. The log is append-only, so re-running against
  an existing log is idempotent.
- **No prose.** Refs, hashes and dispositions are written; memory content never is.
- **Same regime.** The log keeps the visibility and retention it already had
  (telemetry, excluded from semantic recall).
- **The same hit set.** Records supplied only as *evidence* around a hit ride the
  receipt in the response, not this log, so recall statistics keep counting the events
  they always counted.

`palinode trace <file>` reads it back: `recalled` now reports the deliveries a file was
supplied in (`bundles`), the dispositions it was supplied under, and the distinct
source revisions it was supplied at.

---

## The cross-request reuse contract

Palinode caches **per request** today. Nothing caches across requests, and this
document does not introduce one. What it does introduce is the eligibility contract a
future cross-request cache must satisfy — recorded next to the data that satisfies it,
and pinned as `receipt.reuse_key(...)` so the eventual cache consumes a derivation that
was reviewed here.

A previously delivered bundle may be reused only if **all five** still hold at the
moment of the new delivery:

1. **Server-resolved caller access.** The scope chain the *server* resolves for the new
   caller equals the one on the receipt.
2. **Query scope.** The normalized request (query, filters, limits, tiering, resolve
   mode) is identical. Telemetry-only fields such as `session_id` are excluded — they
   change nothing about what is selected.
3. **Policy version.** Unchanged, including the config fingerprint. Widening an
   evidence budget is a policy change.
4. **Source revisions.** Every `(ref, revision)` pair still matches the store. One
   changed file invalidates the bundle. An `unknown` revision can never be shown to
   still match, so a bundle carrying one is not reusable.
5. **Time-sensitive applicability.** The delivery still falls inside the same temporal
   window — the interval bounded by the nearest known transition behind and ahead of
   the evaluation clock.

Point 5 is the one that is easy to get wrong: **an unchanged file is not evidence of an
unchanged answer.** A record with `expires_at` at noon is current at 11:59 and expired
at 12:01 with nothing written in between, so the key differs across that boundary even
though every revision is identical.

Equal keys are a **necessary** condition, not a sufficient one. A cache must still
re-check caller access and applicable time at delivery, because both can change without
any request or file changing. Claim-validity transitions beyond `expires_at` and a
declared future `date` are not modelled here; richer temporal semantics are separate
work.

### The one cache that applies it today

Support checks (`palinode.core.revalidation.SupportCache`) are computed **per
request** and reused inside it only under all five conditions above, applied to one
record's check rather than to a bundle:

| Condition | How the support cache tests it |
| --- | --- |
| Caller access | The scope chain the server resolved must equal the one the entry was stored under. |
| Query scope | The record's own ref and declared backing policy — a support check is not a query. |
| Policy version | The same `PolicyVersion` string a receipt records. |
| Source revisions | Every `(ref, revision)` the walk read is re-read through the caller's reader and must still match. An `unknown` revision can never be shown to match, so an entry carrying one is never reused. |
| Applicable time | The transition pair bracketing the clock, derived from the `expires_at` / declared `date` values the walk actually saw, must be the same one. |

The fifth is the one that bites without any file changing: a source with `expires_at`
at noon supports its dependent at 11:59 and has withdrawn that support at 12:01, so a
result warmed before the boundary is not served after it. The expired grant is not
deleted — it stays readable as history; what changed is whether it may be presented as
currently supporting anything.

### Revalidation receipts are not delivery receipts

Both name a revision, and they answer different questions. A *delivery* receipt records
what was supplied and at which revision; it is written by the server, per delivery,
into the retrieval log. A *revalidation* receipt (`revalidated:` frontmatter) is
authored on a record and records that its author checked a named source at a named
revision — the explicit form of the convention that re-saving a dependent meant it had
been re-verified. They share the `file_sha256` revision basis, and nothing else: see
[EXECUTOR-SPEC](EXECUTOR-SPEC.md#revalidated).

# Upgrading to v0.20 — *memory that knows what still applies*

v0.20 changes what a recall *returns*, not what a memory *is*. Your markdown is
untouched: no file is rewritten, no frontmatter field is required, nothing is
deleted. What changes is that Palinode now distinguishes **what still applies**
from **what it used to say**, and says which it is handing you.

This page is the operator's version of that. It covers what changed, the
migration (one command), what rollback does and does not undo, how to tell which
version and which policy a delivery ran under, and what this release explicitly
does not do.

The measured behaviour behind the claims here is in
[RELEASE-ACCEPTANCE-v0.20.md](RELEASE-ACCEPTANCE-v0.20.md), including the parts
that did not pass.

**TL;DR for a running store:**

```bash
# 1. upgrade, 2. restart services, then:
palinode doctor          # projection_current will report chunks behind
palinode reindex         # one pass; ~13k chunks in under five minutes
palinode prompt sync     # refreshes compaction.md to v5 — no --force needed
palinode doctor          # projection_current and prompts_current green
```

---

## 1. What changed, in plain language

### Current versus history

The consolidation executor has always retired a fact *in place*: it strikes the
old wording through, stamps it, and leaves the successor beside it, so the file
shows its own history. Until now the index was built from that raw text, so the
struck wording still ranked in keyword and vector search and still rendered in
snippets — beside its own replacement.

Now a record is classified by one shared lifecycle rule wherever it is used:
**current** (declared live), **retired**, or **unmarked** (a legacy memory with
no `status:`, which stays usable and stays unlabelled). Retirement is read from
what you actually declared — `status` / `lifecycle` of archived, deprecated,
superseded or retracted; a non-empty `superseded_by`; an `expires_at` in the
past — plus one path rule: **anything under `archive/` is retired by location**,
even if its frontmatter still says `status: active`, because the weekly pass and
`palinode archive` move a note there without rewriting a byte of it.

Nothing about that is a deletion. `archive/` is still indexed, still searchable,
and `palinode read` on an archived file still works. What changed is the label
on the answer.

### The index is derived from projected current text

The indexer now applies a **versioned, pure projection** to each section before
FTS5 and the embedder see it: whole-line supersession and retraction tombstones
are removed, and a mention-level retraction strike removes just the struck span,
keeping the sentences around it. Ordinary markdown strikethrough, malformed
markers, fenced code and inline-code examples are content and stay.

Two hash domains now live side by side and are **never** compared to each other:

| Column | Hashes | Used by |
|---|---|---|
| `content_hash` | the raw section on disk | `freshness`, `palinode blame`, quote-anchor verification |
| `projected_hash` + `projection_version` | the derived current text | the reconcile planner, `palinode doctor` → `projection_current` |

The retired wording is still in the raw file, in the `-history.md` sidecar, in
`git log`, and reachable through `palinode blame` and `palinode read`. It is
simply no longer what search is built from.

### Three answers that used to be one badge

A search result now carries three separate fields, because they answer three
different questions and one of them used to be rendered as if it answered all
three:

| Field | Question | Values |
|---|---|---|
| `freshness` | Does the index agree with the file on disk? | `valid` · `stale` · `unknown` |
| `span_integrity` | Are the record's cited quote anchors still present verbatim in the sources they cite? | `unanchored` · `ok` · `anchor_tampered` · `source_drifted` · `source_missing` |
| `currency` | Does this assertion still apply? | `current` · `retired` · `contested` · `unmarked`, with `currency_reason` naming the deciding signal |

`freshness` is unchanged in name, values and meaning — every existing consumer
keeps working. The old `✓ valid` rendering, which read as *this is true*, is
gone: index/source agreement is now labelled `[index matches source]`, and no
"verified" or "current" wording sits next to it.

### Evidence, and a resolution over it

`search` takes an opt-in `resolve` on every surface (`none` | `linked` | `full`;
default `none`, byte-identical to before).

- `linked` follows `superseded_by`, `contradicts` and `backed_by` forward **and
  in reverse**, so a retired hit surfaces its visible successor, and a correction
  ranked below the cut still rides on the hit it corrects.
- `full` adds bounded unlinked discovery: entity and citation lookups first, then
  a keyword query on the hit's identifiers and a neighbour query on its own
  stored vector.

Every expanded record passes the same path guard and visibility gate the hit
passed. Each hit gains an `evidence` block with explicit `coverage`: `complete`,
or `partial` with a reason from a closed vocabulary that **never names a hidden
record**.

On top of that evidence sits a `resolution` decided once, server-side, so the
MCP, CLI and REST readings of one hit cannot disagree. Three outcomes, and they
stay distinguishable: `supported_current`, `unresolved_conflict` (every visible
side kept, no winner), `insufficient_evidence` (unknown as an explicit result).

**Only mechanically explicit changes resolve.** A `superseded_by` chain ending at
a standing successor, a declared retirement, a past `expires_at`. A `contradicts`
link, a newer date, an `epistemic: fact` label, a similarity score, a rank
position and a recall count can make a conflict *visible* — none of them retires
a record or elects a winner. A newer proposal does not replace an accepted
decision; a decision that disagrees with an observation is reported as a
policy/implementation mismatch; production and staging observations coexist.

### `resolve`, and the per-turn hook that consumes it

`resolve` is one canonical operation on all four surfaces — `palinode resolve`,
`palinode_resolve`, `POST /resolve`, and the core plugin's per-turn path. It asks
what memory holds *right now*, for a question or for one record, and returns a
qualified bundle: what stands (with currency, index freshness and exact source
revision), what replaced what, conflicts with both sides intact, explicit
unknowns, coverage, source revisions and a receipt.

No model runs on that path. With no embedder reachable it seeds from keyword
search alone and says `degraded:keyword_only` in coverage.

**The per-turn hook** `palinode init` installs now routes through it, under a
**250 ms deadline** (`PALINODE_HOOK_RESOLVE_DEADLINE`, milliseconds). This is a
latency budget spent on every prompt, not a failure timeout. Past it, the turn
falls back to the previous search payload **byte for byte, prefixed with a
marker**:

```
resolution unavailable (deadline) — the memories below are unresolved search hits…
```

It never falls back silently. An unchecked hit presented with the authority of a
resolved answer is the failure the whole operation exists to prevent. Set
`PALINODE_HOOK_RESOLVE=0` to restore the pre-resolution channel exactly.

Session-start priming is **deliberately unchanged**: a startup digest is
orientation, not an answer to a question nobody has asked.

### Delivery receipts

Every delivery now produces a receipt: a bundle id, the policy version it was
served under, the server-resolved caller scope, the clock it was evaluated on,
the next known temporal transition among the delivered records, and — per record
— its ref, its exact source revision named by hash basis, currency, freshness,
span integrity, disposition and origin. Known lineage is **grouped**: a snapshot
and a session summary citing one observation's anchor are one group at that
observation's revision, not two independent witnesses.

A receipt records what was **supplied**, never what influenced anything. The
public view carries refs, hashes and dispositions — never memory text and never
the caller's query. `palinode trace` now reports which deliveries a file was
supplied in, at which revisions, under which disposition.

Full field reference: [DELIVERY-RECEIPTS.md](DELIVERY-RECEIPTS.md).

### Injection budgets, and a lint for core memories that outgrew a gist

Injected payloads are packed by one shared packer over indivisible **units**. It
degrades in one direction only: a plain row may be demoted to its gist and
pointer, but a unit is never split, a kept unit never loses a qualifier, and a
conflict group is kept whole or replaced by one explicit
`⚠ N conflicts omitted for budget — see <refs>` stub that keeps every source
pointer. A budget can cost you detail; it can never make a contested claim look
settled. Every omission is visible in the response and in a WARNING log line.

New config on the existing `context` block, budgeted separately because they are
paid at different rates:

| Key | Default | Applies to |
|---|---|---|
| `context.injection_max_chars` | 6000 | the once-a-session startup payload |
| `context.injection_max_tokens` | 1500 | same |
| `context.recall_max_chars` | 3000 | the per-message recall block |
| `context.recall_max_tokens` | 750 | same |
| `context.core_gist_max_chars` | 1500 | the `oversized_core` lint threshold |

Tokens are estimated at 4 chars/token and named as an estimate. Setting **both**
members of a pair to `0` restores the previous behaviour byte for byte.

`palinode lint` gains `oversized_core`: any `core: true` memory whose body
exceeds `context.core_gist_max_chars` is reported with its size, the limit and
the remediation. Core memories are injected at every session start, so their size
is spent whether or not anyone wanted it — *core = gist + pointer; move the detail
to the full file*, where recall fetches it on demand. `0` disables the check.

### Consolidation: when it runs, and what it retires

Three behavioural fixes that an operator will notice on the schedule, not in the
API:

- **The activity gate stamps the pass's *start* time.** It used to stamp
  completion, so a daily cron with a 24 h minimum always arrived a minute short
  the next day and deferred — the nightly silently became every other night on
  any store where the pass did real work. The elapsed floor now tolerates one
  hour of cron jitter (the ceiling does not), and a `partial` pass no longer
  resets the clock, so a failed group is retried at the next tick.
- **The weekly pass no longer retires notes it never consolidated.** A note with
  no `project/` reference stays in place; the current UTC day's daily note is
  never moved; and a group that reached the model and came back with nothing is
  a *decision*, so its notes retire — but the group is named in the log and in
  the run summary rather than passing unrecorded.
- **Dated status log lines are retired by age, deterministically.** A
  `projects/<slug>-status.md` fed by session-end gains one dated line per
  session; at several hundred of them the weekly pass could not finish, because
  an honest proposal naming each line individually ran past any token cap — so
  the pass failed and *nothing* was retired. The weekly pass now retires them
  itself, before the prompt is built, as ordinary `ARCHIVE` operations through
  the same executor: every line kept verbatim in the `-history.md` sibling, its
  own commit, one `## Consolidation Log` line naming the range, and an
  `age_retired` count in the run summary.

  | Key | Default | Meaning |
  |---|---|---|
  | `consolidation.status_log_retention_days` | 90 | A dated log line older than this is archived into the `-history.md` sibling. `0` disables the sweep. |

  No model is involved: a date is arithmetic, not judgement. Identity and profile
  documents are never swept — the sweep asks the same classifier the executor's
  retirement guard reads, so a `superseded-only` document produces no operations
  at all.

  The model can express the same thing in one operation: **`ARCHIVE_BEFORE`**
  retires every dated log line strictly older than a date it names, so a
  proposal's size follows the number of *reasons* rather than the number of
  facts. Weekly `allowed_ops` only, deliberately not nightly. Contract in
  [EXECUTOR-SPEC.md](EXECUTOR-SPEC.md).

### RETRACT must cite what falsified the record

`RETRACT` now carries **`falsified_by`**, naming the memory or fact *in context*
that falsifies the retracted one — the retraction analogue of `superseded_by`.
Two independent deterministic checks, neither of which reads prose:

- **Citation validation.** A `RETRACT` whose citation does not resolve to something that
  was in the prompt is downgraded to `PROPOSE_CONTRADICTS` and counted as
  `retract_downgraded`; when there is no note ref to link to, it is dropped under
  the same count. It is never applied.
- **Document protection.** On an identity or profile document (`superseded-only`), a
  `RETRACT` without a non-empty `falsified_by` is rejected as
  `protected_rejected`: reason logged, content unchanged, no history written.

A citation is *necessary evidence, not proof*. The guard checks that a reference
resolves, and nothing more. Relatedly, a `RETRACT`, `ARCHIVE` or `SUPERSEDE`
aimed at a bullet under the auto-footer is rejected and counted as
`footer_op_rejected` — footer wikilinks are navigation, not claims.

### Prompt sync compares bodies, and `specs/` is not memory

**`palinode prompt sync` no longer needs `--force`.** A store writes its own
prompt frontmatter (the cross-reference updater and the description backfill both
did), and the whole-file hash `sync` compared therefore matched nothing on any
store whose watcher had run — reporting all nine prompts as `kept-edited`, with
`--force` (the *discard my edits* hatch) as the recommended remedy. Both sides
are now hashed **frontmatter-stripped**: an added, reordered or requoted
frontmatter field leaves a prompt refreshable; one changed line of prompt text is
still your edit and is still left alone.

**The store's own prompts are no longer treated as memory.** A provisioned store
keeps its editable prompts at `specs/prompts/*.md`, and five walks tested only a
path's *first* segment against a set naming a legacy top-level `prompts`
directory — so prompt files were listed by `GET /list` (and therefore injected by
the session-start hook), browsable in the provenance UI, in scope for the
advisory project review, candidates for the digest, reachable as `backed_by`
dependents, and indexed for search. One shared predicate now answers "is this a
memory file?" for every surface and for the indexer. A `palinode reindex` drops
the prompt chunks an earlier index left behind.

**Compaction prompt v5** adds `ARCHIVE_BEFORE` to the operations table and tells
the model to prefer it for a run of stale dated status lines. Rules 5, 8 and 9
keep their v4 wording and the numbering is unchanged. The v4 body hash stays in
the shipped-hash catalogue, so a store still on v4 is recognised as pristine and
is *refreshed*, not treated as operator-edited.

---

## 2. Migration

The upgrade adds two nullable columns to `chunks` and re-derives every chunk's
search text. Nothing on disk changes.

```bash
# 1 — upgrade the package (however you install it)
pip install --upgrade palinode

# 2 — restart the services so the schema migration runs
systemctl --user restart palinode-api palinode-watcher
#   (or your equivalent: launchd, Docker Compose, a system-manager unit)

# 3 — see the work
palinode doctor
#   projection_current: warn — N chunk(s) on an older projection version

# 4 — do it in one pass
palinode reindex

# 5 — take the new prompts
palinode prompt sync

# 6 — confirm
palinode doctor
#   projection_current: pass
#   prompts_current:    info — every versioned prompt matches
```

**Cost.** `reindex` re-derives text and re-embeds only what changed. A chunk
whose stored text already *equals* its projection — most of a store, because most
sections were never retired in place — is stamped in place **without
re-embedding**. On a dogfood store of roughly 12,800 chunks the whole migration
completed in under five minutes.

**If the embedder is cold**, rows are re-derived keyword-searchable first and
re-embedded on the next warm pass. That is the same deferral the save path
already reports, and it is observable: the reconcile planner re-embeds on exactly
that signal.

### The incremental alternative — do nothing

You do not have to run `reindex`. Reconcile migrates each chunk the next time its
file is touched: a save, a watcher event, a consolidation write. The store
converges on its own, and `projection_current` is a progress meter while it does —
the count falls as files are visited.

The trade-off is simply time: until a chunk is re-derived, its retired wording is
still in the keyword and vector index, so an old assertion can rank beside its
successor. On a quiet store that can take weeks. `reindex` is the same
destination, reached at once.

Both paths converge on the same rows. That is asserted, not assumed —
`tests/test_migration_v020.py::test_an_upgraded_store_reindexes_to_exactly_the_clean_store`
indexes a store the pre-projection way, reindexes it, and compares every row
against a fresh index of the same files.

---

## 3. Rollback

The migration preserves source files and leaves nullable index columns that
older writers can omit. The checks below simulate legacy-shaped index rows;
they do not certify an end-to-end downgrade with an older installed release.

**What stays behind.** The two columns the upgrade added (`projected_hash`,
`projection_version`) remain on the `chunks` table. Both are nullable with no
default, and older code never names them in a write — so the old indexer writes
rows beside the new ones without error, and simply ignores the stamps. There is
no down-migration to run.

**What it means for search.** Old code derives the index from raw text again, so
retired wording starts ranking again as rows are rewritten. That is the
pre-v0.20 behaviour you are choosing by downgrading.

**What an index rebuild does not undo: recorded retirement.** Retirement lives in your markdown
and in git — a `status:`, a `superseded_by:`, an `expires_at:`, a path under
`archive/` — never in the index. The index is derived state; deleting it and
rebuilding it from the files alone under v0.20 re-derives the same verdicts.
Older releases do not implement all of v0.20's lifecycle rules and may present
those files differently.

That is the claim most worth not taking on trust, so it has a test:
`tests/test_migration_v020.py::test_rollback_does_not_revive_expired_authority_or_retired_conclusions`
throws the database away, rebuilds it the pre-upgrade way, and asserts that the
archived decision, the note retired by location and the lapsed grant are all
still retired — with the same reasons — and that a record retired by location is
served by the index and labelled `retired` anyway. These verdicts are evaluated
by v0.20 code, not an older installed binary.

**What a downgrade *does* lose**, until you upgrade again: the `resolve`
operation and the per-turn resolution channel, the `evidence` / `resolution`
blocks, delivery receipts, the retirement-by-location rule, the age-retention
sweep and `ARCHIVE_BEFORE`, and the body-hash prompt comparison. A store whose
prompts are on v5 and whose code is on v4 will report the prompt as ahead; that
is accurate, not a fault.

---

## 4. Knowing which version, and which policy, you are on

| Surface | Tells you |
|---|---|
| `palinode doctor` → `projection_current` | How many indexed chunks are still on an older projection version (or none). The migration progress meter. |
| `palinode doctor` → `prompts_current` | Whether the store's `specs/prompts/*.md` lag the packaged ones, naming each file with both versions. |
| `palinode doctor` → `store_tree_clean` | Modified and untracked files in the memory dir; warns above ten. The complement to `git_remote_health`, which counts unpushed commits and says nothing about uncommitted files. |
| `palinode doctor` → `watcher_alive` | Probes the **system** manager first and `--user` second, accepts either shipped unit name plus the installer's override, and names the manager and unit that answered — so a host running the watcher as a system unit is no longer told to install one it already has. |
| `palinode status` / `GET /status` | Package version, index stats, reindex state. |
| `receipt.policy_version` | The policy a *specific delivery* ran under. |

`policy_version` is three parts, deliberately the smallest honest set: the
installed **package** version (selection, lifecycle, visibility and resolution
policy are code); **`PROJECTION_VERSION`**, which moves independently; and an
8-hex **config** fingerprint over the values that decide what is in scope and how
far evidence may look. Configuration is edited without a release — without the
fingerprint the tuple would claim an unchanged policy across a real policy
change. Prompt versions are deliberately *not* in it: no prompt runs on the
delivery path.

---

## 5. Cache invalidation

**There is nothing to invalidate.** Palinode caches **per request**. No bundle,
no resolution and no evidence block is reused across requests, and v0.20 does not
add a cache.

What v0.20 *does* add is the eligibility contract any future cross-request cache
must satisfy, recorded next to the data that satisfies it and pinned as
`receipt.reuse_key(...)`. Five conditions, all of which must still hold at the
moment of a new delivery: server-resolved caller access, normalized query scope,
policy version (config fingerprint included), every `(ref, revision)` pair, and
time-sensitive applicability.

The fifth is the one that is easy to get wrong: **an unchanged file is not
evidence of an unchanged answer.** A record with `expires_at` at noon is current
at 11:59 and expired at 12:01 with nothing written in between.

Equal keys are a *necessary* condition, not a sufficient one — caller access and
applicable time can both change without any request or file changing. The one
cache that exists today, the per-request support cache, applies all five to a
single record's check. Full contract:
[DELIVERY-RECEIPTS.md § The cross-request reuse contract](DELIVERY-RECEIPTS.md#the-cross-request-reuse-contract).

---

## 6. Compatibility

**Every new response field is additive.** Nothing was renamed and nothing was
removed.

New keys you may start seeing:

| Field | Where |
|---|---|
| `currency`, `currency_reason`, `span_integrity` | every search result |
| `evidence`, `resolution` | each hit, when `resolve` is not `none` |
| `receipt` | `/search` when the request sets `receipt: true`; every `resolve` bundle; `/context/prime` |
| `projected_hash`, `projection_version` | `chunks` rows (internal; surfaced through `projection_current`) |
| `chunks_reprojected`, `chunks_stamped` | the indexer's per-file result |
| `age_retired`, `archived_by_range` | consolidation run summaries and executor stats |
| `retract_downgraded`, `footer_op_rejected` | consolidation run summaries (dry run included) |
| `translation_skipped` | write-time check `applied_stats` |
| `projects_no_ops` / `groups_no_ops`, `projects_all_ops_filtered` / `groups_all_ops_filtered`, `notes_no_project`, `notes_today_kept` | weekly and nightly run summaries |
| `backing_policy`, `revalidated` | optional frontmatter, both absence-is-neutral |

Unchanged on purpose:

- **`freshness`** keeps its name, its three values and its exact meaning. Every
  existing consumer — the write-time stale check, the JSON output — is untouched.
  The new currency field sits beside it; it does not replace it.
- **`content_hash`** still hashes the raw section on disk. The projected hash is
  a separate column in a separate domain and the two are never compared.
- **`POST /search`** still returns a bare results array. The receipt is returned
  *beside* it, only when asked for.
- **The MCP tool schema** grew by one tool (`palinode_resolve`) and one parameter
  (`resolve` on `palinode_search`, enum `none` | `linked` | `full`) relative to
  v0.19.1. No existing tool changed shape.
- **The retrieval log** (`.audit/retrievals.jsonl`) carries the receipt fields on
  the rows it already wrote — additively, with no migration.
- **The shipped-hash catalogue** now declares `schema: 2` with a
  `hash_domain: "body"` marker. A palinode that compares whole-file hashes
  refuses a body-domain manifest (and vice versa) with a message naming both,
  rather than matching nothing and calling every store prompt edited.

Two behaviour changes that are *not* opt-in, and are worth knowing before you
upgrade a store with automation reading its output:

1. A note under `archive/` is now `retired` even when its frontmatter says
   `status: active`. If you have tooling that treated `archive/` as ordinary
   storage, it will now see those records labelled retired.
2. Retired wording no longer matches in search once a chunk is re-derived. A
   query that used to find a superseded phrase will stop finding it. The phrase
   is still in the file, the sidecar and `git log`.

---

## 7. What v0.20 does not do

Stated here so nothing has to be inferred from an absent parameter.

**Deferred to v0.22.0, deliberately, with no half-implemented surface:**

- **Temporal assertions and as-of queries.** Bi-temporal validity windows —
  asking what memory held *as of* a past date, or when a claim becomes effective
  — are not in this release. **No parameter is advertised for it.** The
  acceptance corpus keeps its as-of scenario family, replays it and records its
  questions, and scores none of them, under an explicit deferred disposition
  rather than quietly dropping the cases.
- **Actionability.** A separate axis for "is this something to act on" is not in
  this release.
- **A queryable injection ledger.** Receipts are written to the existing
  retrieval log and are readable through `palinode trace`. A queryable ledger
  with its own tables, session-history queries and reverse lookup is separate
  work.

**Decided and shipped this release** (recorded so the decisions are findable):

- On an identity or profile document, `RETRACT` is accepted **only** with
  `falsified_by`. An addressable evidence reference alone is not described as
  proof of falsity — it is the citation requirement, nothing more.
- An explicit `retirement_policy: age-eligible` on a `decisions/` document
  overrides the lint proposer's conservatism about that directory: a declared
  decision is nominated for a staleness `ARCHIVE` like any other age-eligible
  document. Undeclared decisions are still skipped, and the skip reason now names
  the declaration as the way to opt in.

**Known limitations, measured rather than assumed.** Each was found by the
release acceptance run and is stated in full, with its disposition, in
[RELEASE-ACCEPTANCE-v0.20.md](RELEASE-ACCEPTANCE-v0.20.md):

- **Unlinked corrections are shown but never decide anything.** The rendered
  bundle prints each discovered record under the assertion it was found for and
  each standing assertion's linked `support:` refs, so a reader sees them. The
  policy does not contest a seed on an unlinked record; discovery is advisory.
  One cost to know: a record discovered for several assertions is printed under
  each, which can roughly double a payload — the budget still bounds it.
- **Each contested side carries its own qualifiers** (`contradicts:`,
  `stale backing:`, `epistemic:`) inline, with the labels the session digest
  uses.
- **A future-effective replacement does not keep the old value current until the
  transition.** The shipped policy retires the predecessor when the successor is
  written and then declines, because the successor is not yet effective. That is
  a correct refusal, not the required answer. **Fixed in v0.20.1** (2026-09-14):
  a scheduled replacement now keeps the predecessor current until the successor's
  effective date. The record's own `currency` still reads `retired` — `search`
  and `resolve` must never disagree about the same file on the same clock — so
  the transition is carried in the stamp, the reason and the qualifier instead.
- **Under index lag, resolution delivers the indexed wording.** The delivery is
  honest — it is stamped `index stale` — but a reader that follows the markers
  still takes the stale value. **Fixed in v0.20.1** (2026-09-14): the seed reads
  through the same live, projected load as every other layer, so the excerpt is
  the file's wording and the receipt names the revision it was read at.
- **Nothing is claimed about semantic paraphrase recall.** The acceptance run
  seeded retrieval with a deterministic lexical stand-in embedder, identically
  for every arm, so paraphrase recall is understated and is not reported.

---

## Related pages

- [HOW-MEMORY-WORKS.md](HOW-MEMORY-WORKS.md) — recall routing, the projection, receipts
- [DELIVERY-RECEIPTS.md](DELIVERY-RECEIPTS.md) — every receipt field, and the reuse contract
- [EXECUTOR-SPEC.md](EXECUTOR-SPEC.md) — `ARCHIVE_BEFORE`, `falsified_by`, `revalidated`, the retirement guard
- [DOCTOR.md](DOCTOR.md) — the full check catalogue
- [OPERATIONS.md](OPERATIONS.md) — upgrade, reindex, status log retention, recovery
- [RELEASE-ACCEPTANCE-v0.20.md](RELEASE-ACCEPTANCE-v0.20.md) — what was measured, and what was not

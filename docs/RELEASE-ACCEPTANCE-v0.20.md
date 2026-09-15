# v0.20 release acceptance — what was measured, and what was not

This is the evidence page for [UPGRADING-v0.20.md](UPGRADING-v0.20.md). It
records what the release was tested against, what the numbers were, what the
release checks returned, and — at the same level of detail — what the run showed
did **not** work.

It is deliberately separate from the changelog. A changelog says what changed; an
acceptance record says what was *demonstrated*, under what method, with what
uncertainty, and where the method runs out.

Two structural notes before the numbers:

- **Two pages, and why.** The full disposition table — every milestone item with
  its implementing change, the recorded rulings, and the raw acceptance report —
  cites private tracker numbers and internal paths and is kept beside them in the
  development repository. This page carries everything that is a claim about the
  software.
- **Reproducing it.** The acceptance corpus and harness ship in
  `bench/current_state/`. `python -m bench.current_state --help` runs it. The
  headline numbers below come from a run on corpus v1 (seed 20260912), Python
  3.12, with the deterministic embedder.

---

## 1. What the measurement is, and what it cannot be

Four stages are scored **separately** — detection, disposition, presentation,
behaviour — so a wrong answer is attributed rather than merely counted. Every
oracle is deterministic: the expected disposition, the expected value and the
retired values follow mechanically from an episode's events. That is why no
semantic judge is used, and why no judge-coverage gate is needed for one. The
gate that *is* enforced is per-family scenario coverage.

Three limits, stated up front rather than in a footnote:

1. **The reader is a rule-following program, not a model.** It reports what a
   reader that honours the markers would conclude. A model-backed reader is
   available behind a flag; its status is in §7.
2. **Seed retrieval uses a deterministic hashed bag-of-words embedder**, so
   similarity is lexical overlap. Paraphrase recall at the seed stage is
   understated relative to a real embedding host — equally for every arm, since
   every arm seeds identically.
3. **The bounded-evidence arm is rendered by the harness** in the bundle's own
   section grammar, so two of the arms differ by grouping, budget and packing
   rather than by whether markers exist at all.

### Scenario-family coverage (a gate, not a report)

44 authored episodes across 21 families, each with a positive and a negative
control; the run **fails** if either disappears. 88 episodes after the held-out
split. 20 families are scored; the claim-validity / as-of family is present,
replays, records its questions, and is scored by nothing — carrying its deferred
disposition explicitly instead of being dropped.

---

## 2. Headline results

Held-out split — unseen projects and paraphrases. 95% bootstrap confidence
intervals over episodes.

| Arm | Detection | Disposition | Presentation | Behaviour | Stale-current | False resolution | Appropriate abstention | Unaffected-fact retention |
|---|---|---|---|---|---|---|---|---|
| `baseline` (plain top-k over raw text) | 95% [88–100] | 73% [60–86] | 55% [40–69] | 65% [52–79] | 19% [8–30] | 26% [13–39] | 8% [0–23] | 95% [85–100] |
| `projection` (same hits, current-text projection) | 95% [88–100] | 75% [62–87] | 60% [45–74] | 69% [56–82] | 17% [7–29] | 25% [13–38] | 15% [0–38] | 95% [85–100] |
| `bounded_evidence` (evidence + resolution policy) | 98% [93–100] | 95% [88–100] | 95% [88–100] | 93% [83–100] | 2% [0–7] | 2% [0–7] | 92% [77–100] | 100% [100–100] |
| `bundle` (the whole `resolve` bundle) | 98% [93–100] | 95% [88–100] | 95% [88–100] | 93% [83–100] | 2% [0–7] | 2% [0–7] | 92% [77–100] | 100% [100–100] |
| `matched_budget` (larger top-k control) | 95% [88–100] | 73% [60–86] | 55% [40–69] | 65% [52–79] | 19% [8–30] | 26% [13–39] | 8% [0–23] | 95% [85–100] |

On the **mechanical families** — the ones whose right answer follows from the
events with no judgement at all:

| Arm | Disposition | Behaviour | Stale-current |
|---|---|---|---|
| `baseline` | 75% [59–89] | 57% [39–73] | 43% [27–61] |
| `projection` | 79% [61–93] | 68% [50–84] | 32% [16–50] |
| `bounded_evidence` | 93% [82–100] | 93% [82–100] | 0% [0–0] |
| `bundle` | 93% [82–100] | 93% [82–100] | 0% [0–0] |
| `matched_budget` | 75% [59–89] | 57% [39–73] | 43% [27–61] |

**Before-state is the denominator that keeps this honest.** It is the same
question asked *before* the transition. An arm that could not answer it then gets
no credit for abstaining afterwards, so "know nothing, abstain from everything"
cannot pass a withdrawal case. 28/28 before-state checks passed on every arm.

`bounded_evidence` and `bundle` score identically here, and that is worth saying
plainly rather than presenting as two independent wins: **the accuracy comes from
the evidence layer and the resolution policy.** What the bundle adds — grouping,
the output budget, conflict-preserving packing, the delivery receipt — is
measured by the token column, the tight-budget family and the release fixtures,
not by these rates.

### Cost

| Arm | Injected tokens (mean / p95) | File reads (mean) | Latency p50 / p95 (ms) |
|---|---|---|---|
| `baseline` | 67.6 / 123 | 1.6 | 7.12 / 8.5 |
| `projection` | 66.2 / 122 | 0.0 | 6.98 / 8.29 |
| `bounded_evidence` | 120.7 / 223 | 1.9 | 13.08 / 19.48 |
| `bundle` | 125.8 / 220 | 1.9 | 14.77 / 22.08 |
| `matched_budget` | 69.1 / 157 | 1.6 | 5.31 / 6.6 |

Tokens are counted with the same estimator the shipping packer budgets with.

---

## 3. Controls

### The matched-budget control — is it just reading more?

The control climbs top-k until its payload costs what the bundle's costs: 69.1 vs
125.8 mean injected tokens, reaching the bundle's total on 3 of 100 questions.
Disposition 73% [63–81] against the bundle's 95% [90–99]; behaviour 65% [55–74]
against 93% [87–98].

Where the ladder cannot reach the target it is because the store holds fewer
matching records than the rung asks for. That is reported as *not reached* rather
than as a match. **On the questions where it does match, the gap stands.**
Reading more material is not what the bundle does.

### Raw-evidence retention versus consolidation

Matched episodes (4): the same events, the same reader, the same questions. One
arm never runs a consolidation pass; the other runs every pass the episode
declares.

| Arm | Condition | Disposition | Behaviour | Stale-current | Tokens |
|---|---|---|---|---|---:|
| `baseline` | consolidated | 100% [100–100] | 25% [0–50] | 75% [50–100] | 125.0 |
| `bundle` | consolidated | 100% [100–100] | 100% [100–100] | 0% [0–0] | 188.8 |
| `baseline` | raw retention | 100% [100–100] | 25% [0–50] | 75% [50–100] | 95.0 |
| `bundle` | raw retention | 100% [100–100] | 25% [0–50] | 75% [50–100] | 162.3 |

This is the control that matters most for the release's central claim.
Consolidation is not tidying: **the rewriting the executor does is the mechanism
that makes the current state answerable at all.** Keep the raw evidence and skip
the passes and the bundle scores what the baseline scores.

### Repeated consolidation and restore

Two episodes run three consolidation cycles plus a delayed import and an explicit
restore, and are checked for silent resurrection of retired facts. Result below,
with the release fixtures.

---

## 4. Mechanical invariants

| Invariant | Checks | Violations |
|---|---:|---:|
| `no_read_triggered_mutation` | 640 | 0 |
| `no_retired_as_current` | 36 | 0 |
| `no_unauthorized_disclosure` | 10 | 0 |
| `rebuild_replay_equivalent` | 88 | 0 |
| **Total** | **774** | **0** |

No read arm moved `HEAD` or dirtied the working tree. No delivery carried a
hidden record's title or content. No mechanical case presented a retired value as
current. Every store rebuilt from its files alone selected the same records at
the same revisions, and every receipt named those revisions.

Two episodes are *expected* to diverge on rebuild, and are declared as such: the
index-lag episodes deliberately leave the index behind the file, so a rebuild
that reproduced the stale derived state would mean the index was not derived
state at all.

---

## 5. The end-to-end gate: A → B → consolidate → fresh session → agent choice

The release gate is not an endpoint test. It is: record A, explicitly replace it
with B, consolidate, start a fresh session, run **the actual shipped hook**, look
at what was injected, and check that a reader picks B with correct evidence.

Two layers run it.

### Through the shipped hook against a live server

`tests/test_resolve_hook_live.py` runs the real `UserPromptSubmit` script
(`palinode.cli.init.USER_PROMPT_SUBMIT_HOOK_SCRIPT`) with real `curl` and `jq`,
against a genuine uvicorn serving the real FastAPI app over a real SQLite store
under `tmp_path`, with real markdown retired through the real archive path. No
stub anywhere on the path.

| Test | What it pins |
|---|---|
| `test_a_fresh_session_receives_the_successor` | The successor ref and its wording are injected; the replaced wording is absent; `Coverage:` travels with the answer; a scripted consumer picks the successor. |
| `test_an_unresolved_conflict_reaches_the_session_with_both_sides` | Both refs and both values are injected, with "no winner"; the scripted consumer picks **nothing**, because a contested question has no current line. |
| `test_a_tight_budget_keeps_the_conflict_whole_or_names_it` | Across five budgets where the cap bites: the conflict arrives whole, or is named with `budget_exhausted:conflicts`. Never half. Every rendered row is complete — a row cut mid-way is a row whose qualification may be the part that went. |
| `test_a_budget_too_small_to_answer_honestly_injects_nothing` | No room for a resolved answer is silence, not a one-sided search hit. |
| `test_deadline_exhaustion_marks_the_fallback` | A 1 ms deadline cannot be met; the turn still answers, and the payload carries the `resolution unavailable (deadline)` marker. |

### Through the corpus, with evidence and receipt references

The bench harness runs the same three release-fixture episodes end to end against
a live API and records the injected size. The episodes are tagged
`release_fixture` in `bench/current_state/episodes.yaml`.

| Fixture episode | Delivered by | Expected | Observed | Result |
|---|---|---|---|---|
| `explicit-replacement-pos` | the real `UserPromptSubmit` script against a live API (905 chars injected) | current / the successor value | current / the successor value | **PASS** |
| `policy-vs-deployment-pos` | same (671 chars injected) | contested / no value | contested / no value | **PASS** |
| `tight-token-budget-pos` | same (487 chars injected) | contested / no value | contested / no value | **PASS** |

**Copied-observation lineage** (`multi-source-two-hop-pos`, tagged `lineage`):
**PASS** — two records reported as one lineage group, and the *delivery receipt*
carries one anchored group. The check reads the receipt's own `lineage` list, so
what is asserted is the grouping the shipping receipt reports, not a separate
harness-side calculation. A snapshot and a session summary citing one
observation's anchor are one witness.

**Repeated consolidation and restore** (`restore-or-deletion-pos`, tagged
`repeated_consolidation`): **PASS** — after three consolidation cycles, a delayed
import and an explicit restore, both questions return the correct current values
and no retired fact is revived.

**Receipts explain revisions.** The rebuild-replay invariant does not merely
check that a rebuilt store selects the same refs — it checks that it selects them
**at the same source revisions**, and that the delivery receipt *names* those
same revisions (`harness._receipt_revisions` over the receipt's public `supplied`
list). That is what "explainable receipt" is being asserted to mean here.

---

## 6. Release checks

Run on the release candidate at the time this record was written.

| Check | Command | Result |
|---|---|---|
| Packaging — build | `python -m build` in a clean venv | **PASS** — wheel and sdist built |
| Packaging — install | `pip install <wheel>` into a fresh venv | **PASS** |
| Packaging — console script | `palinode --version` from that venv | **PASS** |
| Packaging — cold start | `palinode doctor` against an empty tmp store | **PASS (runs)** — 26 checks executed, 17 pass; see note |
| Parity — surfaces | `tests/test_surface_parity.py` | **PASS** |
| Parity — MCP tool table | `tests/test_mcp_tool_count.py` | **PASS** — doc table and advertised tools agree by name and count |
| Parity — MCP schema | `tests/test_mcp_schema_size.py`, `tests/test_mcp_schema_size_budget.py` | **PASS** (38 tests) |
| Parity — REST routes | `tests/test_api_route_inventory.py` | **PASS** |
| Docs alignment | `tests/test_docs_schema_alignment.py`, `tests/test_readme_documents_real_commands.py` | **PASS** |
| Changelog structure | `tests/test_changelog_structure.py` | **PASS** |
| Migration | `tests/test_migration_v020.py` | **PASS** (6 tests) |
| Seeded store, live hook | `tests/test_resolve_hook_live.py` | **PASS** |
| Full corpus (slow) | `tests/test_bench_current_state.py` incl. `test_full_corpus_run` | **PASS** (24 tests, ~60 s) |

**Note on the cold-start doctor run.** The nine non-passing checks on an empty
tmp store are all *absence*, not fault: no database yet, no API or watcher
running, the directory is not a git repository, no audit directory. The checks
this release is accountable for behaved correctly on a store with nothing in it:
`projection_current` skipped cleanly (no DB), `prompts_current` reported that the
store has no prompts directory and named the path, `store_tree_clean` reported
that the directory is not a git repository.

### Release candidate versus the fixed baseline

Diffed against the `v0.19.1` release cut, over the MCP schema-semantics golden
fixture and the MCP tool documentation:

| Change | Detail |
|---|---|
| **1 tool added** | `palinode_resolve` — params `query`, `ref`, `context`, `intent` (enum: `current_state`), `max_items`, `max_chars`; no required params |
| **1 parameter added** | `palinode_search.resolve` — enum `none` \| `linked` \| `full` |
| **0 tools removed** | — |
| **0 existing params changed or removed** | — |
| **Docs** | one tool row added, one row expanded to describe `resolve` |

REST route inventory: no route removed, no route's method set changed.

---

## 7. The model-backed reader arm

Run over a **family-stratified** sample — one episode from each of the 20 scored
families (25 questions), drawn from the same mixed-claim records the scored run
uses rather than from a purpose-built pair set. Coverage gate: **PASS**.

Agreement with the deterministic reader: **disposition 0.92, value 0.96**.

Every disagreement is in the conservative direction — the model abstained where
the rule reader answered (two cases: one where it reported `contested` for a
`current` oracle, one where it reported `unknown` for a `contested` one). That is
the direction a model-backed reader is *allowed* to differ in: it costs accuracy,
not safety. It is reported here rather than folded into the headline, which stays
the deterministic reader's.

The arm is off by default, endpoint-from-environment, and **silent rather than
approximate** when a scored family has no coverage.

---

## 8. Claims supported

- **Mechanically justified resolution works.** On the mechanical families the
  bundle disposes correctly 93% [82–100] of the time against the baseline's
  75% [59–89], with a stale-current rate of 0% [0–0] against 43% [27–61].
- **Consolidation is what makes the current state answerable, and it does not
  damage the record.** On the matched episodes the bundle answers correctly
  100% [100–100] with the consolidation passes and 25% [0–50] without them;
  stale-current goes from 75% [50–100] under raw retention to 0% [0–0].
- **The read path is inert and the store is rebuildable.** 774 invariant checks,
  0 violations.
- **Current, contested and insufficient stay distinguishable at the point of
  delivery**, through the real hook, under the default per-turn budget and under
  a tight one.
- **An upgraded store and a clean store are the same store.** Asserted on real
  files and real SQLite, per row and per projected hash.
- **Rebuilding legacy-shaped index rows preserves recorded retirement.**
  v0.20 re-derives the same verdicts from markdown and git. This fixture does
  not run an older release or certify its lifecycle behaviour after downgrade.

## 9. Claims *not* supported

Stated as the run stated them, because an acceptance record that only lists wins
is a marketing page.

- **Unlinked corrections are shown but not dispositioned.** Discovery reaches
  them, and as of this release the rendered text prints each discovered record
  under the assertion it was found for (`⚠ also found (unlinked): [ref] …`) and
  each standing assertion's linked `support:` refs. On the acceptance corpus the
  unlinked-correction family went from 0.50 to 1.00 on detection, presentation
  and source correctness in the rendered text (dated addendum in the report).
  The policy still does not contest a seed on an unlinked record — discovery is
  advisory by design — so disposition stays at 0.50 for that family.
- **Contested sides now carry their own qualifiers.** `contradicts:`,
  `stale backing:` and `epistemic:` labels render inline on each side of a
  conflict group, with the same labels the session digest uses. Fixed in this
  release; the headline table predates the fix and under-reports the renderer.
- **Future-effective replacement does not preserve the old value.** The design
  calls for the predecessor to remain applicable until the transition; the
  shipped policy retires it when the successor is written and then declines
  because the successor is not yet effective. A correct refusal, not the required
  result. Deferred at the time of writing; **fixed in v0.20.1** (2026-09-14).
  The acceptance numbers above come from the run that found it and were not
  re-measured, so they under-report a fixed build rather than over-report it.
- **Under index lag the resolved arms deliver the indexed wording.** The delivery
  is honest — it is stamped `index stale` — but a rule-following reader still
  takes the stale value. The only arm that gets this right is the one that reads
  the file, which gets everything else wrong. Deferred at the time of writing;
  **fixed in v0.20.1** (2026-09-14) — the seed now reads through the same live,
  projected load as every other layer. Not re-measured, for the reason above.
- **As-of and claim-validity questions are not answered at all**, and are not
  claimed to be. The family is recorded with a deferred disposition and scored by
  nothing. **Deferred**, along with the actionability axis and the queryable
  injection ledger — all three were rescheduled to v0.22.0 on 2026-09-13, since
  each adds a schema axis or table with its own migration decision and they are
  better decided as one story.
- **No statement is made about semantic paraphrase recall.** The seed stage ran
  on a lexical stand-in embedder.

**Status of the first two.** Both are **renderer** defects, not policy or
evidence defects: the information is in the structured payload and is lost on the
way to text. Both were filed off this run and were being fixed while this record
was written, which means the honest thing to say is that **this page cannot tell
you whether your build has them.** [CHANGELOG.md](CHANGELOG.md) is the authority
for a specific version — look for the renderer entries under the release you
installed. What this page *can* say: the acceptance numbers above come from the
run that **found** the two defects and were not re-measured afterwards, so they
under-report a fixed renderer rather than over-report it. A caller reading the
structured payload rather than the rendered text is unaffected either way.

---

## 10. Caching

**No cross-request cache exists.** Nothing in this release reuses a bundle, a
resolution or an evidence block across requests, and a cache is not a release
requirement. What is recorded — and pinned in code as `receipt.reuse_key(...)` —
is the eligibility contract any future cross-request cache must satisfy: caller
access resolved by the *server*, normalized query scope, policy version including
the config fingerprint, every `(ref, revision)` pair, and time-sensitive
applicability. Equal keys are necessary, never sufficient: access and applicable
time can change with no request and no file changing.

The one cache that exists is per-request support checking, and it applies all
five conditions to a single record's check. The noon-boundary case — a source
whose `expires_at` passes with its bytes unchanged — is covered by a fixture, not
by argument.

---

## 11. Dogfood observations

The release ran on a live store, not only on fixtures. Three things it taught:

- **The first honest nightly.** Once the consolidation loop could actually run
  (the point release before this one), the nightly pass started producing real
  proposals on a real store — and the activity gate turned out to have been
  deferring every other night, because it stamped the pass's *completion* rather
  than its start and a daily cron therefore always arrived a minute short. Fixed
  in this release; the fix is a prerequisite for observing the loop at all.
- **The weekly had to be reverted and re-landed.** One weekly run reported one
  project compacted and 65 notes archived — of which 49 carried no project
  reference (no group, no model read them), 16 belonged to a group that proposed
  nothing and was never counted, and one was that day's live daily note, which
  session-end was still appending to. Retiring notes a pass never consolidated is
  not compaction, it is data loss with a log line. Three rules now hold, and the
  zero-op groups are named rather than passing unrecorded.
- **The retention window is a choice, and 90 is the safe default.** The dogfood
  store runs the status-log sweep at **30** days; the shipped default is **90**.
  A store fed by session-end accumulates one dated line per session, so the right
  window depends entirely on how often you work in a project. 90 was chosen as
  the default because the cost of keeping a line too long is a slightly larger
  prompt, and the cost of retiring one too early is that a recent decision leaves
  the current view.

The migration itself was run on that store: roughly 12,800 chunks, fully
converged to projection v1 in **under five minutes** with a single
`palinode reindex`, prompts synced to v5 with a plain `palinode prompt sync` (no
`--force`), `palinode doctor` green afterwards.

---

## 12. Reproducing this

```bash
# the corpus, the arms, the controls, the invariants
python -m bench.current_state --help

# the tests that gate the release
pytest tests/test_migration_v020.py tests/test_resolve_hook_live.py
pytest tests/test_bench_current_state.py -m ''      # includes the slow full run
pytest tests/test_surface_parity.py tests/test_mcp_tool_count.py \
       tests/test_api_route_inventory.py tests/test_mcp_schema_size.py
```

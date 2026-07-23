# PaperAgent Architecture Optimization

## Audit Result

The current failure pattern is not caused by one bad prompt. The main structural risks are:

1. `paper_agent/paper_summary.py` still contains PDF parsing, asset capture, prompts, model calls, guards, repair logic, and DOCX writing in one large compatibility module.
2. The public `harness/` and `agents/` packages describe contracts, but most real behavior still lives in that compatibility module. The contracts are not yet executable schemas.
3. Asset extraction produces one selected crop per caption. There is no candidate pool, crop-quality score, or deterministic winner selection before the LLM sees the asset.
4. Visual failures are transported as free-form strings. A local heuristic, a vision-model finding, and a transport timeout can reach the same repair path with different meanings.
5. A repair attempt mostly re-runs the same capture function. The loop does not maintain an ordered strategy ladder such as recapture, expand, split, replace, or downgrade.
6. The workflow sidecars are useful, but the system does not yet treat each node as a content-addressed checkpoint that can be resumed and replayed independently.
7. The GUI reduces many failures to an `Error` badge. The user cannot see the stage, recovery action, attempt number, or the exact artifact that failed.

The immediate SyncMos failure was a false positive in `_table_asset_text_looks_incomplete`: PyMuPDF flattened a complete table body into one text line separated by `|`, while the guard required at least two text lines. The guard now counts standalone numeric cells and no longer treats `F1`/`N1` header labels as body values.

## Target Architecture

```text
Input
  -> SourceResolver
  -> LayoutEvidence (immutable page blocks, captions, tables, figures, formulas)
  -> AssetCandidatePool
  -> AssetSelector (deterministic score + optional vision adjudicator)
  -> EvidenceGraph / ClaimGraph
  -> Parallel ChunkSynthesizers
  -> Typed ReportDraft
  -> Typed Verifier Findings
  -> Repair State Machine
  -> DOCX Renderer
  -> Rendered-Document QA
```

The key change is to separate **candidate generation**, **selection**, **verification**, and **repair**. An LLM should explain evidence and adjudicate ambiguous candidates, not decide the basic page geometry from scratch.

## Codex Prompt 0: Baseline and Contracts

```text
You are working in E:\\codex\\paper_agent. Read README.md, TECHNICAL_ANALYSIS.md,
paper_agent/paper_summary.py, paper_agent/harness/*, paper_agent/agents/*, and the
existing asset tests before editing.

Create an architecture baseline without changing behavior. Measure:
- module sizes and public entry points;
- workflow node inputs, outputs, and sidecar artifacts;
- all guard result shapes and repair action strings;
- cache files and whether each node can resume independently;
- current test coverage for parse, asset, verifier, repair, and DOCX rendering.

Add typed Protocol/dataclass contracts in a new narrowly scoped module. Do not move
the implementation yet. Add contract tests that fail if a node declares inputs or
outputs that it does not actually read or write. Keep paper_summary.py as the
compatibility facade. Run the focused tests and the complete suite, then report the
baseline and the exact files changed.
```

## Codex Prompt 1: Evidence and Asset Candidate Pool

```text
Read the contracts created in Prompt 0 and the current asset capture functions in
paper_agent/paper_summary.py. Implement an immutable EvidenceBundle containing page
number, source bbox, caption text, object kind, extracted table/formula text, and
rendered image path.

Change extraction to produce multiple AssetCandidate objects per caption when the
geometry is ambiguous. Candidates should include: detector bbox, text-heuristic
bbox, border-enclosed bbox, and split candidates for adjacent objects. Add a
deterministic quality score using caption identity, bbox containment, border
completeness, numeric-cell coverage, object purity, and page-column overlap.

Do not call an LLM in this stage. Preserve the old PaperAsset facade by selecting
the top candidate for existing callers. Add golden tests for complete tables,
multi-line tables, tables beside figures, formulas below captions, and captions
split across PDF text blocks. A bad candidate must remain available for debugging,
but must not become the default asset without a score explanation.
```

## Codex Prompt 2: Typed Verification Findings

```text
Replace free-form visual failure routing with typed Finding objects. Each finding
must contain: finding_id, stage, severity, confidence, asset_id or claim_id,
evidence_refs, reason_code, human_message, and suggested_actions.

Define reason codes for at least:
- table_body_missing;
- table_truncated;
- caption_truncated;
- mixed_objects;
- type_mismatch;
- formula_contamination;
- missing_critical_asset;
- verifier_transport_failure;
- verifier_invalid_json.

Keep backward-compatible rendering of the current error strings. Add a migration
adapter for old verification JSON. Make local deterministic checks return confidence
and provenance. A low-confidence local finding must be a warning until a second
independent signal confirms it; a transport failure must never be reported as a
content defect. Add tests for severity and confidence aggregation.
```

## Codex Prompt 3: Repair State Machine

```text
Read the typed findings and current _build_repair_plan,
_recapture_critical_visual_assets, and PaperWorkflow executor. Implement a bounded
repair state machine with explicit states:

  DETECTED -> CLASSIFIED -> RECAPTURE -> RECHECK -> ACCEPTED
                                      -> SPLIT
                                      -> ALTERNATE_CANDIDATE
                                      -> REPORT_REWRITE
                                      -> BLOCKED

Each reason code gets an ordered action ladder. For example, table_body_missing is
candidate scoring, then border expansion, then split-at-border, then vision
adjudication. mixed_objects must split or discard and must never blindly expand.
The same geometry/content signature cannot count as progress. Every action must
record before/after signatures, confidence, cost, and the next action.

Permit at most two actions per asset and a global repair budget. Re-run only the
affected guard and affected report section when possible. Add state-machine tests
for successful repair, no-op repair, conflicting findings, and exhausted budgets.
```

## Codex Prompt 4: Verification Tiers and Model Adjudication

```text
Separate verification into three tiers:

1. deterministic checks for bbox, caption identity, border closure, image size,
   marker validity, and numeric/body evidence;
2. cheap OCR/text consistency checks for the selected asset;
3. vision-model adjudication only for candidates that remain ambiguous.

The vision prompt must receive the source-page crop, candidate crop, caption, object
kind, and machine measurements. Require strict schema output with JSON mode or a
validated structured response. If the model times out or returns invalid JSON,
record a transport finding and use deterministic checks; do not invent a visual
defect and do not silently convert it into a hard failure.

Add a small fixture set with expected pass/warn/block outcomes. Report precision of
local table/figure guards separately from recall. Keep the current OpenAI-compatible
client and configuration keys unchanged.
```

## Codex Prompt 5: Checkpointed Parallel Workflow

```text
Refactor workflow execution around content-addressed node checkpoints. The cache
key must include source PDF hash, selected pages, max_assets, prompt version,
model/config identity without the secret, and relevant code/schema version.

Persist typed node outputs atomically. Resume from the last valid checkpoint after a
browser disconnect, verifier timeout, or process restart. Parallelize only
independent work: page extraction, asset candidate generation, and chunk synthesis.
Keep final report integration ordered and bounded. Add cancellation and retry
semantics per node, with exponential backoff and a total time budget.

Add tests that kill/restart a fake workflow, invalidate one checkpoint, and verify
that only the dependent nodes rerun. Do not make the GUI responsible for workflow
state.
```

## Codex Prompt 6: DOCX Render QA and GUI Diagnostics

```text
Add a final RenderQA node after DOCX generation. Render the DOCX to PDF or page
images when the platform supports it, then check page count, image dimensions,
caption adjacency, image clipping, overflow, and missing markers. Store a compact
qa.json sidecar with page/asset references and measurements.

Update the GUI result model to show stage, progress, recovery attempts, the latest
reason code, and links to trace.json, verification.json, qa.json, and the failure
report. Keep the detailed traceback in the log, but never show only a generic
"Error" badge. A completed Word file must be offered only after RenderQA passes;
warnings should be visible and non-blocking.

Add a Gradio/API test for success, recoverable warning, blocked content defect, and
transport timeout. Preserve the existing local URL and download behavior.
```

## Codex Prompt 7: Migration and Exit Criteria

```text
Migrate one path at a time behind the existing summarize_paper facade. Start with
asset candidate generation and typed findings, then repair state machine, then
checkpointing, then RenderQA. Do not perform a broad rewrite of paper_summary.py.

For every stage:
- add a golden fixture and a regression test;
- keep old sidecar fields readable for one migration cycle;
- compare old/new asset manifests and report section coverage;
- run the complete suite and one real paper through the GUI;
- record duration, model calls, repair count, hard failures, warnings, and final QA.

The migration is complete only when ten representative papers produce either a
QA-passed DOCX or an actionable blocked report, no identical repair is counted as
progress, and every blocked result identifies a typed reason code and next action.
```

## Recommended Order

1. Apply the current false-positive guard fix and add the SyncMos regression.
2. Execute Prompt 0 and Prompt 1 together, because repair quality depends on having candidate evidence.
3. Execute Prompt 2 and Prompt 3 before adding more visual-model prompts.
4. Execute Prompt 4 to make model failures non-destructive.
5. Execute Prompt 5 for speed and restartability.
6. Execute Prompt 6 for user-visible quality gates.
7. Use Prompt 7 as the migration checklist and release gate.

The architecture should optimize for **fewer invalid final documents**, not merely
fewer raised exceptions. A blocked run with a precise recoverable reason is better
than a fast DOCX containing a wrong table, formula, or claim.

# Architecture Baseline

This baseline was captured before the Prompt 0/1 migration.  The existing
implementation remains available through `paper_agent.paper_summary`.

## Module and Entry-Point Inventory

| Area | Baseline |
| --- | --- |
| Main compatibility facade | `paper_agent/paper_summary.py`: 375,182 bytes / 9,355 lines; `summarize_paper()` is the report entry point |
| CLI entry | `paper_agent/paper_agent.py:main`, `python -m paper_agent` |
| GUI entry | `paper_agent/gui.py:setup_gui` and `paper_agent/app/gui.py` facade |
| Workflow facade | `paper_agent/harness/workflow.py:PaperWorkflow` |
| Workflow executor | `paper_agent/harness/executor.py`: 4,275 bytes / 102 lines |
| Context and node contracts | `paper_agent/harness/context.py`, `node.py`, `policy.py` |
| Agent facades | `paper_agent/agents/reader.py`, `extractor.py`, `synthesizer.py`, `verifier.py`, `reflector.py` |
| Existing shared schemas | `paper_agent/schemas/*.py`; most legacy asset behavior still originates in `paper_summary.py` |

## Workflow Contract Inventory

The legacy node declarations and observed context writes are:

| Node | Inputs read | Outputs written | Sidecars / resume |
| --- | --- | --- | --- |
| PreparePaper | `input_path`, `output_dir` | source/pdf/name/work dir | none; reruns preparation |
| ParsePaper | pdf, work dir, pages, asset limit | raw text, `PaperAsset[]` | capture images; no independent checkpoint |
| ExtractSections | pdf, pages, text, assets, paper identity | title, abstract, formulas, grounding, graph, memories, patches | none |
| SummarizeContribution | text, assets, model config, memories, patches | chunk notes, partial integrations | `*-chunk-notes.json`, `*-partial-integrations.json`; chunk work resumes |
| ExtractMethods | chunk notes, partials, assets, abstract, formulas, model config | draft summary | no independent checkpoint |
| VerifyClaims | draft, source text, grounding, abstract, assets, model config | verification, revised summary, guards, graph | verification/graph sidecars are written on workflow snapshots |
| ReviseReport | verification, draft, revision counters, repair plan | gate decision, revised/blocked report, repair history | `*-verification-failed.md` when blocked |
| GenerateReport | verified summary, assets, prepared output paths | DOCX and Markdown | `*-summary.docx`, `*-summary.md`, trace/grounding/verification/graph JSON |

The typed audit of these declarations is in
`paper_agent/harness/contracts.py`.  `paper_summary.py` remains the facade for
legacy imports and the implementation migration is intentionally incremental.

## Guard and Repair Shapes

The current local guard result is `GuardResult(name, status, errors,
warnings, metrics)`.  Verification is `VerificationResult(passed, errors,
hard_failures, soft_warnings, patch_suggestions, revision_attempted,
revision_applied)`.  Model visual findings are JSON objects with `severity`,
`type`, and `reason`; transport failures are currently represented as warning
strings.  Guard payload sidecars add the guard specification (`problem`,
`implementation`, `blocking`).

The existing repair plan action strings are:

* `missing:<kind>:<number>`
* `recapture:<asset_id>`
* `remove:<asset_id>`
* `rewrite:report`
* `patch:claims`

`action_keys()` is the compatibility source of truth until typed findings are
migrated in a later prompt.

## Cache and Recovery Baseline

The paper workflow currently writes `*-chunk-notes.json` and
`*-partial-integrations.json`.  These caches are keyed by paper/chunk shape,
not by a complete source/config/schema content hash.  Chunk synthesis can
resume completed chunks and partial integrations; Prepare, Parse,
ExtractSections, ExtractMethods, VerifyClaims, ReviseReport, and DOCX rendering
rerun as a unit after process restart.  Harness trace, grounding, verification,
and knowledge-graph JSON files are diagnostic sidecars rather than validated
node checkpoints.

## Test Surface Baseline

The existing suite collected 247 tests (previous clean run: 246 passed, 1
skipped).  The current named-test inventory is intentionally overlapping:

| Area | Named tests containing the area keyword |
| --- | ---: |
| Parse / extraction / workflow | 34 |
| Asset / geometry / visual checks | 121 |
| Verifier / guards / grounding | 37 |
| Repair / recapture / revision | 13 |
| DOCX / render / image sizing | 12 |

These counts describe test inventory, not line coverage.  There was no stable
coverage report for the baseline, and the large facade makes line percentages
misleading; the focused tests are listed so future migration work can compare
behavior directly.

## Prompt 0/1 Additions

The new typed contracts are in `paper_agent/schemas/contracts.py`, deterministic
candidate generation is in `paper_agent/assets/candidates.py`, and ParsePaper
now writes `*-asset-candidates.json` through an immutable candidate-pool sidecar.
The old selected `PaperAsset` list and DOCX asset order remain unchanged.

## Prompt 2 Finding Contract

`paper_agent/schemas/findings.py` defines the immutable `Finding` shape.  Every
finding carries `finding_id`, `stage`, `severity`, `confidence`, optional
`asset_id`/`claim_id`, `evidence_refs`, `reason_code`, `human_message`,
`suggested_actions`, and `provenance`.

The required visual and verifier reason codes are:
`table_body_missing`, `table_truncated`, `caption_truncated`, `mixed_objects`,
`type_mismatch`, `formula_contamination`, `missing_critical_asset`,
`verifier_transport_failure`, and `verifier_invalid_json`.  Legacy strings are
still rendered through `GuardResult.errors/warnings` and
`VerificationResult.errors/hard_failures/soft_warnings`.

`aggregate_findings()` combines independent signals by reason and target.  A
low-confidence local finding remains a warning; two distinct provenance values
can promote it to an error.  Transport and invalid-JSON findings are always
warnings, regardless of upstream severity, and therefore cannot be mistaken
for document-content defects.  `migrate_verification_payload()` adds typed
findings to both old direct verification JSON and sidecar-wrapped payloads.

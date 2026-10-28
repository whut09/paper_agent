# 12081 gateway failure self-check (2026-10-28)

Paper: https://media.eventhosts.cc/Conferences/ECCV2026/pdfs/12081.pdf

## Result: generation remains blocked, not fixed

The real GUI callback `/summarize_file` was exercised against the running local
service with the same URL, all pages, 13 assets, and the existing local config.
Existing intermediate notes were retained; this was not a clean-cache benchmark.
Run `20261028-080400-d51133e8` failed in `SummarizeContribution`, chunk 4/5.
No Word file was exposed, so an HTTP Word-download success could not be tested.

The configured gateway was `https://cpa.returnxxy.cc.cd/v1`, model
`dragon3api/gpt-6.1-sol`, via the locally configured proxy and Responses streaming.
Its model-list endpoint succeeded and listed the configured model. A minimal
text request returned `OK`. These checks do not establish that paper requests
will be accepted. No alternate provider, altered paper content, or filter bypass
was used. Credentials and full configuration are intentionally excluded.

The actual error was HTTP 403, code `request_check_blocked`, message
`Request rejected by preflight safety checks`. Request ID:
`202610280804171430272868268d9d6yvXRv3oN`.
The gateway did not provide the precise preflight rejection reason. It requires
administrator investigation; it cannot honestly be diagnosed as a model-name,
network, Word-integrity, or content-policy problem from this response alone.

## Application corrections

- Preserve gateway-denial classification through wrapped exceptions; do not
  mistake digits in a request ID for a transient HTTP error.
- Display the actual denial and request ID before generic final-check errors.
- Report actual API attempts rather than the configured maximum. Stop on
  terminal status errors, including legacy empty-response streaming fallback.
- Acceptance actions request gateway diagnostics instead of blind retries.

## Verification

The final full regression run passed 474 tests, with 1 skipped and 7 existing
dependency deprecation warnings, including acceptance-action regressions.
Real GUI generation reproduced the denial
and confirmed the corrected user-facing explanation. This is not a successful
end-to-end generation or download result.

## Follow-up: terminal-denial propagation through the workflow

Further local inspection found that the parallel chunk scheduler retried all
failed chunks once, even after the API layer had stopped on a 403. Partial
integration also swallowed denials, final integration could submit a shorter
fallback prompt, and verifier transport/JSON-repair handlers could misclassify
denials as recoverable warnings. These were application bugs, not evidence
that the gateway rejection itself could be resolved locally.

The scheduler now retains successful in-flight notes, cancels queued work, and
does not schedule retries or partial integrations after a denial. A per-client
terminal-denial latch also blocks later fallback calls and concurrent workers
from submitting another retry. Final integration, old-report reuse, section
repair, abstract replacement, and verifier fallbacks propagate terminal denials.
Structured stream error codes and request IDs in long errors are retained.

Offline regressions cover the exact cached-note shape of this paper (notes
1/2/3/5 available, note 4 absent), both serial and concurrent execution, partial
integration failures, final integration, verifier failures, and stream errors.
These use injected gateway errors; they are not real model generation tests.
No blocked paper request was resubmitted or altered to get around the preflight
check during this follow-up. The last real-paper outcome remains blocked.

Follow-up full-suite result: **489 passed, 1 skipped**, with 7 dependency
deprecation warnings. Fifteen new offline workflow regressions passed.

## Clean-cache run and offline payload audit

A subsequent clean-output run `20261028-084343-0a9c13ae` failed after
139.094 seconds, again on chunk 4/5. Notes 1/2/3/5 succeeded; no Word file was
exposed. Request ID: `202610280844529959829938268d9d6vXxfGENX`.
This used the same gateway, model and proxy, without reusing earlier notes.

The exact chunk prompts were reconstructed offline from the ExtractSections
checkpoint using the recorded 8,000-character chunk setting. The fourth prompt
was 9,875 characters; the reconstructed Responses JSON body was 14,639 UTF-8
bytes (excluding SDK transport details). Successful prompts were between
10,153 and 10,502 characters. No prompt contained NUL or Unicode replacement
characters. Apparent Chinese corruption in an earlier terminal display was an
output-encoding artifact, not damaged request text. The fourth chunk contains
the end of page 8 and pages 9/10; its evaluation-metric text was checked against
a rendering of original page 9. These observations do not identify the gateway
preflight trigger.

An independent application defect was verified: two automatic learnings from
another paper, containing asset 8/9 and its section title, were injected as
domain-wide corrections. New automatic findings now remain paper-scoped; legacy
domain findings with local asset IDs are not injected into other papers unless
explicitly promoted. Stored records are preserved. Offline inspection of this
actual checkpoint confirms both unrelated records are excluded. Asset findings
mentioning a section title also no longer teach an unrelated Chinese-report-title
translation rule. Six new regression cases cover these behaviors.

Final full-suite result: **495 passed, 1 skipped**, with 7 dependency deprecation
warnings. No altered version of the denied request was resubmitted to get around
preflight checks. This input-quality fix is not proof of restored generation;
successful same-paper generation, deployment and download remain unverified.

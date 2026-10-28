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

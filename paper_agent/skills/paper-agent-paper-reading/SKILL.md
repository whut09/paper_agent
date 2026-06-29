---
name: paper-agent-paper-reading
description: Grounded scientific paper translation and structured paper-summary workflow for PaperAgent. Use when an agent needs to translate PDFs, summarize research papers into Word reports, preserve formulas/figures/tables, or run PaperAgent through SkillBridge.
metadata:
  keywords: paper, pdf, translation, summarize, Word report, PaperAgent, 论文翻译, 论文总结, 精读笔记, 图表公式
  domains: research, scientific-papers
  taskTypes: translate, summarize, report-generation
---

# PaperAgent Paper Reading

Use this skill to run PaperAgent for grounded scientific paper work.

## Workflows

1. For a structured Word summary, run `scripts/paper-agent.mjs --mode summarize --input <paper.pdf> --output <dir> --config <config.local.json>`.
2. For PDF translation, run `scripts/paper-agent.mjs --mode translate --input <paper.pdf> --output <dir> --config <config.local.json> --service openai`.
3. Read `references/summary-system-prompt.md` and `references/final-note-prompt.md` before modifying summary behavior.
4. Read `references/translation-prompt.md` before modifying translation behavior.

## Output Expectations

- Summary mode writes `*-summary.docx` plus `trace.json`, `grounding-map.json`, `verification.json`, and `knowledge-graph.json`.
- Translation mode uses PaperAgent's normal mono/dual PDF translation outputs.
- Keep claims grounded in the original paper. Do not invent datasets, formulas, figure numbers, table numbers, institutions, years, or DOI values.
- Preserve formulas and rich-text placeholders during translation.

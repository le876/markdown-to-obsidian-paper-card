---
title: Compact bilingual paper translation worker contract
scope: Markdown to Obsidian paper cards
---

# Compact bilingual paper translation worker contract

The authoritative translation-quality rules live in `paper-translation-prompt-fragment.md`. The project agent configuration is generated from that fragment by `scripts/sync_paper_translation_agent_prompt.py`; do not maintain a second handwritten translation style guide here.

This worker receives an assignment manifest that points to a JSONL packet containing only ordered translation units from one paper. It must not read the full Markdown note, `ai-research-writing`, `humanizer`, or any unrelated file.

## Input and output

- Read only the assignment and its frozen compact packet. Semantic body and reference-title units are translated; deterministic passthrough units are copied locally.
- The native worker returns exactly one JSON object matching the assignment's frozen schema: `status`, `translated_units`, `translations`, and `reference_titles`. It does not write JSONL files.
- The parent bridge validates the native rollout and response, restores protected tokens, and writes validated UTF-8 JSONL. Do not include Markdown or commentary outside the final JSON object.

## Failure handling

Import requires runtime evidence matching the frozen role, parent/child session, model, reasoning effort, packet, and output hashes. Missing or mismatched evidence must fail. Accepted unit translations can be cached for bounded recovery; no unverified manual result may bypass final validation. Follow the current status dispatch and recovery limits in `SKILL.md`.

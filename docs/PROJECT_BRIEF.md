# Project brief

This file records the scope and decisions agreed before implementation started. Detailed architecture lives in `ARCHITECTURE.md` once it is written; where the two disagree, `ARCHITECTURE.md` wins.

## Goal

A system that takes multiple documents and a set of user-defined keys, and returns the value of each key in each document together with **verifiable provenance**. Output: JSON is the authoritative format; a table (exportable to Excel/CSV) is the presentation format.

## Positioning

This is an engineering project, not a research paper. Prior work exists (for example LangExtract for grounded extraction, Docling for document parsing, hosted citation features in commercial APIs) and existing components are reused where sensible. CiteCell aims to differ in:

- Excel **cell-level** provenance alongside PDF coordinates and Word paragraphs;
- Chinese-language documents;
- a multi-document comparison table with click-to-highlight evidence and human correction;
- local open-source small models, with a measured comparison against larger open models;
- a layered pipeline that avoids calling a model when documents share a template.

## Provenance model (summary)

- All formats are parsed into **blocks**: stable ID, plain text, and a format-specific locator.
- The model is asked for `value`, `quote` and `block_id`; it is **not** asked for page numbers or coordinates.
- The verifier checks block existence, quote ⊂ block text (exact, then normalized), and that the value can be derived from the quote. It computes span offsets and the locator itself.
- Match levels `exact` / `normalized` / `fuzzy`; `fuzzy` never counts as `grounded`.
- Missing key → `null`, no evidence, status `not_found`. No invented values.
- Derived values carry `evidence` plus an `operation`; the program recomputes the result.
- Status values: `grounded`, `derived`, `not_found`, `needs_review`, `failed`. `grounded` means the value is deterministically derivable from a quote that exists in the source — it does **not** mean the value is correct; correctness is judged by evaluation against ground truth or by human review. `failed` marks infrastructure failures, kept separate from content judgments.

## Layers

1. **Template-consistent documents**: rule / anchor based extraction induced from a model-labelled example; fall back to layer 2 when a rule fails.
2. **Heterogeneous documents**: model extraction with verified quotes.
3. **Answers not literally in the text**: decision layer — a small discriminative model with confidence gating, escalation to a reasoning model, and flagging for human review; derived values are recomputed by the program.

## Constraints and decisions

- Open-source models only; outputs of closed commercial APIs are not used as training data.
- Format scope for the first version: text-based PDF, Word body text and tables, Excel. Out of scope: OCR, complex cross-page tables, Word comments / headers / footers / text boxes.
- Output format: JSON authoritative, table for display.
- Data: a synthetic generator (ground truth and provenance come for free, difficulty is controllable) for training and large-scale tests; a held-out evaluation set of real public documents published as a pointer dataset (annotations + source URLs + hashes + download script), so third-party documents are not redistributed.
- Delivery: local Python backend (FastAPI) with a web UI (React + PDF.js); desktop packaging later.

## Milestones

1. **M1** — synthetic data generator, parsers, layer-2 extraction with verification, basic evaluation metrics. No UI, no fine-tuning.
2. **M2** — layer 1 template induction with fallback; measure model-call and latency savings.
3. **M3** — decision layer and derived-value verification.
4. **After** — web UI, optional LoRA / QLoRA fine-tuning of the local model, desktop packaging.

## Evaluation (to be implemented)

Value accuracy (field-level F1), quote validity, localization accuracy, evidence support, hallucination rate on missing keys, share of cells flagged `needs_review` and how often those are really wrong, plus latency, memory and cost per document. Numbers are reported only when produced by scripts in this repository.

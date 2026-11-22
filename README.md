# CiteCell

**Traceable multi-document key-value extraction.**
Give it a batch of documents (PDF / Word / Excel) and a list of keys; get back a *documents × keys* table in which every value links to the exact place in the source document that supports it.

> **Status: early development (milestone 1 in progress).** Nothing here is released yet, and no benchmark numbers are claimed. Results will be added to this README only after they have been measured and the evaluation scripts are in the repository.

## Idea

Large language models can extract fields from documents, but a value without a verifiable source is hard to trust. CiteCell treats provenance as something the **program guarantees**, not something the model promises:

1. Every document is parsed into text blocks with stable IDs and format-specific locators.
2. The model returns, for each key, a value plus a **verbatim quote** and the block ID it came from.
3. A verifier checks that the quote really occurs in that block (exact, then normalized matching), recovers the character span itself, and maps it back to a location.
4. Anything that fails verification is retried, and if it still fails it is flagged for human review instead of being shown as a confident answer.

| Format | Provenance location |
|---|---|
| PDF (text-based) | page + bounding boxes |
| Word (body text and tables) | paragraph index / table row-column |
| Excel | sheet + cell address (e.g. `Sheet1!C7`) |

## Planned scope

- Chinese-first documents, with open-source models only (Qwen3 family and similar), running locally through an OpenAI-compatible interface (llama.cpp / Ollama / vLLM).
- A layered pipeline: rule/template extraction for uniform documents, model extraction with verified quotes for heterogeneous ones, and a decision layer for values that are derived rather than quoted (sums, counts, date differences, yes/no judgments), verified by recomputation.
- A synthetic document generator that produces documents together with ground truth and provenance, plus a held-out evaluation set of real public documents (released as annotations, source URLs and hashes — not as copies of third-party documents).
- A web UI with click-to-highlight evidence and human correction, later packaged as a desktop app.
- Evaluation of value accuracy, quote validity, localization accuracy, hallucination on missing fields, and human-review burden.

Not planned for the first version: OCR for scanned PDFs, complex cross-page tables, Word comments / headers / footers / text boxes.

See [`docs/PROJECT_BRIEF.md`](docs/PROJECT_BRIEF.md) for the agreed scope and decisions.

## How this project is developed

This project is developed with substantial AI assistance (Claude, via Claude Code and delegated sub-agents). The project owner makes the design, scope and evaluation decisions and reviews the results; AI-assisted commits carry a `Co-Authored-By` trailer. Commit history is kept intact (no force-pushes) so the development process can be inspected.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).

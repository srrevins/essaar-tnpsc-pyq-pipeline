---
language:
- en
- ta
license: other
pretty_name: ESSAAR TNPSC Previous-Year Question Papers
task_categories:
- question-answering
- text-classification
tags:
- tnpsc
- tamil-nadu
- previous-year-questions
- education
---

# ESSAAR TNPSC Previous-Year Question Papers

This public dataset preserves links and structured extracts from question papers published by the Tamil Nadu Public Service Commission (TNPSC).

## Contents

- `manifest.json` — collision-safe source inventory with official provenance.
- `sources/<year>/<paper-id>.pdf` — archived official source PDFs.
- `drafts/<year>/<paper-id>.json` — machine-extracted draft questions requiring review.
- `published/<year>/<paper-id>.json` — reviewed questions approved for website use (added separately).

## Provenance and use

Every record retains its official TNPSC source URL, source page, source hash, and extraction confidence. TNPSC remains the source and rights holder for source documents. ESSAAR does not claim ownership of official papers. Draft data may contain OCR or parsing errors and must not be treated as authoritative.

## Processing

The pipeline uses embedded PDF text where usable and the open-source Tesseract OCR engine (`eng+tam`) when necessary. No paid OCR or generative-AI API is used. Low-confidence outputs remain drafts until reviewed.

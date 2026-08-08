# ESSAAR TNPSC PYQ Pipeline

Zero-paid-API background pipeline for discovering, archiving, extracting, and tagging TNPSC General Studies previous-year question papers.

## What it does

1. Crawls official TNPSC descriptive, objective-without-key, and answer-key archives.
2. Follows answer-key detail pages to the actual preliminary-paper PDFs.
3. Creates collision-safe IDs using exam metadata plus a SHA-256 URL suffix.
4. Uploads missing source PDFs to `essaar/essaar-tnpsc-pyq` on Hugging Face.
5. Extracts embedded PDF text when reliable.
6. Falls back to open-source Tesseract OCR (`eng+tam`) for low-quality pages.
7. Splits numbered questions and options, detects visible answer ticks when possible, and applies deterministic syllabus tags.
8. Uploads compressed outputs under `drafts/`; it never auto-publishes unreviewed questions.

No Gemini, OpenAI, Cloud Vision, paid OCR, or other paid API is called.

## Background schedule

GitHub Actions runs daily at 02:00 IST and restarts `essaar/essaar-tnpsc-pyq-worker` on free Hugging Face CPU Basic hardware. The Space uses the committed, validated official manifest, skips objects already present in the dataset, and processes at most three missing papers per boot. Your computer does not need to remain online. GitHub is only the scheduler because TNPSC blocks connections from GitHub-hosted runner IPs.

The workflow can also be started manually from **Actions → TNPSC PYQ Background Pipeline → Run workflow**. A manual run deploys the worker code and restarts it; scheduled runs only restart the existing worker. The worker status is available at `https://essaar-essaar-tnpsc-pyq-worker.hf.space/status`.

## Required GitHub secrets

- `HF_TOKEN`: fine-grained Hugging Face token with write access to the dataset.
- `HF_DATASET_REPO`: `essaar/essaar-tnpsc-pyq`.

The token is copied into the Space as a write-only secret during deployment; it is never committed to either public repository.

## Local commands

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python pipeline.py discover --output data/manifest.json
python pipeline.py validate --manifest data/manifest.json
```

Extract a known local PDF:

```bash
python pipeline.py extract \
  --manifest data/manifest.json \
  --paper-id PAPER_ID \
  --pdf paper.pdf \
  --output draft.json
```

Sync the next three missing papers to Hugging Face:

```bash
HF_TOKEN=... HF_DATASET_REPO=essaar/essaar-tnpsc-pyq \
python pipeline.py sync --limit 3
```

## Safety and quality

- PDFs are accepted only when the downloaded bytes begin with `%PDF-`.
- Source URLs are deduplicated.
- IDs remain distinct when an exam has multiple GS papers in the same year.
- Every draft retains official URL, page number, and source SHA-256.
- OCR, parsing, answer, and tagging confidence are recorded separately.
- All extracted questions remain `needs_review` until a later review/publishing workflow approves them.

# ESSAAR TNPSC PYQ Pipeline

Zero-paid-API background pipeline for discovering, archiving, extracting, and tagging TNPSC General Studies previous-year question papers.

## What it does

1. Crawls official TNPSC descriptive, objective-without-key, and answer-key archives.
2. Follows answer-key detail pages to the actual preliminary-paper PDFs.
3. Creates collision-safe IDs using exam metadata plus a SHA-256 URL suffix.
4. Reads source PDFs from the verified `tnpsc-sources-v1` GitHub Release mirror.
5. Extracts embedded PDF text when reliable.
6. Falls back to open-source Tesseract OCR (`eng+tam`) for low-quality pages.
7. Splits numbered questions and options, detects visible answer ticks when possible, and applies deterministic syllabus tags.
8. Commits compressed outputs under `data/drafts/`; it never auto-publishes unreviewed questions to the website.

No Gemini, OpenAI, Cloud Vision, paid OCR, or other paid API is called.

## Background schedule

GitHub Actions runs daily at 02:00 IST on free standard runners for this public repository. It reads the verified 184-file GitHub Release mirror, skips drafts already committed to GitHub, and processes at most three missing papers per run. Your computer does not need to remain online. The mirror is used because TNPSC blocks connections from GitHub-hosted runner IPs.

The workflow can also be started manually from **Actions → TNPSC PYQ Background Pipeline → Run workflow**. Use a limit of `1` for a smoke test and `3` for the normal background batch.

## Required GitHub secrets

None. The scheduled path uses only the workflow's short-lived repository token. The optional sync command can still publish to Hugging Face when a direct-write token is available, but the background pipeline does not depend on it.

## Local commands

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python pipeline.py discover --output data/manifest.json
python pipeline.py validate --manifest data/manifest.json
python pipeline.py validate --manifest data/mirror_manifest.json
```

Extract a known local PDF:

```bash
python pipeline.py extract \
  --manifest data/manifest.json \
  --paper-id PAPER_ID \
  --pdf paper.pdf \
  --output draft.json
```

Extract the next three missing mirrored papers into versioned GitHub drafts:

```bash
python pipeline.py sync-github --manifest data/mirror_manifest.json --output-root data --limit 3
```

## Safety and quality

- PDFs are accepted only when the downloaded bytes begin with `%PDF-`.
- Source URLs are deduplicated.
- IDs remain distinct when an exam has multiple GS papers in the same year.
- Every draft retains official URL, page number, and source SHA-256.
- OCR, parsing, answer, and tagging confidence are recorded separately.
- All extracted questions remain `needs_review` until a later review/publishing workflow approves them.

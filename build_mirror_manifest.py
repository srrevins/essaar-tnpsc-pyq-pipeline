#!/usr/bin/env python3
"""Build a reproducible Paper manifest from the verified release-mirror files."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import quote

import pipeline


RELEASE_BASE = (
    "https://github.com/srrevins/essaar-tnpsc-pyq-pipeline/"
    "releases/download/tnpsc-sources-v1"
)
RELEASE_PAGE = (
    "https://github.com/srrevins/essaar-tnpsc-pyq-pipeline/"
    "releases/tag/tnpsc-sources-v1"
)
YEAR_SUFFIX_RE = re.compile(r"_(20\d{2})_GS\.pdf$", re.IGNORECASE)
MAIN_RE = re.compile(r"(?:main|mains|written|descriptive)", re.IGNORECASE)

def metadata_from_filename(filename: str) -> tuple[str, int, str, str]:
    match = YEAR_SUFFIX_RE.search(filename)
    if not match or not filename.casefold().startswith("tnpsc_"):
        raise ValueError(f"Unexpected source filename: {filename}")
    year = int(match.group(1))
    raw_exam = filename[len("TNPSC_") : match.start()]
    stage = "Mains" if MAIN_RE.search(raw_exam) else "Preliminary"
    paper_type = "descriptive" if stage == "Mains" else "objective"
    stage_words = {"objective", "preliminary", "prelims", "main", "mains", "written", "descriptive", "examination"}
    exam_name = " ".join(
        word for word in raw_exam.replace("_", " ").split()
        if word.casefold() not in stage_words
    ).strip(" .-_")
    if not exam_name:
        raise ValueError(f"Missing exam name in source filename: {filename}")
    return exam_name, year, stage, paper_type


def build(source_dir: Path) -> dict:
    papers = []
    for pdf in sorted(source_dir.glob("*.pdf"), key=lambda item: item.name.casefold()):
        exam_name, year, stage, paper_type = metadata_from_filename(pdf.name)
        url = f"{RELEASE_BASE}/{quote(pdf.name)}"
        papers.append(
            pipeline.make_paper(
                exam_name,
                year,
                stage,
                paper_type,
                "General Studies",
                url,
                RELEASE_PAGE,
                "github_release_mirror",
            )
        )
    payload = pipeline.manifest_payload(papers)
    payload["source"] = "Verified mirror of official TNPSC PDFs"
    payload["mirrorRelease"] = RELEASE_PAGE
    pipeline.validate_manifest(payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = build(args.source_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {payload['paperCount']} mirrored papers to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

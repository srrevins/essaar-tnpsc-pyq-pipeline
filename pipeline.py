#!/usr/bin/env python3
"""Discover, validate, extract, and publish TNPSC PYQ data without paid APIs."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import urljoin, urlparse

import fitz
import requests
from bs4 import BeautifulSoup, Tag
from huggingface_hub import HfApi
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST = ROOT / "data" / "manifest.json"
TAG_CONFIG = ROOT / "config" / "syllabus_tags.json"
START_YEAR = 2016
END_YEAR = 2026
TIMEOUT = (20, 120)
CHUNK_SIZE = 256 * 1024
YEAR_RE = re.compile(r"\b(20(?:1[6-9]|2[0-6]))\b")
GS_RE = re.compile(r"\b(?:general\s+stud(?:y|ies)|g\s*\.?\s*s\.?)\b", re.I)
PDF_RE = re.compile(r"\.pdf(?:$|[?#])", re.I)
QUESTION_RE = re.compile(r"(?m)^\s*(\d{1,3})\s*[.)]\s+")
TOP_QUESTION_RE = re.compile(r"(?m)^[ \t]{0,4}(\d{1,3})\s*[.)]\s+")
OPTION_RE = re.compile(r"(?m)^\s*(?:\(([A-D])\)|([A-D])\s*[.)])\s+")
MARK_RE = re.compile(r"[✓✔☑√]")

ARCHIVE_URLS = (
    "https://www.tnpsc.gov.in/English/question_paper_withoutkey.html",
    "https://www.tnpsc.gov.in/English/descriptive-questions.html",
    "https://www.tnpsc.gov.in/English/previous-questions.html",
)
ANSWER_KEY_INDEX_URL = "https://www.tnpsc.gov.in/English/answerkeys.aspx"


@dataclass(frozen=True)
class Paper:
    id: str
    examName: str
    year: int
    stage: str
    paperType: str
    examGroup: str
    subject: str
    languageMode: str
    officialPdfUrl: str
    officialPageUrl: str
    sourceKind: str
    sourcePath: str
    draftPath: str


def log(message: str) -> None:
    print(message, flush=True)


def build_session() -> requests.Session:
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; ESSAAR-PYQ-Archiver/1.0)",
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
        }
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def clean_text(node: Tag | None) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def extract_year(*values: str) -> int | None:
    for value in values:
        match = YEAR_RE.search(value or "")
        if match:
            return int(match.group(1))
    return None


def safe_slug(value: str, limit: int = 72) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    return (normalized or "paper")[:limit].rstrip("-")


def infer_group(exam_name: str) -> str:
    value = exam_name.casefold().replace("–", "-")
    patterns = (
        (r"group\s*[- ]?ii\s*(?:and|&|/)\s*ii\s*a|group\s*[- ]?iia", "Group II/IIA"),
        (r"group\s*[- ]?iv\b", "Group IV"),
        (r"group\s*[- ]?iii\b", "Group III"),
        (r"group\s*[- ]?ii\b", "Group II"),
        (r"group\s*[- ]?i\s*a\b", "Group IA"),
        (r"group\s*[- ]?i\s*b\b", "Group IB"),
        (r"group\s*[- ]?i\s*c\b", "Group IC"),
        (r"group\s*[- ]?i\b", "Group I"),
        (r"village administrative|\bvao\b", "VAO"),
        (r"technical|engineering|diploma|iti", "Technical Services"),
    )
    for pattern, label in patterns:
        if re.search(pattern, value, re.I):
            return label
    return "Other TNPSC"


def infer_language(subject: str) -> str:
    value = subject.casefold()
    has_tamil = "tamil" in value or bool(re.search(r"[\u0b80-\u0bff]", subject))
    has_english = "english" in value
    if has_tamil and has_english:
        return "tamil-english"
    if has_tamil:
        return "tamil-bilingual"
    if has_english:
        return "english-bilingual"
    return "bilingual-or-unspecified"


def make_paper(
    exam_name: str,
    year: int,
    stage: str,
    paper_type: str,
    subject: str,
    pdf_url: str,
    page_url: str,
    source_kind: str,
) -> Paper:
    canonical_url = pdf_url.split("#", 1)[0]
    short_hash = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:12]
    paper_id = "-".join(
        filter(
            None,
            (
                "tnpsc",
                safe_slug(exam_name, 58),
                safe_slug(stage, 16),
                str(year),
                safe_slug(subject, 28),
                short_hash,
            ),
        )
    )
    return Paper(
        id=paper_id,
        examName=exam_name,
        year=year,
        stage=stage,
        paperType=paper_type,
        examGroup=infer_group(exam_name),
        subject=subject,
        languageMode=infer_language(subject),
        officialPdfUrl=canonical_url,
        officialPageUrl=page_url,
        sourceKind=source_kind,
        sourcePath=f"sources/{year}/{paper_id}.pdf",
        draftPath=f"drafts/{year}/{paper_id}.json.gz",
    )


def row_cells(table: Tag) -> Iterator[list[Tag]]:
    """Expand table rowspans so semantic columns remain aligned."""
    spans: dict[int, tuple[int, Tag]] = {}
    for row in table.find_all("tr"):
        if row.find_parent("table") is not table:
            continue
        expanded: dict[int, Tag] = {}
        for column, (remaining, cell) in list(spans.items()):
            expanded[column] = cell
            if remaining <= 1:
                del spans[column]
            else:
                spans[column] = (remaining - 1, cell)
        column = 0
        for cell in row.find_all(["th", "td"], recursive=False):
            while column in expanded:
                column += 1
            try:
                colspan = max(1, int(cell.get("colspan", 1)))
                rowspan = max(1, int(cell.get("rowspan", 1)))
            except (TypeError, ValueError):
                colspan = rowspan = 1
            for offset in range(colspan):
                target = column + offset
                expanded[target] = cell
                if rowspan > 1:
                    spans[target] = (rowspan - 1, cell)
            column += colspan
        if expanded:
            yield [expanded[i] for i in range(max(expanded) + 1)]


def header_key(value: str) -> str | None:
    value = value.casefold().strip()
    if "name of the examination" in value or value in {"examination", "exam name"}:
        return "exam"
    if "name of the subject" in value or value == "subject":
        return "subject"
    if value == "year" or "year of exam" in value:
        return "year"
    if "date of examination" in value:
        return "date"
    if "notification no" in value:
        return "notification"
    if "download" in value or value in {"view", "question paper"}:
        return "download"
    return None


def find_columns(table: Tag) -> dict[str, int]:
    for cells in row_cells(table):
        mapping = {key: index for index, cell in enumerate(cells) if (key := header_key(clean_text(cell)))}
        if "exam" in mapping and "download" in mapping:
            return mapping
    return {}


def ordered_items(cell: Tag) -> list[str]:
    items = [clean_text(item) for item in cell.find_all("li") if clean_text(item)]
    if items:
        return items
    blocks = [clean_text(item) for item in cell.find_all(["p", "div"], recursive=False) if clean_text(item)]
    return blocks or ([clean_text(cell)] if clean_text(cell) else [])


def pdf_anchors(cell: Tag) -> list[Tag]:
    anchors = []
    for anchor in cell.find_all("a", href=True):
        href = anchor["href"].strip()
        if href and not href.startswith(("#", "javascript:", "mailto:")):
            anchors.append(anchor)
    return anchors


def discover_semantic_tables(html: str, page_url: str) -> Iterator[Paper]:
    soup = BeautifulSoup(html, "html.parser")
    descriptive = "descriptive" in page_url.casefold()
    for table in soup.find_all("table"):
        columns = find_columns(table)
        required = {"exam", "subject", "year", "download"}
        if not required.issubset(columns):
            continue
        for cells in row_cells(table):
            if len(cells) <= max(columns.values()):
                continue
            exam_name = clean_text(cells[columns["exam"]])
            year_text = clean_text(cells[columns["year"]])
            year = extract_year(year_text, exam_name)
            if not exam_name or year is None or not START_YEAR <= year <= END_YEAR:
                continue
            subject_cell = cells[columns["subject"]]
            subjects = ordered_items(subject_cell)
            anchors = pdf_anchors(cells[columns["download"]])
            if not anchors:
                continue

            pairs: list[tuple[str, Tag]] = []
            if len(subjects) == len(anchors):
                pairs = list(zip(subjects, anchors))
            elif len(subjects) == 1 and GS_RE.search(subjects[0]):
                pairs = [(clean_text(anchor) or subjects[0], anchor) for anchor in anchors]
            else:
                pairs = [(clean_text(anchor), anchor) for anchor in anchors if GS_RE.search(clean_text(anchor))]

            for subject, anchor in pairs:
                if not GS_RE.search(f"{subject} {clean_text(anchor)}"):
                    continue
                pdf_url = urljoin(page_url, anchor["href"].strip())
                if urlparse(pdf_url).scheme not in {"http", "https"}:
                    continue
                stage = "Mains" if descriptive else "Preliminary"
                yield make_paper(
                    exam_name,
                    year,
                    stage,
                    "descriptive" if descriptive else "objective",
                    subject,
                    pdf_url,
                    page_url,
                    "descriptive_archive" if descriptive else "previous_questions_archive",
                )


def discover_objective_without_key(html: str, page_url: str) -> Iterator[Paper]:
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        columns = find_columns(table)
        if not {"exam", "download"}.issubset(columns):
            continue
        for cells in row_cells(table):
            if len(cells) <= max(columns.values()):
                continue
            exam_name = clean_text(cells[columns["exam"]])
            date_text = clean_text(cells[columns.get("date", 2)]) if len(cells) > 2 else ""
            notification = clean_text(cells[columns.get("notification", 0)])
            year = extract_year(date_text, notification, exam_name)
            if not exam_name or year is None or not START_YEAR <= year <= END_YEAR:
                continue
            row_text = " ".join(clean_text(cell) for cell in cells)
            stage = "Preliminary"
            for anchor in pdf_anchors(cells[columns["download"]]):
                subject = clean_text(anchor)
                href = anchor["href"].strip()
                if not GS_RE.search(subject) or not PDF_RE.search(href):
                    continue
                yield make_paper(
                    exam_name,
                    year,
                    stage,
                    "objective",
                    subject,
                    urljoin(page_url, href),
                    page_url,
                    "objective_without_key",
                )


def discover_answer_key_details(session: requests.Session, html: str, page_url: str) -> Iterator[Paper]:
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        for cells in row_cells(table):
            if len(cells) < 5:
                continue
            exam_name = clean_text(cells[2])
            exam_date = clean_text(cells[3])
            notification_date = clean_text(cells[1])
            year = extract_year(exam_date, notification_date, exam_name)
            if not exam_name or year is None or not START_YEAR <= year <= END_YEAR:
                continue
            row_text = f"{exam_name} {exam_date}"
            stage = "Preliminary"
            detail_urls = [
                urljoin(page_url, anchor["href"].strip())
                for anchor in cells[4].find_all("a", href=True)
                if "tentativesubjects" in anchor["href"].casefold()
            ]
            for detail_url in detail_urls:
                try:
                    response = session.get(detail_url, timeout=TIMEOUT)
                    response.raise_for_status()
                except requests.RequestException as exc:
                    log(f"WARN detail page failed: {detail_url}: {exc}")
                    continue
                detail = BeautifulSoup(response.text, "html.parser")
                for anchor in detail.find_all("a", href=True):
                    subject = clean_text(anchor)
                    href = anchor["href"].strip()
                    if not GS_RE.search(subject) or not PDF_RE.search(href):
                        continue
                    yield make_paper(
                        exam_name,
                        year,
                        stage,
                        "objective",
                        subject,
                        urljoin(response.url, href),
                        detail_url,
                        "answer_key_archive",
                    )


def discover_all(session: requests.Session) -> list[Paper]:
    found: dict[str, Paper] = {}
    for page_url in ARCHIVE_URLS:
        try:
            response = session.get(page_url, timeout=TIMEOUT)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or response.encoding
        except requests.RequestException as exc:
            log(f"WARN archive failed: {page_url}: {exc}")
            continue
        for paper in discover_semantic_tables(response.text, response.url):
            found.setdefault(paper.officialPdfUrl.casefold(), paper)
        if "question_paper_withoutkey" in response.url.casefold():
            for paper in discover_objective_without_key(response.text, response.url):
                found.setdefault(paper.officialPdfUrl.casefold(), paper)

    try:
        response = session.get(ANSWER_KEY_INDEX_URL, timeout=TIMEOUT)
        response.raise_for_status()
        for paper in discover_answer_key_details(session, response.text, response.url):
            found.setdefault(paper.officialPdfUrl.casefold(), paper)
    except requests.RequestException as exc:
        log(f"WARN answer-key index failed: {exc}")

    return sorted(found.values(), key=lambda item: (item.year, item.examGroup, item.examName.casefold(), item.id))


def manifest_payload(papers: Iterable[Paper]) -> dict:
    items = [asdict(paper) for paper in papers]
    return {
        "schemaVersion": 1,
        "source": "Tamil Nadu Public Service Commission official archives",
        "yearRange": {"start": START_YEAR, "end": END_YEAR},
        "paperCount": len(items),
        "papers": items,
    }


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_manifest(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_manifest(payload)
    return payload


def validate_manifest(payload: dict) -> None:
    papers = payload.get("papers")
    if payload.get("schemaVersion") != 1 or not isinstance(papers, list):
        raise ValueError("Manifest must contain schemaVersion=1 and a papers array")
    ids: set[str] = set()
    urls: set[str] = set()
    required = {
        "id", "examName", "year", "stage", "paperType", "examGroup", "subject",
        "officialPdfUrl", "officialPageUrl", "sourcePath", "draftPath",
    }
    for index, paper in enumerate(papers):
        missing = required.difference(paper)
        if missing:
            raise ValueError(f"Paper {index} missing fields: {sorted(missing)}")
        if paper["id"] in ids:
            raise ValueError(f"Duplicate paper id: {paper['id']}")
        if paper["officialPdfUrl"].casefold() in urls:
            raise ValueError(f"Duplicate source URL: {paper['officialPdfUrl']}")
        if not START_YEAR <= int(paper["year"]) <= END_YEAR:
            raise ValueError(f"Out-of-range year: {paper['year']}")
        ids.add(paper["id"])
        urls.add(paper["officialPdfUrl"].casefold())


def download_pdf(session: requests.Session, url: str, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with session.get(url, stream=True, timeout=TIMEOUT) as response:
        response.raise_for_status()
        iterator = response.iter_content(chunk_size=CHUNK_SIZE)
        first = next(iterator, b"")
        if not first.startswith(b"%PDF-"):
            raise ValueError("URL did not return a PDF")
        with destination.open("wb") as handle:
            handle.write(first)
            digest.update(first)
            for chunk in iterator:
                if chunk:
                    handle.write(chunk)
                    digest.update(chunk)
    return digest.hexdigest()


def text_quality(text: str) -> float:
    if not text.strip():
        return 0.0
    printable = sum(char.isprintable() or char in "\n\t" for char in text)
    replacements = text.count("�") + sum(1 for char in text if ord(char) < 32 and char not in "\n\t\r")
    length_score = min(1.0, len(text.strip()) / 800.0)
    cleanliness = max(0.0, (printable - replacements * 5) / max(1, len(text)))
    return round(length_score * cleanliness, 4)


def ocr_page(page: fitz.Page, temp_dir: Path) -> str:
    if shutil.which("tesseract") is None:
        return ""
    pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
    image_path = temp_dir / f"page-{page.number + 1:04d}.png"
    pix.save(image_path)
    result = subprocess.run(
        ["tesseract", str(image_path), "stdout", "-l", "eng+tam", "--psm", "6"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def extract_pages(pdf_path: Path, force_ocr: bool = False) -> tuple[list[dict], dict]:
    pages: list[dict] = []
    ocr_pages = 0
    with fitz.open(pdf_path) as document, tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)
        for page in document:
            embedded = page.get_text("text", sort=True)
            quality = text_quality(embedded)
            method = "embedded"
            text = embedded
            if force_ocr or quality < 0.45:
                ocr_text = ocr_page(page, temp_dir)
                if text_quality(ocr_text) > quality:
                    text = ocr_text
                    quality = text_quality(text)
                    method = "tesseract-eng+tam"
                    ocr_pages += 1
            pages.append({"page": page.number + 1, "method": method, "quality": quality, "text": text})
    average = round(sum(item["quality"] for item in pages) / max(1, len(pages)), 4)
    return pages, {"pageCount": len(pages), "ocrPageCount": ocr_pages, "averageTextQuality": average}


def load_tags() -> list[dict]:
    return json.loads(TAG_CONFIG.read_text(encoding="utf-8"))


def tag_question(text: str, tags: list[dict]) -> dict:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    scored = []
    for entry in tags:
        hits = [keyword for keyword in entry["keywords"] if keyword.casefold() in normalized]
        if hits:
            scored.append((len(hits), max(map(len, hits)), entry, hits))
    if not scored:
        return {"unitId": "unmapped", "subject": "Unmapped subject", "subtopic": "Unmapped subtopic", "matchedKeywords": [], "confidence": 0.0}
    _, _, entry, hits = max(scored, key=lambda item: (item[0], item[1]))
    confidence = min(0.95, 0.45 + 0.12 * len(hits))
    return {"unitId": entry["unitId"], "subject": entry["subject"], "subtopic": "Unmapped subtopic", "matchedKeywords": hits[:8], "confidence": round(confidence, 2)}


def detect_language(text: str) -> str:
    tamil = len(re.findall(r"[\u0b80-\u0bff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if tamil and latin:
        return "bilingual"
    if tamil:
        return "ta"
    if latin:
        return "en"
    return "unknown"


STATEMENT_LINE_RE = re.compile(r"^\s*(\(?[0-9IVXivx]+\)?)[.)-]\s+(.+?)\s*$")
MATCH_ROW_RE = re.compile(
    r"^\s*(\(?[A-Za-z0-9]+\)?)[.)-]?\s+(.+?)\s{4,}[.]?\s*(\(?[A-Za-z0-9]+\)?)[.)-]?\s+(.+?)\s*$"
)


def question_layout(question_text: str) -> dict:
    raw_lines = [line.rstrip() for line in question_text.splitlines()]
    display_lines = [line.strip() for line in raw_lines if line.strip()]
    joined = "\n".join(display_lines)
    folded = joined.casefold()
    question_type = "standard"
    structured: dict = {}

    assertion_match = re.search(
        r"(?is)(?:\bassertion(?:\s*\([aA]\))?|கூற்று)\s*[:.-]\s*(.+?)"
        r"(?=(?:\breason(?:\s*\([rR]\))?|காரணம்)\s*[:.-])",
        joined,
    )
    reason_match = re.search(
        r"(?is)(?:\breason(?:\s*\([rR]\))?|காரணம்)\s*[:.-]\s*(.+)$",
        joined,
    )
    if assertion_match and reason_match:
        question_type = "assertion_reason"
        structured = {
            "assertion": assertion_match.group(1).strip(),
            "reason": reason_match.group(1).strip(),
        }
    elif any(marker in folded for marker in ("match the following", "match the pairs", "list i", "list-i", "பொருத்துக")):
        question_type = "match_following"
        parsed_rows = []
        for line in display_lines:
            row = MATCH_ROW_RE.match(line)
            if row:
                left_text = row.group(2).strip()
                right_text = row.group(4).strip()
                if re.fullmatch(r"\(?[A-Za-z0-9]+\)?", left_text) and re.fullmatch(r"\(?[A-Za-z0-9]+\)?", right_text):
                    continue
                parsed_rows.append(
                    {
                        "leftLabel": row.group(1).strip("()"),
                        "leftText": left_text,
                        "rightLabel": row.group(3).strip("()"),
                        "rightText": right_text,
                    }
                )
        structured = {"parsedRows": parsed_rows, "rawRows": display_lines}
    else:
        statements = []
        for line in display_lines:
            statement = STATEMENT_LINE_RE.match(line)
            if statement:
                statements.append(
                    {"label": statement.group(1).strip("()"), "text": statement.group(2).strip()}
                )
        if len(statements) >= 2:
            question_type = "multiple_statement"
            structured = {"statements": statements}

    return {
        "version": 1,
        "type": question_type,
        "displayMode": "preformatted",
        "preserveLineBreaks": True,
        "rawText": question_text,
        "rawLines": raw_lines,
        "structured": structured,
    }
def parse_questions(pages: list[dict], paper: dict) -> list[dict]:
    tags = load_tags()
    questions: list[dict] = []
    seen_numbers: set[int] = set()
    for page in pages:
        text = page["text"]
        matches = list(TOP_QUESTION_RE.finditer(text)) or list(QUESTION_RE.finditer(text))
        for index, match in enumerate(matches):
            number = int(match.group(1))
            if number < 1 or number > 250 or number in seen_numbers:
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            block = text[match.end():end].strip()
            if len(block) < 15:
                continue
            option_matches = list(OPTION_RE.finditer(block))
            question_text = block[: option_matches[0].start()].strip() if option_matches else block
            options = []
            marked = ""
            for option_index, option_match in enumerate(option_matches[:4]):
                option_end = option_matches[option_index + 1].start() if option_index + 1 < len(option_matches) else len(block)
                label = option_match.group(1) or option_match.group(2) or ""
                value = block[option_match.end():option_end].strip()
                canonical = chr(ord("A") + int(label) - 1) if label.isdigit() else label.upper()
                if MARK_RE.search(value) or MARK_RE.search(option_match.group(0)):
                    marked = canonical
                options.append({"label": canonical, "text": MARK_RE.sub("", value).strip()})
            layout = question_layout(question_text)
            language = detect_language(question_text)
            tag = tag_question(f"{question_text} {' '.join(item['text'] for item in options)}", tags)
            structure_confidence = 0.92 if len(options) == 4 else 0.55 if options else 0.3
            seen_numbers.add(number)
            questions.append(
                {
                    "id": f"{paper['id']}-q{number:03d}",
                    "testId": paper["id"],
                    "questionNumber": number,
                    "page": page["page"],
                    "language": language,
                    "questionTextRaw": question_text,
                    "questionTextEn": question_text if language in {"en", "bilingual"} else "",
                    "questionTextTa": question_text if language in {"ta", "bilingual"} else "",
                    "questionType": layout["type"],
                    "layout": layout,
                    "options": options,
                    "correctOption": marked,
                    "subject": tag["subject"],
                    "unitId": tag["unitId"],
                    "subtopic": tag["subtopic"],
                    "matchedKeywords": tag["matchedKeywords"],
                    "source": {"officialPdfUrl": paper["officialPdfUrl"], "page": page["page"]},
                    "confidence": {
                        "text": page["quality"],
                        "structure": structure_confidence,
                        "answer": 0.9 if marked else 0.0,
                        "tagging": tag["confidence"],
                    },
                    "reviewStatus": "needs_review",
                }
            )
    return sorted(questions, key=lambda item: item["questionNumber"])


def extract_draft(pdf_path: Path, paper: dict, sha256: str, force_ocr: bool = False) -> dict:
    pages, audit = extract_pages(pdf_path, force_ocr=force_ocr)
    questions = parse_questions(pages, paper)
    return {
        "schemaVersion": 2,
        "paper": paper,
        "sourceSha256": sha256,
        "extraction": {
            **audit,
            "questionCount": len(questions),
            "answerCount": sum(bool(item["correctOption"]) for item in questions),
            "requiresHumanReview": True,
            "paidApiUsed": False,
            "layoutPreserved": True,
        },
        "questions": questions,
    }


def hf_exists(api: HfApi, repo_id: str, path: str, token: str) -> bool:
    return api.file_exists(
        repo_id=repo_id,
        filename=path,
        repo_type="dataset",
        token=token,
    )


def upload_bytes(api: HfApi, repo_id: str, token: str, path: str, data: bytes, message: str) -> None:
    api.upload_file(
        path_or_fileobj=data,
        path_in_repo=path,
        repo_id=repo_id,
        repo_type="dataset",
        token=token,
        commit_message=message,
    )


def bootstrap_dataset(api: HfApi, repo_id: str, token: str, manifest_path: Path) -> None:
    api.upload_file(
        path_or_fileobj=str(ROOT / "dataset" / "README.md"),
        path_in_repo="README.md",
        repo_id=repo_id,
        repo_type="dataset",
        token=token,
        commit_message="Document TNPSC PYQ dataset",
    )
    api.upload_file(
        path_or_fileobj=str(manifest_path),
        path_in_repo="manifest.json",
        repo_id=repo_id,
        repo_type="dataset",
        token=token,
        commit_message="Update official TNPSC source manifest",
    )


def sync_missing(manifest_path: Path, limit: int, start: int, force_ocr: bool) -> dict:
    token = os.environ.get("HF_TOKEN", "").strip()
    repo_id = os.environ.get("HF_DATASET_REPO", "").strip()
    if not token or not repo_id:
        raise RuntimeError("HF_TOKEN and HF_DATASET_REPO are required")
    manifest = load_manifest(manifest_path)
    api = HfApi(token=token)
    bootstrap_dataset(api, repo_id, token, manifest_path)
    selected = manifest["papers"][max(0, start):]
    completed = skipped = failed = 0
    session = build_session()
    with tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)
        for paper in selected:
            if completed >= limit:
                break
            source_exists = hf_exists(api, repo_id, paper["sourcePath"], token)
            draft_exists = hf_exists(api, repo_id, paper["draftPath"], token)
            if source_exists and draft_exists:
                skipped += 1
                continue
            pdf_path = temp_dir / f"{paper['id']}.pdf"
            try:
                log(f"Processing {paper['year']} | {paper['examName']} | {paper['subject']}")
                sha256 = download_pdf(session, paper["officialPdfUrl"], pdf_path)
                if not source_exists:
                    api.upload_file(
                        path_or_fileobj=str(pdf_path),
                        path_in_repo=paper["sourcePath"],
                        repo_id=repo_id,
                        repo_type="dataset",
                        token=token,
                        commit_message=f"Archive TNPSC source {paper['id']}",
                    )
                if not draft_exists:
                    draft = extract_draft(pdf_path, paper, sha256, force_ocr=force_ocr)
                    compressed = gzip.compress(
                        (json.dumps(draft, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"),
                        compresslevel=9,
                    )
                    upload_bytes(api, repo_id, token, paper["draftPath"], compressed, f"Add extraction draft {paper['id']}")
                completed += 1
                log(f"Completed {paper['id']}")
            except Exception as exc:
                failed += 1
                log(f"ERROR {paper['id']}: {exc}")
            finally:
                pdf_path.unlink(missing_ok=True)
    return {"processed": completed, "skippedExisting": skipped, "failed": failed}


def extract_missing_to_directory(
    manifest_path: Path,
    output_root: Path,
    limit: int,
    start: int,
    force_ocr: bool,
    shard_index: int = 0,
    shard_count: int = 1,
) -> dict:
    if shard_count < 1 or shard_index < 0 or shard_index >= shard_count:
        raise ValueError("Invalid shard index/count")
    manifest = load_manifest(manifest_path)
    selected = manifest["papers"][max(0, start):][shard_index::shard_count]
    completed = skipped = failed = 0
    session = build_session()
    with tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)
        for paper in selected:
            if completed >= limit:
                break
            draft_path = output_root / paper["draftPath"]
            if draft_path.is_file() and draft_path.stat().st_size > 0:
                skipped += 1
                continue
            pdf_path = temp_dir / f"{paper['id']}.pdf"
            partial = draft_path.with_suffix(draft_path.suffix + ".part")
            try:
                log(f"Processing {paper['year']} | {paper['examName']} | {paper['subject']}")
                sha256 = download_pdf(session, paper["officialPdfUrl"], pdf_path)
                draft = extract_draft(pdf_path, paper, sha256, force_ocr=force_ocr)
                compressed = gzip.compress(
                    (json.dumps(draft, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"),
                    compresslevel=9,
                )
                draft_path.parent.mkdir(parents=True, exist_ok=True)
                partial.write_bytes(compressed)
                partial.replace(draft_path)
                completed += 1
                log(f"Completed {paper['id']} -> {draft_path}")
            except Exception as exc:
                failed += 1
                partial.unlink(missing_ok=True)
                log(f"ERROR {paper['id']}: {exc}")
            finally:
                pdf_path.unlink(missing_ok=True)
    return {"processed": completed, "skippedExisting": skipped, "failed": failed}


def command_sync_github(args: argparse.Namespace) -> int:
    summary = extract_missing_to_directory(
        args.manifest,
        args.output_root,
        args.limit,
        args.start,
        args.force_ocr,
        args.shard_index,
        args.shard_count,
    )
    log(json.dumps(summary, indent=2))
    return 1 if summary["failed"] and not summary["processed"] else 0
def command_discover(args: argparse.Namespace) -> int:
    with build_session() as session:
        papers = discover_all(session)
    if not papers:
        raise RuntimeError("No TNPSC GS papers were discovered")
    payload = manifest_payload(papers)
    validate_manifest(payload)
    write_json(args.output, payload)
    log(f"Discovered {len(papers)} unique GS PDFs -> {args.output}")
    return 0


def command_validate(args: argparse.Namespace) -> int:
    payload = load_manifest(args.manifest)
    groups: dict[str, int] = {}
    stages: dict[str, int] = {}
    for paper in payload["papers"]:
        groups[paper["examGroup"]] = groups.get(paper["examGroup"], 0) + 1
        stages[paper["stage"]] = stages.get(paper["stage"], 0) + 1
    log(json.dumps({"paperCount": len(payload["papers"]), "groups": groups, "stages": stages}, indent=2))
    return 0


def command_extract(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    paper = next((item for item in manifest["papers"] if item["id"] == args.paper_id), None)
    if not paper:
        raise ValueError(f"Unknown paper id: {args.paper_id}")
    digest = hashlib.sha256(args.pdf.read_bytes()).hexdigest()
    draft = extract_draft(args.pdf, paper, digest, force_ocr=args.force_ocr)
    write_json(args.output, draft)
    log(f"Extracted {draft['extraction']['questionCount']} draft questions -> {args.output}")
    return 0


def command_sync(args: argparse.Namespace) -> int:
    summary = sync_missing(args.manifest, args.limit, args.start, args.force_ocr)
    log(json.dumps(summary, indent=2))
    return 1 if summary["failed"] and not summary["processed"] else 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    discover = commands.add_parser("discover", help="crawl official TNPSC archives")
    discover.add_argument("--output", type=Path, default=DEFAULT_MANIFEST)
    discover.set_defaults(handler=command_discover)
    validate = commands.add_parser("validate", help="validate and summarize a manifest")
    validate.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    validate.set_defaults(handler=command_validate)
    extract = commands.add_parser("extract", help="extract one local PDF into a review draft")
    extract.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    extract.add_argument("--paper-id", required=True)
    extract.add_argument("--pdf", type=Path, required=True)
    extract.add_argument("--output", type=Path, required=True)
    extract.add_argument("--force-ocr", action="store_true")
    extract.set_defaults(handler=command_extract)
    sync = commands.add_parser("sync", help="archive and extract missing papers to Hugging Face")
    sync.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    sync.add_argument("--limit", type=int, default=3)
    sync.add_argument("--start", type=int, default=0)
    sync.add_argument("--force-ocr", action="store_true")
    sync.set_defaults(handler=command_sync)
    github = commands.add_parser("sync-github", help="extract missing mirrored papers into the repository")
    github.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    github.add_argument("--output-root", type=Path, default=ROOT / "data")
    github.add_argument("--limit", type=int, default=3)
    github.add_argument("--start", type=int, default=0)
    github.add_argument("--force-ocr", action="store_true")
    github.add_argument("--shard-index", type=int, default=0)
    github.add_argument("--shard-count", type=int, default=1)
    github.set_defaults(handler=command_sync_github)
    return root


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args()
    started = time.monotonic()
    try:
        result = args.handler(args)
    except Exception as exc:
        log(f"FATAL: {exc}")
        return 1
    log(f"Finished in {time.monotonic() - started:.1f}s")
    return result


if __name__ == "__main__":
    raise SystemExit(main())

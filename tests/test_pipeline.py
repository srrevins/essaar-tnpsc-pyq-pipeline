import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_mirror_manifest
import pipeline


class PipelineTests(unittest.TestCase):
    def test_url_hash_prevents_same_exam_year_collision(self):
        first = pipeline.make_paper("Group I", 2024, "Mains", "descriptive", "General Studies II", "https://example.test/a.pdf", "https://example.test", "test")
        second = pipeline.make_paper("Group I", 2024, "Mains", "descriptive", "General Studies II", "https://example.test/b.pdf", "https://example.test", "test")
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.sourcePath, second.sourcePath)

    def test_question_parser_and_rule_tagger(self):
        paper = pipeline.make_paper("Group I", 2024, "Preliminary", "objective", "General Studies", "https://example.test/paper.pdf", "https://example.test", "test")
        pages = [{
            "page": 3,
            "method": "embedded",
            "quality": 0.95,
            "text": "1. Which Article protects Fundamental Rights?\n(A) Article 12\n(B) Article 32\n(C) Article 40\n(D) Article 50\n(E) Answer not known\n2. The RBI primarily regulates which sector?\n(A) Agriculture\n(B) Banking\n(C) Judiciary\n(D) Education\n(E) Answer not known\n",
        }]
        questions = pipeline.parse_questions(pages, pipeline.asdict(paper))
        self.assertEqual([item["questionNumber"] for item in questions], [1, 2])
        self.assertEqual(questions[0]["subject"], "Indian Polity")
        self.assertEqual(questions[1]["subject"], "Indian Economy")
        # Answers come from the official final key, never from the paper text.
        self.assertEqual(questions[0]["correctOption"], "")

    def test_all_objective_archive_rows_are_preliminary(self):
        html = """<table><tr><th>Name of the Examination</th><th>Date of Examination</th><th>Download</th></tr><tr><td>Group IV Services</td><td>01/01/2024</td><td><a href="/paper.pdf">General Studies</a></td></tr></table>"""
        papers = list(pipeline.discover_objective_without_key(html, "https://www.tnpsc.gov.in/archive.html"))
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].stage, "Preliminary")
        self.assertEqual(papers[0].paperType, "objective")

    def test_release_filename_metadata(self):
        objective = build_mirror_manifest.metadata_from_filename(
            "TNPSC_Group_IV_Services_Objective_2024_GS.pdf"
        )
        mains = build_mirror_manifest.metadata_from_filename(
            "TNPSC_Group_I_Main_Written_Examination_2023_GS.pdf"
        )
        self.assertEqual(objective, ("Group IV Services", 2024, "Preliminary", "objective"))
        self.assertEqual(mains, ("Group I", 2023, "Mains", "descriptive"))

    def test_structured_question_layouts_preserve_raw_lines(self):
        assertion = pipeline.question_layout(
            "Assertion (A): The Constitution is supreme.\nReason (R): All laws derive authority from it."
        )
        matching = pipeline.question_layout(
            "Match the following:\n1. Article 14    A. Equality\n2. Article 21    B. Life and liberty"
        )
        statements = pipeline.question_layout(
            "Consider the following statements:\n1. The RBI issues currency.\n2. SEBI regulates securities."
        )
        self.assertEqual(assertion["type"], "assertion_reason")
        self.assertEqual(assertion["structured"]["assertion"], "The Constitution is supreme.")
        self.assertEqual(matching["type"], "match_following")
        self.assertEqual(len(matching["structured"]["parsedRows"]), 2)
        self.assertEqual(statements["type"], "multiple_statement")
        self.assertEqual([item["label"] for item in statements["structured"]["statements"]], ["1", "2"])
        self.assertTrue(statements["preserveLineBreaks"])
        self.assertEqual(len(statements["rawLines"]), 3)

    def test_shards_select_non_overlapping_papers(self):
        papers = [
            pipeline.make_paper("Exam", 2020 + index, "Preliminary", "objective", "General Studies", f"https://example.test/{index}.pdf", "https://example.test", "test")
            for index in range(4)
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps(pipeline.manifest_payload(papers)), encoding="utf-8")

            def fake_download(_session, _url, destination):
                destination.write_bytes(b"%PDF-test")
                return "abc123"

            with mock.patch.object(pipeline, "download_pdf", side_effect=fake_download), mock.patch.object(pipeline, "extract_draft", return_value={"schemaVersion": 2, "questions": []}):
                summary = pipeline.extract_missing_to_directory(manifest, root / "data", 10, 0, False, 1, 2)

            self.assertEqual(summary["processed"], 2)
            self.assertFalse((root / "data" / papers[0].draftPath).exists())
            self.assertTrue((root / "data" / papers[1].draftPath).exists())
            self.assertFalse((root / "data" / papers[2].draftPath).exists())
            self.assertTrue((root / "data" / papers[3].draftPath).exists())

    def test_github_output_is_atomic_and_skips_existing(self):
        paper = pipeline.make_paper("Group IV", 2024, "Preliminary", "objective", "General Studies", "https://example.test/paper.pdf", "https://example.test", "test")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps(pipeline.manifest_payload([paper])), encoding="utf-8")

            def fake_download(_session, _url, destination):
                destination.write_bytes(b"%PDF-test")
                return "abc123"

            fake_draft = {"schemaVersion": 1, "questions": [], "paper": pipeline.asdict(paper)}
            with mock.patch.object(pipeline, "download_pdf", side_effect=fake_download), mock.patch.object(pipeline, "extract_draft", return_value=fake_draft):
                first = pipeline.extract_missing_to_directory(manifest, root / "data", 1, 0, False)
                second = pipeline.extract_missing_to_directory(manifest, root / "data", 1, 0, False)

            output = root / "data" / paper.draftPath
            self.assertEqual(first["processed"], 1)
            self.assertEqual(second["skippedExisting"], 1)
            self.assertEqual(json.loads(gzip.decompress(output.read_bytes())), fake_draft)
            self.assertFalse(output.with_suffix(output.suffix + ".part").exists())

    def test_manifest_validation_rejects_duplicate_url(self):
        paper = pipeline.make_paper("Group I", 2024, "Preliminary", "objective", "General Studies", "https://example.test/paper.pdf", "https://example.test", "test")
        payload = pipeline.manifest_payload([paper, paper])
        with self.assertRaises(ValueError):
            pipeline.validate_manifest(payload)


class OptionParsingTests(unittest.TestCase):
    """Behaviour pinned against real Tesseract output, not synthetic text.

    The fixture is page 13 of the CCSE-I 2021 Group I paper OCRed with
    `-l eng+tam --psm 6`, the same call the pipeline makes. Its three questions
    cover the layouts that used to break the parser: a two-across option grid,
    a label destroyed by the examiner's highlighter, and an option whose own
    text contains a parenthesised fragment.
    """

    @classmethod
    def setUpClass(cls):
        fixture = Path(__file__).resolve().parent / "fixtures" / "ccse1-2021-page013.ocr.txt"
        paper = pipeline.make_paper("Group I", 2021, "Preliminary", "objective", "General Studies", "https://example.test/ccse1-2021.pdf", "https://example.test", "test")
        pages = [{"page": 13, "method": "tesseract-eng+tam", "quality": 0.9, "text": fixture.read_text(encoding="utf-8")}]
        cls.questions = pipeline.parse_questions(pages, pipeline.asdict(paper))
        cls.by_number = {item["questionNumber"]: item for item in cls.questions}

    def test_every_question_recovers_all_five_english_options(self):
        self.assertEqual(sorted(self.by_number), [16, 17, 18])
        for number, question in self.by_number.items():
            with self.subTest(question=number):
                self.assertEqual(
                    [option["label"] for option in question["options"]],
                    list("ABCDE"),
                )

    def test_two_across_option_grid_is_split(self):
        # "(A) Hindi and Urdu    (B) Hindi and Sindhi" sits on one printed line.
        options = {item["label"]: item["text"] for item in self.by_number[16]["options"]}
        self.assertEqual(options["A"], "Hindi and Urdu")
        self.assertEqual(options["C"], "Persian and Urdu")
        self.assertEqual(options["D"], "Sanskrit and Hindi")

    def test_parenthesised_text_stays_with_its_own_option(self):
        # "(A) Article 16 (4)" must not be split at the "(4)".
        options = {item["label"]: item["text"] for item in self.by_number[17]["options"]}
        self.assertEqual(options["A"], "Article 16 (4)")
        self.assertEqual(options["B"], "Article 17")

    def test_destroyed_label_is_recovered_and_flagged(self):
        # The highlighter sits on the correct option, so OCR returns junk for
        # that label. The official final key gives 16 A, 17 B, 18 A.
        self.assertEqual(self.by_number[16]["markedCandidates"], ["A"])
        self.assertEqual(self.by_number[17]["markedCandidates"], ["B"])
        self.assertEqual(self.by_number[18]["markedCandidates"], ["A"])

    def test_bilingual_halves_are_separated(self):
        for number, question in self.by_number.items():
            with self.subTest(question=number):
                self.assertEqual(question["language"], "bilingual")
                self.assertNotIn("Answer not known", question["questionTextTa"])
                self.assertRegex(question["questionTextTa"], r"[஀-௿]")
                self.assertNotRegex(question["questionTextEn"], r"[஀-௿]")

    def test_answers_are_not_taken_from_the_paper(self):
        for question in self.questions:
            self.assertEqual(question["correctOption"], "")
            self.assertEqual(question["confidence"]["answer"], 0.0)


if __name__ == "__main__":
    unittest.main()
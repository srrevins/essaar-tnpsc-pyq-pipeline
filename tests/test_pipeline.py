import sys
import tempfile
import unittest
from pathlib import Path

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
            "text": "1. Which Article protects Fundamental Rights?\n(A) Article 12\n(B) Article 32 ✓\n(C) Article 40\n(D) Article 50\n2. The RBI primarily regulates which sector?\n(A) Agriculture\n(B) Banking\n(C) Judiciary\n(D) Education\n",
        }]
        questions = pipeline.parse_questions(pages, pipeline.asdict(paper))
        self.assertEqual([item["questionNumber"] for item in questions], [1, 2])
        self.assertEqual(questions[0]["correctOption"], "B")
        self.assertEqual(questions[0]["subject"], "Indian Polity")
        self.assertEqual(questions[1]["subject"], "Indian Economy")

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
    def test_manifest_validation_rejects_duplicate_url(self):
        paper = pipeline.make_paper("Group I", 2024, "Preliminary", "objective", "General Studies", "https://example.test/paper.pdf", "https://example.test", "test")
        payload = pipeline.manifest_payload([paper, paper])
        with self.assertRaises(ValueError):
            pipeline.validate_manifest(payload)


if __name__ == "__main__":
    unittest.main()

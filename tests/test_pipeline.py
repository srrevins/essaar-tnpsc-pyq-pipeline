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
        # Unit names are the official ones, so they match the site's taxonomy.
        self.assertEqual(questions[0]["subject"], "Unit 5: Indian Polity")
        self.assertEqual(questions[1]["subject"], "Unit 6: Indian Economy")
        # And a subtopic is now produced, not just a unit.
        self.assertNotEqual(questions[0]["subtopic"], "Unmapped subtopic")
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

class AnswerKeyTests(unittest.TestCase):
    """Pinned against real OCR of the official CCSE-I 2021 final answer key.

    Two fixtures, because the key needs two passes: a character whitelist
    reads the table cleanly but destroys the header, and an unconstrained pass
    reads the header. Both are Tesseract output, not idealised text.
    """

    FIXTURES = Path(__file__).resolve().parent / "fixtures"

    @classmethod
    def setUpClass(cls):
        cls.table = (cls.FIXTURES / "ccse1-2021-final-key.whitelist.ocr.txt").read_text(encoding="utf-8")
        cls.plain = (cls.FIXTURES / "ccse1-2021-final-key.plain.ocr.txt").read_text(encoding="utf-8")
        cls.answers, cls.unreadable = pipeline.parse_answer_key(cls.table)
        cls.meta = pipeline.answer_key_metadata(cls.plain)

    def test_reads_most_of_the_grid(self):
        self.assertGreaterEqual(len(self.answers), 175)
        self.assertEqual(len(self.answers) + len(self.unreadable), 200)

    def test_reads_cells_from_every_column(self):
        # Row 1 spans all five columns: questions 1, 41, 81, 121 and 161.
        self.assertEqual(self.answers[1], ["D"])
        self.assertEqual(self.answers[41], ["A"])
        self.assertEqual(self.answers[81], ["B"])
        self.assertEqual(self.answers[121], ["B"])
        self.assertEqual(self.answers[161], ["A"])
        self.assertEqual(self.answers[200], ["B"])

    def test_disputed_questions_keep_every_accepted_option(self):
        self.assertEqual(self.answers[16], ["A", "D"])
        self.assertEqual(self.answers[47], ["A", "D"])
        self.assertEqual(self.answers[56], ["A", "B", "C"])
        self.assertEqual(self.answers[64], ["A", "B", "C", "D"])

    def test_withdrawn_questions_accept_all_options(self):
        # "ALL" means the question was withdrawn and every candidate marked.
        self.assertEqual(self.answers[48], list("ABCDE"))

    def test_unreadable_cells_are_reported_not_guessed(self):
        for question in self.unreadable:
            self.assertNotIn(question, self.answers)

    def test_metadata_identifies_the_paper(self):
        self.assertEqual(self.meta["qbCode"], "GR1P21")
        self.assertEqual(self.meta["versionKey"], "D")
        self.assertEqual(self.meta["subjectCode"], "003")

    def test_paper_code_comes_from_repeated_footers(self):
        pages = [{"text": "some question text\nGR1P/21 7 va)"} for _ in range(4)]
        self.assertEqual(pipeline.paper_qb_code(pages), "GR1P21")
        # A code seen only once is not enough to decide which key to trust.
        self.assertEqual(pipeline.paper_qb_code(pages[:1]), "")
        self.assertEqual(pipeline.paper_qb_code([{"text": "no code here"}]), "")

    def test_merge_prefers_the_majority_and_flags_a_split(self):
        merged, contested = pipeline.merge_answer_keys([
            {1: ["A"], 2: ["B"]},
            {1: ["A"], 2: ["C"]},
            {1: ["D"]},
        ])
        self.assertEqual(merged[1], ["A"])
        self.assertNotIn(1, contested)
        # Two passes, two readings: the more trusted one is kept but flagged.
        self.assertEqual(merged[2], ["B"])
        self.assertIn(2, contested)


class AnswerKeyJoinTests(unittest.TestCase):
    """The join must refuse any key it cannot prove belongs to the paper."""

    def build(self, marked=None):
        return [{
            "questionNumber": 1,
            "markedCandidates": marked if marked is not None else [],
            "confidence": {"text": 0.9, "structure": 1.0, "answer": 0.0, "tagging": 0.5},
        }]

    def test_refuses_a_key_from_another_booklet_version(self):
        questions = self.build()
        report = pipeline.join_answer_key(questions, {1: ["A"]}, {"qbCode": "GR1P21"}, "CCS1P22")
        self.assertFalse(report["joined"])
        self.assertEqual(report["reason"], "qb_code_mismatch")
        self.assertNotIn("acceptedOptions", questions[0])

    def test_refuses_when_either_code_is_unreadable(self):
        for key_code, paper_code in (("", "GR1P21"), ("GR1P21", "")):
            with self.subTest(key=key_code, paper=paper_code):
                questions = self.build()
                report = pipeline.join_answer_key(questions, {1: ["A"]}, {"qbCode": key_code}, paper_code)
                self.assertFalse(report["joined"])
                self.assertEqual(report["reason"], "qb_code_unreadable")
                self.assertNotIn("acceptedOptions", questions[0])

    def test_applies_the_key_when_the_codes_agree(self):
        questions = self.build()
        report = pipeline.join_answer_key(questions, {1: ["C"]}, {"qbCode": "GR1P21", "versionKey": "D"}, "GR1P21")
        self.assertTrue(report["joined"])
        self.assertEqual(questions[0]["correctOption"], "C")
        self.assertEqual(questions[0]["acceptedOptions"], ["C"])
        self.assertFalse(questions[0]["allCorrect"])
        self.assertEqual(questions[0]["answerSource"], "tnpsc_final_answer_key")
        self.assertEqual(questions[0]["answerKeyVersion"], "D")

    def test_a_disputed_question_has_no_single_correct_option(self):
        questions = self.build()
        pipeline.join_answer_key(questions, {1: ["A", "D"]}, {"qbCode": "X"}, "X")
        self.assertEqual(questions[0]["acceptedOptions"], ["A", "D"])
        self.assertEqual(questions[0]["correctOption"], "")
        self.assertFalse(questions[0]["allCorrect"])

    def test_a_withdrawn_question_is_marked_all_correct(self):
        questions = self.build()
        pipeline.join_answer_key(questions, {1: list("ABCDE")}, {"qbCode": "X"}, "X")
        self.assertTrue(questions[0]["allCorrect"])
        self.assertEqual(questions[0]["correctOption"], "")

    def test_the_highlighter_hint_corroborates_or_contradicts_the_key(self):
        agreeing = self.build(marked=["C"])
        report = pipeline.join_answer_key(agreeing, {1: ["C"]}, {"qbCode": "X"}, "X")
        self.assertEqual(report["corroborated"], 1)
        self.assertEqual(agreeing[0]["confidence"]["answer"], 1.0)

        conflicting = self.build(marked=["A"])
        report = pipeline.join_answer_key(conflicting, {1: ["C"]}, {"qbCode": "X"}, "X")
        self.assertEqual(report["contradicted"], [1])
        # The key is authoritative; the hint only lowers confidence for review.
        self.assertEqual(conflicting[0]["correctOption"], "C")
        self.assertEqual(conflicting[0]["confidence"]["answer"], 0.5)


class SyllabusTaggerTests(unittest.TestCase):
    TAGS = [
        {"unitId": "unit-4", "subject": "Unit 4: History and Culture of India",
         "subtopic": "Art and Architecture", "keywords": ["art", "temple architecture", "chola"]},
        {"unitId": "unit-5", "subject": "Unit 5: Indian Polity",
         "subtopic": "Fundamental Rights", "keywords": ["fundamental rights", "article 32", "writ"]},
    ]

    def test_matches_whole_words_only(self):
        # Substring matching pulled any question mentioning a part, a chart or
        # the Charter Act into "Art and Architecture".
        result = pipeline.tag_question("Which part of the chart shows the Charter Act?", self.TAGS)
        self.assertEqual(result["unitId"], "unmapped")

    def test_assigns_a_subtopic_not_just_a_unit(self):
        result = pipeline.tag_question(
            "Which article guarantees fundamental rights, and which writ enforces them?", self.TAGS)
        self.assertEqual(result["subject"], "Unit 5: Indian Polity")
        self.assertEqual(result["subtopic"], "Fundamental Rights")
        self.assertGreaterEqual(result["confidence"], pipeline.TAG_CONFIDENCE_FLOOR)

    def test_one_short_incidental_word_is_not_a_topic(self):
        result = pipeline.tag_question("A discussion of art.", self.TAGS)
        self.assertEqual(result["unitId"], "unmapped")
        # What was matched is still reported, so a near miss can be reviewed.
        self.assertEqual(result["matchedKeywords"], ["art"])

    def test_a_long_keyword_carries_more_weight_than_a_short_one(self):
        short = pipeline.tag_question("art", self.TAGS)["confidence"]
        long = pipeline.tag_question("temple architecture", self.TAGS)["confidence"]
        self.assertGreater(long, short)

    def test_empty_text_is_unmapped(self):
        self.assertEqual(pipeline.tag_question("   ", self.TAGS)["unitId"], "unmapped")

    def test_shipped_config_covers_every_prelims_unit_with_subtopics(self):
        tags = pipeline.load_tags()
        units = {entry["unitId"] for entry in tags}
        self.assertEqual(len(units), 10)
        self.assertTrue(all(entry["subtopic"] for entry in tags))
        self.assertGreater(sum(len(entry["keywords"]) for entry in tags), 800)

    def test_an_older_flat_config_still_loads(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tags.json"
            path.write_text(json.dumps([
                {"unitId": "unit-1", "subject": "General Science", "keywords": ["physics"]}
            ]), encoding="utf-8")
            with mock.patch.object(pipeline, "TAG_CONFIG", path):
                tags = pipeline.load_tags()
        self.assertEqual(tags[0]["subtopic"], "")
        self.assertEqual(tags[0]["unitId"], "unit-1")


class DestroyedLabelTests(unittest.TestCase):
    """Options set one per line, where the highlighter destroyed a label.

    Verbatim Tesseract output for question 10 of the CCSE-I 2021 paper. The
    examiner's mark sat on (D) and OCR returned "Qs" for it, leaving nothing
    bracket-shaped to find. The line break is the boundary.
    """

    REGION = (
        "_ (A) _ Dr. Muthulakshmi Reddy\n"
        "(B) TM. Nair\n"
        "(C) Thanthai Periyar\n"
        "Qs Ramalinga Adigal\n"
        "(E) Answer not known\n"
    )

    def test_a_line_break_separates_options_when_no_label_survives(self):
        options, recovered, _ = pipeline.parse_options(self.REGION)
        self.assertEqual([item["label"] for item in options], list("ABCDE"))
        texts = {item["label"]: item["text"] for item in options}
        # Without the line boundary, C came back empty and D swallowed both.
        self.assertEqual(texts["C"], "Thanthai Periyar")
        self.assertEqual(texts["D"], "Ramalinga Adigal")
        self.assertIn("D", recovered)

    def test_the_junk_left_by_the_mark_is_trimmed(self):
        options, _, _ = pipeline.parse_options(self.REGION)
        for item in options:
            self.assertNotIn("Qs", item["text"])

    def test_a_gap_with_no_line_break_still_falls_back(self):
        # Two-across grid: (A) destroyed, and (B) on the same printed line, so
        # there is no line break to split on and the leading junk is dropped.
        options, _, _ = pipeline.parse_options(
            "6 Hindi and Urdu (B) Hindi and Sindhi\n(C) Persian\n(D) Sanskrit\n(E) Answer not known\n")
        texts = {item["label"]: item["text"] for item in options}
        self.assertEqual(texts["A"], "Hindi and Urdu")
        self.assertEqual(texts["B"], "Hindi and Sindhi")


class QuestionIssueTests(unittest.TestCase):
    """A question that failed to parse is worth more as a labelled gap."""

    def build(self, **overrides):
        question = {
            "questionType": "standard",
            "questionTextEn": "Which article of the Constitution abolishes untouchability?",
            "questionTextTa": "இந்திய அரசியலமைப்பின் எந்த சரத்து?",
            "options": [{"label": letter, "text": letter} for letter in "ABCDE"],
        }
        question.update(overrides)
        return question

    def test_a_fully_parsed_question_has_no_issues(self):
        self.assertEqual(pipeline.question_issues(self.build()), [])

    def test_missing_options_are_named(self):
        self.assertIn("incomplete_options", pipeline.question_issues(
            self.build(options=[{"label": letter, "text": letter} for letter in "ABCD"])))
        self.assertIn("no_options_found", pipeline.question_issues(self.build(options=[])))

    def test_a_short_structured_question_is_flagged_separately(self):
        # The lettered list above a match-the-following is indistinguishable
        # from the options once OCR has flattened it, so a short one probably
        # never found the option block at all.
        issues = pipeline.question_issues(self.build(
            questionType="match_following",
            options=[{"label": "A", "text": "1 2 3 4"}]))
        self.assertIn("structured_layout_unresolved", issues)
        # A complete one is not flagged just for being structured.
        self.assertNotIn("structured_layout_unresolved",
                         pipeline.question_issues(self.build(questionType="match_following")))

    def test_missing_or_truncated_text_is_named(self):
        self.assertIn("no_question_text", pipeline.question_issues(
            self.build(questionTextEn="", questionTextTa="")))
        self.assertIn("no_english_text", pipeline.question_issues(
            self.build(questionTextEn="")))
        self.assertIn("question_text_truncated", pipeline.question_issues(
            self.build(questionTextEn="Match the")))

    def test_issues_are_attached_to_every_parsed_question(self):
        paper = pipeline.make_paper("Group I", 2021, "Preliminary", "objective", "General Studies", "https://example.test/p.pdf", "https://example.test", "test")
        pages = [{"page": 1, "method": "embedded", "quality": 0.9, "text":
                  "1. Which Article protects Fundamental Rights?\n(A) Article 12\n(B) Article 32\n(C) Article 40\n(D) Article 50\n(E) Answer not known\n"}]
        questions = pipeline.parse_questions(pages, pipeline.asdict(paper))
        self.assertEqual(questions[0]["issues"], [])

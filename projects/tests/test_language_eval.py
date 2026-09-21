from django.test import SimpleTestCase

from projects.language_eval import (
    CaseResult,
    CheckResult,
    JudgeResult,
    _check_no_project_name_repeat,
    _format_calls,
    _format_texts,
    check_date_format,
    check_du_form,
    check_no_anglicisms,
    check_no_emoji,
    check_question_count,
    check_sentence_count,
    format_report,
)


class CheckDuFormTest(SimpleTestCase):
    def test_passes_on_du_form(self):
        result = check_du_form(["Du hast diese Woche viel geschafft."])
        self.assertTrue(result.passed)

    def test_fails_on_formal_pronoun(self):
        result = check_du_form(["Bitte prüfen Sie Ihre Aufgaben."])
        self.assertFalse(result.passed)
        self.assertIn("Ihre", result.detail)

    def test_does_not_false_positive_on_sentence_initial_sie(self):
        # "Sie" here is the plural "they", capitalized only because it
        # starts the sentence — must not be mistaken for formal address.
        result = check_du_form(["Sie laufen alle nach Plan."])
        self.assertTrue(result.passed)


class CheckDateFormatTest(SimpleTestCase):
    def test_passes_without_leading_zero(self):
        result = check_date_format(["Fällig am 5. August."])
        self.assertTrue(result.passed)

    def test_fails_with_leading_zero(self):
        result = check_date_format(["Fällig am 05. August."])
        self.assertFalse(result.passed)


class CheckNoEmojiTest(SimpleTestCase):
    def test_passes_without_emoji(self):
        result = check_no_emoji(["Alles im Plan."])
        self.assertTrue(result.passed)

    def test_fails_with_emoji(self):
        result = check_no_emoji(["Alles im Plan 🎉"])
        self.assertFalse(result.passed)


class CheckSentenceCountTest(SimpleTestCase):
    def test_passes_within_limit(self):
        result = check_sentence_count(["Ein Satz reicht."], max_sentences=1)
        self.assertTrue(result.passed)

    def test_fails_over_limit(self):
        result = check_sentence_count(["Erster Satz. Zweiter Satz."], max_sentences=1)
        self.assertFalse(result.passed)

    def test_does_not_split_on_german_ordinal_date(self):
        # "17." is a date ("17. September"), not the end of a sentence.
        result = check_sentence_count(
            ["Ab dem 17. September müssen die Plakate hängen."], max_sentences=1
        )
        self.assertTrue(result.passed)


class CheckQuestionCountTest(SimpleTestCase):
    def test_passes_within_limit(self):
        text = "1. Erste Frage?\n2. Zweite Frage?"
        result = check_question_count(text, max_questions=4)
        self.assertTrue(result.passed)

    def test_fails_over_limit(self):
        text = "\n".join(f"{i}. Frage {i}?" for i in range(1, 6))
        result = check_question_count(text, max_questions=4)
        self.assertFalse(result.passed)


class CheckNoAnglicismsTest(SimpleTestCase):
    def test_passes_on_german_wording(self):
        result = check_no_anglicisms(["Die Aufgabe ist erledigt."])
        self.assertTrue(result.passed)

    def test_fails_on_anglicism(self):
        result = check_no_anglicisms(["Dieser Task ist erledigt."])
        self.assertFalse(result.passed)


class CheckNoProjectNameRepeatTest(SimpleTestCase):
    def test_passes_without_project_name(self):
        blocks = [
            {"project_name": "Adventskonzert", "assessment": "Plakat muss noch raus."}
        ]
        result = _check_no_project_name_repeat(blocks)
        self.assertTrue(result.passed)

    def test_fails_when_name_repeated(self):
        blocks = [
            {
                "project_name": "Adventskonzert",
                "assessment": "Das Adventskonzert braucht noch ein Plakat.",
            }
        ]
        result = _check_no_project_name_repeat(blocks)
        self.assertFalse(result.passed)


class FormatTextsTest(SimpleTestCase):
    def test_numbers_each_text(self):
        lines = _format_texts(["Erster Text.", "Zweiter Text."])
        self.assertIn("--- Output (2) ---", lines)
        self.assertIn("[1] Erster Text.", lines)
        self.assertIn("[2] Zweiter Text.", lines)

    def test_indents_the_continuation_of_a_multiline_text(self):
        lines = _format_texts(["1. Wann ist der Termin?\n2. Wer singt?"])
        self.assertIn("[1] 1. Wann ist der Termin?", lines)
        self.assertIn("    2. Wer singt?", lines)

    def test_says_so_when_there_is_no_text(self):
        lines = _format_texts([])
        self.assertIn("(no text)", lines)


class FormatCallsTest(SimpleTestCase):
    def a_call(self, name="generate_plan", in_tokens=100, out_tokens=20):
        return {
            "call": name,
            "model": "claude-sonnet-4-6",
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
        }

    def test_reports_tokens_per_call(self):
        lines = _format_calls([self.a_call()])
        self.assertIn("generate_plan: 100 in / 20 out (claude-sonnet-4-6)", lines)

    def test_adds_a_case_total_only_when_a_call_was_retried(self):
        single = _format_calls([self.a_call()])
        self.assertFalse([line for line in single if line.startswith("case total")])
        retried = _format_calls([self.a_call(), self.a_call()])
        self.assertIn("case total: 200 in / 40 out over 2 calls", retried)

    def test_says_so_when_no_call_completed(self):
        lines = _format_calls([])
        self.assertIn("(no completed call)", lines)

    def test_counts_a_missing_token_figure_as_zero(self):
        # usage is None on a response the SDK could not report on; the run
        # should still produce a total rather than raise.
        lines = _format_calls([self.a_call(in_tokens=None, out_tokens=None)])
        self.assertIn("generate_plan: None in / None out (claude-sonnet-4-6)", lines)


class FormatReportTest(SimpleTestCase):
    def a_result(self, **overrides):
        defaults = {
            "key": "c",
            "title": "Wochenabschluss-Rückschau",
            "texts": ["Du hast diese Woche sieben Aufgaben geschafft."],
            "checks": [CheckResult("Du-Form", True)],
            "judge": JudgeResult(True, None, None, None, "Reads well."),
            "calls": [
                {
                    "call": "generate_closeout_summary",
                    "model": "claude-sonnet-4-6",
                    "input_tokens": 227,
                    "output_tokens": 97,
                }
            ],
        }
        return CaseResult(**{**defaults, **overrides})

    def test_shows_the_generated_text(self):
        report = format_report([self.a_result()])
        self.assertIn("Du hast diese Woche sieben Aufgaben geschafft.", report)

    def test_shows_the_prompt_cost(self):
        report = format_report([self.a_result()])
        self.assertIn("generate_closeout_summary: 227 in / 97 out", report)

    def test_totals_exclude_the_judge(self):
        # The judge runs outside the collecting block, so its cost never
        # reaches CaseResult.calls — the total is the app's cost, not the
        # eval's.
        report = format_report([self.a_result(), self.a_result()])
        self.assertIn("454 in / 194 out over 2 calls", report)

    def test_a_case_that_never_reached_the_api_reports_and_adds_nothing(self):
        report = format_report(
            [
                self.a_result(),
                self.a_result(key="d", error="Claude request failed", calls=[]),
            ]
        )
        self.assertIn("[ERROR] API call failed: Claude request failed", report)
        self.assertIn("227 in / 97 out over 1 calls", report)

    def test_a_case_that_gave_up_after_two_bad_answers_still_costs_its_tokens(self):
        # AIUnavailableError after two unusable responses means both were
        # really sent and really billed — leaving them out of the total would
        # understate what the run cost.
        report = format_report(
            [self.a_result(key="f", error="Claude returned an unusable plan twice")]
        )
        self.assertIn("227 in / 97 out over 1 calls", report)

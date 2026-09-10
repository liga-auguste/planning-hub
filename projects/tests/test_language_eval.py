from django.test import SimpleTestCase

from projects.language_eval import (
    _check_no_project_name_repeat,
    check_date_format,
    check_du_form,
    check_no_anglicisms,
    check_no_emoji,
    check_question_count,
    check_sentence_count,
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

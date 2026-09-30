"""The shared voice and output-format instruction every Claude touchpoint
sends, and the guarantee that each rule is stated exactly once.

#262: the tone rules used to be copied into each prompt body, in five
slightly different wordings for the JSON rule alone — and three of the six
touchpoints never carried "Auf Deutsch, Du-Form" at all. They are now one
constant each, sent as `system`. These tests are the drift guard: a rule
that reappears in a prompt body, or a call site that stops sending the
shared instruction, fails here.
"""

import tokenize
from datetime import (
    date,
    timedelta,
)
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase

from ..ai import (
    JSON_ONLY_INSTRUCTION,
    VOICE_INSTRUCTION,
    build_closeout_prompt,
    build_prompt,
    generate_closeout_summary,
    generate_timelapse_moments,
    generate_weekly_summary,
    system_instruction,
)
from ..planner import (
    generate_plan,
    get_clarifying_questions,
)
from .base import (
    _fake_response,
    _fake_stream,
    _fake_upcoming_project_with_task,
)

TODAY = date(2026, 9, 1)
EMPTY_SUMMARY_JSON = '{"jetzt_faellig": [], "naechste_woche": []}'
VALID_MOMENTS_JSON = '[{"date": "2026-10-01", "label": "Start", "description": "Los."}]'
VALID_PLAN_JSON = '{"project_name": "Testkonzert", "tasks": []}'
CLOSEOUT_STATS = {"completed_count": 3, "rescheduled_count": 1, "added_count": 2}


def _capture(key):
    """Runs one touchpoint against a mocked SDK and returns the
    (system, user message) pair it actually sent.

    Asserted against the real call rather than against the prompt builders,
    because `system` never passes through a builder — reading it back off
    the mocked client is the only way to see what Claude was told.
    """
    with patch("anthropic.Anthropic") as MockAnthropic:
        stream = MockAnthropic.return_value.messages.stream
        create = MockAnthropic.return_value.messages.create
        if key in ("a", "b"):
            stream.return_value = _fake_stream(EMPTY_SUMMARY_JSON)
            generate_weekly_summary(
                [_fake_upcoming_project_with_task()],
                TODAY,
                single_project_demo=(key == "b"),
            )
            call = stream.call_args
        elif key == "c":
            stream.return_value = _fake_stream('{"summary_text": "Gute Woche."}')
            generate_closeout_summary(CLOSEOUT_STATS, TODAY)
            call = stream.call_args
        elif key == "d":
            create.return_value = _fake_response(VALID_MOMENTS_JSON)
            generate_timelapse_moments(
                "Testkonzert", TODAY + timedelta(days=60), [{"name": "X", "date": None}]
            )
            call = create.call_args
        elif key == "e":
            create.return_value = _fake_response("1. Wann genau?")
            get_clarifying_questions("Ein Konzert im Dezember", [])
            call = create.call_args
        elif key == "f":
            create.return_value = _fake_response(VALID_PLAN_JSON)
            generate_plan("Ein Konzert im Dezember", "keine Angaben", [])
            call = create.call_args
        else:  # pragma: no cover - a typo in a test's own key
            raise AssertionError(f"unknown touchpoint key {key!r}")
    return call.kwargs["system"], call.kwargs["messages"][0]["content"]


ALL_KEYS = ("a", "b", "c", "d", "e", "f")
# The five that answer in JSON. (e) answers in prose, so the JSON rule would
# be actively wrong there — which is why this step needs two blocks, not one.
JSON_KEYS = ("a", "b", "c", "d", "f")


class VoiceInstructionContentTest(SimpleTestCase):
    """What the shared instruction has to say. Three of these rules existed
    somewhere before (#262's table); the point of the constant is that they
    now exist in one place."""

    def test_it_asks_for_german_in_the_du_form(self):
        self.assertIn("Auf Deutsch, Du-Form", VOICE_INSTRUCTION)

    def test_it_fixes_the_date_format(self):
        self.assertIn("keine führenden Nullen", VOICE_INSTRUCTION)

    def test_it_states_the_voice_bar_the_eval_judges(self):
        # language_eval.py scores "clear, friendly, short" — a bar no prompt
        # actually stated. #262's finding is that prompt length drives output
        # length, so brevity has to be asked for, not hoped for.
        for word in ("klar", "freundlich", "kurz"):
            with self.subTest(word=word):
                self.assertIn(word, VOICE_INSTRUCTION)

    def test_it_carries_no_json_rule_of_its_own(self):
        self.assertNotIn("JSON", VOICE_INSTRUCTION)


class SystemInstructionAssemblyTest(SimpleTestCase):
    def test_the_default_appends_the_json_rule(self):
        instruction = system_instruction()
        self.assertIn(VOICE_INSTRUCTION, instruction)
        self.assertIn(JSON_ONLY_INSTRUCTION, instruction)

    def test_the_voice_comes_first(self):
        instruction = system_instruction()
        self.assertLess(
            instruction.index(VOICE_INSTRUCTION),
            instruction.index(JSON_ONLY_INSTRUCTION),
        )

    def test_prose_touchpoints_get_the_voice_without_the_json_rule(self):
        instruction = system_instruction(json_only=False)
        self.assertEqual(instruction, VOICE_INSTRUCTION)
        self.assertNotIn(JSON_ONLY_INSTRUCTION, instruction)


class EveryTouchpointSendsTheVoiceInstructionTest(SimpleTestCase):
    """All six, including the three that never carried a Du-Form rule
    before — `generate_timelapse_moments`, `get_clarifying_questions` and
    `generate_plan` (#262)."""

    def test_all_six_send_it_as_system(self):
        for key in ALL_KEYS:
            with self.subTest(touchpoint=key):
                system, _ = _capture(key)
                self.assertIn(VOICE_INSTRUCTION, system)

    def test_the_json_rule_reaches_the_five_json_touchpoints(self):
        for key in JSON_KEYS:
            with self.subTest(touchpoint=key):
                system, _ = _capture(key)
                self.assertIn(JSON_ONLY_INSTRUCTION, system)

    def test_the_prose_touchpoint_is_not_told_to_answer_in_json(self):
        system, user_message = _capture("e")
        self.assertNotIn(JSON_ONLY_INSTRUCTION, system)
        self.assertNotIn("NUR mit JSON", user_message)


class MovedRulesAreGoneFromThePromptBodiesTest(SimpleTestCase):
    """A relocation only counts once the old copy is gone — otherwise the
    next tone change has two places to miss."""

    MOVED = ("Auf Deutsch, Du-Form", "keine führenden Nullen", "NUR mit JSON")

    def test_no_user_message_repeats_a_moved_rule(self):
        for key in ALL_KEYS:
            _, user_message = _capture(key)
            for fragment in self.MOVED:
                with self.subTest(touchpoint=key, fragment=fragment):
                    self.assertNotIn(fragment, user_message)

    def test_the_summary_prompt_still_describes_its_own_json_shape(self):
        # The shared rule says "answer in JSON"; *which* JSON stays with the
        # touchpoint that knows its own keys.
        prompt = build_prompt([_fake_upcoming_project_with_task()], TODAY)
        self.assertIn('"jetzt_faellig"', prompt)
        self.assertIn('"naechste_woche"', prompt)

    def test_the_closeout_prompt_still_describes_its_own_json_shape(self):
        self.assertIn("summary_text", build_closeout_prompt(CLOSEOUT_STATS, TODAY))


class SharedRulesAreStatedOnceInTheSourceTest(SimpleTestCase):
    """#262's actual complaint: the JSON rule existed in five wordings and
    the Du-Form rule in three copies, so every tone change had to be made
    everywhere or nowhere. A second occurrence in the application source is
    the regression this catches."""

    # Every module that builds a prompt or calls Claude. Tests are excluded
    # on purpose: asserting on a string requires naming it.
    SOURCES = ("ai.py", "planner.py", "language_eval.py")

    def source(self):
        """The three modules' code with comments removed — a comment that
        quotes a rule to explain it is documentation, not a second copy of
        the instruction, and tokenize is what tells the two apart."""
        parts = []
        for name in self.SOURCES:
            path = settings.BASE_DIR / "projects" / name
            with tokenize.open(path) as handle:
                parts.append(
                    "".join(
                        token.string if token.type != tokenize.COMMENT else ""
                        for token in tokenize.generate_tokens(handle.readline)
                    )
                )
        return "\n".join(parts)

    def test_each_shared_rule_appears_exactly_once(self):
        for fragment in ("Auf Deutsch, Du-Form", "keine führenden Nullen"):
            with self.subTest(fragment=fragment):
                self.assertEqual(self.source().count(fragment), 1)

    def test_the_json_rule_appears_exactly_once(self):
        # Counted on "Antworte NUR mit JSON" rather than on "JSON": the word
        # itself legitimately appears in each touchpoint's own format block.
        self.assertEqual(self.source().count("Antworte NUR mit JSON"), 1)

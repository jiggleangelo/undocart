import random
import unittest
from copy import deepcopy

from remedy.artefacts import generate
from remedy.classify import classify
from remedy.lint import BANNED, lint
from remedy.sample_data import DEMO_TODAY, SAMPLE_CASES, make_sources


class LintTests(unittest.TestCase):
    def test_banned_phrases(self):
        case = SAMPLE_CASES["sample-3"]
        for phrase in BANNED:
            with self.assertRaises(ValueError): lint(phrase, "C", case, [])

    def test_orphan_number(self):
        with self.assertRaises(ValueError): lint("mystery 987654", "C", SAMPLE_CASES["sample-3"], [])

    def test_all_samples_pass(self):
        for case in SAMPLE_CASES.values():
            generate(case, classify(case, make_sources(), DEMO_TODAY))

    def test_titles_attributes_and_event_timestamps_are_allowed(self):
        case = deepcopy(SAMPLE_CASES["sample-6"])
        case.order.items[0].title = "Demo Model 2026"
        case.order.items[0].attributes["revision"] = "42"
        case.events[0].at = case.mandate.issued_at
        artefact = generate(case, classify(case, make_sources(), DEMO_TODAY))
        self.assertIn(case.mandate.issued_at.isoformat(), artefact.text)

    def test_seeded_mutations(self):
        rng = random.Random(42)
        cases = list(SAMPLE_CASES.values())
        for _ in range(200):
            case = deepcopy(rng.choice(cases))
            case.claim.note = rng.choice(["ordinary note", "<script>alert(1)</script>", ""])
            art = generate(case, classify(case, make_sources(), DEMO_TODAY))
            self.assertFalse(any(p in art.text.lower() for p in BANNED))


if __name__ == "__main__": unittest.main()

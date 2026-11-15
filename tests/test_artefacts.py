import unittest

from remedy.artefacts import generate, verify_packet
from remedy.classify import classify
from remedy.lint import DISCLOSURE
from remedy.sample_data import DEMO_TODAY, SAMPLE_CASES, make_sources


class ArtefactTests(unittest.TestCase):
    def test_packet_is_frozen_and_has_no_reason_code_key(self):
        case = SAMPLE_CASES["sample-7"]
        art = generate(case, classify(case, make_sources(), DEMO_TODAY))
        self.assertTrue(verify_packet(art.facts, art.packet_hash))
        self.assertFalse(any("reason_code" in fact for fact in art.facts))
        art.facts[0]["value"] = "changed"
        self.assertFalse(verify_packet(art.facts, art.packet_hash))

    def test_note_excluded(self):
        case = SAMPLE_CASES["sample-6"]
        case.claim.note = "SECRET NOTE"
        art = generate(case, classify(case, make_sources(), DEMO_TODAY))
        self.assertNotIn("SECRET NOTE", art.text)

    def test_disclosure(self):
        for key in ("sample-1", "sample-3", "sample-5"):
            case = SAMPLE_CASES[key]
            self.assertIn(DISCLOSURE, generate(case, classify(case, make_sources(), DEMO_TODAY)).text)


if __name__ == "__main__": unittest.main()

import unittest

from tests.issue83_cases import (
    LENGTHS, alena_cases, automatic_cases, rollover_cases, rejection_cases, comparison_case_ids, length_silo, native_cases, speech_text, TAIL,
)


class Issue83CaseTests(unittest.TestCase):
    def test_silos_are_lengths_and_boundaries_remain_separate_checks(self):
        expected = {1:"<250", 249:"<250", 250:"boundary-250", 251:"250-4999",
                    4999:"250-4999", 5000:"boundary-5000", 5001:">5000"}
        for length, silo in expected.items():
            self.assertEqual(length_silo(length), silo)
        with self.assertRaises(ValueError):
            length_silo(0)

    def test_whole_text_survives_actual_stock_normalizer_at_exact_length(self):
        from tools.tts_text_normalize import prepare_spoken_text
        for length in LENGTHS:
            text = speech_text(length)
            self.assertEqual(len(text), length)
            self.assertEqual(prepare_spoken_text(text, max_chars=None), text)
            self.assertTrue(text.endswith(TAIL))

    def test_matched_route_matrix_is_complete_without_impossible_voice_substitutions(self):
        cases = native_cases()
        self.assertEqual(len(cases), 55)
        for voice, expected in (("ermil", 6), ("sergey", 5)):
            for length in LENGTHS:
                selected = [case for case in cases if case["voice"] == voice and case["length"] == length]
                self.assertEqual(len(selected), expected)
                self.assertEqual({case["text"] for case in selected}, {speech_text(length)})
                self.assertEqual(len({case["route"].name for case in selected}), expected)
                self.assertTrue(all(case["model"] == ("general" if voice == "ermil" else "livetts")
                                    for case in selected))
        self.assertEqual(len(alena_cases()), 6)
        self.assertEqual(len(automatic_cases()), 15)
        self.assertEqual(len(rollover_cases()), 4)
        self.assertEqual(len(comparison_case_ids()), 80)
        self.assertTrue({case["id"] for case in rejection_cases()}.isdisjoint(comparison_case_ids()))
        self.assertTrue(all(case["route"].api_version == "3"
                            for case in cases if case["voice"] == "sergey"))

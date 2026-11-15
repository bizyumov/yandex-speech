"""Reviewed current vendor catalog must agree with installed instructions and routing."""
from pathlib import Path
import re
import unittest

from yandex_speech.tts_voices import CATALOG

CURRENT = frozenset("""lea john naomi amira madi saule zhanar filipp ermil jane omazh zahar
dasha julia lera masha marina alexander kirill anton madi_ru saule_ru zamira_ru zhanar_ru
yulduz_ru nigora zamira yulduz sofia vera vasily sergey denis irina""".split())
FEMALE = frozenset("""lea naomi amira saule zhanar jane omazh dasha julia lera masha marina
saule_ru zamira_ru zhanar_ru yulduz_ru nigora zamira yulduz sofia vera irina""".split())


def documented_rows():
    text = (Path(__file__).resolve().parents[1] / "references/synthesis-provider-testing.md").read_text()
    section = text.split("### Complete official voice catalog", 1)[1].split("### Agent procedure", 1)[0]
    rows = {}
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        fields = [field.strip() for field in line.strip().strip("|").split("|")]
        if fields[0] == "Voice ID" or set(fields[0]) == {"-"}:
            continue
        if len(fields) != 8 or fields[0] in rows:
            raise AssertionError("voice rows must be complete and unique")
        rows[fields[0]] = fields
    return rows


class VoiceCatalogTests(unittest.TestCase):
    def test_complete_current_catalog_and_legacy_are_distinct(self):
        rows = documented_rows()
        self.assertEqual(len(CURRENT), 34)
        self.assertEqual(set(rows), CURRENT)
        self.assertEqual(set(CATALOG), CURRENT | {"alena"})
        self.assertEqual(rows["sofia"][1], "Софья")
        self.assertIn("legacy", (Path(__file__).resolve().parents[1] / "references/synthesis-provider-testing.md").read_text())

    def test_documented_languages_families_apis_roles_and_defaults_match_code(self):
        for voice, fields in documented_rows().items():
            with self.subTest(voice=voice):
                info = CATALOG[voice]
                self.assertTrue(all(fields))
                self.assertIn(info["language"], fields[2])
                self.assertEqual(fields[3], "F" if voice in FEMALE else "M")
                self.assertEqual(fields[4], info["model"])
                self.assertEqual(set(re.findall(r"\bv([13])\b", fields[5])), set(info["versions"]))
                roles = set() if fields[6] == "—" else {role.strip() for role in fields[6].split(",")}
                self.assertEqual(roles, set(info["roles"]))
                self.assertEqual(None if fields[7] == "—" else fields[7], info["default_role"])

    def test_saved_literal_styles_do_not_become_another_voices_style(self):
        self.assertEqual(CATALOG["ermil"]["roles"], {"neutral", "good"})
        self.assertNotIn("friendly", CATALOG["ermil"]["roles"])
        self.assertIn("friendly", CATALOG["marina"]["roles"])
        self.assertEqual(CATALOG["alena"]["versions"], {"1", "3"})

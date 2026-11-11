import unittest

from utils.theme_membership_updater import ThemeMembershipUpdater


class ThemeMembershipUpdaterTests(unittest.TestCase):
    def test_parse_themes_uses_only_known_concept_catalog(self):
        self.assertEqual(
            ThemeMembershipUpdater._parse_themes({"ssbk": [
                {"BOARD_CODE": "1283"}, {"BOARD_CODE": "BK0437"},
                {"BOARD_CODE": "bad"},
            ]}, {"BK1283", "BK0437"}),
            {"BK1283", "BK0437"},
        )


if __name__ == "__main__":
    unittest.main()

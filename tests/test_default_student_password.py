import unittest
from unittest.mock import patch

import gspread

from tools.set_default_student_password import build_password_updates


class DefaultStudentPasswordTests(unittest.TestCase):
    @patch("tools.set_default_student_password.create_password_hash", return_value="scrypt$fake")
    def test_builds_updates_only_for_requested_usernames(self, create_hash):
        rows = [
            ["full_name", "username", "password", "email", "role"],
            ["Mr Huy", "huynt", "old", "huynt@school.com", "teacher"],
            ["Demo", "demo", "old", "demo@abc.com", "student"],
        ]

        updates = build_password_updates(rows, ["demo"])

        self.assertEqual(updates, [{
            "range": gspread.utils.rowcol_to_a1(3, 3),
            "values": [["scrypt$fake"]],
        }])
        create_hash.assert_called_once_with("123456")

    def test_rejects_missing_or_ambiguous_username_before_writing(self):
        rows = [
            ["username", "password"],
            ["demo", "old"],
            ["demo", "old"],
        ]
        with self.assertRaisesRegex(ValueError, "found 2"):
            build_password_updates(rows, ["demo"])

    def test_rejects_sheet_without_password_column(self):
        with self.assertRaisesRegex(ValueError, "username and password"):
            build_password_updates([["username"], ["demo"]], ["demo"])


if __name__ == "__main__":
    unittest.main()

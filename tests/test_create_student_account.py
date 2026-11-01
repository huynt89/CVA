import unittest
from unittest.mock import patch

from tools.create_student_account import build_account_row
from tools.generate_password_hash import create_password_hash, verify_password_hash


class CreateStudentAccountTests(unittest.TestCase):
    @patch("tools.create_student_account.create_password_hash")
    def test_builds_active_account_and_hashes_default_password(self, create_hash):
        create_hash.return_value = "scrypt$hashed-default"
        headers = [
            "full_name",
            "username",
            "password",
            "email",
            "role",
            "class_name",
            "status",
            "total_points",
            "average_score",
        ]

        row = build_account_row(
            [headers],
            "Demo User",
            "demo",
            "DEMO@example.com",
            "student",
            "6A",
        )

        self.assertEqual(row, [
            "Demo User",
            "demo",
            "scrypt$hashed-default",
            "demo@example.com",
            "student",
            "6A",
            "active",
            "0",
            "0",
        ])
        create_hash.assert_called_once_with("123456")

    def test_rejects_duplicate_username_and_email(self):
        rows = [
            ["full_name", "username", "password", "email", "role"],
            ["Existing User", "demo", "existing-hash", "existing@example.com", "student"],
        ]
        with self.assertRaisesRegex(ValueError, "username already exists"):
            build_account_row(rows, "New User", "DEMO", "new@example.com", "student")
        with self.assertRaisesRegex(ValueError, "email already exists"):
            build_account_row(rows, "New User", "new", "EXISTING@example.com", "student")

    def test_hash_created_for_default_password_matches_verifier(self):
        encoded = create_password_hash("123456")
        self.assertTrue(verify_password_hash("123456", encoded))


if __name__ == "__main__":
    unittest.main()

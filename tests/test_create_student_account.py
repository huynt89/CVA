import base64
import json
import unittest
from unittest.mock import patch

from tools.create_student_account import (
    build_account_row,
    build_existing_teacher_update,
    load_credentials,
)
from tools.generate_password_hash import create_password_hash, verify_password_hash


class CreateStudentAccountTests(unittest.TestCase):
    def test_loads_base64_service_account_credentials(self):
        credential_json = base64.b64encode(
            json.dumps({"type": "service_account"}).encode("utf-8")
        ).decode("ascii")
        with patch(
            "tools.create_student_account.service_account.Credentials.from_service_account_info",
            return_value="credentials",
        ) as from_info:
            credentials = load_credentials({
                "GOOGLE_SERVICE_ACCOUNT_JSON_B64": credential_json,
            })

        self.assertEqual(credentials, "credentials")
        from_info.assert_called_once_with(
            {"type": "service_account"},
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )

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

    @patch("tools.create_student_account.create_password_hash", return_value="scrypt$updated")
    def test_builds_existing_teacher_account_update(self, create_hash):
        rows = [
            ["full_name", "username", "password", "email", "role", "class_name", "status"],
            ["Old Name", "huynt", "scrypt$old", "old@example.com", "teacher", "English", "locked"],
            ["Student", "student1", "scrypt$student", "student@example.com", "student", "6A", "active"],
        ]

        row_number, updated = build_existing_teacher_update(
            rows,
            "Mr.Huy",
            "huynt",
            "huynt@abc.com",
        )

        self.assertEqual(row_number, 2)
        self.assertEqual(updated, [
            "Mr.Huy",
            "huynt",
            "scrypt$updated",
            "huynt@abc.com",
            "teacher",
            "English",
            "active",
        ])
        create_hash.assert_called_once_with("123456")

    def test_existing_teacher_update_rejects_non_teacher_and_duplicate_email(self):
        rows = [
            ["full_name", "username", "password", "email", "role"],
            ["Student", "student1", "hash", "student@example.com", "student"],
        ]
        with self.assertRaisesRegex(ValueError, "not a teacher"):
            build_existing_teacher_update(rows, "Mr.Huy", "student1", "new@example.com")

        rows[1][4] = "teacher"
        rows.append(["Other", "other", "hash", "new@example.com", "student"])
        with self.assertRaisesRegex(ValueError, "email already belongs"):
            build_existing_teacher_update(rows, "Mr.Huy", "student1", "new@example.com")

    def test_hash_created_for_default_password_matches_verifier(self):
        encoded = create_password_hash("123456")
        self.assertTrue(verify_password_hash("123456", encoded))


if __name__ == "__main__":
    unittest.main()

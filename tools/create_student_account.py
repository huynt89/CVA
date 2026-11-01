import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path

import gspread
from dotenv import dotenv_values
from google.oauth2 import service_account

from tools.generate_password_hash import create_password_hash


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PASSWORD = "123456"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
REQUIRED_HEADERS = ("full_name", "username", "password", "email", "role")


def normalize_header(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").strip().lower())


def normalize_account_value(value):
    return str(value or "").strip().casefold()


def build_account_row(rows, full_name, username, email, role, class_name=""):
    if not rows:
        raise ValueError("The Users worksheet must contain a header row.")
    headers = [normalize_header(value) for value in rows[0]]
    indexes = {}
    for name in REQUIRED_HEADERS:
        normalized_name = normalize_header(name)
        matches = [index for index, value in enumerate(headers) if value == normalized_name]
        if len(matches) != 1:
            raise ValueError(f"The Users worksheet must have exactly one {name} column.")
        indexes[name] = matches[0]

    optional_indexes = {}
    for name in ("class_name", "status", "total_points", "average_score"):
        normalized_name = normalize_header(name)
        matches = [index for index, value in enumerate(headers) if value == normalized_name]
        if len(matches) > 1:
            raise ValueError(f"The Users worksheet contains duplicate {name} columns.")
        if matches:
            optional_indexes[name] = matches[0]

    normalized_username = normalize_account_value(username)
    normalized_email = normalize_account_value(email)
    if not full_name.strip() or not normalized_username or not normalized_email:
        raise ValueError("Full name, username, and email are required.")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized_email):
        raise ValueError("Enter a valid email address.")
    if role not in ("student", "teacher"):
        raise ValueError("Role must be student or teacher.")

    for row in rows[1:]:
        existing_username = (
            row[indexes["username"]] if indexes["username"] < len(row) else ""
        )
        existing_email = row[indexes["email"]] if indexes["email"] < len(row) else ""
        if normalize_account_value(existing_username) == normalized_username:
            raise ValueError("That username already exists in the Users worksheet.")
        if normalize_account_value(existing_email) == normalized_email:
            raise ValueError("That email already exists in the Users worksheet.")

    values = [""] * len(headers)
    values[indexes["full_name"]] = full_name.strip()
    values[indexes["username"]] = username.strip()
    values[indexes["password"]] = create_password_hash(DEFAULT_PASSWORD)
    values[indexes["email"]] = normalized_email
    values[indexes["role"]] = role
    if "class_name" in optional_indexes:
        values[optional_indexes["class_name"]] = class_name.strip()
    if "status" in optional_indexes:
        values[optional_indexes["status"]] = "active"
    if "total_points" in optional_indexes:
        values[optional_indexes["total_points"]] = "0"
    if "average_score" in optional_indexes:
        values[optional_indexes["average_score"]] = "0"
    return values


def build_existing_teacher_update(rows, full_name, username, email):
    if not rows:
        raise ValueError("The Users worksheet must contain a header row.")
    headers = [normalize_header(value) for value in rows[0]]
    indexes = {}
    for name in REQUIRED_HEADERS:
        matches = [index for index, value in enumerate(headers) if value == normalize_header(name)]
        if len(matches) != 1:
            raise ValueError(f"The Users worksheet must have exactly one {name} column.")
        indexes[name] = matches[0]
    status_matches = [index for index, value in enumerate(headers) if value == "status"]
    if len(status_matches) > 1:
        raise ValueError("The Users worksheet contains duplicate status columns.")

    username_key = normalize_account_value(username)
    email_key = normalize_account_value(email)
    matches = [
        (row_number, row)
        for row_number, row in enumerate(rows[1:], start=2)
        if normalize_account_value(
            row[indexes["username"]] if indexes["username"] < len(row) else ""
        ) == username_key
    ]
    if len(matches) != 1:
        raise ValueError("The username must match exactly one existing Users account.")
    row_number, existing_row = matches[0]
    role = existing_row[indexes["role"]] if indexes["role"] < len(existing_row) else ""
    if normalize_account_value(role) != "teacher":
        raise ValueError("The existing username is not a teacher account; no changes were made.")
    if any(
        index + 2 != row_number
        and normalize_account_value(row[indexes["email"]] if indexes["email"] < len(row) else "") == email_key
        for index, row in enumerate(rows[1:])
    ):
        raise ValueError("That email already belongs to another Users account.")
    if not full_name.strip() or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email_key):
        raise ValueError("Enter a valid full name and email address.")

    updated = list(existing_row)
    if len(updated) < len(headers):
        updated.extend([""] * (len(headers) - len(updated)))
    updated[indexes["full_name"]] = full_name.strip()
    updated[indexes["username"]] = username.strip()
    updated[indexes["password"]] = create_password_hash(DEFAULT_PASSWORD)
    updated[indexes["email"]] = email_key
    updated[indexes["role"]] = "teacher"
    if status_matches:
        updated[status_matches[0]] = "active"
    return row_number, updated


def load_credentials(env):
    raw_json_b64 = (env.get("GOOGLE_SERVICE_ACCOUNT_JSON_B64") or "").strip()
    if raw_json_b64:
        credential_info = json.loads(base64.b64decode(raw_json_b64, validate=True).decode("utf-8"))
        return service_account.Credentials.from_service_account_info(
            credential_info,
            scopes=SCOPES,
        )
    raw_json = (env.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    credential_path = (env.get("GOOGLE_SERVICE_ACCOUNT_PATH") or "").strip()
    if raw_json.startswith("{"):
        return service_account.Credentials.from_service_account_info(
            json.loads(raw_json),
            scopes=SCOPES,
        )
    if credential_path:
        path = Path(credential_path.strip('"')).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        if path.is_file():
            return service_account.Credentials.from_service_account_file(
                str(path),
                scopes=SCOPES,
            )
    raise ValueError(
        "Google credentials are unavailable. Configure GOOGLE_SERVICE_ACCOUNT_PATH "
        "or GOOGLE_SERVICE_ACCOUNT_JSON in the local .env."
    )


def main():
    parser = argparse.ArgumentParser(
        description="Create an active Users worksheet account with a hashed default password."
    )
    parser.add_argument("full_name")
    parser.add_argument("username")
    parser.add_argument("email")
    parser.add_argument("role", choices=("student", "teacher"))
    parser.add_argument("--class-name", default="")
    parser.add_argument(
        "--update-existing",
        action="store_true",
        help="Update the matching existing teacher account instead of creating a new row.",
    )
    args = parser.parse_args()

    env_file = ROOT / ".env"
    if not env_file.is_file():
        raise SystemExit("Local .env file not found; no sheet changes were made.")
    env = {**dotenv_values(env_file), **os.environ}
    spreadsheet_id = str(env.get("GOOGLE_SHEET_ID") or "").strip()
    if not spreadsheet_id:
        raise SystemExit("GOOGLE_SHEET_ID is missing; no sheet changes were made.")

    try:
        credentials = load_credentials(env)
        spreadsheet = gspread.authorize(credentials).open_by_key(spreadsheet_id)
        worksheet = spreadsheet.worksheet("Users")
        current_values = worksheet.get_all_values()
        if args.update_existing:
            if args.role != "teacher":
                raise ValueError("--update-existing is only available for teacher accounts.")
            account_row_number, account_row = build_existing_teacher_update(
                current_values,
                args.full_name,
                args.username,
                args.email,
            )
        else:
            account_row = build_account_row(
                current_values,
                args.full_name,
                args.username,
                args.email,
                args.role,
                args.class_name,
            )
    except Exception as error:
        raise SystemExit(f"Could not prepare the Users account: {error}") from error

    action = "update" if args.update_existing else "create"
    print(f"Account to {action}: {args.username.strip()} ({args.role}, active)")
    print("Initial password: 123456. Only its scrypt hash will be saved in the sheet.")
    print("This common initial password is weak; use it only temporarily or for test accounts.")
    if input("Type APPLY to create this account: ").strip() != "APPLY":
        print("Cancelled. The sheet was not changed.")
        return 0

    if args.update_existing:
        worksheet.update(
            values=[account_row],
            range_name=f"A{account_row_number}",
            value_input_option="RAW",
        )
        print(f"Updated active {args.role} account '{args.username.strip()}'.")
    else:
        worksheet.append_row(account_row, value_input_option="RAW")
        print(f"Created active {args.role} account '{args.username.strip()}'.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Account creation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error

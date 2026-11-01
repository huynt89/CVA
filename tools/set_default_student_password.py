pyimport argparse
import getpass
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


def normalize_header(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").strip().lower())


def build_password_updates(rows, usernames, password=DEFAULT_PASSWORD):
    if not rows:
        raise ValueError("The Student worksheet is empty.")
    headers = [normalize_header(value) for value in rows[0]]
    username_index = headers.index("username") if "username" in headers else -1
    password_index = headers.index("password") if "password" in headers else -1
    if username_index < 0 or password_index < 0:
        raise ValueError("The Student worksheet must contain username and password columns.")

    requested = list(dict.fromkeys(value.strip().casefold() for value in usernames if value.strip()))
    if not requested:
        raise ValueError("Specify at least one username to update.")

    updates = []
    for username in requested:
        matches = [
            row_number
            for row_number, row in enumerate(rows[1:], start=2)
            if username_index < len(row)
            and str(row[username_index] or "").strip().casefold() == username
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Expected exactly one Student row for username '{username}', found {len(matches)}."
            )
        updates.append({
            "range": gspread.utils.rowcol_to_a1(matches[0], password_index + 1),
            "values": [[create_password_hash(password)]],
        })
    return updates


def load_credentials(env):
    raw_json = (env.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    credential_path = (env.get("GOOGLE_SERVICE_ACCOUNT_PATH") or "").strip()
    if raw_json.startswith("{"):
        import json

        credentials_info = json.loads(raw_json)
        return service_account.Credentials.from_service_account_info(
            credentials_info,
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
        description="Set selected Student accounts to the default password and store only scrypt hashes."
    )
    parser.add_argument("usernames", nargs="+", help="Exact usernames to update in the Student sheet")
    parser.add_argument(
        "--custom-password",
        action="store_true",
        help="Prompt for a different password without showing it in the terminal.",
    )
    args = parser.parse_args()
    password = DEFAULT_PASSWORD
    if args.custom_password:
        password = getpass.getpass("New password (input hidden): ")
        confirmation = getpass.getpass("Confirm new password: ")
        if len(password) < 6:
            raise SystemExit("Use a password with at least 6 characters.")
        if password != confirmation:
            raise SystemExit("Passwords do not match.")

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
        worksheet = spreadsheet.worksheet("Student")
        updates = build_password_updates(
            worksheet.get_all_values(),
            args.usernames,
            password=password,
        )
    except Exception as error:
        raise SystemExit(f"Could not prepare Student password updates: {error}") from error

    print(f"Accounts to update: {', '.join(args.usernames)}")
    if args.custom_password:
        print("A custom password was chosen. Only scrypt hashes will be written to the sheet.")
    else:
        print("The initial password will be 123456. Only scrypt hashes will be written to the sheet.")
        print("This common initial password is weak; use it only temporarily or for test accounts.")
    if input("Type APPLY to update these rows: ").strip() != "APPLY":
        print("Cancelled. The sheet was not changed.")
        return 0

    worksheet.batch_update(updates, value_input_option="RAW")
    print(f"Updated {len(updates)} account(s). No plain-text password was written to the sheet.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Password update failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error

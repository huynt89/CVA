#!/usr/bin/env python3
"""Create a Vercel-only .env file without exposing credential values."""

import argparse
import base64
import getpass
import json
import re
import secrets
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values


def prompt_value(label: str, *, secret: bool = False) -> str:
    prompt = getpass.getpass if secret else input
    value = prompt(f"{label}: ").strip()
    if not value:
        raise SystemExit(f"{label} is required.")
    return value


def extract_id(value: str, kind: str) -> str:
    value = value.strip()
    if value.startswith(("https://", "http://")):
        path = urlparse(value).path
        marker = "/spreadsheets/d/" if kind == "sheet" else "/folders/"
        match = re.search(re.escape(marker) + r"([A-Za-z0-9_-]+)", path)
        if not match:
            raise SystemExit(f"Could not find the {kind} ID in the supplied URL.")
        value = match.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise SystemExit(f"The {kind} ID must be an ID, not a URL or invalid value.")
    return value


def read_service_account(source: dict[str, str | None], env_path: Path) -> dict[str, object]:
    raw_json = source.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw_json:
        value = json.loads(raw_json)
    else:
        configured_path = source.get("GOOGLE_SERVICE_ACCOUNT_PATH", "")
        if not configured_path:
            configured_path = prompt_value("Path to the Google service-account JSON file")
        credential_path = Path(configured_path.strip().strip('"')).expanduser()
        if not credential_path.is_absolute():
            credential_path = env_path.parent / credential_path
        try:
            value = json.loads(credential_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SystemExit(
                f"Could not read a valid service-account JSON file at {credential_path}."
            ) from error
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("client_email"), str)
        or not value["client_email"]
        or not isinstance(value.get("private_key"), str)
        or not value["private_key"]
    ):
        raise SystemExit("Service-account JSON must include client_email and private_key.")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(".env"), help="Local env file")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".env.vercel"),
        help="Generated Vercel import file",
    )
    args = parser.parse_args()
    env_path = args.source.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    if env_path == output_path:
        raise SystemExit("Output must not overwrite the source .env file.")
    if not env_path.is_file():
        raise SystemExit(f"Local environment file not found: {env_path}")

    source = dotenv_values(env_path, interpolate=False)
    existing_output = dotenv_values(output_path, interpolate=False) if output_path.exists() else {}
    values: dict[str, str] = {}

    values["APP_SESSION_SECRET"] = (
        source.get("APP_SESSION_SECRET")
        or existing_output.get("APP_SESSION_SECRET")
        or secrets.token_urlsafe(48)
    )

    for key in ("GEMINI_API_KEY", "GROQ_API_KEY"):
        values[key] = source.get(key) or prompt_value(key, secret=True)
    if source.get("GROQ_EVAL_MODEL"):
        values["GROQ_EVAL_MODEL"] = source["GROQ_EVAL_MODEL"] or ""

    sheet_id = source.get("GOOGLE_SHEET_ID") or prompt_value("Google Spreadsheet ID")
    values["GOOGLE_SHEET_ID"] = extract_id(sheet_id, "sheet")
    folder_id = source.get("GOOGLE_DRIVE_FOLDER_ID") or prompt_value("Google Drive folder ID")
    values["GOOGLE_DRIVE_FOLDER_ID"] = extract_id(folder_id, "folder")

    credentials = read_service_account(source, env_path)
    compact_json = json.dumps(credentials, ensure_ascii=False, separators=(",", ":"))
    values["GOOGLE_SERVICE_ACCOUNT_JSON_B64"] = base64.b64encode(
        compact_json.encode("utf-8")
    ).decode("ascii")

    lines = [f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in values.items()]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Vercel environment file created: {output_path}")
    print("It contains secrets. Do not commit, email, or share it.")


if __name__ == "__main__":
    main()

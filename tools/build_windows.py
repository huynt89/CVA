import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIRECTORY = ROOT / "dist" / "cva-distribution"
WORK_DIRECTORY = ROOT / "build" / "cva-distribution"
EXECUTABLE_NAME = "CVA Let's speak!"


def main():
    icon = ROOT / "static" / "images" / "cva-logo.ico"
    if not icon.is_file():
        raise FileNotFoundError(f"Application icon not found: {icon}")

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--noconsole",
        "--name",
        EXECUTABLE_NAME,
        "--icon",
        str(icon),
        "--distpath",
        str(OUTPUT_DIRECTORY),
        "--workpath",
        str(WORK_DIRECTORY / "work"),
        "--specpath",
        str(WORK_DIRECTORY),
        "--add-data",
        f"{ROOT / 'templates'};templates",
        "--add-data",
        f"{ROOT / 'static'};static",
        str(ROOT / "desktop_entry.py"),
    ]
    subprocess.run(command, cwd=ROOT, check=True)

    executable = OUTPUT_DIRECTORY / f"{EXECUTABLE_NAME}.exe"
    if not executable.is_file():
        raise FileNotFoundError(
            f"PyInstaller completed without producing {EXECUTABLE_NAME}.exe."
        )
    unexpected_files = [
        path.name for path in OUTPUT_DIRECTORY.iterdir()
        if path != executable
    ]
    if unexpected_files:
        raise RuntimeError(
            "The standalone output directory contains unexpected files: "
            + ", ".join(unexpected_files)
        )
    print(f"Built standalone application: {executable}")
    print(f"The output directory contains only {EXECUTABLE_NAME}.exe.")


if __name__ == "__main__":
    main()

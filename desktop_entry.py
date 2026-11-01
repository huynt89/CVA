import base64
import ctypes
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter
from ctypes import wintypes
from pathlib import Path
from tkinter import messagebox

import requests
from werkzeug.serving import WSGIRequestHandler, make_server


APP_NAME = "CVA Let's Speak"
STARTUP_TIMEOUT_SECONDS = 30


class QuietRequestHandler(WSGIRequestHandler):
    def log(self, log_type, message, *args):
        logging.getLogger("werkzeug").info("%s: %s", log_type, message % args if args else message)


def app_data_directory():
    configured_directory = os.getenv("ENG_APP_DATA_DIR", "").strip()
    if configured_directory:
        return Path(configured_directory).expanduser()
    local_app_data = os.getenv("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return root / "CVA" / "EngClassAI" / "data"


def find_browser(candidates=None):
    if candidates is None:
        program_files = [
            os.getenv("PROGRAMFILES(X86)", ""),
            os.getenv("PROGRAMFILES", ""),
            os.getenv("LOCALAPPDATA", ""),
        ]
        candidates = [
            Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            for base in program_files
            if base
        ] + [
            Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
            for base in program_files
            if base
        ]
        candidates.extend(
            Path(found)
            for executable in ("msedge.exe", "chrome.exe")
            if (found := shutil.which(executable))
        )

    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return path
    raise FileNotFoundError(
        "Microsoft Edge or Google Chrome is required to run the application."
    )


def browser_command(browser, url, profile_directory):
    return [
        str(browser),
        f"--app={url}",
        f"--user-data-dir={profile_directory}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
    ]


def find_browser_process_id(profile_directory):
    profile = str(profile_directory).replace("'", "''")
    script = (
        "$item = Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -in @('msedge.exe', 'chrome.exe') -and "
        f"$_.CommandLine.Contains('{profile}') -and "
        "$_.CommandLine -notmatch ' --type=' "
        "} | Select-Object -First 1 -ExpandProperty ProcessId; "
        "if ($item) { $item }"
    )
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    powershell = shutil.which("powershell.exe")
    if not powershell:
        raise FileNotFoundError("Windows PowerShell is required to monitor the app window.")

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-EncodedCommand",
                encoded_script,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        match = re.search(r"(?m)^\s*(\d+)\s*$", result.stdout)
        if match:
            return int(match.group(1))
        time.sleep(0.25)
    raise RuntimeError("Could not find the browser app window to monitor.")


def find_browser_process_ids(profile_directory):
    profile = str(profile_directory).replace("'", "''")
    script = (
        "Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -in @('msedge.exe', 'chrome.exe') -and "
        "$_.CommandLine -and "
        f"$_.CommandLine.Contains('{profile}') "
        "} | Select-Object -ExpandProperty ProcessId"
    )
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    powershell = shutil.which("powershell.exe")
    if not powershell:
        raise FileNotFoundError("Windows PowerShell is required to monitor the app window.")
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-EncodedCommand",
            encoded_script,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return {
        int(match)
        for match in re.findall(r"(?m)^\s*(\d+)\s*$", result.stdout)
    }


def wait_for_browser_profile_exit(profile_directory, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not find_browser_process_ids(profile_directory):
            return
        time.sleep(0.25)
    remaining = find_browser_process_ids(profile_directory)
    if remaining:
        raise RuntimeError(
            "Browser background processes did not exit after the app window closed."
        )


def wait_for_process_exit(process_id):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(0x00100000, False, process_id)
    if not handle:
        return
    try:
        result = kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)


def wait_for_browser_window(browser_process, profile_directory):
    try:
        return_code = browser_process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return_code = browser_process.wait()
        return return_code

    if return_code:
        return return_code
    process_id = find_browser_process_id(profile_directory)
    wait_for_process_exit(process_id)
    return 0


def wait_for_server(url, server_thread, timeout=STARTUP_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not server_thread.is_alive():
            raise RuntimeError("The local application server stopped during startup.")
        try:
            response = requests.get(url, timeout=0.5)
            if response.status_code < 500:
                return
        except requests.RequestException:
            time.sleep(0.2)
    raise TimeoutError("The application did not start in time.")


def show_error(message):
    root = tkinter.Tk()
    root.withdraw()
    try:
        messagebox.showerror(APP_NAME, message, parent=root)
    finally:
        root.destroy()


def main():
    data_directory = app_data_directory()
    data_directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=data_directory / "application.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    os.environ.setdefault("ENG_APP_DATA_DIR", str(data_directory))

    server = None
    server_thread = None
    browser_process = None
    try:
        browser = find_browser()
        from app import app

        server = make_server(
            "127.0.0.1",
            0,
            app,
            threaded=True,
            request_handler=QuietRequestHandler,
        )
        port = server.server_port
        url = f"http://127.0.0.1:{port}/"
        server_thread = threading.Thread(
            target=server.serve_forever,
            name="cva-local-web-server",
            daemon=True,
        )
        server_thread.start()
        wait_for_server(url, server_thread)

        with tempfile.TemporaryDirectory(
            prefix="cva-browser-",
            dir=data_directory,
        ) as profile_directory:
            browser_process = subprocess.Popen(
                browser_command(browser, url, profile_directory),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return_code = wait_for_browser_window(browser_process, profile_directory)
            if return_code:
                raise RuntimeError(
                    f"The browser window exited with code {return_code}."
                )
            wait_for_browser_profile_exit(profile_directory)
        return 0
    except Exception as error:
        logging.exception("The desktop application could not run.")
        show_error(str(error))
        return 1
    finally:
        if browser_process is not None and browser_process.poll() is None:
            browser_process.terminate()
            try:
                browser_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                browser_process.kill()
                browser_process.wait()
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())

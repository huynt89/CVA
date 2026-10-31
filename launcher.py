import base64
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser
import zipfile
from pathlib import Path

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from launcher_config import DISTRIBUTION_REPOSITORY, UPDATE_PUBLIC_KEY_B64

APP_NAME = 'EngClassAI'
LAUNCHER_NAME = 'CVA-Launcher'
RELEASE_API_TIMEOUT = (10, 90)
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024
POLL_SECONDS = 20


def install_root():
    base = Path(os.getenv('LOCALAPPDATA') or (Path.home() / 'AppData' / 'Local'))
    return base / 'CVA' / APP_NAME


def versions_directory():
    return install_root() / 'versions'


def read_current_version():
    version_file = install_root() / 'current-version.txt'
    try:
        version = version_file.read_text(encoding='utf-8').strip()
    except OSError:
        return None
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,60}', version):
        return None
    app_exe = versions_directory() / version / APP_NAME / f'{APP_NAME}.exe'
    return version if app_exe.is_file() else None


def installed_versions():
    root = versions_directory()
    if not root.is_dir():
        return []
    return sorted(
        (
            path.name for path in root.iterdir()
            if path.is_dir()
            and re.fullmatch(r'[A-Za-z0-9._-]{1,60}', path.name)
            and (path / APP_NAME / f'{APP_NAME}.exe').is_file()
        ),
        key=lambda name: (root / name).stat().st_mtime,
        reverse=True,
    )


def canonical_manifest(manifest):
    return json.dumps(
        manifest,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
    ).encode('utf-8')


def verify_manifest(manifest, signature_b64, public_key_b64):
    if not isinstance(manifest, dict):
        raise ValueError('The update manifest is not a JSON object.')
    required = {'version', 'asset', 'sha256'}
    if set(manifest) != required:
        raise ValueError('The update manifest has an unexpected format.')
    version = manifest['version']
    if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,60}', version):
        raise ValueError('The update version is invalid.')
    asset = manifest['asset']
    if asset != f'CVA-EngClassAI-{version}.zip':
        raise ValueError('The update archive name is invalid.')
    digest = manifest['sha256']
    if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValueError('The update checksum is invalid.')

    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
        signature = base64.b64decode(signature_b64, validate=True)
        public_key.verify(signature, canonical_manifest(manifest))
    except (ValueError, InvalidSignature) as exc:
        raise ValueError('The update signature could not be verified.') from exc
    return version


def github_asset(release, name):
    for asset in release.get('assets', []):
        if asset.get('name') == name and isinstance(asset.get('browser_download_url'), str):
            return asset['browser_download_url']
    raise ValueError(f'The release is missing {name}.')


def fetch_latest_manifest():
    repository = DISTRIBUTION_REPOSITORY.strip()
    public_key = UPDATE_PUBLIC_KEY_B64.strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('The distribution repository is not configured in the launcher.')
    if not public_key:
        raise ValueError('The updater signing public key is not configured in the launcher.')

    api_url = f'https://api.github.com/repos/{repository}/releases/latest'
    response = requests.get(
        api_url,
        headers={'Accept': 'application/vnd.github+json', 'User-Agent': LAUNCHER_NAME},
        timeout=RELEASE_API_TIMEOUT,
    )
    response.raise_for_status()
    release = response.json()
    manifest_response = requests.get(
        github_asset(release, 'update-manifest.json'),
        timeout=RELEASE_API_TIMEOUT,
    )
    manifest_response.raise_for_status()
    signature_response = requests.get(
        github_asset(release, 'update-manifest.sig'),
        timeout=RELEASE_API_TIMEOUT,
    )
    signature_response.raise_for_status()
    manifest = manifest_response.json()
    version = verify_manifest(manifest, signature_response.text.strip(), public_key)
    if str(release.get('tag_name') or '') != version:
        raise ValueError('The signed update version does not match the GitHub release tag.')
    archive_url = github_asset(release, manifest['asset'])
    return version, manifest, archive_url


def download_archive(url, expected_hash, destination):
    digest = hashlib.sha256()
    total_bytes = 0
    response = requests.get(url, stream=True, timeout=RELEASE_API_TIMEOUT)
    response.raise_for_status()
    with destination.open('wb') as archive:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            total_bytes += len(chunk)
            if total_bytes > MAX_ARCHIVE_BYTES:
                raise ValueError('The update archive exceeds the download size limit.')
            digest.update(chunk)
            archive.write(chunk)
    if digest.hexdigest() != expected_hash:
        raise ValueError('The downloaded update checksum does not match the signed manifest.')


def safe_extract(archive_path, destination):
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    total_size = 0
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            path = Path(member.filename)
            if path.is_absolute() or '..' in path.parts:
                raise ValueError('The update archive contains an unsafe path.')
            mode = member.external_attr >> 16
            if mode & 0o170000 == 0o120000:
                raise ValueError('The update archive contains a symbolic link.')
            total_size += member.file_size
            if total_size > MAX_EXTRACTED_BYTES:
                raise ValueError('The extracted update exceeds the size limit.')
            target = (destination / path).resolve()
            if os.path.commonpath((str(root), str(target))) != str(root):
                raise ValueError('The update archive attempts to write outside its staging folder.')
        archive.extractall(destination)

    executable = destination / APP_NAME / f'{APP_NAME}.exe'
    if not executable.is_file():
        raise ValueError('The update archive does not contain the application executable.')


def install_release(version, manifest, archive_url):
    root = versions_directory()
    root.mkdir(parents=True, exist_ok=True)
    final_directory = root / version
    executable = final_directory / APP_NAME / f'{APP_NAME}.exe'
    if executable.is_file():
        return final_directory

    staging = root / f'.{version}.staging'
    if staging.exists():
        shutil.rmtree(staging)
    try:
        with tempfile.TemporaryDirectory(prefix='cva-update-') as download_dir:
            archive_path = Path(download_dir) / manifest['asset']
            download_archive(archive_url, manifest['sha256'], archive_path)
            safe_extract(archive_path, staging)
        if final_directory.exists():
            shutil.rmtree(final_directory)
        os.replace(staging, final_directory)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return final_directory


def available_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(('127.0.0.1', 0))
        return server.getsockname()[1]


def start_application(version):
    app_directory = versions_directory() / version / APP_NAME
    executable = app_directory / f'{APP_NAME}.exe'
    if not executable.is_file():
        raise FileNotFoundError(f'Application executable not found for version {version}.')

    port = available_port()
    data_directory = install_root() / 'data'
    data_directory.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment['ENG_APP_PORT'] = str(port)
    environment['ENG_APP_DATA_DIR'] = str(data_directory)
    creation_flags = 0
    if os.name == 'nt':
        creation_flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(
        [str(executable)],
        cwd=str(app_directory),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )
    app_url = f'http://127.0.0.1:{port}/'
    deadline = time.monotonic() + POLL_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f'Application version {version} exited during startup.')
        try:
            response = requests.get(app_url, timeout=0.5)
            if response.status_code < 500:
                webbrowser.open(app_url)
                return process
        except requests.RequestException:
            time.sleep(0.25)
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
    raise RuntimeError(f'Application version {version} did not become ready in time.')


def save_current_version(version):
    root = install_root()
    root.mkdir(parents=True, exist_ok=True)
    version_file = root / 'current-version.txt'
    temporary_file = root / 'current-version.txt.tmp'
    temporary_file.write_text(version, encoding='utf-8')
    os.replace(temporary_file, version_file)


def notify(message, error=False):
    logging.error(message) if error else logging.warning(message)
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        if error:
            messagebox.showerror('CVA Let\'s Speak', message)
        else:
            messagebox.showwarning('CVA Let\'s Speak', message)
        root.destroy()
    except Exception as exc:
        logging.warning('Could not display the launcher notification: %s', exc)


def main():
    root = install_root()
    root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=root / 'launcher.log',
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
    )
    current = read_current_version()
    try:
        version, manifest, archive_url = fetch_latest_manifest()
        version_directory = install_release(version, manifest, archive_url)
        try:
            start_application(version)
            save_current_version(version)
            return 0
        except Exception as startup_error:
            logging.exception('Updated application failed to start.')
            previous = current if current and current != version else next(
                (item for item in installed_versions() if item != version),
                None,
            )
            if not previous:
                raise startup_error
            start_application(previous)
            notify(f'Update {version} could not start. Reopened version {previous}.')
            return 0
    except Exception as update_error:
        logging.exception('Update check or installation failed.')
        fallback = current or next(iter(installed_versions()), None)
        if fallback:
            try:
                start_application(fallback)
                notify(f'Could not verify or install the latest update. Running version {fallback} instead.')
                return 0
            except Exception:
                logging.exception('The previously installed application could not start.')
        notify(str(update_error), error=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

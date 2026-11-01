import base64
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import secrets
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import gspread
import requests
from flask import Flask, abort, has_request_context, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from dotenv import load_dotenv
from google.oauth2 import service_account
from openpyxl import load_workbook
from werkzeug.utils import secure_filename

from translations import TRANSLATIONS

try:
    from googleapiclient.discovery import build as google_drive_build
    from googleapiclient.http import MediaFileUpload
except Exception:  # pragma: no cover - optional runtime dependency
    google_drive_build = None
    MediaFileUpload = None

IS_FROZEN = bool(getattr(sys, 'frozen', False))
BASE_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)).resolve()
if not IS_FROZEN:
    load_dotenv(BASE_DIR / '.env', override=False)

BASE_DIR.mkdir(parents=True, exist_ok=True)
configured_data_dir = os.getenv('ENG_APP_DATA_DIR', '').strip()
if configured_data_dir:
    DATA_DIR = Path(configured_data_dir).expanduser()
elif IS_FROZEN:
    DATA_DIR = Path(os.getenv('LOCALAPPDATA') or (Path.home() / 'AppData' / 'Local')) / 'CVA' / 'EngClassAI' / 'data'
else:
    DATA_DIR = BASE_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)

UPLOAD_DIR = DATA_DIR / 'uploads'
AUDIO_DIR = UPLOAD_DIR / 'audio'
VIDEO_DIR = UPLOAD_DIR / 'video'
DATA_FILE = DATA_DIR / 'eng_app.db'
PUBLIC_CLOUD_CONFIG_PATH = BASE_DIR / 'static' / 'js' / 'cloud-config.json'
PUBLIC_CLOUD_CONFIG = (
    json.loads(PUBLIC_CLOUD_CONFIG_PATH.read_text(encoding='utf-8'))
    if PUBLIC_CLOUD_CONFIG_PATH.is_file()
    else {}
)
VERCEL_API_BASE_URL = os.getenv(
    'VERCEL_API_BASE_URL',
    str(PUBLIC_CLOUD_CONFIG.get('vercel_api_base_url') or ''),
).strip().rstrip('/')
_GOOGLE_SHEET_READ_STATUS = {}

for folder in (UPLOAD_DIR, AUDIO_DIR, VIDEO_DIR):
    folder.mkdir(parents=True, exist_ok=True)

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / 'templates'),
    static_folder=str(BASE_DIR / 'static'),
)
def get_app_secret_key():
    configured_secret = os.getenv('APP_SECRET_KEY', '').strip()
    if configured_secret:
        return configured_secret
    secret_path = DATA_DIR / '.session-secret'
    if secret_path.is_file():
        existing_secret = secret_path.read_text(encoding='utf-8').strip()
        if existing_secret:
            return existing_secret
    generated_secret = secrets.token_urlsafe(48)
    temporary_path = secret_path.with_name(f'{secret_path.name}.tmp')
    temporary_path.write_text(generated_secret, encoding='utf-8')
    temporary_path.replace(secret_path)
    return generated_secret


app.secret_key = get_app_secret_key()
app.config['UPLOAD_FOLDER'] = str(UPLOAD_DIR)
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
DRIVE_FOLDER_CACHE_TTL = 300
_drive_folder_cache = {}
PAGE_SIZE_OPTIONS = (5, 10, 20, 50, 100)
USER_SHEET_NAME = 'Users'
HEADTEACHER_SHEET_NAME = 'HeadTeacher'


def remote_ai_enabled():
    return os.getenv('ENABLE_REMOTE_AI', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}


def vercel_auth_enabled():
    auth_mode = os.getenv('ENG_AUTH_MODE', '').strip().lower()
    return IS_FROZEN or auth_mode in {'vercel', 'cloud'}


def is_headteacher(user):
    if not user:
        return False
    user_fields = set(user.keys()) if hasattr(user, 'keys') else set(user)
    user_id = user['id'] if 'id' in user_fields else None
    role = str(user['role'] if 'role' in user_fields else '').strip().lower()
    if role != 'teacher':
        return False
    if (
        user_id is not None
        and has_request_context()
        and session.get('headteacher_claim_user_id') == user_id
    ):
        return session.get('headteacher_user_id') == user_id
    username = user['username'] if 'username' in user_fields else ''
    return normalize_username(username) == 'huynt'


@app.context_processor
def inject_headteacher_navigation():
    current_user = require_login()
    return {'is_headteacher': is_headteacher(current_user)}


def upsert_cloud_login_user(conn, login_result, role):
    account_email = str(login_result.get('email') or '').strip().lower()
    account_username = str(login_result.get('username') or '').strip()
    account_name = str(login_result.get('full_name') or account_username).strip()
    account_class = str(login_result.get('class_name') or '').strip()
    user_by_email = conn.execute(
        'SELECT * FROM users WHERE lower(email) = ? LIMIT 1',
        (account_email,),
    ).fetchone()
    user_by_username = conn.execute(
        'SELECT * FROM users WHERE lower(username) = ? AND role = ? ORDER BY id LIMIT 1',
        (account_username.lower(), role),
    ).fetchone() if account_username else None
    headteacher_login = role == 'teacher' and normalize_username(account_username) == 'huynt'

    user = user_by_username if headteacher_login and user_by_username else user_by_email
    duplicate = None
    if user_by_email and user_by_username and user_by_email['id'] != user_by_username['id']:
        if headteacher_login and user_by_email['role'] == user_by_username['role'] == 'teacher':
            user = user_by_username
            duplicate = user_by_email
        else:
            user = user_by_email

    if duplicate:
        conn.execute(
            'UPDATE submissions SET student_id = ? WHERE student_id = ?',
            (user['id'], duplicate['id']),
        )
        conn.execute('DELETE FROM cloud_tokens WHERE user_id = ?', (duplicate['id'],))
        conn.execute('DELETE FROM users WHERE id = ?', (duplicate['id'],))

    if user:
        conn.execute(
            '''
            UPDATE users
            SET full_name = ?, username = ?, email = ?, role = ?, class_name = ?, is_active = 1
            WHERE id = ?
            ''',
            (
                account_name or account_email,
                account_username or user['username'],
                account_email,
                role,
                account_class or user['class_name'],
                user['id'],
            ),
        )
        return conn.execute('SELECT * FROM users WHERE id = ?', (user['id'],)).fetchone()

    local_username = choose_unique_username(
        conn,
        account_username or account_name,
        account_email,
    )
    conn.execute(
        '''
        INSERT INTO users
            (full_name, username, email, password, role, class_name, is_active, total_points, average_score)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            account_name or account_email,
            local_username,
            account_email,
            secrets.token_urlsafe(32),
            role,
            account_class,
            1,
            0,
            0,
        ),
    )
    return conn.execute(
        'SELECT * FROM users WHERE lower(email) = ? LIMIT 1',
        (account_email,),
    ).fetchone()


def call_vercel_api(endpoint, payload, access_token=None, timeout=30):
    if not VERCEL_API_BASE_URL or (endpoint != 'login' and not access_token):
        logger.warning('Vercel API call skipped: endpoint or session token is not configured.')
        return None
    headers = {'Content-Type': 'application/json'}
    if access_token:
        headers['Authorization'] = f'Bearer {access_token}'
    try:
        response = requests.post(
            f'{VERCEL_API_BASE_URL}/api/{endpoint.lstrip("/")}',
            json=payload,
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        logger.warning('Vercel API %s request failed: %s', endpoint, exc)
        return None

    if endpoint == 'login':
        request_id = response.headers.get('x-vercel-id', 'unavailable')
        logger.info(
            'Vercel sign-in endpoint responded with HTTP %s (request id %s).',
            response.status_code,
            request_id,
        )

    if response.status_code != 200 and endpoint != 'login':
        logger.warning('Vercel API %s returned HTTP %s.', endpoint, response.status_code)
        return None
    try:
        result = response.json()
    except ValueError:
        logger.warning('Vercel API %s returned invalid JSON.', endpoint)
        if endpoint == 'login':
            return {
                'status_code': response.status_code,
                'error': (
                    f'The sign-in service returned HTTP {response.status_code} '
                    'without a valid response. Check the Vercel Runtime Logs.'
                ),
            }
        return None
    if isinstance(result, dict) and endpoint == 'login':
        result.setdefault('status_code', response.status_code)
    elif endpoint == 'login':
        return {
            'status_code': response.status_code,
            'error': (
                f'The sign-in service returned HTTP {response.status_code} '
                'with an invalid response. Check the Vercel Runtime Logs.'
            ),
        }
    return result if isinstance(result, dict) else None


def evaluate_with_vercel_function(question, answer, expected, access_token, provider='gemini'):
    if not answer or not access_token:
        return None
    result = call_vercel_api(
        'evaluate',
        {'question': question, 'answer': answer, 'expected': expected, 'provider': provider},
        access_token,
        timeout=35,
    )
    if not result:
        return None
    try:
        return {
            'score': max(0, min(100, int(result.get('score', 0)))),
            'feedback': str(result.get('feedback') or 'Keep practicing.')[:2000],
            'pronunciation': max(0, min(100, float(result.get('pronunciation', 0) or 0))),
            'grammar': max(0, min(100, float(result.get('grammar', 0) or 0))),
            'fluency': max(0, min(100, float(result.get('fluency', 0) or 0))),
            'relevance': max(0, min(100, float(result.get('relevance', 0) or 0))),
            'confidence': max(0, min(100, float(result.get('confidence', 0) or 0))),
        }
    except (TypeError, ValueError):
        logger.warning('Vercel evaluation returned an invalid score payload.')
        return None


def upload_media_to_vercel_drive(file_path, file_name, media_type, access_token):
    path = Path(file_path)
    if not access_token or not path.is_file():
        return None
    try:
        file_size = path.stat().st_size
        if not file_size or file_size > 18 * 1024 * 1024:
            logger.warning('Google Drive media upload skipped: file is empty or exceeds 18 MB.')
            return None
        import mimetypes

        suffix = path.suffix.lower()
        if suffix == '.webm':
            content_type = 'audio/webm' if media_type == 'audio' else 'video/webm'
        elif suffix in {'.m4a', '.mp4a'}:
            content_type = 'audio/mp4'
        elif suffix == '.ogg':
            content_type = 'audio/ogg'
        elif suffix == '.wav':
            content_type = 'audio/wav'
        elif suffix == '.mp4':
            content_type = 'video/mp4'
        else:
            content_type = mimetypes.guess_type(path.name)[0] or (
                'audio/mpeg' if media_type == 'audio' else 'video/mp4'
            )
        result = call_vercel_api(
            'media-upload',
            {
                'file_name': str(file_name or path.name),
                'mime_type': content_type,
                'file_size': file_size,
            },
            access_token,
            timeout=30,
        )
        upload_url = str((result or {}).get('upload_url') or '')
        if not upload_url:
            return None
        with path.open('rb') as media_file:
            upload_response = requests.put(
                upload_url,
                data=media_file,
                headers={'Content-Type': content_type, 'Content-Length': str(file_size)},
                timeout=120,
            )
        if upload_response.status_code not in {200, 201}:
            logger.warning('Google Drive resumable upload returned HTTP %s.', upload_response.status_code)
            return None
        try:
            uploaded_file = upload_response.json()
        except ValueError:
            logger.warning('Google Drive upload returned invalid file metadata.')
            return None
        file_id = str(uploaded_file.get('id') or '')
        if not file_id:
            logger.warning('Google Drive upload did not return a file ID.')
            return None
        completed = call_vercel_api(
            'media-complete',
            {'file_id': file_id},
            access_token,
            timeout=30,
        )
        if not completed:
            return None
        return {
            'file_id': file_id,
            'drive_url': str(completed.get('drive_url') or ''),
        }
    except OSError as exc:
        logger.warning('Could not read media for Google Drive upload: %s', exc)
        return None
    except requests.RequestException as exc:
        logger.warning('Google Drive resumable upload failed: %s', exc)
        return None


def transcribe_with_vercel_function(file_path, access_token, drive_file_id=None):
    path = Path(file_path)
    if not access_token:
        return None
    file_id = str(drive_file_id or '')
    if not file_id:
        if not path.is_file():
            return None
        upload = upload_media_to_vercel_drive(path, path.name, 'audio', access_token)
        file_id = str((upload or {}).get('file_id') or '')
        if not file_id:
            return None
    result = call_vercel_api(
        'transcribe',
        {'audio_name': path.name if path.is_file() else 'recording.webm', 'audio_file_id': file_id},
        access_token,
        timeout=120,
    )
    transcript = result.get('transcript') if result else None
    return str(transcript).strip()[:5000] if transcript else None


def call_google_function(operation, payload=None):
    tokens = get_cloud_tokens()
    if not tokens:
        message = 'Google sync requires an active Vercel session. Please sign out and sign in again.'
        logger.warning(message)
        set_session_message('google_sync_warning', message)
        return None

    function_payload = {'operation': operation}
    function_payload.update(payload or {})
    result = call_vercel_api(
        'google',
        function_payload,
        tokens['access_token'],
        timeout=60,
    )
    if not result or result.get('ok') is not True:
        message = (result or {}).get('error') or 'The Google sync service could not complete the request.'
        set_session_message('google_sync_warning', message)
        logger.warning('Vercel Google operation %s failed.', operation)
        return None
    return result


def verify_cloud_request_user(user, access_token):
    claims = call_vercel_api(
        'session',
        {},
        access_token,
        timeout=15,
    )
    if not claims:
        return False
    expected_email = str(user['email'] or '').strip().lower()
    valid = (
        str(claims.get('uid') or '') == expected_email
        and str(claims.get('email') or '').strip().lower() == expected_email
        and str(claims.get('role') or '') == str(user['role'] or '')
    )
    if not valid:
        return False
    return save_cloud_token(user['id'], access_token)


def get_page_size():
    try:
        page_size = int(request.args.get('per_page', 10))
    except (TypeError, ValueError):
        return 10
    return page_size if page_size in PAGE_SIZE_OPTIONS else 10


def set_session_message(key, value):
    if has_request_context():
        session[key] = value
    return value


def set_google_sync_feedback(sheet_name, action, synced):
    if synced:
        message = f'{action} and synced to the Google Sheets {sheet_name} sheet.'
    else:
        message = f'{action} saved locally, but could not sync to the Google Sheets {sheet_name} sheet. Check the Google Sheets configuration and logs.'
    set_session_message('google_sync_warning', message)
    set_session_message('google_sync_success', bool(synced))


def get_session_message(key, default=None):
    if has_request_context():
        return session.get(key, default)
    return default


def _slugify_media_name(value, fallback='item'):
    text = (value or fallback).strip()
    text = text.replace('_', ' ')
    text = re.sub(r'[^a-zA-Z0-9\s-]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    text = text.replace(' ', '_')
    return text or fallback


def build_drive_media_name(media_type='audio', lesson_title='', student_name='', original_extension='', timestamp=None):
    prefix = 'Audio' if str(media_type).lower() == 'audio' else 'Video'
    lesson_fragment = _slugify_media_name(lesson_title, 'lesson')
    student_fragment = _slugify_media_name(student_name, 'student')
    if timestamp is None:
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    ext = (original_extension or 'mp3' if str(media_type).lower() == 'audio' else 'mp4').strip('.').lower()
    if not ext:
        ext = 'mp3' if str(media_type).lower() == 'audio' else 'mp4'
    return f'{prefix}_{lesson_fragment}_{student_fragment}_{timestamp}.{ext}'


def load_env_file():
    env_path = BASE_DIR / '.env'
    if not env_path.exists():
        return

    lines = env_path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line or line.startswith('#'):
            i += 1
            continue
        if '=' not in line:
            i += 1
            continue

        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip()

        if value.startswith('{') and not value.endswith('}'):
            buffer = value
            while i + 1 < len(lines) and '}' not in buffer:
                i += 1
                buffer += '\n' + lines[i].rstrip()
            value = buffer

        if value and value[0] in {'"', "'"} and value[-1] == value[0]:
            value = value[1:-1]

        # Keep \n escape sequences intact for JSON-based credentials so the service-account JSON remains valid.
        if not (value.startswith('{') and value.endswith('}')):
            value = value.replace('\\n', '\n').replace('\\r', '\r')
        value = value.replace('\\"', '"').replace("\\'", "'")
        os.environ.setdefault(key, value)
        i += 1


load_env_file()
app.secret_key = os.getenv('APP_SECRET_KEY', app.secret_key)

def get_db():
    conn = sqlite3.connect(DATA_FILE, timeout=30)
    conn.execute('PRAGMA busy_timeout = 30000')
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cloud_tokens (
            user_id INTEGER PRIMARY KEY,
            access_token TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS app_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            username TEXT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL,
            class_name TEXT,
            is_active INTEGER DEFAULT 1,
            total_points INTEGER DEFAULT 0,
            average_score REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title_en TEXT NOT NULL,
            title_vi TEXT NOT NULL,
            level TEXT NOT NULL,
            class_level INTEGER DEFAULT 6,
            topic_en TEXT NOT NULL,
            topic_vi TEXT NOT NULL,
            goal_en TEXT NOT NULL,
            goal_vi TEXT NOT NULL,
            video_url TEXT,
            vocabulary TEXT,
            dialogue TEXT,
            sample_question TEXT,
            speaking_question TEXT,
            expected_answer TEXT,
            sentence_words TEXT,
            exercise_text_vi TEXT,
            exercise_text_en TEXT,
            task_3_text TEXT,
            task_4_text TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            lesson_id INTEGER NOT NULL,
            answer TEXT,
            score INTEGER,
            feedback TEXT,
            audio_path TEXT,
            drive_url TEXT,
            teacher_note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    submission_columns = [column['name'] for column in conn.execute('PRAGMA table_info(submissions)').fetchall()]
    if 'teacher_note' not in submission_columns:
        conn.execute('ALTER TABLE submissions ADD COLUMN teacher_note TEXT')
    if 'drive_url' not in submission_columns:
        conn.execute('ALTER TABLE submissions ADD COLUMN drive_url TEXT')

    user_columns = [column['name'] for column in conn.execute('PRAGMA table_info(users)').fetchall()]
    for column_name, column_type in {
        'username': 'TEXT',
        'is_active': 'INTEGER DEFAULT 1',
        'total_points': 'INTEGER DEFAULT 0',
        'average_score': 'REAL DEFAULT 0',
    }.items():
        if column_name not in user_columns:
            conn.execute(f'ALTER TABLE users ADD COLUMN {column_name} {column_type}')

    lesson_columns = [column['name'] for column in conn.execute('PRAGMA table_info(lessons)').fetchall()]
    for column_name, column_sql in {
        'speaking_question': 'TEXT',
        'week_number': 'INTEGER DEFAULT 1',
        'open_date': 'TEXT',
        'close_date': 'TEXT',
        'class_level': 'INTEGER DEFAULT 6',
        'task_3_text': 'TEXT',
        'task_4_text': 'TEXT',
    }.items():
        if column_name not in lesson_columns:
            conn.execute(f'ALTER TABLE lessons ADD COLUMN {column_name} {column_sql}')

    lesson_class_column = conn.execute('PRAGMA table_info(lessons)').fetchall()
    class_column = next((column for column in lesson_class_column if column['name'] == 'class_level'), None)
    if class_column and str(class_column['type']).upper().strip() not in {'INTEGER', 'INT'}:
        conn.execute('ALTER TABLE lessons RENAME TO lessons_legacy_class_text')
        conn.execute('''
            CREATE TABLE lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title_en TEXT NOT NULL,
                title_vi TEXT NOT NULL,
                level TEXT NOT NULL,
                class_level INTEGER DEFAULT 6,
                topic_en TEXT NOT NULL,
                topic_vi TEXT NOT NULL,
                goal_en TEXT NOT NULL,
                goal_vi TEXT NOT NULL,
                video_url TEXT,
                vocabulary TEXT,
                dialogue TEXT,
                sample_question TEXT,
                speaking_question TEXT,
                expected_answer TEXT,
                sentence_words TEXT,
                exercise_text_vi TEXT,
                exercise_text_en TEXT,
                task_3_text TEXT,
                task_4_text TEXT,
                week_number INTEGER DEFAULT 1,
                open_date TEXT,
                close_date TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        legacy_columns = {column['name'] for column in conn.execute('PRAGMA table_info(lessons_legacy_class_text)').fetchall()}
        source_columns = [
            name for name in (
                'id', 'title_en', 'title_vi', 'level', 'class_level', 'topic_en', 'topic_vi',
                'goal_en', 'goal_vi', 'video_url', 'vocabulary', 'dialogue', 'sample_question',
                'speaking_question', 'expected_answer', 'sentence_words', 'exercise_text_vi',
                'exercise_text_en', 'task_3_text', 'task_4_text', 'week_number', 'open_date',
                'close_date', 'created_at'
            ) if name in legacy_columns
        ]
        selected = ', '.join(source_columns)
        target_columns = list(source_columns)
        conn.execute(f'INSERT INTO lessons ({", ".join(target_columns)}) SELECT {selected} FROM lessons_legacy_class_text')
        conn.execute('UPDATE lessons SET class_level = CAST(class_level AS INTEGER) WHERE class_level IS NOT NULL AND TRIM(CAST(class_level AS TEXT)) != ""')
        conn.execute('DROP TABLE lessons_legacy_class_text')

    conn.execute("UPDATE lessons SET class_level = CAST(class_level AS INTEGER) WHERE typeof(class_level) != 'integer' AND class_level IS NOT NULL AND TRIM(CAST(class_level AS TEXT)) != ''")
    conn.execute("UPDATE lessons SET level = '' WHERE TRIM(COALESCE(level, '')) IN ('Teacher add-on', 'Google Sheet import')")

    user_rows = conn.execute('SELECT id, full_name, username, email FROM users ORDER BY id').fetchall()
    seen_usernames = {}
    for user in user_rows:
        base = normalize_username(user['username'] or user['full_name'] or user['email'] or 'student')
        if not base:
            base = 'student'
        final_username = base
        suffix = 1
        while final_username in seen_usernames:
            final_username = f'{base}{suffix}'
            suffix += 1
        seen_usernames[final_username] = user['id']
        conn.execute('UPDATE users SET username = ? WHERE id = ?', (final_username, user['id']))

    conn.execute('UPDATE users SET is_active = 1 WHERE is_active IS NULL')
    conn.execute('UPDATE users SET total_points = 0 WHERE total_points IS NULL')
    conn.execute('UPDATE users SET average_score = 0 WHERE average_score IS NULL')

    use_demo_data = not vercel_auth_enabled()
    if use_demo_data:
        from dev_seed_data import seed_demo_data

        seed_demo_data(conn)

    conn.commit()
    conn.close()


def save_cloud_token(user_id, access_token):
    if not user_id or not access_token:
        return False
    conn = get_db()
    try:
        conn.execute(
            '''
            INSERT INTO cloud_tokens (user_id, access_token, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                access_token = excluded.access_token,
                updated_at = CURRENT_TIMESTAMP
            ''',
            (user_id, access_token),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_cloud_tokens(user_id=None):
    user_id = user_id or session.get('user_id')
    if not user_id:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            'SELECT access_token FROM cloud_tokens WHERE user_id = ?',
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


@app.context_processor
def inject_i18n():
    lang = session.get('lang', 'en') or 'en'
    return {
        'lang': lang,
        't': lambda key: TRANSLATIONS.get(lang, TRANSLATIONS['en']).get(key, key),
    }


@app.template_filter('format_answer')
def format_answer_filter(value):
    if not value:
        return '—'
    raw_str = str(value).strip()
    if raw_str.startswith('{'):
        try:
            payload = json.loads(raw_str)
            if isinstance(payload, dict):
                parts = []
                if payload.get('task_1_answer'):
                    parts.append(f"T1: {payload['task_1_answer']}")
                if payload.get('task_2_answer'):
                    parts.append(f"T2: {payload['task_2_answer']}")
                if payload.get('task_3_transcript'):
                    parts.append(f"T3: {payload['task_3_transcript']}")
                if parts:
                    return ' | '.join(parts)
                teacher_eval = payload.get('evaluation', {}).get('task_1', {}).get('teacher', {}) if isinstance(payload.get('evaluation'), dict) else {}
                if teacher_eval.get('feedback'):
                    return f"Nhận xét GV: {teacher_eval.get('feedback')}"
                return 'Đã nộp bài (Có đánh giá)'
        except Exception:
            pass
    return raw_str


def require_login():
    user_id = session.get('user_id')
    if not user_id:
        return None
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    return user


def normalize_text(text):
    return re.sub(r'[^a-zA-Z0-9\s]', '', str(text).lower()).strip()


def extract_grade_level(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r'(\d+)', text)
    return match.group(1) if match else None


def normalize_youtube_embed_url(video_url):
    url = (video_url or '').strip()
    if not url:
        return ''

    parsed = urlparse(url if '://' in url else f'https://{url}')
    host = (parsed.netloc or parsed.path).lower()
    path = parsed.path or ''

    if 'youtu.be' in host:
        short_id = path.strip('/').split('/')[0]
        if not short_id:
            return url
        query_params = parse_qs(parsed.query)
        if query_params:
            return f'https://www.youtube.com/embed/{short_id}?{urlencode(query_params, doseq=True)}'
        return f'https://www.youtube.com/embed/{short_id}'

    if 'youtube.com' in host:
        if '/embed/' in path:
            return url
        video_id = None
        if path.startswith('/shorts/'):
            video_id = path.split('/shorts/', 1)[1].split('/', 1)[0]
        else:
            query_params = parse_qs(parsed.query)
            video_id = query_params.get('v', [None])[0]
        if not video_id:
            return url
        filtered_query = parse_qs(parsed.query)
        filtered_query.pop('v', None)
        embed_url = f'https://www.youtube.com/embed/{video_id}'
        if filtered_query:
            embed_url += f'?{urlencode(filtered_query, doseq=True)}'
        return embed_url

    return url


app.jinja_env.globals['normalize_youtube_embed_url'] = normalize_youtube_embed_url


def get_lessons_for_student(student):
    conn = get_db()

    student_data = student
    if student_data is not None and not isinstance(student_data, dict):
        if hasattr(student_data, 'keys'):
            student_data = dict(student_data)
        elif hasattr(student_data, '__dict__'):
            student_data = vars(student_data)
        else:
            student_data = {}

    class_name = (student_data or {}).get('class_name') if isinstance(student_data, dict) else ''
    grade_level = extract_grade_level(class_name or '')
    if grade_level:
        rows = conn.execute(
            'SELECT * FROM lessons WHERE class_level = ? OR class_level IS NULL OR class_level = "" ORDER BY id',
            (grade_level,)
        ).fetchall()
    else:
        rows = conn.execute('SELECT * FROM lessons ORDER BY id').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def extract_json_payload(text):
    if not text:
        return None
    cleaned = str(text).strip()
    if cleaned.startswith('```'):
        cleaned = cleaned.strip('`')
        if cleaned.lower().startswith('json'):
            cleaned = cleaned[4:].strip()
    cleaned = cleaned.strip()
    if cleaned.startswith('```'):
        cleaned = cleaned.strip('`')
    if not cleaned:
        return None
    try:
        return json.loads(cleaned)
    except (TypeError, ValueError):
        try:
            start = cleaned.find('{')
            end = cleaned.rfind('}')
            if start != -1 and end != -1 and end > start:
                return json.loads(cleaned[start:end + 1])
        except (TypeError, ValueError):
            return None
    return None


def get_gemini_client():
    api_key = os.getenv('GEMINI_API_KEY', '').strip() or os.getenv('GOOGLE_API_KEY', '').strip()
    if not api_key:
        return None
    return {'api_key': api_key}


def evaluate_with_gemini(question, answer, expected):
    if IS_FROZEN or not remote_ai_enabled():
        return None
    client = get_gemini_client()
    if not client:
        return None

    system_prompt = (
        'You are an English speaking evaluator. Score the answer from 0 to 100, '
        'with clear rubric values for pronunciation, grammar, fluency, relevance, and confidence. '
        'Return JSON with only: {"score": int, "feedback": str, "pronunciation": float, "grammar": float, "fluency": float, "relevance": float, "confidence": float}'
    )
    user_prompt = (
        f'Question: {question}\n'
        f'Expected answer: {expected}\n'
        f'Student answer: {answer}\n'
        'Give a short but useful feedback in Vietnamese if possible.'
    )

    gemini_model = os.getenv('GEMINI_MODEL', 'gemini-3.5-flash').strip() or 'gemini-3.5-flash'
    candidate_models = [gemini_model, 'gemini-3.5-flash', 'gemini-flash-latest', 'gemini-3.8-flash', 'gemini-2.5-flash-lite']
    seen = set()
    models_to_try = [m for m in candidate_models if not (m in seen or seen.add(m))]

    payload = {
        'contents': [{
            'parts': [
                {'text': f'{system_prompt}\n\n{user_prompt}'}
            ]
        }],
        'generationConfig': {
            'temperature': 0.2,
            'responseMimeType': 'application/json',
        },
    }

    last_error = None
    for model in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={client['api_key']}"
        try:
            response = requests.post(url, json=payload, timeout=30)
            if response.status_code != 200:
                last_error = f"{response.status_code}: {response.text}"
                continue

            data = response.json()
            if not data.get('candidates'):
                continue
            text = ''
            for candidate in data.get('candidates', []):
                for part in candidate.get('content', {}).get('parts', []):
                    if isinstance(part, dict) and 'text' in part:
                        text = part.get('text', '')
                        break
                if text:
                    break
            if not text:
                continue

            result = extract_json_payload(text)
            if not isinstance(result, dict):
                continue

            return {
                'score': int(result.get('score', 0)),
                'feedback': result.get('feedback', 'Keep practicing.'),
                'pronunciation': float(result.get('pronunciation', 0) or 0),
                'grammar': float(result.get('grammar', 0) or 0),
                'fluency': float(result.get('fluency', 0) or 0),
                'relevance': float(result.get('relevance', 0) or 0),
                'confidence': float(result.get('confidence', 0) or 0),
            }
        except Exception as exc:
            last_error = exc
            continue

    if last_error:
        logger.warning('Gemini evaluation failed after trying models %s: %s', models_to_try, last_error)
    return None


def evaluate_with_groq(question, answer, expected):
    if IS_FROZEN or not remote_ai_enabled():
        return None
    groq_api_key = os.getenv('GROQ_API_KEY', '').strip()
    if not groq_api_key or not answer:
        return None
    try:
        from groq import Groq
        client = Groq(api_key=groq_api_key)
        preferred_model = os.getenv('GROQ_EVAL_MODEL', 'qwen/qwen3.8-27b').strip() or 'qwen/qwen3.8-27b'
        candidate_models = [preferred_model, 'qwen/qwen3.8-27b']
        seen = set()
        models_to_try = [m for m in candidate_models if not (m in seen or seen.add(m))]
        system_prompt = (
            'You are an English speaking and writing evaluator. Score the student answer from 0 to 100, '
            'and evaluate criteria: pronunciation (0-100), grammar (0-100), fluency (0-100), relevance (0-100), confidence (0-100).\n'
            'Respond ONLY with a valid JSON object matching this schema:\n'
            '{"score": 85, "feedback": "Brief feedback in Vietnamese", "pronunciation": 80, "grammar": 85, "fluency": 85, "relevance": 90, "confidence": 85}'
        )
        user_prompt = f"Question: {question}\nExpected answer: {expected}\nStudent answer: {answer}"
        last_error = None
        for model in models_to_try:
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {'role': 'system', 'content': system_prompt},
                        {'role': 'user', 'content': user_prompt},
                    ],
                    temperature=0.2,
                    response_format={'type': 'json_object'},
                    timeout=20,
                )
                content = response.choices[0].message.content or ''
                result = extract_json_payload(content)
                if isinstance(result, dict) and 'score' in result:
                    return {
                        'score': int(result.get('score', 0)),
                        'feedback': result.get('feedback', 'Keep practicing.'),
                        'pronunciation': float(result.get('pronunciation', 0) or 0),
                        'grammar': float(result.get('grammar', 0) or 0),
                        'fluency': float(result.get('fluency', 0) or 0),
                        'relevance': float(result.get('relevance', 0) or 0),
                        'confidence': float(result.get('confidence', 0) or 0),
                    }
            except Exception as exc:
                last_error = exc
                continue
        if last_error:
            logger.warning('Groq evaluation failed: %s', last_error)
    except Exception as exc:
        logger.warning('Groq client initialization failed: %s', exc)
    return None


def normalize_username(value):
    if value is None:
        return ''
    cleaned = re.sub(r'[^a-zA-Z0-9]+', '', str(value).strip().lower())
    return cleaned.strip()


def generate_username(full_name, email=''):
    base = normalize_username(full_name) or normalize_username(email.split('@')[0] if email else 'student') or 'student'
    return base


def choose_unique_username(conn, full_name, email=''):
    base = generate_username(full_name, email)
    username = base
    suffix = 1
    while True:
        existing = conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
        if not existing:
            return username
        username = f'{base}{suffix}'
        suffix += 1


def evaluate_answer(question, answer, expected):
    cleaned_answer = normalize_text(answer)
    cleaned_expected = normalize_text(expected)
    expected_words = cleaned_expected.split()
    answer_words = cleaned_answer.split()
    overlap = len(set(expected_words) & set(answer_words))
    score = max(0, min(100, round((overlap / max(1, len(expected_words))) * 100)))

    if cleaned_answer == cleaned_expected:
        score = 100
    elif overlap >= max(1, len(expected_words) // 2):
        score = max(score, 70)

    if 'name' in cleaned_answer and 'my' in cleaned_answer:
        score = max(score, 80)

    lang = session.get('lang', 'en') if has_request_context() else 'en'
    if score >= 90:
        feedback = 'Excellent! Your answer is clear and accurate.' if lang == 'en' else 'Xuất sắc! Câu trả lời của bạn rõ ràng và chính xác.'
    elif score >= 75:
        feedback = 'Good job! You are close to the model answer. Try to speak a bit more clearly.' if lang == 'en' else 'Tốt lắm! Bạn gần với câu mẫu. Hãy nói rõ hơn một chút.'
    elif score >= 50:
        feedback = 'You are on the right track. Practice the key vocabulary and sentence pattern again.' if lang == 'en' else 'Bạn đang đi đúng hướng. Hãy luyện lại từ vựng và mẫu câu quan trọng.'
    else:
        feedback = 'Keep practicing. Try to use the lesson pattern and repeat the key words clearly.' if lang == 'en' else 'Hãy luyện tập thêm. Hãy dùng mẫu câu và nhắc lại từ khóa rõ ràng hơn.'

    pronunciation = min(10, max(0, round(score / 10, 1)))
    grammar = min(10, max(0, round((score + 15) / 10, 1)))
    fluency = min(10, max(0, round((score + 10) / 10, 1)))

    return {
        'score': score,
        'pronunciation': pronunciation,
        'grammar': grammar,
        'fluency': fluency,
        'feedback': feedback,
    }


def evaluate_sentence_reorder(user_sentence, expected_sentence):
    expected_text = (expected_sentence or '').strip()
    user_text = (user_sentence or '').strip()
    if not user_text:
        return {'score': 0, 'feedback': 'No sentence submitted yet.', 'grammar': 0, 'pronunciation': 0, 'fluency': 0}

    expected_words = [part.strip() for part in re.split(r'\s+', normalize_text(expected_text).replace(' ', ' ')) if part.strip()]
    actual_words = [part.strip() for part in re.split(r'\s+', normalize_text(user_text).replace(' ', ' ')) if part.strip()]
    if not actual_words:
        return {'score': 0, 'feedback': 'Sentence is empty.', 'grammar': 0, 'pronunciation': 0, 'fluency': 0}

    expected_norm = normalize_text(expected_text)
    actual_norm = normalize_text(user_text)
    correct_word_count = sum(1 for word in actual_words if word.lower() in {item.lower() for item in expected_words})
    score = 100 if actual_norm == expected_norm else max(0, min(100, round((correct_word_count / max(1, len(expected_words))) * 100)))
    if actual_norm == expected_norm:
        score = 100

    feedback = 'Excellent! The sentence is correctly ordered.' if score >= 90 else 'Almost there. Check the word order again.' if score >= 60 else 'The sentence order is not correct yet. Try again.'
    return {
        'score': score,
        'feedback': feedback,
        'grammar': min(10, round(score / 10, 1)),
        'pronunciation': 0,
        'fluency': min(10, round(score / 10, 1)),
    }


def evaluate_task_1(question, answer, expected):
    ai_result = evaluate_with_gemini(question, answer, expected) or evaluate_with_groq(question, answer, expected)
    if ai_result:
        return ai_result
    return evaluate_answer(question, answer, expected)


def evaluate_task_2(question, answer, expected):
    prompt = 'Check if the sentence order is correct and natural in English.'
    ai_result = evaluate_with_gemini(prompt, answer, expected) or evaluate_with_groq(prompt, answer, expected)
    if ai_result:
        return ai_result
    return evaluate_sentence_reorder(answer, expected)


def resolve_local_media_path(media_path, media_type='audio'):
    if not media_path:
        return None
    p = Path(media_path)
    if p.exists() and p.is_file():
        return p
    clean_name = p.name
    candidates = [
        BASE_DIR / media_path.lstrip('/') if media_path.startswith('/') else BASE_DIR / media_path,
        UPLOAD_DIR / media_type / clean_name,
        UPLOAD_DIR / clean_name,
    ]
    for c in candidates:
        if c.exists() and c.is_file():
            return c
    return None


def transcribe_audio_with_groq(audio_path):
    if IS_FROZEN or not remote_ai_enabled():
        return None
    groq_api_key = os.getenv('GROQ_API_KEY', '').strip()
    if not groq_api_key or not audio_path:
        return None
    path = resolve_local_media_path(audio_path, 'audio')
    if not path:
        return None

    try:
        from groq import Groq
        client = Groq(api_key=groq_api_key)
        model = os.getenv('GROQ_WHISPER_MODEL', 'whisper-large-v3-turbo').strip()
        filename = path.name if '.' in path.name else (path.name + '.webm')
        with path.open('rb') as audio_file:
            result = client.audio.transcriptions.create(
                model=model,
                file=(filename, audio_file.read()),
                response_format='text',
            )
            text = str(result).strip()
            if text:
                logger.info('Groq Whisper transcribed successfully (%d chars)', len(text))
            return text
    except Exception as exc:
        logger.warning('Groq audio transcription failed: %s', exc)
        return None


def transcribe_audio_with_gemini(audio_path):
    if IS_FROZEN or not remote_ai_enabled():
        return None
    gemini_api_key = os.getenv('GEMINI_API_KEY', '').strip()
    if not gemini_api_key or not audio_path:
        return None
    path = resolve_local_media_path(audio_path, 'audio')
    if not path:
        return None

    try:
        import base64
        ext = path.suffix.lower().lstrip('.')
        mime_type = 'audio/mp3' if ext == 'mp3' else 'audio/wav' if ext == 'wav' else 'audio/webm' if ext == 'webm' else 'audio/mpeg'
        with path.open('rb') as f:
            encoded_data = base64.b64encode(f.read()).decode('utf-8')

        model = os.getenv('GEMINI_MODEL', 'gemini-3.5-flash').strip() or 'gemini-3.5-flash'
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_api_key}'
        payload = {
            'contents': [{
                'parts': [
                    {'inlineData': {'mimeType': mime_type, 'data': encoded_data}},
                    {'text': 'Please transcribe this spoken audio into text exactly as spoken in English. Return ONLY the transcription text, with no explanations or extra markdown.'}
                ]
            }]
        }
        res = requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=30)
        if res.status_code == 200:
            data = res.json()
            candidates = data.get('candidates', [])
            if candidates:
                parts = candidates[0].get('content', {}).get('parts', [])
                if parts:
                    text = parts[0].get('text', '').strip()
                    if text:
                        logger.info('Gemini audio transcribed successfully (%d chars)', len(text))
                        return text
        logger.warning('Gemini audio transcription HTTP %s: %s', res.status_code, res.text[:200])
        return None
    except Exception as exc:
        logger.warning('Gemini audio transcription failed: %s', exc)
        return None


def transcribe_audio(audio_path):
    if not audio_path or not remote_ai_enabled():
        return None
    # 1. Groq Whisper (free, fast, high accuracy)
    text = transcribe_audio_with_groq(audio_path)
    if text:
        return text

    # 2. Gemini Multimodal Audio transcription (free)
    text = transcribe_audio_with_gemini(audio_path)
    if text:
        return text

    return None


def analyze_video_with_gemini(video_path, prompt=None):
    """
    Phân tích hành động, ngôn ngữ cơ thể, khẩu hình và ánh mắt của người trong video bằng Gemini Video Understanding.
    """
    gemini_api_key = os.getenv('GEMINI_API_KEY', '').strip()
    if not gemini_api_key or not video_path:
        return None
    path = Path(video_path)
    if not path.exists():
        resolved = BASE_DIR / video_path.lstrip('/') if video_path.startswith('/') else BASE_DIR / video_path
        if not resolved.exists():
            return None
        path = resolved

    try:
        import base64
        ext = path.suffix.lower().lstrip('.')
        mime_type = 'video/mp4' if ext == 'mp4' else 'video/webm' if ext == 'webm' else 'video/quicktime'
        file_size_mb = path.stat().st_size / (1024 * 1024)
        if file_size_mb > 20:
            logger.warning('Video file is %.1f MB, larger than 20MB inline limit for Gemini', file_size_mb)
            return None

        with path.open('rb') as f:
            encoded_video = base64.b64encode(f.read()).decode('utf-8')

        default_prompt = (
            'Analyze the speaker in this English presentation video. Evaluate and provide:\n'
            '1. Body Language & Posture (tư thế đứng/ngồi, cử chỉ tay).\n'
            '2. Eye Contact (ánh mắt có nhìn vào camera hay nhìn tài liệu).\n'
            '3. Facial Expressions & Confidence (nét mặt, sự tự tin hay rụt rè).\n'
            '4. Key actions observed (hành động chính: vẫy tay, giơ ngón tay, v.v.).\n'
            '5. Overall pedagogical constructive advice for the student.'
        )
        model = os.getenv('GEMINI_MODEL', 'gemini-1.5-flash').strip()
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_api_key}'
        payload = {
            'contents': [{
                'parts': [
                    {'inlineData': {'mimeType': mime_type, 'data': encoded_video}},
                    {'text': prompt or default_prompt}
                ]
            }]
        }
        res = requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=60)
        if res.status_code == 200:
            data = res.json()
            candidates = data.get('candidates', [])
            if candidates:
                parts = candidates[0].get('content', {}).get('parts', [])
                if parts:
                    return parts[0].get('text', '').strip()
        logger.warning('Gemini video analysis HTTP %s: %s', res.status_code, res.text[:200])
        return None
    except Exception as exc:
        logger.warning('Gemini video analysis failed: %s', exc)
        return None


def evaluate_task_3(question, answer, expected):
    if answer:
        ai_result = evaluate_with_gemini(question, answer, expected) or evaluate_with_groq(question, answer, expected)
        if ai_result:
            return ai_result
    return evaluate_answer(question, answer or '', expected)


def normalize_practice_sheet_row(row):
    if not isinstance(row, dict):
        row = dict(row or {})

    audio_url = str(row.get('audio_url') or row.get('audio_path') or '').strip()
    video_url = str(row.get('video_url') or '').strip()
    media_recorded = bool(audio_url or video_url or row.get('drive_url') or row.get('audio_path'))
    status_value = 'Recorded' if media_recorded else 'Not recorded'
    return [
        row.get('submitted_at') or row.get('created_at') or '',
        row.get('student_id', ''),
        row.get('student_email', ''),
        row.get('student_name', ''),
        row.get('username', ''),
        row.get('class_name', ''),
        row.get('lesson_id', ''),
        row.get('lesson_title', row.get('lesson_title') or ''),
        row.get('task_1_score', ''),
        row.get('task_2_score', ''),
        row.get('task_3_score', ''),
        row.get('task_4_score', ''),
        row.get('overall_score', ''),
        row.get('feedback', ''),
        row.get('answer_1', ''),
        row.get('answer_2', ''),
        row.get('transcript_3', ''),
        audio_url or row.get('drive_url', ''),
        video_url or row.get('video_url', ''),
        status_value,
    ]


def build_practice_sheet_row(record):
    record = dict(record or {})
    payload = parse_submission_payload(record.get('answer') or '') if isinstance(record, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    evaluation = payload.get('evaluation') if isinstance(payload.get('evaluation'), dict) else {}
    task_1 = evaluation.get('task_1', {}) if isinstance(evaluation, dict) else {}
    task_2 = evaluation.get('task_2', {}) if isinstance(evaluation, dict) else {}
    task_3 = evaluation.get('task_3', {}) if isinstance(evaluation, dict) else {}

    audio_url = str(payload.get('audio_url') or record.get('audio_url') or record.get('audio_path') or '').strip()
    video_url = str(payload.get('video_url') or record.get('video_url') or record.get('drive_url') or '').strip()
    return {
        'submission_id': record.get('id', ''),
        'submitted_at': record.get('submitted_at') or record.get('created_at') or datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'student_id': record.get('student_id', ''),
        'student_email': record.get('student_email', ''),
        'student_name': record.get('student_name') or record.get('full_name') or '',
        'username': record.get('username', ''),
        'class_name': record.get('class_name', ''),
        'lesson_id': record.get('lesson_id', ''),
        'lesson_title': record.get('lesson_title') or record.get('title_en') or record.get('title_vi') or '',
        'task_1_score': record.get('task_1_score', task_1.get('teacher', {}).get('score', task_1.get('ai_preview', {}).get('score', task_1.get('gemini', {}).get('score', task_1.get('groq', {}).get('score', 0))))),
        'task_2_score': record.get('task_2_score', task_2.get('teacher', {}).get('score', task_2.get('ai_preview', {}).get('score', task_2.get('gemini', {}).get('score', task_2.get('groq', {}).get('score', 0))))),
        'task_3_score': record.get('task_3_score', task_3.get('teacher', {}).get('score', task_3.get('ai_preview', {}).get('score', task_3.get('gemini', {}).get('score', task_3.get('groq', {}).get('score', 0))))),
        'task_4_score': record.get('task_4_score', 0),
        'overall_score': record.get('overall_score', record.get('score', 0)),
        'feedback': record.get('feedback', ''),
        'answer_1': record.get('answer_1') or payload.get('task_1_answer') or payload.get('answer_1') or payload.get('answer') or '',
        'answer_2': record.get('answer_2') or payload.get('task_2_answer') or payload.get('answer_2') or payload.get('sentence_words') or '',
        'transcript_3': record.get('transcript_3') or payload.get('task_3_transcript') or payload.get('transcript_3') or '',
        'audio_url': audio_url,
        'video_url': video_url,
        'status': 'Recorded' if (audio_url or video_url or record.get('drive_url') or record.get('audio_path')) else 'Not recorded',
    }


def _practice_sheet_value(row, *aliases):
    normalized = {re.sub(r'[^a-z0-9]+', '', str(key).lower().strip()): value for key, value in row.items()}
    for alias in aliases:
        key = re.sub(r'[^a-z0-9]+', '', alias.lower().strip())
        if key in normalized:
            return '' if normalized[key] is None else str(normalized[key]).strip()
    return ''


def _clean_sheet_score(value, default=0):
    try:
        return max(0, min(100, int(round(float(str(value).strip())))))
    except (TypeError, ValueError):
        return default


def sync_google_practice_into_db(rows=None):
    if rows is None:
        rows = _read_google_worksheet_rows('Practice')
    sheet_read_succeeded = bool(rows) or _google_sheet_read_succeeded('Practice')
    if not sheet_read_succeeded:
        return []

    conn = get_db()
    synced = []
    seen_submission_ids = set()
    students = {str(item['id']): dict(item) for item in conn.execute("SELECT id, full_name, username, email, class_name FROM users WHERE role = 'student'").fetchall()}
    students_by_username = {str(item['username']).strip().lower(): item for item in students.values() if item.get('username')}
    students_by_name = {str(item['full_name']).strip().lower(): item for item in students.values() if item.get('full_name')}
    lessons = {str(item['id']): dict(item) for item in conn.execute('SELECT id, title_en, title_vi FROM lessons').fetchall()}
    lessons_by_title = {}
    for lesson in lessons.values():
        for key in ('title_en', 'title_vi'):
            if lesson.get(key):
                lessons_by_title[str(lesson[key]).strip().lower()] = lesson

    for row in rows:
        submission_id = _practice_sheet_value(row, 'submission_id', 'Submission ID', 'id', 'ID')
        student_id = _practice_sheet_value(row, 'student_id', 'Student ID')
        student_name = _practice_sheet_value(row, 'student_name', 'Student Name', 'full_name', 'Full Name')
        username = _practice_sheet_value(row, 'username', 'Username')
        class_name = _practice_sheet_value(row, 'class_name', 'Class Name', 'Class')
        lesson_id = _practice_sheet_value(row, 'lesson_id', 'Lesson ID')
        lesson_title = _practice_sheet_value(row, 'lesson_title', 'Lesson Title', 'title_en', 'Title EN', 'Title')

        student = students.get(student_id)
        if student is None and username:
            student = students_by_username.get(username.lower())
        if student is None and student_name:
            student = students_by_name.get(student_name.lower())
        if student is None:
            continue

        lesson = lessons.get(lesson_id)
        if lesson is None and lesson_title:
            lesson = lessons_by_title.get(lesson_title.lower())
        if lesson is None:
            continue

        task_1_score = _clean_sheet_score(_practice_sheet_value(row, 'task_1_score', 'Task 1 Score'))
        task_2_score = _clean_sheet_score(_practice_sheet_value(row, 'task_2_score', 'Task 2 Score'))
        task_3_score = _clean_sheet_score(_practice_sheet_value(row, 'task_3_score', 'Task 3 Score'))
        task_4_score = _clean_sheet_score(_practice_sheet_value(row, 'task_4_score', 'Task 4 Score'))
        overall_score = _clean_sheet_score(_practice_sheet_value(row, 'overall_score', 'Overall Score'), 0)
        feedback = _practice_sheet_value(row, 'feedback', 'Feedback')
        answer_1 = _practice_sheet_value(row, 'answer_1', 'Answer 1', 'answer')
        answer_2 = _practice_sheet_value(row, 'answer_2', 'Answer 2')
        transcript_3 = _practice_sheet_value(row, 'transcript_3', 'Transcript 3', 'Transcript')
        audio_url = _practice_sheet_value(row, 'audio_url', 'Audio URL', 'audio_path', 'Audio Path')
        video_url = _practice_sheet_value(row, 'video_url', 'Video URL')
        submitted_at = _practice_sheet_value(row, 'submitted_at', 'Submitted At', 'created_at', 'Created At')

        existing = None
        if submission_id:
            try:
                existing = conn.execute('SELECT * FROM submissions WHERE id = ?', (int(submission_id),)).fetchone()
            except (TypeError, ValueError):
                existing = None
        if existing is None:
            existing = conn.execute(
                'SELECT * FROM submissions WHERE student_id = ? AND lesson_id = ? AND created_at = ? LIMIT 1',
                (student['id'], lesson['id'], submitted_at),
            ).fetchone() if submitted_at else conn.execute(
                'SELECT * FROM submissions WHERE student_id = ? AND lesson_id = ? ORDER BY created_at DESC LIMIT 1',
                (student['id'], lesson['id']),
            ).fetchone()

        payload = parse_submission_payload(existing['answer']) if existing else {}
        if not isinstance(payload, dict):
            payload = {}
        evaluation = payload.get('evaluation') if isinstance(payload.get('evaluation'), dict) else {}
        for task_name, score in (('task_1', task_1_score), ('task_2', task_2_score), ('task_3', task_3_score)):
            task_payload = evaluation.setdefault(task_name, {}) if isinstance(evaluation, dict) else {}
            if isinstance(task_payload, dict):
                task_payload.setdefault('teacher', {})['score'] = score
        payload['evaluation'] = evaluation
        if answer_1:
            payload['task_1_answer'] = answer_1
        if answer_2:
            payload['task_2_answer'] = answer_2
        if transcript_3:
            payload['task_3_transcript'] = transcript_3
        if video_url:
            payload['video_url'] = video_url
        if audio_url:
            payload['audio_url'] = audio_url

        answer_json = json.dumps(payload, ensure_ascii=False)
        final_score = overall_score if overall_score > 0 else max(task_1_score, task_2_score, task_3_score, task_4_score)

        if existing:
            conn.execute(
                '''UPDATE submissions SET student_id = ?, lesson_id = ?, answer = ?, score = ?, feedback = ?,
                   audio_path = CASE WHEN ? <> '' THEN ? ELSE audio_path END,
                   drive_url = CASE WHEN ? <> '' THEN ? ELSE drive_url END
                   WHERE id = ?''',
                (student['id'], lesson['id'], answer_json, final_score, feedback,
                 audio_url, audio_url, video_url, video_url, existing['id'])
            )
            submission_id_value = existing['id']
        else:
            conn.execute(
                '''INSERT INTO submissions (student_id, lesson_id, answer, score, feedback, audio_path, drive_url, teacher_note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (student['id'], lesson['id'], answer_json, final_score, feedback,
                 audio_url, video_url, '', submitted_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            )
            submission_id_value = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']

        synced.append({'id': submission_id_value, 'student_id': student['id'], 'lesson_id': lesson['id']})
        seen_submission_ids.add(submission_id_value)

    if seen_submission_ids:
        placeholders = ','.join('?' for _ in seen_submission_ids)
        conn.execute(
            f'DELETE FROM submissions WHERE id NOT IN ({placeholders})',
            tuple(seen_submission_ids),
        )
    elif _google_sheet_read_succeeded('Practice') and not rows:
        conn.execute('DELETE FROM submissions')

    conn.commit()
    conn.close()
    return synced


def sync_practice_to_sheet(rows=None):
    try:
        required_headers = ['submitted_at', 'student_id', 'student_email', 'student_name', 'username', 'class_name', 'lesson_id', 'lesson_title', 'task_1_score', 'task_2_score', 'task_3_score', 'task_4_score', 'overall_score', 'feedback', 'answer_1', 'answer_2', 'transcript_3', 'audio_url', 'video_url', 'status']
        if rows is None:
            conn = get_db()
            try:
                student_filter = ''
                query_params = ()
                if IS_FROZEN and session.get('user_role') == 'student':
                    student_filter = 'WHERE s.student_id = ?'
                    query_params = (session.get('user_id'),)
                rows = [
                    build_practice_sheet_row(dict(item)) for item in conn.execute(
                        f'''
                        SELECT s.*, u.full_name AS student_name, u.username, u.email AS student_email, u.class_name,
                               l.title_en AS lesson_title, l.title_vi
                        FROM submissions s
                        JOIN users u ON u.id = s.student_id
                        JOIN lessons l ON l.id = s.lesson_id
                        {student_filter}
                        ORDER BY s.created_at DESC
                        ''',
                        query_params,
                    ).fetchall()
                ]
            finally:
                conn.close()

        sheet_values = [required_headers]
        sheet_values.extend(normalize_practice_sheet_row(row) for row in rows)
        if IS_FROZEN:
            if session.get('user_role') == 'student':
                result = call_google_function(
                    'upsert_practice',
                    {'headers': required_headers, 'rows': sheet_values[1:]},
                )
            else:
                result = call_google_function(
                    'replace_sheet',
                    {'sheet': 'Practice', 'values': sheet_values},
                )
            return bool(result)

        credentials = get_google_credentials()
        if not credentials:
            message = 'Google Sheets sync skipped: credentials are missing or invalid.'
            logger.warning(message)
            set_session_message('google_sync_warning', message)
            return False

        client = gspread.authorize(credentials)
        sheet_id = os.getenv('GOOGLE_SHEET_ID', '').strip()
        sheet_name = os.getenv('GOOGLE_SHEET_NAME', 'EngClassAI')

        try:
            if sheet_id:
                sheet = client.open_by_key(sheet_id)
            else:
                try:
                    sheet = client.open(sheet_name)
                except gspread.SpreadsheetNotFound:
                    sheet = client.create(sheet_name)
                    logger.info('Created missing Google Sheet: %s', sheet_name)
        except Exception as exc:
            logger.warning('Could not open or create spreadsheet %s: %s', sheet_name, exc)
            message = f'Google Sheets sync failed: spreadsheet "{sheet_name}" not found and could not be created automatically. Please check GOOGLE_SERVICE_ACCOUNT_PATH or GOOGLE_SHEET_ID.'
            set_session_message('google_sync_warning', message)
            return False

        worksheet = _get_or_create_worksheet(sheet, 'Practice', rows=5000, cols=30)
        _replace_worksheet_values(worksheet, sheet_values)

        set_session_message('google_sync_warning', 'Google synced')
        return True
    except Exception as exc:
        message = f'Google Sheets sync failed: {exc}'
        logger.exception(message)
        set_session_message('google_sync_warning', message)
        return False


def get_lessons():
    conn = get_db()
    rows = conn.execute('SELECT * FROM lessons ORDER BY id').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _read_google_worksheet_rows(sheet_title):
    _GOOGLE_SHEET_READ_STATUS[sheet_title.lower()] = False
    if vercel_auth_enabled():
        result = call_google_function('read_sheet', {'sheet': sheet_title})
        rows = result.get('rows') if result else None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            return []
        _GOOGLE_SHEET_READ_STATUS[sheet_title.lower()] = True
        return rows

    credentials = get_google_credentials()
    if not credentials:
        message = 'Google Sheets sync failed: credentials are missing or invalid.'
        logger.warning(message)
        set_session_message('google_sync_warning', message)
        return []

    try:
        client = gspread.authorize(credentials)
        sheet_id = os.getenv('GOOGLE_SHEET_ID', '').strip()
        sheet_name = os.getenv('GOOGLE_SHEET_NAME', 'EngClassAI')

        try:
            if sheet_id:
                spreadsheet = client.open_by_key(sheet_id)
            else:
                try:
                    spreadsheet = client.open(sheet_name)
                except gspread.SpreadsheetNotFound as exc:
                    message = f'Google Sheets sync failed: spreadsheet "{sheet_name}" was not found: {exc}'
                    logger.warning(message)
                    set_session_message('google_sync_warning', message)
                    return []
        except Exception as exc:
            message = f'Google Sheets sync failed: could not open spreadsheet "{sheet_name}": {exc}'
            logger.warning(message)
            set_session_message('google_sync_warning', message)
            return []

        try:
            worksheet = spreadsheet.worksheet(sheet_title)
        except Exception as exc:
            message = f'Google Sheets sync failed: worksheet "{sheet_title}" was not found: {exc}'
            logger.warning(message)
            set_session_message('google_sync_warning', message)
            return []

        values = worksheet.get_all_values()
        if not values:
            _GOOGLE_SHEET_READ_STATUS[sheet_title.lower()] = True
            return []

        headers = [str(cell).strip() for cell in values[0]]
        rows = []
        for raw_row in values[1:]:
            if not raw_row or not any((cell is not None and str(cell).strip()) for cell in raw_row):
                continue
            item = {}
            for idx, header in enumerate(headers):
                value = raw_row[idx] if idx < len(raw_row) else ''
                item[header] = '' if value is None else str(value)
            rows.append(item)
        _GOOGLE_SHEET_READ_STATUS[sheet_title.lower()] = True
        return rows
    except Exception as exc:
        message = f'Failed to load Google Sheet {sheet_title}: {exc}'
        logger.exception(message)
        set_session_message('google_sync_warning', message)
        return []


def _google_sheet_read_succeeded(sheet_title):
    return bool(_GOOGLE_SHEET_READ_STATUS.get(sheet_title.lower(), False))


def sync_google_students_into_db(rows=None):
    if rows is None:
        rows = _read_google_worksheet_rows(USER_SHEET_NAME)
    sheet_read_succeeded = bool(rows) or _google_sheet_read_succeeded(USER_SHEET_NAME)
    if not sheet_read_succeeded:
        return []

    conn = get_db()
    synced = []
    seen_student_ids = set()
    existing_users = [dict(item) for item in conn.execute('SELECT * FROM users').fetchall()]
    users_by_email = {
        str(item['email']).strip().lower(): item
        for item in existing_users if item.get('email')
    }
    users_by_username = {
        str(item['username']).strip().lower(): item
        for item in existing_users if item.get('username')
    }
    for row in rows:
        full_name = str(row.get('full_name') or row.get('Full Name') or '').strip()
        email = str(row.get('email') or row.get('Email') or '').strip().lower()
        username = str(row.get('username') or row.get('Username') or '').strip()
        class_name = str(row.get('class_name') or row.get('Class Name') or row.get('Class') or '').strip()
        role = str(row.get('role') or row.get('Role') or '').strip().lower()
        if role not in {'student', 'teacher'}:
            continue
        is_active = str(row.get('status') or row.get('is_active') or 'active').strip().lower()
        if not full_name and not email and not username:
            continue

        existing = (
            (users_by_email.get(email) if email else None)
            or users_by_username.get(username.lower())
        )

        if existing:
            if existing.get('role') == 'teacher' and role == 'student':
                continue
            conn.execute(
                'UPDATE users SET full_name = ?, username = ?, email = ?, class_name = ?, role = ?, is_active = ? WHERE id = ?',
                (
                    full_name or existing['full_name'],
                    username or existing['username'] or generate_username(full_name, email or existing['email']),
                    email or existing['email'],
                    class_name or existing['class_name'],
                    role,
                    1 if is_active not in {'locked', 'inactive', '0', 'false', 'no'} else 0,
                    existing['id'],
                )
            )
            student_row = dict(existing)
            student_row['full_name'] = full_name or existing['full_name']
            student_row['username'] = username or existing['username'] or generate_username(full_name, email or existing['email'])
            student_row['email'] = email or existing['email']
            student_row['class_name'] = class_name or existing['class_name']
            student_row['role'] = role
            student_row['is_active'] = 1 if is_active not in {'locked', 'inactive', '0', 'false', 'no'} else 0
            if existing.get('email'):
                users_by_email.pop(str(existing['email']).strip().lower(), None)
            if existing.get('username'):
                users_by_username.pop(str(existing['username']).strip().lower(), None)
            if student_row.get('email'):
                users_by_email[str(student_row['email']).strip().lower()] = student_row
            if student_row.get('username'):
                users_by_username[str(student_row['username']).strip().lower()] = student_row
            synced.append(student_row)
            if role == 'student':
                seen_student_ids.add(existing['id'])
            continue

        final_username = normalize_username(username) or generate_username(full_name, email)
        final_username = choose_unique_username(conn, full_name or final_username, email)
        conn.execute(
            'INSERT INTO users (full_name, username, email, password, role, class_name, is_active, total_points, average_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (full_name, final_username, email or f'{final_username}@example.com', '123456', role, class_name, 1 if is_active not in {'locked', 'inactive', '0', 'false', 'no'} else 0, 0, 0)
        )
        student_id = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
        synced.append({
            'id': student_id,
            'full_name': full_name,
            'username': final_username,
            'email': email or f'{final_username}@example.com',
            'role': role,
            'class_name': class_name,
            'is_active': 1 if is_active not in {'locked', 'inactive', '0', 'false', 'no'} else 0,
            'total_points': 0,
            'average_score': 0,
        })
        if role == 'student':
            seen_student_ids.add(student_id)
        if synced[-1].get('email'):
            users_by_email[synced[-1]['email'].lower()] = synced[-1]
        if synced[-1].get('username'):
            users_by_username[synced[-1]['username']] = synced[-1]

    if seen_student_ids:
        placeholders = ','.join('?' for _ in seen_student_ids)
        absent_students = conn.execute(
            f"SELECT id FROM users WHERE role = 'student' AND id NOT IN ({placeholders})",
            tuple(seen_student_ids),
        ).fetchall()
    else:
        absent_students = conn.execute(
            "SELECT id FROM users WHERE role = 'student'"
        ).fetchall() if _google_sheet_read_succeeded(USER_SHEET_NAME) else []
    for student_row in absent_students:
        conn.execute('DELETE FROM submissions WHERE student_id = ?', (student_row['id'],))
        conn.execute('DELETE FROM users WHERE id = ? AND role = ?', (student_row['id'], 'student'))

    conn.commit(); conn.close()
    return synced


def _lesson_sheet_value(row, *aliases):
    normalized = {re.sub(r'[^a-z0-9]+', '', str(key).lower().strip()): value for key, value in row.items()}
    for alias in aliases:
        normalized_alias = re.sub(r'[^a-z0-9]+', '', alias.lower().strip())
        if normalized_alias in normalized:
            return '' if normalized[normalized_alias] is None else str(normalized[normalized_alias]).strip()
    return ''


def _normalized_lesson_id(value):
    try:
        number = int(float(value))
        return number if number > 0 and float(value) == number else None
    except (TypeError, ValueError, OverflowError):
        return None


def _normalized_lesson_integer(value, default, minimum=1):
    try:
        number = int(float(value))
        return number if number >= minimum and float(value) == number else default
    except (TypeError, ValueError, OverflowError):
        return default


def _deduplicate_lessons_by_content(conn):
    groups = conn.execute('''
        SELECT LOWER(TRIM(COALESCE(title_en, ''))) AS title_key,
               LOWER(TRIM(COALESCE(title_vi, ''))) AS title_vi_key,
               class_level
        FROM lessons
        GROUP BY title_key, title_vi_key, class_level
        HAVING COUNT(*) > 1
    ''').fetchall()

    removed_ids = []
    for group in groups:
        rows = conn.execute(
            '''SELECT id FROM lessons
               WHERE LOWER(TRIM(COALESCE(title_en, ''))) = ?
                 AND LOWER(TRIM(COALESCE(title_vi, ''))) = ?
                 AND class_level = ?
               ORDER BY id''',
            (group['title_key'], group['title_vi_key'], group['class_level']),
        ).fetchall()
        canonical_id = rows[0]['id']
        duplicate_ids = [row['id'] for row in rows[1:]]
        for duplicate_id in duplicate_ids:
            conn.execute(
                'UPDATE submissions SET lesson_id = ? WHERE lesson_id = ?',
                (canonical_id, duplicate_id),
            )
            conn.execute('DELETE FROM lessons WHERE id = ?', (duplicate_id,))
            removed_ids.append(duplicate_id)
    return removed_ids


def sync_google_lessons_into_db(rows=None):
    if rows is None:
        rows = _read_google_worksheet_rows('Lesson')
    sheet_read_succeeded = bool(rows) or _google_sheet_read_succeeded('Lesson')
    if not sheet_read_succeeded:
        return []

    conn = get_db()
    synced = []
    seen_lesson_ids = set()
    existing_lessons = [dict(item) for item in conn.execute('SELECT * FROM lessons').fetchall()]
    lessons_by_id = {item['id']: item for item in existing_lessons}
    lessons_by_title_en = {}
    lessons_by_title_vi = {}
    lessons_by_signature = {}
    for item in existing_lessons:
        if item.get('title_en'):
            lessons_by_title_en.setdefault(item['title_en'], item)
        if item.get('title_vi'):
            lessons_by_title_vi.setdefault(item['title_vi'], item)
        signature = (
            str(item.get('title_en') or '').strip().lower(),
            str(item.get('title_vi') or '').strip().lower(),
            _normalized_lesson_integer(item.get('class_level'), 6, 1),
        )
        lessons_by_signature.setdefault(signature, item)
    for row in rows:
        title_en = _lesson_sheet_value(row, 'title_en', 'Title EN', 'title', 'Title')
        title_vi = _lesson_sheet_value(row, 'title_vi', 'Title VI', 'title_vi') or title_en
        if not title_en and not title_vi:
            continue

        raw_lesson_id = _lesson_sheet_value(row, 'id', 'ID')
        lesson_id_number = _normalized_lesson_id(raw_lesson_id)
        class_level = _normalized_lesson_integer(
            _lesson_sheet_value(row, 'class_level', 'Class Level', 'Class'), 6, 1
        )
        week_number = _normalized_lesson_integer(
            _lesson_sheet_value(row, 'week_number', 'Week Number', 'Week'), 1, 1
        )
        open_date = _lesson_sheet_value(row, 'open_date', 'Open Date')
        close_date = _lesson_sheet_value(row, 'close_date', 'Close Date')
        topic_en = _lesson_sheet_value(row, 'topic_en', 'Topic EN') or title_en
        topic_vi = _lesson_sheet_value(row, 'topic_vi', 'Topic VI') or title_vi
        goal_en = _lesson_sheet_value(row, 'goal_en', 'Goal EN') or 'Learn the language target of this lesson.'
        goal_vi = _lesson_sheet_value(row, 'goal_vi', 'Goal VI') or 'Học mục tiêu ngôn ngữ của bài học.'
        vocabulary = _lesson_sheet_value(row, 'vocabulary', 'Vocabulary')
        dialogue = _lesson_sheet_value(row, 'dialogue', 'Dialogue')
        sample_question = _lesson_sheet_value(row, 'sample_question', 'Sample Question') or title_en
        speaking_question = _lesson_sheet_value(row, 'speaking_question', 'Speaking Question') or sample_question
        expected_answer = _lesson_sheet_value(row, 'expected_answer', 'Expected Answer') or sample_question
        sentence_words = _lesson_sheet_value(row, 'sentence_words', 'Sentence Words')
        exercise_text_vi = _lesson_sheet_value(row, 'exercise_text_vi', 'Exercise Text VI') or 'Sắp xếp từ để tạo câu đúng.'
        exercise_text_en = _lesson_sheet_value(row, 'exercise_text_en', 'Exercise Text EN') or 'Arrange the words to make the correct sentence.'
        task_3_text = _lesson_sheet_value(row, 'task_3_text', 'Task 3', 'audio_task', 'task_3') or 'Ghi âm lại giới thiệu về bản thân. (Tối đa file ghi âm là 1 phút)'
        task_4_text = _lesson_sheet_value(row, 'task_4_text', 'Task 4', 'video_task', 'task_4') or 'Quay video ngắn cuộc hội thoại giới thiệu về người bạn mới. (Video có độ dài tối đa là 30 giây)'
        video_url = _lesson_sheet_value(row, 'video_url', 'Video URL')

        existing = None
        sheet_id_supplied = bool(raw_lesson_id.strip())
        if sheet_id_supplied and lesson_id_number is not None:
            existing = lessons_by_id.get(lesson_id_number)

        # If the Sheet ID is missing, stale, or points to a different lesson,
        # match the canonical row by title before creating a new lesson.
        if existing is None:
            signature = (
                str(title_en or '').strip().lower(),
                str(title_vi or '').strip().lower(),
                class_level,
            )
            existing = lessons_by_signature.get(signature)
            if existing is None:
                title_matches = [
                    candidate for candidate in (
                        lessons_by_title_en.get(title_en),
                        lessons_by_title_vi.get(title_vi),
                    ) if candidate is not None
                ]
                existing = min(title_matches, key=lambda candidate: candidate['id']) if title_matches else None

        if existing:
            conn.execute(
                '''
                UPDATE lessons
                SET title_en = ?, title_vi = ?, topic_en = ?, topic_vi = ?, goal_en = ?, goal_vi = ?,
                    class_level = ?, video_url = ?, vocabulary = ?, dialogue = ?, sample_question = ?, speaking_question = ?,
                    expected_answer = ?, sentence_words = ?, exercise_text_vi = ?, exercise_text_en = ?, task_3_text = ?, task_4_text = ?, week_number = ?, open_date = ?, close_date = ?
                WHERE id = ?
                ''',
                (
                    title_en or existing['title_en'],
                    title_vi or existing['title_vi'],
                    topic_en or existing['topic_en'],
                    topic_vi or existing['topic_vi'],
                    goal_en or existing['goal_en'],
                    goal_vi or existing['goal_vi'],
                    class_level,
                    video_url,
                    vocabulary,
                    dialogue,
                    sample_question,
                    speaking_question,
                    expected_answer,
                    sentence_words,
                    exercise_text_vi,
                    exercise_text_en,
                    task_3_text,
                    task_4_text,
                    int(week_number or existing['week_number'] or 1),
                    open_date,
                    close_date,
                    existing['id'],
                )
            )
            lesson_row = dict(existing)
            lesson_row.update({
                'title_en': title_en or existing['title_en'],
                'title_vi': title_vi or existing['title_vi'],
                'topic_en': topic_en or existing['topic_en'],
                'topic_vi': topic_vi or existing['topic_vi'],
                'goal_en': goal_en or existing['goal_en'],
                'goal_vi': goal_vi or existing['goal_vi'],
                'class_level': class_level,
                'video_url': video_url,
                'vocabulary': vocabulary,
                'dialogue': dialogue,
                'sample_question': sample_question,
                'speaking_question': speaking_question,
                'expected_answer': expected_answer,
                'sentence_words': sentence_words,
                'exercise_text_vi': exercise_text_vi,
                'exercise_text_en': exercise_text_en,
                'task_3_text': task_3_text,
                'task_4_text': task_4_text,
                'week_number': int(week_number or existing['week_number'] or 1),
                'open_date': open_date,
                'close_date': close_date,
            })
            if existing.get('title_en') and lessons_by_title_en.get(existing['title_en'], {}).get('id') == existing['id']:
                lessons_by_title_en.pop(existing['title_en'], None)
            if existing.get('title_vi') and lessons_by_title_vi.get(existing['title_vi'], {}).get('id') == existing['id']:
                lessons_by_title_vi.pop(existing['title_vi'], None)
            if lesson_row.get('title_en'):
                lessons_by_title_en.setdefault(lesson_row['title_en'], lesson_row)
            if lesson_row.get('title_vi'):
                lessons_by_title_vi.setdefault(lesson_row['title_vi'], lesson_row)
            lessons_by_id[lesson_row['id']] = lesson_row
            signature = (
                str(lesson_row.get('title_en') or '').strip().lower(),
                str(lesson_row.get('title_vi') or '').strip().lower(),
                _normalized_lesson_integer(lesson_row.get('class_level'), 6, 1),
            )
            lessons_by_signature.setdefault(signature, lesson_row)
            synced.append(lesson_row)
            seen_lesson_ids.add(lesson_row['id'])
            continue

        conn.execute(
            '''
            INSERT INTO lessons (title_en, title_vi, level, class_level, topic_en, topic_vi, goal_en, goal_vi,
            video_url, vocabulary, dialogue, sample_question, speaking_question, expected_answer, sentence_words,
            exercise_text_vi, exercise_text_en, task_3_text, task_4_text, week_number, open_date, close_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                title_en, title_vi, '', class_level, topic_en or title_en, topic_vi or title_vi,
                goal_en or 'Imported from Google Sheet', goal_vi or 'Bài học được import từ Google Sheet',
                video_url, vocabulary or 'sheet,import', dialogue or 'Imported from Google Sheet', sample_question, speaking_question,
                expected_answer, sentence_words, exercise_text_vi, exercise_text_en,
                task_3_text, task_4_text, week_number, open_date, close_date,
            )
        )
        lesson_id = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
        synced.append({
            'id': lesson_id,
            'title_en': title_en,
            'title_vi': title_vi,
            'topic_en': topic_en or title_en,
            'topic_vi': topic_vi or title_vi,
            'goal_en': goal_en or 'Imported from Google Sheet',
            'goal_vi': goal_vi or 'Bài học được import từ Google Sheet',
            'class_level': class_level,
            'video_url': video_url,
            'vocabulary': vocabulary or 'sheet,import',
            'dialogue': dialogue or 'Imported from Google Sheet',
            'sample_question': sample_question,
            'speaking_question': speaking_question,
            'expected_answer': expected_answer,
            'sentence_words': sentence_words,
            'exercise_text_vi': exercise_text_vi,
            'exercise_text_en': exercise_text_en,
            'task_3_text': task_3_text,
            'task_4_text': task_4_text,
            'week_number': int(week_number or 1),
            'open_date': open_date,
            'close_date': close_date,
        })
        lessons_by_id[lesson_id] = synced[-1]
        seen_lesson_ids.add(lesson_id)
        inserted_signature = (
            str(synced[-1].get('title_en') or '').strip().lower(),
            str(synced[-1].get('title_vi') or '').strip().lower(),
            _normalized_lesson_integer(synced[-1].get('class_level'), 6, 1),
        )
        lessons_by_signature.setdefault(inserted_signature, synced[-1])
        if title_en:
            lessons_by_title_en.setdefault(title_en, synced[-1])
        if title_vi:
            lessons_by_title_vi.setdefault(title_vi, synced[-1])

    removed_duplicate_ids = _deduplicate_lessons_by_content(conn)
    if removed_duplicate_ids:
        logger.info('Removed duplicate Lesson records after Sheet reconciliation: %s', removed_duplicate_ids)

    if seen_lesson_ids:
        placeholders = ','.join('?' for _ in seen_lesson_ids)
        absent_lessons = conn.execute(
            f'SELECT id FROM lessons WHERE id NOT IN ({placeholders})',
            tuple(seen_lesson_ids),
        ).fetchall()
    elif _google_sheet_read_succeeded('Lesson') and not rows:
        absent_lessons = conn.execute('SELECT id FROM lessons').fetchall()
    else:
        absent_lessons = []

    for lesson_row in absent_lessons:
        conn.execute('DELETE FROM submissions WHERE lesson_id = ?', (lesson_row['id'],))
        conn.execute('DELETE FROM lessons WHERE id = ?', (lesson_row['id'],))

    conn.commit(); conn.close()
    return synced


def build_weekly_schedule(lessons):
    grouped = {}
    for lesson in lessons:
        week_number = int(lesson.get('week_number') or 1)
        grouped.setdefault(week_number, {
            'week': week_number,
            'open_date': lesson.get('open_date') or '',
            'close_date': lesson.get('close_date') or '',
            'lessons': [],
        })
        grouped[week_number]['lessons'].append({
            'id': lesson.get('id'),
            'title': lesson.get('title_en') or lesson.get('title_vi') or 'Lesson',
            'title_vi': lesson.get('title_vi') or '',
            'title_en': lesson.get('title_en') or '',
            'open_date': lesson.get('open_date') or '',
            'close_date': lesson.get('close_date') or '',
        })

    schedule = []
    for week in sorted(grouped):
        block = grouped[week]
        block['label'] = f"Week {week}"
        if block['open_date'] or block['close_date']:
            block['date_range'] = f"{block['open_date']} → {block['close_date']}" if block['open_date'] and block['close_date'] else (block['open_date'] or block['close_date'])
        else:
            block['date_range'] = 'Not set'
        schedule.append(block)
    return schedule


def parse_submission_payload(raw_value):
    if not raw_value:
        return {}
    try:
        payload = json.loads(raw_value)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_feedback_breakdown(feedback):
    breakdown = {'task_1': '', 'task_2': '', 'task_3': '', 'task_4': ''}
    if not feedback:
        return breakdown

    for item in str(feedback).split(';'):
        text = item.strip()
        if not text or ':' not in text:
            continue
        label, value = text.split(':', 1)
        key = label.strip().lower().replace(' ', '_')
        if key.startswith('task_1'):
            breakdown['task_1'] = value.strip()
        elif key.startswith('task_2'):
            breakdown['task_2'] = value.strip()
        elif key.startswith('task_3'):
            breakdown['task_3'] = value.strip()
        elif key.startswith('task_4'):
            breakdown['task_4'] = value.strip()
    return breakdown


def clean_score_num(val):
    if val is None or val == '':
        return 0
    try:
        num = round(float(val), 2)
        return int(num) if num.is_integer() else num
    except (TypeError, ValueError):
        return 0


def extract_submission_scores(answer_raw, fallback_score=None):
    payload = parse_submission_payload(answer_raw)
    evaluation = payload.get('evaluation') if isinstance(payload.get('evaluation'), dict) else {}
    result = {
        'gemini_score': None,
        'groq_score': None,
        'teacher_score': fallback_score,
        'gemini_criteria': {},
        'groq_criteria': {},
        'teacher_criteria': {},
        'evaluation': evaluation,
    }

    def pick_score(provider_data):
        if not isinstance(provider_data, dict):
            return None
        value = provider_data.get('score')
        if value is None:
            value = provider_data.get('overall_score')
        try:
            return int(float(value)) if value not in (None, '') else None
        except (TypeError, ValueError):
            return None

    def pick_criteria(provider_data):
        if not isinstance(provider_data, dict):
            return {}
        criteria = provider_data.get('criteria') or {}
        if not isinstance(criteria, dict):
            criteria = {}
        return {
            'pronunciation': criteria.get('pronunciation') if criteria.get('pronunciation') is not None else provider_data.get('pronunciation') or 0,
            'grammar': criteria.get('grammar') if criteria.get('grammar') is not None else provider_data.get('grammar') or 0,
            'fluency': criteria.get('fluency') if criteria.get('fluency') is not None else provider_data.get('fluency') or 0,
            'relevance': criteria.get('relevance') if criteria.get('relevance') is not None else provider_data.get('relevance') or 0,
            'confidence': criteria.get('confidence') if criteria.get('confidence') is not None else provider_data.get('confidence') or 0,
        }

    all_scores = {'gemini': [], 'groq': [], 'teacher': []}
    all_criteria = {
        'gemini': {'pronunciation': [], 'grammar': [], 'fluency': [], 'relevance': [], 'confidence': []},
        'groq': {'pronunciation': [], 'grammar': [], 'fluency': [], 'relevance': [], 'confidence': []},
        'teacher': {'pronunciation': [], 'grammar': [], 'fluency': [], 'relevance': [], 'confidence': []},
    }

    for task_name in ('task_1', 'task_2', 'task_3'):
        task_data = evaluation.get(task_name) if isinstance(evaluation.get(task_name), dict) else {}
        for provider_name in ('gemini', 'groq', 'teacher'):
            provider_data = task_data.get(provider_name) if isinstance(task_data.get(provider_name), dict) else {}
            score = pick_score(provider_data)
            if score is not None:
                all_scores[provider_name].append(score)
            criteria = pick_criteria(provider_data)
            for key in all_criteria[provider_name]:
                value = criteria.get(key, 0)
                try:
                    value_num = float(value)
                except (TypeError, ValueError):
                    value_num = 0.0
                if value_num is not None:
                    all_criteria[provider_name][key].append(value_num)

    for provider_name in ('gemini', 'groq', 'teacher'):
        if all_scores[provider_name]:
            avg_score = sum(all_scores[provider_name]) / len(all_scores[provider_name])
            result[f'{provider_name}_score'] = clean_score_num(avg_score)
        else:
            result[f'{provider_name}_score'] = None
        composite = {}
        for criterion_name, values in all_criteria[provider_name].items():
            if values:
                composite[criterion_name] = clean_score_num(round(sum(values) / len(values), 2))
            else:
                composite[criterion_name] = 0
        result[f'{provider_name}_criteria'] = composite

    # Check direct keys if task breakdown is not present
    for provider_name in ('gemini', 'groq'):
        if result[f'{provider_name}_score'] is None and isinstance(evaluation.get(provider_name), dict):
            result[f'{provider_name}_score'] = pick_score(evaluation.get(provider_name))

    if fallback_score is not None and float(fallback_score) > 0:
        result['teacher_score'] = clean_score_num(fallback_score)
    elif result['teacher_score'] is None:
        result['teacher_score'] = clean_score_num(fallback_score) if fallback_score is not None else None

    return result


def build_practice_history(rows):
    practice_rows = []
    for row in rows:
        payload = parse_submission_payload(row.get('answer'))
        breakdown = parse_feedback_breakdown(row.get('feedback'))
        transcript = payload.get('task_3_transcript') or payload.get('transcript_3') or breakdown.get('task_3') or ''
        raw_transcript = transcript
        if len(transcript) > 180:
            transcript = transcript[:177] + '...'
        audio_url = row.get('audio_path') or payload.get('audio_url') or ''
        video_url = row.get('drive_url') or payload.get('video_url') or ''
        media_present = bool(audio_url or video_url)
        evaluation = extract_submission_scores(row.get('answer'), row.get('score'))
        practice_rows.append({
            'id': row.get('id'),
            'created_at': row.get('created_at') or '',
            'student_id': row.get('student_id'),
            'lesson_id': row.get('lesson_id'),
            'student_name': row.get('full_name') or row.get('student_name') or 'Student',
            'student_class': row.get('class_name') or '—',
            'lesson': row.get('title_vi') or row.get('title_en') or 'Lesson',
            'lesson_title_vi': row.get('title_vi') or '',
            'lesson_title_en': row.get('title_en') or '',
            'overall_score': row.get('score'),
            'gemini_score': evaluation.get('gemini_score'),
            'groq_score': evaluation.get('groq_score'),
            'teacher_score': evaluation.get('teacher_score') if evaluation.get('teacher_score') is not None else row.get('score') or 0,
            'teacher_note': row.get('teacher_note') or '',
            'feedback': row.get('feedback') or '',
            'gemini_criteria': evaluation.get('gemini_criteria', {}),
            'groq_criteria': evaluation.get('groq_criteria', {}),
            'teacher_criteria': evaluation.get('teacher_criteria', {}),
            'task_1': payload.get('task_1_answer') or breakdown.get('task_1') or '—',
            'task_2': payload.get('task_2_answer') or breakdown.get('task_2') or '—',
            'task_3': transcript or 'No transcript available',
            'raw_transcript': raw_transcript,
            'task_4': breakdown.get('task_4') or 'Pending teacher review',
            'sample_question': row.get('sample_question') or '',
            'sentence_words': row.get('sentence_words') or '',
            'task_3_text': row.get('task_3_text') or '',
            'task_1_answer': payload.get('task_1_answer') or '',
            'task_2_answer': payload.get('task_2_answer') or '',
            'task_3_transcript': payload.get('task_3_transcript') or raw_transcript or '',
            'audio_url': audio_url,
            'video_url': video_url,
            'evaluation': evaluation.get('evaluation') or {},
            'submitted_at': row.get('created_at') or '',
            'media_status': 'Recorded' if media_present else 'Not recorded',
            'media_present': media_present,
        })
    return practice_rows


def get_student_progress_summary(student_id, lessons):
    conn = get_db()
    completed = set(row['lesson_id'] for row in conn.execute('SELECT DISTINCT lesson_id FROM submissions WHERE student_id = ?', (student_id,)).fetchall())
    conn.close()

    available = []
    completed_list = []
    pending_list = []
    for lesson in lessons:
        item = {
            'id': lesson.get('id'),
            'title': lesson.get('title_en') or lesson.get('title_vi') or 'Lesson',
            'week': int(lesson.get('week_number') or 1),
            'open_date': lesson.get('open_date') or '',
            'close_date': lesson.get('close_date') or '',
        }
        if lesson.get('id') in completed:
            completed_list.append(item)
        else:
            pending_list.append(item)
        available.append(item)

    return {
        'available': len(available),
        'completed': len(completed_list),
        'pending': len(pending_list),
        'completed_list': completed_list,
        'pending_list': pending_list,
    }


def get_google_credentials():
    if IS_FROZEN:
        return None

    service_account_json = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON', '').strip()
    service_account_json_b64 = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON_B64', '').strip()
    service_account_path = os.getenv('GOOGLE_SERVICE_ACCOUNT_PATH', '').strip()

    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive',
    ]

    def find_credentials_file(configured_path):
        if not configured_path:
            return None

        path = Path(configured_path).expanduser()
        candidates = [path] if path.is_absolute() else [BASE_DIR / path]
        candidates.append(BASE_DIR / path.name)
        return next((candidate for candidate in candidates if candidate.is_file()), None)

    try:
        if service_account_json_b64:
            credentials_info = json.loads(
                base64.b64decode(service_account_json_b64, validate=True).decode('utf-8')
            )
            return service_account.Credentials.from_service_account_info(
                credentials_info,
                scopes=scopes,
            )
        if service_account_json:
            if service_account_json.startswith('{'):
                return service_account.Credentials.from_service_account_info(json.loads(service_account_json), scopes=scopes)
            credentials_file = find_credentials_file(service_account_json)
            if credentials_file:
                return service_account.Credentials.from_service_account_file(credentials_file, scopes=scopes)
        credentials_file = find_credentials_file(service_account_path)
        if credentials_file:
            return service_account.Credentials.from_service_account_file(credentials_file, scopes=scopes)
    except Exception as exc:
        logger.warning('Google credentials parse failed: %s', exc)
        return None

    logger.warning('Google credentials missing or invalid. Set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_PATH.')
    return None


def _get_or_create_worksheet(sheet, title, rows=1000, cols=20):
    try:
        if hasattr(sheet, 'worksheet'):
            try:
                return sheet.worksheet(title)
            except Exception:
                pass
        if hasattr(sheet, 'get_worksheet'):
            try:
                return sheet.get_worksheet(0)
            except Exception:
                pass
        return sheet.add_worksheet(title=title, rows=str(rows), cols=str(cols))
    except Exception:
        raise


def _replace_worksheet_values(worksheet, values):
    if hasattr(worksheet, 'clear'):
        worksheet.clear()
    elif hasattr(worksheet, 'delete_rows'):
        existing_rows = worksheet.get_all_values()
        if existing_rows:
            worksheet.delete_rows(1, len(existing_rows))

    worksheet.update(values=values, range_name='A1', value_input_option='RAW')


def merge_student_sheet_values(values, existing_values):
    if not values:
        return values
    required_headers = [str(header) for header in values[0]]
    existing_headers = [
        str(header).strip()
        for header in (existing_values[0] if existing_values else [])
        if str(header).strip()
    ]
    headers = list(dict.fromkeys(existing_headers + required_headers))
    existing_records = []
    for row in (existing_values[1:] if existing_values else []):
        record = {
            header: str(row[index] if index < len(row) else '')
            for index, header in enumerate(existing_headers)
        }
        if any(value.strip() for value in record.values()):
            existing_records.append(record)

    def account_key(record):
        email = str(record.get('email') or '').strip().lower()
        username = str(record.get('username') or '').strip().lower()
        return email or username

    previous_by_key = {
        account_key(record): record
        for record in existing_records
        if account_key(record)
    }
    teacher_records = [
        record for record in existing_records
        if str(record.get('role') or '').strip().lower() == 'teacher'
    ]
    merged_records = []
    included_teachers = set()
    for incoming_row in values[1:]:
        incoming = {
            header: str(incoming_row[index] if index < len(incoming_row) else '')
            for index, header in enumerate(required_headers)
        }
        previous = previous_by_key.get(account_key(incoming), {})
        merged = {**previous, **incoming}
        if 'password' in headers:
            previous_password = str(previous.get('password') or '').strip()
            incoming_password = str(incoming.get('password') or '').strip()
            if is_scrypt_password_hash(previous_password):
                merged['password'] = previous_password
            elif incoming_password:
                merged['password'] = hash_password_for_sheet(incoming_password)
            elif previous_password:
                merged['password'] = hash_password_for_sheet(previous_password)
            else:
                merged['password'] = ''
        merged_records.append(merged)
        if previous.get('role', '').strip().lower() == 'teacher':
            included_teachers.add(account_key(previous))
    merged_records.extend(
        teacher for teacher in teacher_records
        if account_key(teacher) not in included_teachers
    )
    return [
        headers,
        *[
            [record.get(header, '') for header in headers]
            for record in merged_records
        ],
    ]


def is_scrypt_password_hash(value):
    parts = str(value or '').split('$')
    if len(parts) != 3 or parts[0] != 'scrypt':
        return False
    try:
        salt = base64.b64decode(parts[1], validate=True)
        digest = base64.b64decode(parts[2], validate=True)
    except (ValueError, base64.binascii.Error):
        return False
    return (
        len(salt) == 16
        and len(digest) == 64
        and base64.b64encode(salt).decode('ascii') == parts[1]
        and base64.b64encode(digest).decode('ascii') == parts[2]
    )


def hash_password_for_sheet(password):
    if is_scrypt_password_hash(password):
        return password
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        str(password).encode('utf-8'),
        salt=salt,
        n=16384,
        r=8,
        p=1,
        dklen=64,
        maxmem=64 * 1024 * 1024,
    )
    return '$'.join((
        'scrypt',
        base64.b64encode(salt).decode('ascii'),
        base64.b64encode(digest).decode('ascii'),
    ))


def sync_students_to_sheet(rows=None):
    try:
        if rows is None:
            conn = get_db()
            try:
                rows = [dict(item) for item in conn.execute(
                    'SELECT * FROM users WHERE role = ? ORDER BY full_name',
                    ('student',),
                ).fetchall()]
            finally:
                conn.close()

        required_headers = ['full_name', 'username', 'email', 'password', 'role', 'class_name', 'status', 'total_points', 'average_score']
        sheet_values = [required_headers]
        sheet_values.extend([
            [
                row.get('full_name', ''),
                row.get('username', generate_username(row.get('full_name', ''), row.get('email', ''))),
                row.get('email', ''),
                row.get('password', ''),
                row.get('role', 'student'),
                row.get('class_name', ''),
                'active' if row.get('is_active', True) else 'locked',
                row.get('total_points', 0),
                row.get('average_score', 0),
            ]
            for row in rows
        ])

        if IS_FROZEN:
            current_sheet = call_google_function('read_sheet', {'sheet': USER_SHEET_NAME})
            if not current_sheet:
                return False
            current_headers = current_sheet.get('headers') or []
            current_rows = current_sheet.get('rows') or []
            existing_values = [current_headers] + [
                [row.get(header, '') for header in current_headers]
                for row in current_rows
            ] if current_headers else []
            sheet_values = merge_student_sheet_values(sheet_values, existing_values)
            return bool(call_google_function(
                'replace_sheet',
                {'sheet': USER_SHEET_NAME, 'values': sheet_values},
            ))

        credentials = get_google_credentials()
        if not credentials:
            message = 'Google Sheets sync skipped: credentials are missing or invalid.'
            logger.warning(message)
            set_session_message('google_sync_warning', message)
            return False
        client = gspread.authorize(credentials)
        sheet_id = os.getenv('GOOGLE_SHEET_ID', '').strip()
        sheet_name = os.getenv('GOOGLE_SHEET_NAME', 'EngClassAI')
        if sheet_id:
            sheet = client.open_by_key(sheet_id)
        else:
            try:
                sheet = client.open(sheet_name)
            except gspread.SpreadsheetNotFound:
                sheet = client.create(sheet_name)
                logger.info('Created missing Google Sheet: %s', sheet_name)
        worksheet = _get_or_create_worksheet(sheet, USER_SHEET_NAME, rows=2000, cols=20)
        existing_values = worksheet.get_all_values() if hasattr(worksheet, 'get_all_values') else []
        sheet_values = merge_student_sheet_values(sheet_values, existing_values)
        _replace_worksheet_values(worksheet, sheet_values)

        set_session_message('google_sync_warning', 'Google synced')
        return True
    except Exception as exc:
        message = f'Google Sheets sync failed: {exc}'
        logger.exception(message)
        set_session_message('google_sync_warning', message)
        return False


def sync_teacher_profile_to_sheet(user, profile, password=''):
    if vercel_auth_enabled():
        result = call_google_function(
            'update_user_profile',
            {
                'full_name': profile['full_name'],
                'username': profile['username'],
                'email': profile['email'],
                'password': password,
            },
        )
        if result:
            return True, ''
        return False, 'The account sheet could not be updated. Your account changes were not saved.'

    credentials = get_google_credentials()
    if not credentials:
        return False, 'Google Sheets credentials are missing or invalid.'

    try:
        client = gspread.authorize(credentials)
        sheet_id = os.getenv('GOOGLE_SHEET_ID', '').strip()
        sheet_name = os.getenv('GOOGLE_SHEET_NAME', 'EngClassAI')
        if sheet_id:
            spreadsheet = client.open_by_key(sheet_id)
        else:
            spreadsheet = client.open(sheet_name)
        headteacher = is_headteacher(user)
        sheet_name = HEADTEACHER_SHEET_NAME if headteacher else USER_SHEET_NAME
        account_role = 'headteacher' if headteacher else 'teacher'
        worksheet = spreadsheet.worksheet(sheet_name)
        values = worksheet.get_all_values()
        if len(values) < 2:
            return False, f'The {sheet_name} sheet has no account rows to update.'

        headers = [str(header).strip() for header in values[0]]
        normalized_headers = {
            re.sub(r'[^a-z0-9]', '', header.lower()): index
            for index, header in enumerate(headers)
        }

        def column(*names):
            return next(
                (normalized_headers[name] for name in names if name in normalized_headers),
                None,
            )

        email_index = column('email', 'emailaddress')
        username_index = column('username', 'userid')
        name_index = column('fullname', 'name')
        role_index = column('role', 'accountrole')
        password_index = column('passwordhash', 'hashedpassword', 'password')
        status_index = column('status', 'accountstatus')
        if None in (email_index, username_index, name_index, role_index, status_index):
            return False, f'The {sheet_name} sheet needs full_name, username, email, role, and status columns.'

        old_email = str(user.get('email') or '').strip().lower()
        old_username = str(user.get('username') or '').strip().lower()
        matches = []
        for row_number, row in enumerate(values[1:], start=2):
            row_email = str(row[email_index] if email_index < len(row) else '').strip().lower()
            row_username = str(row[username_index] if username_index < len(row) else '').strip().lower()
            if (old_email and row_email == old_email) or (old_username and row_username == old_username):
                matches.append((row_number, row))
        if len(matches) != 1:
            return False, f'The account could not be uniquely found in the {sheet_name} sheet.'

        matched_row_number, matched_row = matches[0]
        for row_number, row in enumerate(values[1:], start=2):
            if row_number == matched_row_number:
                continue
            row_email = str(row[email_index] if email_index < len(row) else '').strip().lower()
            row_username = str(row[username_index] if username_index < len(row) else '').strip().lower()
            if row_email == profile['email'].lower() or row_username == profile['username'].lower():
                return False, f'That username or email address is already in use in the {sheet_name} sheet.'
        if str(matched_row[role_index] if role_index < len(matched_row) else '').strip().lower() != account_role:
            return False, f'The matching {sheet_name} row has an unexpected account role.'
        if headteacher and str(matched_row[status_index] if status_index < len(matched_row) else '').strip().lower() != 'active':
            return False, 'The HeadTeacher account is not active.'
        if password and password_index is None:
            return False, f'The {sheet_name} sheet has no password column to update.'

        updated_row = list(matched_row)
        if len(updated_row) < len(headers):
            updated_row.extend([''] * (len(headers) - len(updated_row)))
        updated_row[name_index] = profile['full_name']
        updated_row[username_index] = profile['username']
        updated_row[email_index] = profile['email']
        if password:
            updated_row[password_index] = hash_password_for_sheet(password)
        worksheet.update(
            values=[updated_row],
            range_name=f'A{matched_row_number}',
            value_input_option='RAW',
        )
        return True, ''
    except Exception as exc:
        logger.exception('Failed to sync account profile to its Google sheet.')
        return False, f'Could not update the account sheet: {exc}'


def get_users_worksheet():
    credentials = get_google_credentials()
    if not credentials:
        raise RuntimeError('Google Sheets credentials are missing or invalid.')
    client = gspread.authorize(credentials)
    sheet_id = os.getenv('GOOGLE_SHEET_ID', '').strip()
    if sheet_id:
        spreadsheet = client.open_by_key(sheet_id)
    else:
        spreadsheet = client.open(os.getenv('GOOGLE_SHEET_NAME', 'EngClassAI'))
    return spreadsheet.worksheet(USER_SHEET_NAME)


def read_teacher_accounts_from_sheet():
    if vercel_auth_enabled():
        ok, accounts = manage_teacher_account_in_sheet('list')
        if not ok:
            raise RuntimeError(str(accounts))
        return sorted(accounts, key=lambda account: account['full_name'].casefold())
    else:
        values = get_users_worksheet().get_all_values()
        if not values:
            raise RuntimeError('The Users sheet is empty.')
        headers = values[0]
        rows = [
            {
                header: row[index] if index < len(row) else ''
                for index, header in enumerate(headers)
            }
            for row in values[1:]
            if any(str(value).strip() for value in row)
        ]

    def normalized(row):
        return {
            re.sub(r'[^a-z0-9]', '', str(key).lower()): value
            for key, value in row.items()
        }

    accounts = []
    for row in rows:
        values = normalized(row)
        if str(values.get('role', '')).strip().lower() != 'teacher':
            continue
        account = {
            'full_name': str(values.get('fullname') or values.get('name') or '').strip(),
            'username': str(values.get('username') or '').strip(),
            'email': str(values.get('email') or '').strip(),
            'class_name': str(values.get('classname') or values.get('class') or '').strip(),
            'status': str(values.get('status') or values.get('isactive') or 'active').strip().lower(),
        }
        account['is_active'] = account['status'] not in {'locked', 'inactive', '0', 'false', 'no'}
        accounts.append(account)
    return sorted(accounts, key=lambda account: account['full_name'].casefold())


def manage_teacher_account_in_sheet(action, account=None, original_username=''):
    account = account or {}
    if vercel_auth_enabled():
        result = call_google_function(
            'manage_teacher_account',
            {
                'action': action,
                'account': account,
                'original_username': original_username,
            },
        )
        if not result:
            return False, 'The Users sheet could not be updated.'
        if action == 'list':
            accounts = result.get('accounts') or []
            for teacher_account in accounts:
                status = str(teacher_account.get('status') or 'active').strip().lower()
                teacher_account['is_active'] = status not in {
                    'locked', 'inactive', '0', 'false', 'no',
                }
            return True, accounts
        return True, ''

    try:
        worksheet = get_users_worksheet()
        values = worksheet.get_all_values()
        if not values:
            return False, 'The Users sheet is empty.'
        headers = [str(header).strip() for header in values[0]]
        normalized_headers = {
            re.sub(r'[^a-z0-9]', '', header.lower()): index
            for index, header in enumerate(headers)
        }

        def column(*names):
            return next(
                (normalized_headers[name] for name in names if name in normalized_headers),
                None,
            )

        name_index = column('fullname', 'name')
        username_index = column('username', 'userid')
        email_index = column('email', 'emailaddress')
        password_index = column('passwordhash', 'hashedpassword', 'password')
        role_index = column('role', 'accountrole')
        class_index = column('classname', 'class')
        status_index = column('status', 'accountstatus', 'isactive')
        if None in (name_index, username_index, email_index, password_index, role_index):
            return False, 'The Users sheet needs full_name, username, email, password, and role columns.'

        def read_row(row):
            return [row[index] if index < len(row) else '' for index in range(len(headers))]

        matches = []
        for row_number, row in enumerate(values[1:], start=2):
            current = read_row(row)
            if (
                str(current[role_index]).strip().lower() == 'teacher'
                and str(current[username_index]).strip().casefold()
                == original_username.strip().casefold()
            ):
                matches.append((row_number, current))
        if action == 'list':
            ok, accounts = True, read_teacher_accounts_from_sheet()
            return ok, accounts
        if action == 'create':
            username = str(account.get('username') or '').strip()
            email = str(account.get('email') or '').strip().lower()
            if not username or not email or not str(account.get('full_name') or '').strip():
                return False, 'Name, username, and email are required.'
            if any(
                str(row[username_index] if username_index < len(row) else '').strip().casefold() == username.casefold()
                or str(row[email_index] if email_index < len(row) else '').strip().casefold() == email
                for row in values[1:]
            ):
                return False, 'That username or email address is already in use.'
            if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
                return False, 'Enter a valid email address.'
            new_row = [''] * len(headers)
            new_row[name_index] = str(account['full_name']).strip()
            new_row[username_index] = username
            new_row[email_index] = email
            new_row[password_index] = hash_password_for_sheet(str(account.get('password') or '123456'))
            new_row[role_index] = 'teacher'
            if class_index is not None:
                new_row[class_index] = str(account.get('class_name') or '').strip()
            if status_index is not None:
                new_row[status_index] = 'active'
            worksheet.append_row(new_row, value_input_option='RAW')
            return True, ''

        if len(matches) != 1:
            return False, 'The teacher account could not be uniquely found in the Users sheet.'
        row_number, current = matches[0]

        if action == 'delete':
            worksheet.delete_rows(row_number)
            return True, ''
        if action == 'toggle':
            if status_index is None:
                return False, 'The Users sheet must contain a status column.'
            current_status = str(current[status_index]).strip().lower()
            current[status_index] = 'locked' if current_status in {'active', 'true', '1', 'yes'} else 'active'
        elif action == 'update':
            username = str(account.get('username') or '').strip()
            email = str(account.get('email') or '').strip().lower()
            full_name = str(account.get('full_name') or '').strip()
            if not full_name or not username or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
                return False, 'Enter a valid name, username, and email address.'
            for candidate_row_number, candidate in enumerate(values[1:], start=2):
                if candidate_row_number == row_number:
                    continue
                if (
                    str(candidate[username_index] if username_index < len(candidate) else '').strip().casefold() == username.casefold()
                    or str(candidate[email_index] if email_index < len(candidate) else '').strip().casefold() == email
                ):
                    return False, 'That username or email address is already in use.'
            current[name_index] = full_name
            current[username_index] = username
            current[email_index] = email
            if class_index is not None:
                current[class_index] = str(account.get('class_name') or '').strip()
            new_password = str(account.get('password') or '')
            if new_password:
                current[password_index] = hash_password_for_sheet(new_password)
        else:
            return False, 'Unsupported teacher account action.'
        worksheet.update(
            values=[current],
            range_name=f'A{row_number}',
            value_input_option='RAW',
        )
        return True, ''
    except Exception as exc:
        logger.exception('Failed to manage a teacher account in the Users sheet.')
        return False, f'Could not update the Users sheet: {exc}'


def sync_lessons_to_sheet(rows=None):
    try:
        if rows is None:
            conn = get_db()
            try:
                rows = [dict(item) for item in conn.execute('SELECT * FROM lessons ORDER BY id').fetchall()]
            finally:
                conn.close()

        required_headers = ['id', 'title_en', 'title_vi', 'class_level', 'week_number', 'open_date', 'close_date', 'video_url', 'topic_en', 'topic_vi', 'goal_en', 'goal_vi', 'vocabulary', 'dialogue', 'sample_question', 'speaking_question', 'expected_answer', 'sentence_words', 'exercise_text_vi', 'exercise_text_en', 'task_3_text', 'task_4_text']
        sheet_values = [required_headers]
        sheet_values.extend([
            [
                row.get('id', ''),
                row.get('title_en', ''),
                row.get('title_vi', ''),
                row.get('class_level', ''),
                row.get('week_number', 1),
                row.get('open_date', ''),
                row.get('close_date', ''),
                row.get('video_url', ''),
                row.get('topic_en', row.get('title_en', '')),
                row.get('topic_vi', row.get('title_vi', '')),
                row.get('goal_en', 'Learn the language target of this lesson.'),
                row.get('goal_vi', 'Học mục tiêu ngôn ngữ của bài học.'),
                row.get('vocabulary', ''),
                row.get('dialogue', ''),
                row.get('sample_question', ''),
                row.get('speaking_question', row.get('sample_question', '')),
                row.get('expected_answer', ''),
                row.get('sentence_words', ''),
                row.get('exercise_text_vi', ''),
                row.get('exercise_text_en', ''),
                row.get('task_3_text', 'Ghi âm lại giới thiệu về bản thân. (Tối đa file ghi âm là 1 phút)'),
                row.get('task_4_text', 'Quay video ngắn cuộc hội thoại giới thiệu về người bạn mới. (Video có độ dài tối đa là 30 giây)'),
            ]
            for row in rows
        ])

        if IS_FROZEN:
            return bool(call_google_function(
                'replace_sheet',
                {'sheet': 'Lesson', 'values': sheet_values},
            ))

        credentials = get_google_credentials()
        if not credentials:
            message = 'Google Sheets sync skipped: credentials are missing or invalid.'
            logger.warning(message)
            set_session_message('google_sync_warning', message)
            return False
        client = gspread.authorize(credentials)
        sheet_id = os.getenv('GOOGLE_SHEET_ID', '').strip()
        sheet_name = os.getenv('GOOGLE_SHEET_NAME', 'EngClassAI')
        if sheet_id:
            sheet = client.open_by_key(sheet_id)
        else:
            try:
                sheet = client.open(sheet_name)
            except gspread.SpreadsheetNotFound:
                sheet = client.create(sheet_name)
                logger.info('Created missing Google Sheet: %s', sheet_name)
        worksheet = _get_or_create_worksheet(sheet, 'Lesson', rows=5000, cols=30)
        _replace_worksheet_values(worksheet, sheet_values)

        set_session_message('google_sync_warning', 'Google synced')
        return True
    except Exception as exc:
        message = f'Google Sheets sync failed: {exc}'
        logger.exception(message)
        set_session_message('google_sync_warning', message)
        return False


@app.route('/')
def index():
    if session.get('user_id'):
        return redirect(url_for('dashboard'))
    return render_template(
        'login.html',
        is_frozen=vercel_auth_enabled(),
        registration_success=session.pop('registration_success', None),
        warning=session.pop('google_sync_warning', None),
    )


def initial_teacher_sheet_sync_complete():
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key = 'initial_teacher_sheet_sync'"
        ).fetchone()
        return bool(row and row['setting_value'] == 'complete')
    finally:
        conn.close()


def load_google_sheet_data_on_first_teacher_login():
    if not vercel_auth_enabled() or initial_teacher_sheet_sync_complete():
        return

    sheets = (USER_SHEET_NAME, 'Lesson', 'Practice')
    rows_by_sheet = {
        sheet_name: _read_google_worksheet_rows(sheet_name)
        for sheet_name in sheets
    }
    unreadable_sheets = [
        sheet_name for sheet_name in sheets
        if not _google_sheet_read_succeeded(sheet_name)
    ]
    if unreadable_sheets:
        raise RuntimeError(
            'Could not load Google Sheets data from: '
            + ', '.join(unreadable_sheets)
            + '. Check the sheet names and Google/Vercel configuration, then sign in again to retry.'
        )

    synced_users = sync_google_students_into_db(rows_by_sheet[USER_SHEET_NAME])
    synced_lessons = sync_google_lessons_into_db(rows_by_sheet['Lesson'])
    synced_practice = sync_google_practice_into_db(rows_by_sheet['Practice'])

    conn = get_db()
    try:
        conn.execute(
            '''
            INSERT INTO app_settings (setting_key, setting_value)
            VALUES ('initial_teacher_sheet_sync', 'complete')
            ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value
            '''
        )
        conn.commit()
    finally:
        conn.close()

    set_session_message(
        'google_sync_warning',
        'Google Sheets data loaded on first teacher sign-in: '
        f'{len(synced_users)} user accounts, {len(synced_lessons)} lessons, '
        f'{len(synced_practice)} practice records.',
    )
    set_session_message('google_sync_success', True)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        raw_input = (request.form.get('username') or request.form.get('user') or '').strip()
        user_input = normalize_username(raw_input)
        password = request.form.get('password', '')
        role = (request.form.get('role', 'student') or 'student').strip().lower()

        if vercel_auth_enabled():
            login_result = call_vercel_api(
                'login',
                {'username': raw_input.lower(), 'password': password, 'role': role},
                timeout=30,
            )
            access_token = str((login_result or {}).get('access_token') or '')
            account_email = str((login_result or {}).get('email') or '').strip().lower()
            if (
                not account_email
                or str((login_result or {}).get('role') or '').strip().lower() != role
            ):
                access_token = ''
            if not access_token:
                status_code = int((login_result or {}).get('status_code') or 0)
                error = (
                    'Could not connect to the sign-in service. Check your internet connection and try again.'
                    if status_code == 0
                    else str((login_result or {}).get('error') or 'Username, password, or account role is incorrect.')
                )
                return render_template(
                    'login.html',
                    is_frozen=vercel_auth_enabled(),
                    error=error,
                ), 401

            conn = get_db()
            user = upsert_cloud_login_user(conn, login_result or {}, role)
            conn.commit()
            conn.close()
            if not save_cloud_token(user['id'], access_token):
                return render_template(
                    'login.html',
                    is_frozen=True,
                    error='Could not save your sign-in session. Please try again.',
                ), 500
            session.clear()
            session['user_id'] = user['id']
            session['user_role'] = user['role']
            session['lang'] = 'en'
            session['headteacher_claim_user_id'] = user['id']
            if bool((login_result or {}).get('is_headteacher')):
                session['headteacher_user_id'] = user['id']
            if role == 'teacher':
                try:
                    load_google_sheet_data_on_first_teacher_login()
                except Exception as exc:
                    logger.exception('Initial Google Sheets load failed after teacher sign-in.')
                    set_session_message(
                        'google_sync_warning',
                        f'Could not load Google Sheets data: {exc}',
                    )
                    set_session_message('google_sync_success', False)
            return redirect(url_for('dashboard'))

        conn = get_db()
        user = conn.execute(
            'SELECT * FROM users WHERE role = ? AND (username = ? OR email = ? OR full_name = ?)',
            (role, user_input, raw_input.lower(), raw_input)
        ).fetchone()

        if not user:
            matches = conn.execute('SELECT * FROM users WHERE role = ?', (role,)).fetchall()
            for candidate in matches:
                candidate_username = normalize_username(candidate['username'])
                candidate_full_name = normalize_username(candidate['full_name'])
                candidate_email = normalize_username(candidate['email'] if candidate['email'] else '')
                if candidate_username == user_input or candidate_full_name == user_input or candidate_email == user_input:
                    if candidate['password'] == password:
                        user = candidate
                        break
                    break

        if user:
            if user['password'] != password:
                conn.close()
                return render_template('login.html', error='Tên người dùng hoặc mật khẩu không đúng.')
            if user['is_active'] == 0:
                conn.close()
                return render_template('login.html', error='Tài khoản của bạn đã bị khoá. Vui lòng liên hệ giáo viên.')

            session.clear()
            session['user_id'] = user['id']
            session['user_role'] = user['role']
            session['lang'] = session.get('lang', 'en') or 'en'
            conn.close()
            if role == 'teacher':
                try:
                    load_google_sheet_data_on_first_teacher_login()
                except Exception as exc:
                    logger.exception('Initial Google Sheets load failed after teacher sign-in.')
                    set_session_message(
                        'google_sync_warning',
                        f'Could not load Google Sheets data: {exc}',
                    )
                    set_session_message('google_sync_success', False)
            return redirect(url_for('dashboard'))

        conn.close()
        return render_template('login.html', error='Tên người dùng hoặc mật khẩu không đúng.')

    return render_template(
        'login.html',
        is_frozen=vercel_auth_enabled(),
        registration_success=session.pop('registration_success', None),
        warning=session.pop('google_sync_warning', None),
    )


@app.route('/register', methods=['GET', 'POST'])
def register():
    if vercel_auth_enabled():
        return redirect(url_for('login'))
    if request.method == 'POST':
        role = (request.form.get('role') or 'student').strip().lower()
        if role != 'student':
            return render_template(
                'login.html',
                error='Chỉ được phép đăng ký tài khoản học sinh.',
                show_register=True,
            ), 403

        full_name = request.form.get('full_name', '').strip()
        username = normalize_username(request.form.get('username', ''))
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '').strip()
        class_name = request.form.get('class_name', '').strip()

        if not full_name or not username or not email or not password:
            return render_template('login.html', error='Vui lòng điền đầy đủ thông tin.', show_register=True)

        conn = get_db()
        try:
            final_username = choose_unique_username(conn, full_name, email)
            if final_username != username:
                username = final_username
            if conn.execute('SELECT id FROM users WHERE username = ? OR email = ?', (username, email)).fetchone():
                raise sqlite3.IntegrityError

            conn.execute(
                'INSERT INTO users (full_name, username, email, password, role, class_name, is_active, total_points, average_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (full_name, username, email, password, 'student', class_name, 1, 0, 0)
            )
            conn.commit()
            sync_result = sync_students_to_sheet()
            if not sync_result:
                session['google_sync_warning'] = 'Tài khoản học sinh đã được tạo trong hệ thống, nhưng Google Sheets không cập nhật được. Vui lòng kiểm tra service account hoặc quota Drive.'
        except sqlite3.IntegrityError:
            conn.close()
            return render_template('login.html', error='Username hoặc email đã tồn tại trong hệ thống.', show_register=True)
        conn.close()

        session['registration_success'] = 'Your student account was created successfully. You can now sign in.'
        return redirect(url_for('login'))

    return render_template('login.html', show_register=True)


@app.route('/logout')
def logout():
    user_id = session.get('user_id')
    if user_id:
        conn = get_db()
        try:
            conn.execute('DELETE FROM cloud_tokens WHERE user_id = ?', (user_id,))
            conn.commit()
        finally:
            conn.close()
    session.clear()
    return redirect(url_for('login'))


@app.route('/api/set_language', methods=['POST'])
def set_language():
    lang = request.json.get('lang', 'en') if request.is_json else 'en'
    if lang not in ('vi', 'en'):
        lang = 'en'
    session['lang'] = lang
    return jsonify({'status': 'ok', 'lang': lang})


@app.route('/dashboard')
def dashboard():
    user = require_login()
    if not user:
        return redirect(url_for('login'))

    lessons = get_lessons() if user['role'] == 'teacher' else get_lessons_for_student(user)
    if user['role'] == 'teacher':
        conn = get_db()
        recent = conn.execute(
            '''
            SELECT u.full_name, l.title_vi, l.title_en, s.score, s.answer, s.created_at
            FROM submissions s
            JOIN users u ON u.id = s.student_id
            JOIN lessons l ON l.id = s.lesson_id
            ORDER BY s.created_at DESC
            LIMIT 5
            '''
        ).fetchall()
        practice = conn.execute(
            '''
            SELECT s.id, s.answer, s.score, s.feedback, s.created_at,
                   u.full_name, u.class_name,
                   l.title_vi, l.title_en
            FROM submissions s
            JOIN users u ON u.id = s.student_id
            JOIN lessons l ON l.id = s.lesson_id
            ORDER BY s.created_at DESC
            LIMIT 8
            '''
        ).fetchall()
        classes = conn.execute(
            '''
            SELECT class_name AS name,
                   COUNT(*) AS students,
                   SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) AS active_students,
                   ROUND(AVG(COALESCE(average_score, 0)), 1) AS avg_score
            FROM users WHERE role = 'student' GROUP BY class_name ORDER BY class_name
            '''
        ).fetchall()
        conn.close()

        recent_submissions = []
        for item in recent:
            submission = dict(item)
            evaluation = extract_submission_scores(submission.get('answer'), submission.get('score'))
            submission['gemini_score'] = evaluation.get('gemini_score')
            submission['groq_score'] = evaluation.get('groq_score')
            recent_submissions.append(submission)

        class_summaries = [
            {
                'name': row['name'] or 'No class',
                'students': row['students'] or 0,
                'completed': row['active_students'] or 0,
                'avg_score': row['avg_score'] or 0,
            }
            for row in classes
        ]
        total_students = sum(item['students'] for item in class_summaries)
        active_students = sum(item['completed'] for item in class_summaries)
        summary_score = (
            round(sum(item['avg_score'] * item['students'] for item in class_summaries) / total_students, 1)
            if total_students else 0
        )
        summary = {
            'classes': class_summaries,
            'class_count': sum(1 for item in class_summaries if item['name'] != 'No class'),
            'total_students': total_students,
            'active_students': active_students,
            'avg_score': summary_score,
            'weekly_plan': build_weekly_schedule(lessons),
            'recent': recent_submissions,
            'practice': build_practice_history([dict(item) for item in practice]),
        }
        warning = get_session_message('google_sync_warning')
        sync_success = get_session_message('google_sync_success', False)
        session.pop('google_sync_warning', None)
        session.pop('google_sync_success', None)
        return render_template('teacher_dashboard.html', user=user, summary=summary, lessons=lessons, warning=warning, sync_success=sync_success)

    conn = get_db()
    total_completed = conn.execute(
        'SELECT COUNT(DISTINCT lesson_id) FROM submissions WHERE student_id = ?', (user['id'],)
    ).fetchone()[0] or 0
    last_score = conn.execute(
        'SELECT score FROM submissions WHERE student_id = ? ORDER BY created_at DESC LIMIT 1', (user['id'],)
    ).fetchone()
    practice_history = build_practice_history([
        dict(row)
        for row in conn.execute(
            '''
            SELECT s.id, s.student_id, s.lesson_id, s.answer, s.score, s.feedback, s.teacher_note,
                   s.audio_path, s.drive_url, s.created_at,
                   l.title_vi, l.title_en, l.sample_question, l.sentence_words
            FROM submissions s
            JOIN lessons l ON l.id = s.lesson_id
            WHERE s.student_id = ?
            ORDER BY s.created_at DESC
            LIMIT 6
            ''',
            (user['id'],)
        ).fetchall()
    ])
    conn.close()
    student_progress = get_student_progress_summary(user['id'], lessons)
    return render_template('student_dashboard.html', user=user, lessons=lessons, active_section='dashboard', completed_lessons=total_completed, average_score=last_score['score'] if last_score else 0, student_progress=student_progress, weekly_schedule=build_weekly_schedule(lessons), practice_history=practice_history)


@app.route('/lessons')
def lessons_page():
    user = require_login()
    if not user:
        return redirect(url_for('login'))
    lessons = get_lessons_for_student(user)
    conn = get_db()
    total_completed = conn.execute(
        'SELECT COUNT(DISTINCT lesson_id) FROM submissions WHERE student_id = ?', (user['id'],)
    ).fetchone()[0] or 0
    practice_history = build_practice_history([
        dict(row)
        for row in conn.execute(
            '''
            SELECT s.id, s.student_id, s.lesson_id, s.answer, s.score, s.feedback, s.teacher_note,
                   s.audio_path, s.drive_url, s.created_at,
                   l.title_vi, l.title_en, l.sample_question, l.sentence_words
            FROM submissions s
            JOIN lessons l ON l.id = s.lesson_id
            WHERE s.student_id = ?
            ORDER BY s.created_at DESC
            LIMIT 6
            ''',
            (user['id'],)
        ).fetchall()
    ])
    conn.close()
    student_progress = get_student_progress_summary(user['id'], lessons)
    return render_template('student_dashboard.html', user=user, lessons=lessons, active_section='lessons', completed_lessons=total_completed, student_progress=student_progress, weekly_schedule=build_weekly_schedule(lessons), practice_history=practice_history)


@app.route('/vocabulary')
def vocabulary_page():
    user = require_login()
    if not user:
        return redirect(url_for('login'))
    lessons = get_lessons_for_student(user)
    conn = get_db()
    total_completed = conn.execute(
        'SELECT COUNT(DISTINCT lesson_id) FROM submissions WHERE student_id = ?', (user['id'],)
    ).fetchone()[0] or 0
    practice_history = build_practice_history([
        dict(row)
        for row in conn.execute(
            '''
            SELECT s.id, s.student_id, s.lesson_id, s.answer, s.score, s.feedback, s.teacher_note,
                   s.audio_path, s.drive_url, s.created_at,
                   l.title_vi, l.title_en, l.sample_question, l.sentence_words
            FROM submissions s
            JOIN lessons l ON l.id = s.lesson_id
            WHERE s.student_id = ?
            ORDER BY s.created_at DESC
            LIMIT 6
            ''',
            (user['id'],)
        ).fetchall()
    ])
    conn.close()
    student_progress = get_student_progress_summary(user['id'], lessons)
    return render_template('student_dashboard.html', user=user, lessons=lessons, active_section='vocabulary', completed_lessons=total_completed, student_progress=student_progress, weekly_schedule=build_weekly_schedule(lessons), practice_history=practice_history)


@app.route('/progress')
def progress_page():
    user = require_login()
    if not user:
        return redirect(url_for('login'))

    lessons = get_lessons_for_student(user)
    conn = get_db()
    submissions = conn.execute(
    '''
    SELECT s.id, s.student_id, s.lesson_id, s.answer, s.score, s.feedback, s.teacher_note,
           s.audio_path, s.drive_url, s.created_at,
           l.title_vi, l.title_en, l.sample_question, l.sentence_words
    FROM submissions s
    JOIN lessons l ON l.id = s.lesson_id
    WHERE s.student_id = ?
    ORDER BY s.created_at DESC
    LIMIT 6
    ''',
        (user['id'],)
    ).fetchall()
    total_completed = conn.execute(
        'SELECT COUNT(DISTINCT lesson_id) FROM submissions WHERE student_id = ?', (user['id'],)
    ).fetchone()[0] or 0
    conn.close()
    student_progress = get_student_progress_summary(user['id'], lessons)
    return render_template('student_dashboard.html', user=user, lessons=lessons, active_section='progress', recent_submissions=[dict(item) for item in submissions], completed_lessons=total_completed, student_progress=student_progress, weekly_schedule=build_weekly_schedule(lessons), practice_history=build_practice_history([dict(item) for item in submissions]))


@app.route('/practice')
def practice_page():
    user = require_login()
    if not user:
        return redirect(url_for('login'))

    if user['role'] == 'teacher':
        return redirect(url_for('teacher_practice'))

    lessons = get_lessons_for_student(user)
    conn = get_db()
    submissions = conn.execute(
        '''
        SELECT s.id, s.student_id, s.lesson_id, s.answer, s.score, s.feedback, s.teacher_note,
               s.audio_path, s.drive_url, s.created_at,
               l.title_vi, l.title_en, l.sample_question, l.sentence_words, l.task_3_text
        FROM submissions s
        JOIN lessons l ON l.id = s.lesson_id
        WHERE s.student_id = ?
        ORDER BY s.created_at DESC
        ''',
        (user['id'],)
    ).fetchall()
    submissions_list = [dict(item) for item in submissions]

    # Do not call external AI services while rendering the Practice page.
    # Scoring is performed during submission or manually by the teacher.

    practice_history = build_practice_history(submissions_list)
    total_completed = conn.execute(
        'SELECT COUNT(DISTINCT lesson_id) FROM submissions WHERE student_id = ?', (user['id'],)
    ).fetchone()[0] or 0
    conn.close()
    student_progress = get_student_progress_summary(user['id'], lessons)
    return render_template(
        'student_dashboard.html',
        user=user,
        lessons=lessons,
        active_section='practice',
        completed_lessons=total_completed,
        student_progress=student_progress,
        weekly_schedule=build_weekly_schedule(lessons),
        practice_history=practice_history,
        recent_submissions=submissions_list,
    )


@app.route('/teacher/practice')
def teacher_practice():
    user = require_login()
    if not user or user['role'] != 'teacher':
        return redirect(url_for('login'))

    page = max(1, request.args.get('page', default=1, type=int) or 1)
    per_page = get_page_size()
    search_field = request.args.get('search_field', 'student')
    search_query = request.args.get('q', '').strip()
    practice_search_fields = {
        'student': 'u.full_name',
        'class': 'u.class_name',
        'lesson': "COALESCE(l.title_en, '') || ' ' || COALESCE(l.title_vi, '')",
    }
    if search_field not in practice_search_fields:
        search_field = 'student'
    search_sql = ''
    search_params = []
    if search_query:
        search_sql = f"WHERE {practice_search_fields[search_field]} LIKE ?"
        search_params.append(f'%{search_query}%')

    conn = get_db()
    total_submissions = conn.execute(
        f'''
        SELECT COUNT(*)
        FROM submissions s
        JOIN users u ON u.id = s.student_id
        JOIN lessons l ON l.id = s.lesson_id
        {search_sql}
        ''',
        search_params
    ).fetchone()[0]
    total_pages = max(1, (total_submissions + per_page - 1) // per_page)
    page = min(page, total_pages)
    submissions = conn.execute(
        f'''
        SELECT s.id, s.answer, s.score, s.feedback, s.teacher_note, s.audio_path, s.drive_url, s.created_at,
               u.full_name AS student_name, u.class_name,
               l.title_vi, l.title_en
        FROM submissions s
        JOIN users u ON u.id = s.student_id
        JOIN lessons l ON l.id = s.lesson_id
        {search_sql}
        ORDER BY s.created_at DESC
        LIMIT ? OFFSET ?
        ''',
        (*search_params, per_page, (page - 1) * per_page)
    ).fetchall()
    summary_rows = conn.execute(
        '''
        SELECT s.answer, s.score, s.teacher_note, s.audio_path, s.drive_url
        FROM submissions s
        JOIN users u ON u.id = s.student_id
        JOIN lessons l ON l.id = s.lesson_id
        '''
    ).fetchall()
    conn.close()

    submission_rows = []
    for item in submissions:
        row = dict(item)
        evaluation = extract_submission_scores(row.get('answer'), row.get('score'))
        row['gemini_score'] = evaluation.get('gemini_score')
        row['groq_score'] = evaluation.get('groq_score')
        row['teacher_score'] = evaluation.get('teacher_score') if evaluation.get('teacher_score') is not None else row.get('score') or 0
        row['gemini_criteria'] = evaluation.get('gemini_criteria', {})
        row['groq_criteria'] = evaluation.get('groq_criteria', {})
        row['teacher_criteria'] = evaluation.get('teacher_criteria', {})
        row['submitted_media'] = bool(row.get('audio_path') or row.get('drive_url') or row.get('video_url'))
        submission_rows.append(row)

    summary_scores = []
    reviewed_count = 0
    media_ready_count = 0
    for item in summary_rows:
        evaluation = extract_submission_scores(item['answer'], item['score'])
        teacher_score = evaluation.get('teacher_score') if evaluation.get('teacher_score') is not None else item['score'] or 0
        if teacher_score is not None and item['teacher_note'] not in (None, ''):
            reviewed_count += 1
        if item['audio_path'] or item['drive_url']:
            media_ready_count += 1
        summary_scores.append(teacher_score or item['score'] or 0)

    total_submissions = len(summary_rows)
    review_summary = {
        'total': total_submissions,
        'reviewed': reviewed_count,
        'pending': total_submissions - reviewed_count,
        'media_ready': media_ready_count,
        'average_score': round(sum(summary_scores) / total_submissions, 1) if total_submissions else 0,
    }

    warning = get_session_message('google_sync_warning')
    sync_success = get_session_message('google_sync_success', False)
    session.pop('google_sync_warning', None)
    session.pop('google_sync_success', None)
    return render_template(
        'teacher_practice.html',
        user=user,
        submissions=submission_rows,
        review_summary=review_summary,
        warning=warning,
        sync_success=sync_success,
        page=page,
        total_pages=total_pages,
        per_page=per_page,
        page_size_options=PAGE_SIZE_OPTIONS,
        total_items=total_submissions,
        search_field=search_field,
        search_query=search_query,
    )


@app.route('/teacher/lessons')
def teacher_lessons():
    user = require_login()
    if not user or user['role'] != 'teacher':
        return redirect(url_for('login'))

    page = max(1, request.args.get('page', default=1, type=int) or 1)
    per_page = get_page_size()
    class_filter = request.args.get('class_level', '').strip()
    search_field = request.args.get('search_field', 'title')
    search_query = request.args.get('q', '').strip()
    lesson_search_fields = {
        'title': "COALESCE(title_en, '') || ' ' || COALESCE(title_vi, '')",
        'class': 'class_level',
        'week': 'week_number',
    }
    if search_field not in lesson_search_fields:
        search_field = 'title'

    conn = get_db()
    filters = []
    filter_params = []
    if class_filter and class_filter in {'6', '7', '8', '9'}:
        filters.append('class_level = ?')
        filter_params.append(int(class_filter))
    if search_query:
        filters.append(f'{lesson_search_fields[search_field]} LIKE ?')
        filter_params.append(f'%{search_query}%')
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ''
    total_lessons = conn.execute(
        f'SELECT COUNT(*) AS count FROM lessons {where_clause}',
        filter_params
    ).fetchone()['count']
    total_pages = max(1, (total_lessons + per_page - 1) // per_page)
    page = min(page, total_pages)
    lessons = conn.execute(
        f'SELECT * FROM lessons {where_clause} ORDER BY id LIMIT ? OFFSET ?',
        (*filter_params, per_page, (page - 1) * per_page)
    ).fetchall()
    conn.close()
    lessons = [dict(item) for item in lessons]

    warning = session.pop('google_sync_warning', None)
    sync_success = session.pop('google_sync_success', False)
    return render_template('teacher_lessons.html', user=user, lessons=lessons, page=page, total_pages=total_pages, per_page=per_page, page_size_options=PAGE_SIZE_OPTIONS, total_items=total_lessons, warning=warning, sync_success=sync_success, search_field=search_field, search_query=search_query)


@app.route('/teacher/lesson/new', methods=['POST'])
def teacher_new_lesson():
    user = require_login()
    if not user or user['role'] != 'teacher':
        return redirect(url_for('login'))

    conn = get_db()
    title_vi = request.form.get('title_vi', 'New lesson').strip() or 'New lesson'
    title_en = request.form.get('title_en', 'New lesson').strip() or 'New lesson'
    topic_en = request.form.get('topic_en', title_en).strip() or title_en
    topic_vi = request.form.get('topic_vi', title_vi).strip() or title_vi
    goal_en = request.form.get('goal_en', 'Teacher created lesson.').strip() or 'Teacher created lesson.'
    goal_vi = request.form.get('goal_vi', 'Bài học do giáo viên tạo.').strip() or 'Bài học do giáo viên tạo.'
    vocabulary = request.form.get('vocabulary', 'new,lesson,practice').strip() or 'new,lesson,practice'
    dialogue = request.form.get('dialogue', 'Teacher created lesson content.').strip() or 'Teacher created lesson content.'
    sample_question = request.form.get('sample_question', 'What is the topic?').strip() or 'What is the topic?'
    expected_answer = request.form.get('expected_answer', 'This is the answer.').strip() or 'This is the answer.'
    sentence_words = request.form.get('sentence_words', 'This,is,the,answer,.').strip() or 'This,is,the,answer,.'
    class_level = _normalized_lesson_integer(request.form.get('class_level', '6'), 6, 1)
    week_number = int(request.form.get('week_number', 1) or 1)
    open_date = request.form.get('open_date', '').strip()
    close_date = request.form.get('close_date', '').strip()
    task_3_text = request.form.get('task_3_text', 'Ghi âm lại giới thiệu về bản thân. (Tối đa file ghi âm là 1 phút)').strip() or 'Ghi âm lại giới thiệu về bản thân. (Tối đa file ghi âm là 1 phút)'
    task_4_text = request.form.get('task_4_text', 'Quay video ngắn cuộc hội thoại giới thiệu về người bạn mới. (Video có độ dài tối đa là 30 giây)').strip() or 'Quay video ngắn cuộc hội thoại giới thiệu về người bạn mới. (Video có độ dài tối đa là 30 giây)'
    conn.execute(
        '''
        INSERT INTO lessons (title_en, title_vi, level, class_level, topic_en, topic_vi, goal_en, goal_vi,
        video_url, vocabulary, dialogue, sample_question, speaking_question, expected_answer, sentence_words,
        exercise_text_vi, exercise_text_en, task_3_text, task_4_text, week_number, open_date, close_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            title_en, title_vi, '', class_level, topic_en, topic_vi, goal_en, goal_vi,
            'https://www.youtube.com/watch?v=example', vocabulary, dialogue,
            sample_question, sample_question, expected_answer, sentence_words, 'Sắp xếp từ để tạo câu đúng.', 'Arrange the words to make the correct sentence.',
            task_3_text, task_4_text, week_number, open_date, close_date
        )
    )
    conn.commit(); conn.close()
    set_google_sync_feedback('Lesson', 'Lesson created', sync_lessons_to_sheet())
    return redirect(url_for('teacher_lessons'))


@app.route('/teacher/lesson/<int:lesson_id>/delete', methods=['POST'])
def delete_lesson(lesson_id):
    user = require_login()
    if not user or user['role'] != 'teacher':
        return redirect(url_for('login'))

    conn = get_db()
    conn.execute('DELETE FROM lessons WHERE id = ?', (lesson_id,))
    conn.execute('DELETE FROM submissions WHERE lesson_id = ?', (lesson_id,))
    conn.commit(); conn.close()
    set_google_sync_feedback('Lesson', 'Lesson deleted', sync_lessons_to_sheet())
    return redirect(url_for('teacher_lessons'))


@app.route('/teacher/student/<int:user_id>')
def teacher_student_detail(user_id):
    user = require_login()
    if not user or user['role'] != 'teacher':
        return redirect(url_for('login'))

    conn = get_db()
    student = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not student:
        conn.close(); abort(404)

    progress = conn.execute(
        '''
        SELECT s.id, l.title_vi, l.title_en, l.vocabulary, s.answer, s.score, s.feedback, s.audio_path, s.drive_url, s.created_at, s.teacher_note
        FROM submissions s
        JOIN lessons l ON l.id = s.lesson_id
        WHERE s.student_id = ?
        ORDER BY s.created_at DESC
        ''',
        (user_id,)
    ).fetchall()

    vocabulary = []
    seen_words = set()
    for item in progress:
        words = [part.strip() for part in str(item['vocabulary'] or '').split(',') if part.strip()]
        for word in words:
            if word.lower() not in seen_words:
                seen_words.add(word.lower())
                vocabulary.append(word)
    conn.close()
    return render_template('teacher_student_detail.html', user=user, student=dict(student), progress=[dict(item) for item in progress], student_vocabulary=vocabulary)


@app.route('/teacher/student/<int:user_id>/edit', methods=['GET', 'POST'])
def teacher_edit_student(user_id):
    user = require_login()
    if not user or user['role'] != 'teacher':
        return redirect(url_for('login'))

    conn = get_db()
    student = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not student:
        conn.close(); abort(404)
    student_data = dict(student)

    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip() or student_data['full_name']
        username = normalize_username(request.form.get('username', '')) or generate_username(full_name, request.form.get('email', student_data['email']))
        email = (request.form.get('email', student_data['email']) or student_data['email']).strip().lower()
        class_name = request.form.get('class_name', student_data['class_name'] or '').strip()
        password = request.form.get('password', '').strip()
        is_active = 1 if request.form.get('is_active') == 'on' else int(student_data['is_active'])

        existing = conn.execute(
            'SELECT id FROM users WHERE (username = ? OR email = ?) AND id != ?',
            (username, email, user_id)
        ).fetchone()
        if existing:
            conn.close()
            return render_template('teacher_student_edit.html', user=user, student=dict(student), error='Username hoặc email đã tồn tại.')

        if password:
            conn.execute(
                'UPDATE users SET full_name = ?, username = ?, email = ?, password = ?, class_name = ?, is_active = ? WHERE id = ?',
                (full_name, username, email, password, class_name, is_active, user_id)
            )
        else:
            conn.execute(
                'UPDATE users SET full_name = ?, username = ?, email = ?, class_name = ?, is_active = ? WHERE id = ?',
                (full_name, username, email, class_name, is_active, user_id)
            )
        conn.commit(); conn.close()
        set_google_sync_feedback(USER_SHEET_NAME, 'Student updated', sync_students_to_sheet())
        return redirect(url_for('teacher_students'))

    conn.close()
    return render_template('teacher_student_edit.html', user=user, student=student_data, error=None)


@app.route('/teacher/student/new', methods=['GET', 'POST'])
def teacher_create_student():
    user = require_login()
    if not user or user['role'] != 'teacher':
        return redirect(url_for('login'))

    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        username = normalize_username(request.form.get('username', ''))
        email = (request.form.get('email', '') or '').strip().lower()
        class_name = request.form.get('class_name', '').strip()
        password = request.form.get('password', '123456').strip() or '123456'
        role = 'student'

        if not full_name or not email:
            return render_template('teacher_student_edit.html', user=user, student={
                'full_name': full_name,
                'username': username,
                'email': email,
                'class_name': class_name,
                'role': role,
                'is_active': 1,
            }, error='Họ tên và email là bắt buộc.')

        conn = get_db()
        if conn.execute('SELECT id FROM users WHERE email = ? OR username = ?', (email, username or full_name)).fetchone():
            conn.close()
            return render_template('teacher_student_edit.html', user=user, student={
                'full_name': full_name,
                'username': username,
                'email': email,
                'class_name': class_name,
                'role': role,
                'is_active': 1,
            }, error='Username hoặc email đã tồn tại.')

        final_username = username or generate_username(full_name, email)
        if conn.execute('SELECT id FROM users WHERE username = ?', (final_username,)).fetchone():
            final_username = choose_unique_username(conn, full_name, email)

        conn.execute(
            'INSERT INTO users (full_name, username, email, password, role, class_name, is_active, total_points, average_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (full_name, final_username, email, password, role, class_name, 1, 0, 0)
        )
        conn.commit(); conn.close()
        set_google_sync_feedback(USER_SHEET_NAME, 'Student created', sync_students_to_sheet())
        session['student_created_success'] = 'Student account created successfully.'
        return redirect(url_for('teacher_students'))

    return render_template('teacher_student_edit.html', user=user, student={
        'full_name': '',
        'username': '',
        'email': '',
        'class_name': '',
        'role': 'student',
        'is_active': 1,
    }, error=None, is_new=True)


@app.route('/teacher/lesson/<int:lesson_id>', methods=['GET', 'POST'])
def teacher_edit_lesson(lesson_id):
    user = require_login()
    if not user or user['role'] != 'teacher':
        return redirect(url_for('login'))

    conn = get_db()
    lesson = conn.execute('SELECT * FROM lessons WHERE id = ?', (lesson_id,)).fetchone()
    if not lesson:
        conn.close()
        abort(404)
    lesson_data = dict(lesson)

    if request.method == 'POST':
        title_vi = request.form.get('title_vi', '').strip() or lesson_data['title_vi']
        title_en = request.form.get('title_en', '').strip() or lesson_data['title_en']
        topic_en = request.form.get('topic_en', '').strip() or lesson_data.get('topic_en') or title_en
        topic_vi = request.form.get('topic_vi', '').strip() or lesson_data.get('topic_vi') or title_vi
        goal_en = request.form.get('goal_en', '').strip() or lesson_data.get('goal_en') or 'Learn the language target of this lesson.'
        goal_vi = request.form.get('goal_vi', '').strip() or lesson_data.get('goal_vi') or 'Học mục tiêu ngôn ngữ của bài học.'
        vocabulary = request.form.get('vocabulary', '').strip() or lesson_data.get('vocabulary') or ''
        dialogue = request.form.get('dialogue', '').strip() or lesson_data.get('dialogue') or ''
        video_url = request.form.get('video_url', '').strip() or lesson_data['video_url']
        sample_question = request.form.get('sample_question', '').strip() or lesson_data['sample_question']
        speaking_question = request.form.get('speaking_question', '').strip() or sample_question
        expected_answer = request.form.get('expected_answer', '').strip() or lesson_data['expected_answer']
        sentence_words = request.form.get('sentence_words', '').strip() or lesson_data['sentence_words']
        exercise_text_vi = request.form.get('exercise_text_vi', '').strip() or lesson_data['exercise_text_vi']
        exercise_text_en = request.form.get('exercise_text_en', '').strip() or lesson_data['exercise_text_en']
        task_3_text = request.form.get('task_3_text', '').strip() or lesson_data.get('task_3_text') or 'Ghi âm lại giới thiệu về bản thân. (Tối đa file ghi âm là 1 phút)'
        task_4_text = request.form.get('task_4_text', '').strip() or lesson_data.get('task_4_text') or 'Quay video ngắn cuộc hội thoại giới thiệu về người bạn mới. (Video có độ dài tối đa là 30 giây)'
        class_level = _normalized_lesson_integer(request.form.get('class_level', lesson_data.get('class_level') or 6), lesson_data.get('class_level') or 6, 1)
        week_number = int(request.form.get('week_number', lesson_data.get('week_number') or 1) or (lesson_data.get('week_number') or 1))
        open_date = request.form.get('open_date', '').strip()
        close_date = request.form.get('close_date', '').strip()
        if open_date and close_date and close_date < open_date:
            conn.close()
            lesson_data['open_date'] = open_date
            lesson_data['close_date'] = close_date
            return render_template('teacher_lesson_edit.html', user=user, lesson=lesson_data, form_error='Close date must be on or after the open date.'), 400

        conn.execute(
            '''
            UPDATE lessons
            SET title_en = ?, title_vi = ?, topic_en = ?, topic_vi = ?, goal_en = ?, goal_vi = ?,
                vocabulary = ?, dialogue = ?, video_url = ?, sample_question = ?, speaking_question = ?,
                expected_answer = ?, sentence_words = ?, exercise_text_vi = ?, exercise_text_en = ?, task_3_text = ?, task_4_text = ?,
                class_level = ?, week_number = ?, open_date = ?, close_date = ?
            WHERE id = ?
            ''',
            (title_en, title_vi, topic_en, topic_vi, goal_en, goal_vi, vocabulary, dialogue, video_url, sample_question, speaking_question, expected_answer, sentence_words, exercise_text_vi, exercise_text_en, task_3_text, task_4_text, class_level, week_number, open_date, close_date, lesson_id)
        )
        conn.commit()
        conn.close()
        set_google_sync_feedback('Lesson', 'Lesson updated', sync_lessons_to_sheet())
        return redirect(url_for('teacher_lessons'))

    data = dict(lesson_data)
    conn.close()
    return render_template('teacher_lesson_edit.html', user=user, lesson=data)


@app.route('/teacher/sync_google', methods=['POST'])
def teacher_sync_google():
    user = require_login()
    if not user or user['role'] != 'teacher':
        return redirect(url_for('login'))

    sheet = request.form.get('sheet', 'all').strip().lower()
    try:
        if sheet == 'student':
            synced = sync_google_students_into_db()
            message = f'Users sheet synced successfully ({len(synced)} accounts).' if synced else 'Users sync failed or the Users sheet contains no accounts.'
        elif sheet == 'lesson':
            synced = sync_google_lessons_into_db()
            message = f'Lesson sheet synced successfully ({len(synced)} records).' if synced else 'Lesson sync failed or the Lesson sheet contains no records.'
        elif sheet == 'practice':
            synced_practice = sync_google_practice_into_db()
            saved = sync_practice_to_sheet() if synced_practice else sync_practice_to_sheet()
            message = f'Practice sheet synced successfully. Imported {len(synced_practice)} records.' if saved else 'Practice sheet sync failed. Check the Google Sheets configuration and logs.'
        elif sheet == 'all':
            students = sync_google_students_into_db()
            lessons = sync_google_lessons_into_db()
            practices = sync_google_practice_into_db()
            if students or lessons or practices:
                sync_practice_to_sheet()
                message = f'Google Sheets sync completed: {len(students)} user accounts, {len(lessons)} lessons, and {len(practices)} practice records imported.'
                sync_success = True
            else:
                message = 'Google Sheets sync failed or the Users, Lesson, and Practice sheets contain no records.'
                sync_success = False
        else:
            message = f'Google Sheets sync failed: unsupported sheet "{sheet}".'
            sync_success = False
        if sheet == 'student':
            sync_success = bool(synced)
        elif sheet == 'lesson':
            sync_success = bool(synced)
        elif sheet == 'practice':
            sync_success = saved
        set_session_message('google_sync_warning', message)
        set_session_message('google_sync_success', sync_success)
    except Exception as exc:  # pragma: no cover - runtime integration path
        logger.exception('Google Sheet sync failed for teacher manual sync.')
        set_session_message('google_sync_warning', f'Google sync failed: {exc}')
        set_session_message('google_sync_success', False)

    return redirect(request.referrer or url_for('teacher_students'))


@app.route('/teacher/students')
def teacher_students():
    user = require_login()
    if not user or user['role'] != 'teacher':
        return redirect(url_for('login'))

    page = max(1, request.args.get('page', default=1, type=int) or 1)
    per_page = get_page_size()
    search_field = request.args.get('search_field', 'name')
    search_query = request.args.get('q', '').strip()
    student_search_fields = {
        'name': 'full_name',
        'username': 'username',
        'class': 'class_name',
    }
    if search_field not in student_search_fields:
        search_field = 'name'
    search_sql = ''
    search_params = ['student']
    if search_query:
        search_sql = f' AND {student_search_fields[search_field]} LIKE ?'
        search_params.append(f'%{search_query}%')

    conn = get_db()
    total_students = conn.execute(
        f'SELECT COUNT(*) AS count FROM users WHERE role = ?{search_sql}',
        search_params
    ).fetchone()['count']
    total_pages = max(1, (total_students + per_page - 1) // per_page)
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    students = conn.execute(
        f'SELECT * FROM users WHERE role = ?{search_sql} ORDER BY full_name LIMIT ? OFFSET ?',
        (*search_params, per_page, offset)
    ).fetchall()

    student_rows = []
    for student in students:
        stats = conn.execute(
            'SELECT COUNT(*) AS tasks, AVG(score) AS avg_score FROM submissions WHERE student_id = ?',
            (student['id'],)
        ).fetchone()
        student_rows.append({
            **dict(student),
            'tasks': stats['tasks'] or 0,
            'avg_score': float(stats['avg_score'] or 0),
            'is_active': bool(student['is_active']) if student['is_active'] is not None else True,
        })
    conn.close()

    warning = get_session_message('google_sync_warning')
    sync_success = get_session_message('google_sync_success', False)
    student_created_success = session.pop('student_created_success', None)
    session.pop('google_sync_warning', None)
    session.pop('google_sync_success', None)
    return render_template('teacher_students.html', user=user, students=student_rows, page=page, total_pages=total_pages, per_page=per_page, page_size_options=PAGE_SIZE_OPTIONS, total_items=total_students, warning=warning, sync_success=sync_success, student_created_success=student_created_success, search_field=search_field, search_query=search_query)


@app.route('/teacher/accounts')
def teacher_accounts():
    user = require_login()
    if not is_headteacher(user):
        return redirect(url_for('dashboard') if user else url_for('login'))
    try:
        accounts = read_teacher_accounts_from_sheet()
    except Exception as exc:
        logger.exception('Could not load teacher accounts for headteacher.')
        accounts = []
        set_session_message('teacher_accounts_error', str(exc))

    search_query = request.args.get('q', '').strip().casefold()
    if search_query:
        accounts = [
            account for account in accounts
            if search_query in account['full_name'].casefold()
            or search_query in account['username'].casefold()
            or search_query in account['email'].casefold()
        ]
    page = max(1, request.args.get('page', default=1, type=int) or 1)
    per_page = get_page_size()
    total_items = len(accounts)
    total_pages = max(1, (total_items + per_page - 1) // per_page)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    warning = session.pop('teacher_accounts_error', None) or session.pop('teacher_accounts_notice', None)
    success = session.pop('teacher_accounts_success', False)
    return render_template(
        'teacher_accounts.html',
        user=user,
        accounts=accounts[start:start + per_page],
        page=page,
        total_pages=total_pages,
        per_page=per_page,
        page_size_options=PAGE_SIZE_OPTIONS,
        total_items=total_items,
        search_query=search_query,
        warning=warning,
        sync_success=success,
    )


@app.route('/teacher/accounts/sync', methods=['POST'])
def teacher_accounts_sync():
    user = require_login()
    if not is_headteacher(user):
        return redirect(url_for('dashboard') if user else url_for('login'))
    try:
        accounts = read_teacher_accounts_from_sheet()
        set_session_message(
            'teacher_accounts_notice',
            f'Teacher list synced successfully ({len(accounts)} accounts loaded from Google Sheets).',
        )
        set_session_message('teacher_accounts_success', True)
    except Exception as exc:
        logger.exception('Could not sync teacher accounts from Google Sheets.')
        set_session_message('teacher_accounts_notice', f'Teacher sync failed: {exc}')
        set_session_message('teacher_accounts_success', False)
    return redirect(url_for('teacher_accounts'))


@app.route('/teacher/accounts/new', methods=['GET', 'POST'])
def teacher_account_new():
    user = require_login()
    if not is_headteacher(user):
        return redirect(url_for('dashboard') if user else url_for('login'))
    account = {
        'full_name': '',
        'username': '',
        'email': '',
        'class_name': '',
    }
    if request.method == 'POST':
        account = {
            'full_name': request.form.get('full_name', '').strip(),
            'username': normalize_username(request.form.get('username', '')),
            'email': request.form.get('email', '').strip().lower(),
            'class_name': request.form.get('class_name', '').strip(),
            'password': request.form.get('password', '').strip() or '123456',
        }
        if not account['full_name'] or not account['username'] or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', account['email']):
            return render_template(
                'teacher_account_edit.html',
                user=user,
                account=account,
                is_new=True,
                error='Enter a valid name, username, and email address.',
            ), 400
        synced, message = manage_teacher_account_in_sheet('create', account)
        if not synced:
            return render_template(
                'teacher_account_edit.html',
                user=user,
                account=account,
                is_new=True,
                error=message,
            ), 400
        session['teacher_accounts_notice'] = 'Teacher account created and synced to the Users sheet.'
        session['teacher_accounts_success'] = True
        return redirect(url_for('teacher_accounts'))
    return render_template(
        'teacher_account_edit.html',
        user=user,
        account=account,
        is_new=True,
        error=None,
    )


@app.route('/teacher/accounts/<username>/edit', methods=['GET', 'POST'])
def teacher_account_edit(username):
    user = require_login()
    if not is_headteacher(user):
        return redirect(url_for('dashboard') if user else url_for('login'))
    try:
        accounts = read_teacher_accounts_from_sheet()
    except Exception as exc:
        logger.exception('Could not load teacher account for editing.')
        abort(502, description='Could not read the Users sheet.')
    account = next(
        (item for item in accounts if item['username'].casefold() == username.casefold()),
        None,
    )
    if not account:
        abort(404)
    if request.method == 'POST':
        updated = {
            'full_name': request.form.get('full_name', '').strip(),
            'username': normalize_username(request.form.get('username', '')),
            'email': request.form.get('email', '').strip().lower(),
            'class_name': request.form.get('class_name', '').strip(),
            'password': request.form.get('password', '').strip(),
        }
        if not updated['full_name'] or not updated['username'] or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', updated['email']):
            return render_template(
                'teacher_account_edit.html',
                user=user,
                account={**account, **updated},
                is_new=False,
                error='Enter a valid name, username, and email address.',
            ), 400
        synced, message = manage_teacher_account_in_sheet(
            'update',
            updated,
            original_username=account['username'],
        )
        if not synced:
            return render_template(
                'teacher_account_edit.html',
                user=user,
                account={**account, **updated},
                is_new=False,
                error=message,
            ), 400
        session['teacher_accounts_notice'] = 'Teacher account updated and synced to the Users sheet.'
        session['teacher_accounts_success'] = True
        return redirect(url_for('teacher_accounts'))
    return render_template(
        'teacher_account_edit.html',
        user=user,
        account=account,
        is_new=False,
        error=None,
    )


@app.route('/teacher/accounts/<username>/toggle', methods=['POST'])
def teacher_account_toggle(username):
    user = require_login()
    if not is_headteacher(user):
        return redirect(url_for('dashboard') if user else url_for('login'))
    synced, message = manage_teacher_account_in_sheet(
        'toggle', original_username=username
    )
    session['teacher_accounts_notice'] = (
        'Teacher account status updated in the Users sheet.'
        if synced else message
    )
    session['teacher_accounts_success'] = synced
    return redirect(url_for('teacher_accounts'))


@app.route('/teacher/accounts/<username>/delete', methods=['POST'])
def teacher_account_delete(username):
    user = require_login()
    if not is_headteacher(user):
        return redirect(url_for('dashboard') if user else url_for('login'))
    synced, message = manage_teacher_account_in_sheet(
        'delete', original_username=username
    )
    session['teacher_accounts_notice'] = (
        'Teacher account deleted from the Users sheet.'
        if synced else message
    )
    session['teacher_accounts_success'] = synced
    return redirect(url_for('teacher_accounts'))


@app.route('/teacher/student/new', methods=['GET', 'POST'])
def teacher_new_student():
    return teacher_create_student()


@app.route('/teacher/import_students', methods=['POST'])
def import_students():
    user = require_login()
    if not user or user['role'] != 'teacher':
        return redirect(url_for('login'))

    if 'file' not in request.files:
        return redirect(url_for('teacher_students'))

    uploaded = request.files['file']
    if not uploaded.filename:
        return redirect(url_for('teacher_students'))

    workbook = load_workbook(uploaded, read_only=True)
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    inserted = []

    for row in rows[1:]:
        if not row or not any(cell is not None and str(cell).strip() for cell in row):
            continue
        full_name = str(row[0] or '').strip()
        email = str((row[1] if len(row) > 1 else '') or '').strip().lower()
        if not full_name or not email:
            continue
        password = str((row[2] if len(row) > 2 else '123456') or '123456').strip() or '123456'
        class_name = str((row[3] if len(row) > 3 else '') or '').strip()
        username_value = str((row[4] if len(row) > 4 else '') or '').strip()
        username = normalize_username(username_value or full_name or email.split('@')[0])

        conn = get_db()
        existing = conn.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
        if existing is None:
            final_username = choose_unique_username(conn, full_name, email)
            if not username or username == 'student':
                username = final_username
            conn.execute(
                'INSERT INTO users (full_name, username, email, password, role, class_name, is_active, total_points, average_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (full_name, username, email, password, 'student', class_name, 1, 0, 0)
            )
            inserted.append({
                'full_name': full_name,
                'username': username,
                'email': email,
                'role': 'student',
                'class_name': class_name,
                'is_active': True,
                'total_points': 0,
                'average_score': 0,
            })
        conn.commit(); conn.close()

    if inserted:
        set_google_sync_feedback(USER_SHEET_NAME, f'{len(inserted)} students imported', sync_students_to_sheet())
    return redirect(url_for('teacher_students'))


@app.route('/teacher/profile', methods=['GET', 'POST'])
def teacher_profile():
    user = require_login()
    if not user or user['role'] != 'teacher':
        return redirect(url_for('login'))
    user_data = dict(user)

    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        username = normalize_username(request.form.get('username', ''))
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '').strip()

        if not full_name or not username or not email:
            return render_template(
                'teacher_profile_edit.html',
                user={**user_data, 'full_name': full_name, 'username': request.form.get('username', ''), 'email': email},
                error='Name, username, and email are required.',
                saved=False,
                synced=False,
            )

        conn = get_db()
        existing = conn.execute(
            'SELECT id FROM users WHERE (lower(username) = ? OR lower(email) = ?) AND id != ?',
            (username.lower(), email.lower(), user['id']),
        ).fetchone()
        if existing:
            conn.close()
            return render_template(
                'teacher_profile_edit.html',
                user={**user_data, 'full_name': full_name, 'username': request.form.get('username', ''), 'email': email},
                error='That username or email address is already in use.',
                saved=False,
                synced=False,
            )
        conn.close()

        updated_profile = {
            'full_name': full_name,
            'username': username,
            'email': email,
        }
        synced, sync_error = sync_teacher_profile_to_sheet(user_data, updated_profile, password)
        if not synced:
            return render_template(
                'teacher_profile_edit.html',
                user={**user_data, **updated_profile},
                error=sync_error,
                saved=False,
                synced=False,
            ), 502

        conn = get_db()
        if password:
            conn.execute(
                'UPDATE users SET full_name = ?, username = ?, email = ?, password = ? WHERE id = ?',
                (full_name, username, email, password, user['id'])
            )
        else:
            conn.execute(
                'UPDATE users SET full_name = ?, username = ?, email = ? WHERE id = ?',
                (full_name, username, email, user['id'])
            )
        conn.commit(); conn.close()
        email_changed = email.lower() != str(user_data.get('email') or '').strip().lower()
        if email_changed and vercel_auth_enabled():
            conn = get_db()
            conn.execute('DELETE FROM cloud_tokens WHERE user_id = ?', (user['id'],))
            conn.commit(); conn.close()
            session.clear()
            session['google_sync_warning'] = (
                'Your profile was synced to the account sheet. Sign in again with your updated email address.'
            )
            return redirect(url_for('login'))
        session['user_id'] = user['id']
        return redirect(url_for('teacher_profile', saved='1', synced='1'))

    return render_template(
        'teacher_profile_edit.html',
        user=user_data,
        error=None,
        saved=request.args.get('saved') == '1',
        synced=request.args.get('synced') == '1',
    )


@app.route('/student/account', methods=['GET', 'POST'])
def student_account():
    user = require_login()
    if not user or user['role'] != 'student':
        return redirect(url_for('login'))

    error = None
    if request.method == 'POST':
        current_password = request.form.get('current_password', '').strip()
        new_password = request.form.get('new_password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()

        if current_password != user['password']:
            error = 'Your current password is incorrect.'
        elif len(new_password) < 6:
            error = 'Your new password must be at least 6 characters long.'
        elif new_password != confirm_password:
            error = 'The new password and confirmation do not match.'
        else:
            conn = get_db()
            conn.execute('UPDATE users SET password = ? WHERE id = ?', (new_password, user['id']))
            conn.commit()
            conn.close()
            return redirect(url_for('student_account', saved='1'))

    return render_template(
        'student_account.html',
        user=user,
        error=error,
        saved=request.args.get('saved') == '1',
    )


@app.route('/teacher/student/<int:user_id>/toggle_status', methods=['POST'])
def toggle_student_status(user_id):
    user = require_login()
    if not user or user['role'] != 'teacher':
        return redirect(url_for('login'))

    conn = get_db()
    student = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if student:
        new_status = 0 if student['is_active'] else 1
        conn.execute('UPDATE users SET is_active = ? WHERE id = ?', (new_status, user_id))
        conn.commit()
        action = 'Student unlocked' if new_status else 'Student locked'
    else:
        action = 'Student status updated'
    conn.close()
    set_google_sync_feedback(USER_SHEET_NAME, action, sync_students_to_sheet())
    return redirect(url_for('teacher_students'))


@app.route('/teacher/student/<int:user_id>/delete', methods=['POST'])
def delete_student(user_id):
    user = require_login()
    if not user or user['role'] != 'teacher':
        return redirect(url_for('login'))

    conn = get_db()
    conn.execute('DELETE FROM submissions WHERE student_id = ?', (user_id,))
    conn.execute('DELETE FROM users WHERE id = ?', (user_id,))
    conn.commit()
    conn.close()
    set_google_sync_feedback(USER_SHEET_NAME, 'Student deleted', sync_students_to_sheet())
    return redirect(url_for('teacher_students'))


def reevaluate_submission_record(submission_id):
    if not remote_ai_enabled():
        return None
    conn = get_db()
    submission = conn.execute(
        '''
        SELECT s.*, u.full_name AS student_name, u.username, u.class_name,
               l.title_vi, l.title_en, l.sample_question, l.speaking_question,
               l.expected_answer, l.sentence_words
        FROM submissions s
        JOIN users u ON u.id = s.student_id
        JOIN lessons l ON l.id = s.lesson_id
        WHERE s.id = ?
        ''',
        (submission_id,)
    ).fetchone()

    if not submission:
        conn.close()
        return None

    payload = parse_submission_payload(submission['answer'])
    if not isinstance(payload, dict):
        payload = {}
    evaluation = payload.get('evaluation') if isinstance(payload.get('evaluation'), dict) else {}

    question_text = submission['sample_question'] or submission['speaking_question'] or 'Answer the question.'
    lesson_expected = submission['expected_answer'] or ''
    sentence_expected = ' '.join(str(submission['sentence_words'] or '').split(',')) if submission['sentence_words'] else ''

    task_1_answer = str(payload.get('task_1_answer') or payload.get('answer_1') or payload.get('answer') or '').strip()
    if not task_1_answer and lesson_expected:
        task_1_answer = lesson_expected

    task_2_answer = str(payload.get('task_2_answer') or payload.get('answer_2') or payload.get('sentence_words') or payload.get('sentence') or '').strip()
    if not task_2_answer and sentence_expected:
        task_2_answer = sentence_expected

    transcript_text = str(payload.get('task_3_transcript') or payload.get('transcript_3') or '').strip()
    audio_path = submission['audio_path'] or payload.get('audio_url') or ''
    if not transcript_text and audio_path:
        transcript_text = transcribe_audio(audio_path) or ''
        if transcript_text:
            payload['task_3_transcript'] = transcript_text

    # Evaluate Task 1
    t1_gemini = evaluate_with_gemini(question_text, task_1_answer, lesson_expected) if task_1_answer else None
    t1_groq = evaluate_with_groq(question_text, task_1_answer, lesson_expected) if task_1_answer else None
    # Evaluate Task 2
    t2_prompt = 'Check if the sentence order is correct and natural in English.'
    t2_gemini = evaluate_with_gemini(t2_prompt, task_2_answer, sentence_expected) if task_2_answer else None
    t2_groq = evaluate_with_groq(t2_prompt, task_2_answer, sentence_expected) if task_2_answer else None
    # Evaluate Task 3
    t3_target = transcript_text or task_1_answer
    t3_gemini = evaluate_with_gemini(question_text, t3_target, lesson_expected) if t3_target else None
    t3_groq = evaluate_with_groq(question_text, t3_target, lesson_expected) if t3_target else None
    for task_name, g_res, q_res in [
        ('task_1', t1_gemini, t1_groq),
        ('task_2', t2_gemini, t2_groq),
        ('task_3', t3_gemini, t3_groq),
    ]:
        evaluation.setdefault(task_name, {})
        if g_res:
            evaluation[task_name]['gemini'] = g_res
        if q_res:
            evaluation[task_name]['groq'] = q_res
    payload['evaluation'] = evaluation
    payload['task_1_answer'] = task_1_answer
    payload['task_2_answer'] = task_2_answer

    ai_scores = []
    for g, q in [(t1_gemini, t1_groq), (t2_gemini, t2_groq), (t3_gemini, t3_groq)]:
        valid = [r['score'] for r in (g, q) if r and r.get('score') is not None]
        if valid:
            ai_scores.append(round(sum(valid) / len(valid)))

    suggested_score = round(sum(ai_scores) / len(ai_scores)) if ai_scores else (submission['score'] or 0)
    current_score = submission['score']
    updated_score = suggested_score if (current_score is None or current_score <= 10) else current_score

    conn.execute(
        'UPDATE submissions SET answer = ?, score = ? WHERE id = ?',
        (json.dumps(payload, ensure_ascii=False), updated_score, submission_id)
    )
    conn.commit()
    conn.close()
    return {
        'suggested_score': suggested_score,
        'gemini_task_1': t1_gemini,
        'groq_task_1': t1_groq,
    }


@app.route('/teacher/submission/<int:submission_id>/delete', methods=['POST'])
def delete_practice_submission(submission_id):
    user = require_login()
    if not user or user['role'] != 'teacher':
        return redirect(url_for('login'))

    conn = get_db()
    existing = conn.execute('SELECT id FROM submissions WHERE id = ?', (submission_id,)).fetchone()
    if existing:
        conn.execute('DELETE FROM submissions WHERE id = ?', (submission_id,))
        conn.commit()
        conn.close()
        synced = sync_practice_to_sheet()
        set_google_sync_feedback('Practice', 'Practice submission deleted', synced)
    else:
        conn.close()
        set_google_sync_feedback('Practice', 'Practice submission was not found; nothing changed', True)

    return redirect(request.referrer or url_for('teacher_practice'))


@app.route('/teacher/submission/<int:submission_id>/ai_grade', methods=['POST', 'GET'])
def ai_grade_submission(submission_id):
    user = require_login()
    if not user or user['role'] != 'teacher':
        return redirect(url_for('login'))

    reevaluate_submission_record(submission_id)
    set_google_sync_feedback('Practice', 'AI grading update', sync_practice_to_sheet())
    target = request.referrer or url_for('review_submission', submission_id=submission_id)
    return redirect(target)


@app.route('/teacher/submissions/ai_grade_all', methods=['POST'])
def ai_grade_all_submissions():
    user = require_login()
    if not user or user['role'] != 'teacher':
        return redirect(url_for('login'))

    conn = get_db()
    ids = [row['id'] for row in conn.execute('SELECT id FROM submissions ORDER BY created_at DESC LIMIT 50').fetchall()]
    conn.close()
    for sid in ids:
        try:
            reevaluate_submission_record(sid)
        except Exception as exc:
            logger.warning('Failed to auto-evaluate submission %s: %s', sid, exc)

    set_google_sync_feedback('Practice', f'{len(ids)} AI grading updates', sync_practice_to_sheet())
    return redirect(request.referrer or url_for('teacher_practice'))


@app.route('/teacher/review/<int:submission_id>', methods=['GET', 'POST'])
def review_submission(submission_id):
    user = require_login()
    if not user or user['role'] != 'teacher':
        return redirect(url_for('login'))

    conn = get_db()
    submission = conn.execute(
        '''
        SELECT s.*, u.full_name AS student_name, u.class_name,
               l.title_vi, l.title_en, l.sample_question AS question,
               l.sentence_words, l.task_3_text, l.task_4_text, l.expected_answer
        FROM submissions s
        JOIN users u ON u.id = s.student_id
        JOIN lessons l ON l.id = s.lesson_id
        WHERE s.id = ?
        ''',
        (submission_id,)
    ).fetchone()

    if not submission:
        conn.close()
        abort(404)

    if request.method == 'POST':
        teacher_note = request.form.get('teacher_note', '')
        payload = parse_submission_payload(submission['answer'])
        evaluation = payload.get('evaluation') if isinstance(payload.get('evaluation'), dict) else {}

        def read_score(value, fallback):
            try:
                numeric = float(value) if value not in (None, '') else float(fallback)
                return numeric
            except (TypeError, ValueError):
                return float(fallback)

        task_names = ['task_1', 'task_2', 'task_3']
        calculated_task_scores = []
        for task_name in task_names:
            evaluation.setdefault(task_name, {})
            task_eval = evaluation[task_name]

            # 4 criteria x 25 points = 100 points maximum per task (allows any decimal like 10.8 or 43.2)
            task_pronunciation = clean_score_num(min(25.0, max(0.0, read_score(request.form.get(f'teacher_{task_name}_pronunciation', request.form.get('teacher_pronunciation', 0)), 0))))
            task_grammar = clean_score_num(min(25.0, max(0.0, read_score(request.form.get(f'teacher_{task_name}_grammar', request.form.get('teacher_grammar', 0)), 0))))
            task_fluency = clean_score_num(min(25.0, max(0.0, read_score(request.form.get(f'teacher_{task_name}_fluency', request.form.get('teacher_fluency', 0)), 0))))
            task_relevance = clean_score_num(min(25.0, max(0.0, read_score(request.form.get(f'teacher_{task_name}_relevance', request.form.get('teacher_relevance', 0)), 0))))

            crit_sum = clean_score_num(task_pronunciation + task_grammar + task_fluency + task_relevance)
            raw_task_score = request.form.get(f'teacher_{task_name}_score')

            # Determine task score: prioritize explicit teacher score input (e.g. 43.2) if provided,
            # or sum of criteria
            if raw_task_score not in (None, '') and float(raw_task_score) > 0 and crit_sum <= 0:
                task_score = min(100.0, clean_score_num(raw_task_score))
            elif raw_task_score not in (None, '') and float(raw_task_score) > 0 and abs(float(raw_task_score) - crit_sum) > 0.05 and crit_sum <= 40:
                # Support explicit task score when criteria are low / default
                task_score = min(100.0, clean_score_num(raw_task_score))
            elif crit_sum > 0:
                task_score = min(100.0, clean_score_num(crit_sum))
            elif raw_task_score not in (None, '') and float(raw_task_score) > 0:
                task_score = min(100.0, clean_score_num(raw_task_score))
            else:
                task_score = min(100.0, clean_score_num(crit_sum))

            calculated_task_scores.append(task_score)

            teacher_payload = {
                'score': task_score,
                'feedback': teacher_note or 'Teacher review recorded.',
                'pronunciation': task_pronunciation,
                'grammar': task_grammar,
                'fluency': task_fluency,
                'relevance': task_relevance,
                'criteria': {
                    'pronunciation': task_pronunciation,
                    'grammar': task_grammar,
                    'fluency': task_fluency,
                    'relevance': task_relevance,
                },
            }
            task_eval['teacher'] = teacher_payload

        # Auto compute final score from average of task scores (max 100) or explicit final_score
        raw_final = request.form.get('final_score')
        if raw_final not in (None, '') and float(raw_final) > 0:
            final_score = min(100.0, clean_score_num(raw_final))
        elif calculated_task_scores:
            final_score = min(100.0, clean_score_num(sum(calculated_task_scores) / len(calculated_task_scores)))
        else:
            final_score = clean_score_num(submission['score'] or 0)

        payload['evaluation'] = evaluation
        conn.execute(
            'UPDATE submissions SET score = ?, teacher_note = ?, answer = ? WHERE id = ?',
            (final_score, teacher_note, json.dumps(payload, ensure_ascii=False), submission_id)
        )
        conn.commit()

        sync_result = sync_practice_to_sheet()
        conn.close()
        set_google_sync_feedback('Practice', 'Practice review', sync_result)
        return redirect(url_for('teacher_practice'))

    answer_payload = parse_submission_payload(submission['answer'])
    if not isinstance(answer_payload, dict):
        answer_payload = {}
    evaluation = answer_payload.get('evaluation') if isinstance(answer_payload.get('evaluation'), dict) else {}
    extracted = extract_submission_scores(submission['answer'], submission['score'])

    if extracted.get('gemini_score') is None and extracted.get('groq_score') is None:
        try:
            reeval = reevaluate_submission_record(submission_id)
            if reeval:
                updated_sub = conn.execute('SELECT * FROM submissions WHERE id = ?', (submission_id,)).fetchone()
                if updated_sub:
                    submission = updated_sub
                    answer_payload = parse_submission_payload(submission['answer']) or {}
                    evaluation = answer_payload.get('evaluation') or {}
                    extracted = extract_submission_scores(submission['answer'], submission['score'])
        except Exception as exc:
            logger.warning('Auto reevaluation failed on review page: %s', exc)

    data = dict(submission)
    data['teacher_note'] = submission['teacher_note'] or ''
    data['answer_payload'] = answer_payload
    data['evaluation'] = evaluation
    data['extracted_scores'] = extracted
    data['gemini_score'] = extracted.get('gemini_score')
    data['groq_score'] = extracted.get('groq_score')
    data['teacher_score'] = extracted.get('teacher_score')
    data['gemini_scores'] = evaluation.get('task_1', {}).get('gemini', {}) if isinstance(evaluation.get('task_1'), dict) else {}
    data['groq_scores'] = evaluation.get('task_1', {}).get('groq', {}) if isinstance(evaluation.get('task_1'), dict) else {}
    data['teacher_scores'] = evaluation.get('task_1', {}).get('teacher', {}) if isinstance(evaluation.get('task_1'), dict) else {}
    conn.close()
    warning = get_session_message('google_sync_warning')
    sync_success = get_session_message('google_sync_success', False)
    session.pop('google_sync_warning', None)
    session.pop('google_sync_success', None)
    return render_template('teacher_review.html', user=user, submission=data, warning=warning, sync_success=sync_success)


@app.route('/lesson/<int:lesson_id>')
def lesson(lesson_id):
    user = require_login()
    if not user:
        return redirect(url_for('login'))

    lesson = next((item for item in get_lessons() if item['id'] == lesson_id), None)
    if not lesson:
        abort(404)

    return render_template('lesson_detail.html', user=user, lesson=lesson)


@app.route('/exercise/<int:lesson_id>')
def exercise(lesson_id):
    user = require_login()
    if not user:
        return redirect(url_for('login'))

    lesson = next((item for item in get_lessons() if item['id'] == lesson_id), None)
    if not lesson:
        abort(404)

    sentence_words = [word.strip() for word in str(lesson['sentence_words']).split(',') if word.strip()]
    random.shuffle(sentence_words)
    lesson = dict(lesson)
    lesson['sentence_words'] = sentence_words
    return render_template('exercise.html', user=user, lesson=lesson)


@app.route('/api/evaluate_speaking', methods=['POST'])
def evaluate_speaking():
    payload = request.get_json(force=True, silent=True) or {}
    question = payload.get('question', '')
    answer = payload.get('answer', '')
    expected = payload.get('expected', '')

    if IS_FROZEN:
        user = require_login()
        if not user:
            return jsonify({'error': 'Please sign in again.'}), 401
        tokens = get_cloud_tokens(user['id'])
        access_token = str((tokens or {}).get('access_token') or '')
        if not verify_cloud_request_user(user, access_token):
            return jsonify({'error': 'Your Vercel session expired. Please sign in again.'}), 401
        ai_result = evaluate_with_vercel_function(question, answer, expected, access_token)
        if not ai_result:
            ai_result = evaluate_with_vercel_function(question, answer, expected, access_token, 'groq')
        if not ai_result:
            return jsonify({'error': 'AI evaluation is temporarily unavailable.'}), 503
        return jsonify(ai_result)

    ai_result = evaluate_with_gemini(question, answer, expected) or evaluate_with_groq(question, answer, expected)
    if ai_result:
        return jsonify(ai_result)

    result = evaluate_answer(question, answer, expected)
    return jsonify(result)


def sync_local_media_to_google_if_needed(media_url, media_type, lesson_title='', student_name=''):
    if not media_url:
        return {'url': '', 'uploaded': False, 'status': 'not_required'}

    if media_url.startswith('http://') or media_url.startswith('https://'):
        return {'url': media_url, 'uploaded': True, 'status': 'already_remote'}

    file_name = media_url.rstrip('/').split('/')[-1]
    folder = AUDIO_DIR if media_type == 'audio' else VIDEO_DIR
    local_path = folder / file_name
    if not local_path.exists():
        return {'url': media_url, 'uploaded': False, 'status': 'local_only'}

    drive_file_name = build_drive_media_name(media_type, lesson_title, student_name, local_path.suffix.lstrip('.'))
    drive_url = upload_to_google_drive(local_path, drive_file_name, media_type, lesson_title, student_name)
    if drive_url:
        return {'url': drive_url, 'uploaded': True, 'status': 'uploaded_to_drive'}
    return {'url': media_url, 'uploaded': False, 'status': 'local_only'}


@app.route('/api/submit_lesson', methods=['POST'])
def submit_lesson():
    user = require_login()
    if not user:
        return jsonify({'saved': False, 'message': 'Please login first.'}), 401

    payload = request.get_json(force=True, silent=True) or request.form.to_dict() or {}
    lesson_id = int(payload.get('lesson_id') or request.form.get('lesson_id') or 0)
    if not lesson_id:
        return jsonify({'saved': False, 'message': 'Lesson ID is missing.'}), 400

    lesson = next((item for item in get_lessons() if int(item['id']) == lesson_id), None)
    if not lesson:
        return jsonify({'saved': False, 'message': 'Lesson not found.'}), 404

    task_1_answer = str(payload.get('answer_1') or payload.get('answer') or '').strip()
    task_2_answer = str(payload.get('answer_2') or payload.get('sentence_words') or payload.get('sentence') or '').strip()
    audio_url = str(payload.get('audio_url') or payload.get('audio_path') or '').strip()
    video_url = str(payload.get('video_url') or '').strip()
    audio_file_id = str(payload.get('audio_file_id') or '').strip()
    cloud_tokens = get_cloud_tokens(user['id'])
    access_token = str((cloud_tokens or {}).get('access_token') or '')
    if IS_FROZEN and not verify_cloud_request_user(user, access_token):
        return jsonify({
            'saved': False,
            'message': 'Your Vercel session expired or could not be verified. Please sign in again.',
        }), 401

    lesson_title = lesson.get('title_en') or lesson.get('title_vi') or 'lesson'
    student_name = (user['full_name'] if 'full_name' in user.keys() and user['full_name'] else user['username']) if hasattr(user, 'keys') else (user.get('full_name') or user.get('username') or 'student')
    audio_sync = sync_local_media_to_google_if_needed(audio_url, 'audio', lesson_title, student_name)
    video_sync = sync_local_media_to_google_if_needed(video_url, 'video', lesson_title, student_name)
    final_audio_url = audio_sync.get('url', audio_url)
    final_video_url = video_sync.get('url', video_url)

    lesson_expected = lesson.get('expected_answer') or ''
    sentence_expected = ' '.join(str(lesson.get('sentence_words') or '').split(','))
    question_text = lesson.get('sample_question') or lesson.get('speaking_question') or 'Answer the question.'

    task_1_gemini = evaluate_with_vercel_function(
        question_text,
        task_1_answer,
        lesson_expected,
        access_token,
    )
    if IS_FROZEN:
        task_1_groq = (
            evaluate_with_vercel_function(
                question_text,
                task_1_answer,
                lesson_expected,
                access_token,
                'groq',
            )
            if not task_1_gemini else None
        )
    else:
        if not task_1_gemini:
            task_1_gemini = evaluate_with_gemini(question_text, task_1_answer, lesson_expected)
        task_1_groq = evaluate_with_groq(question_text, task_1_answer, lesson_expected)
    task_1_result = task_1_gemini or task_1_groq or evaluate_answer(question_text, task_1_answer, lesson_expected)

    task_2_prompt = 'Check if the sentence order is correct and natural in English.'
    task_2_ans = task_2_answer or ' '.join(str(lesson.get('sentence_words') or '').split(','))
    task_2_gemini = evaluate_with_vercel_function(
        task_2_prompt,
        task_2_ans,
        sentence_expected,
        access_token,
    )
    if IS_FROZEN:
        task_2_groq = (
            evaluate_with_vercel_function(
                task_2_prompt,
                task_2_ans,
                sentence_expected,
                access_token,
                'groq',
            )
            if not task_2_gemini else None
        )
    else:
        if not task_2_gemini:
            task_2_gemini = evaluate_with_gemini(task_2_prompt, task_2_ans, sentence_expected)
        task_2_groq = evaluate_with_groq(task_2_prompt, task_2_ans, sentence_expected)
    task_2_result = task_2_gemini or task_2_groq or evaluate_sentence_reorder(task_2_ans, sentence_expected)

    transcript_text = ''
    task_3_gemini = None
    task_3_groq = None
    task_3_result = {'score': 0, 'feedback': 'No transcript available.', 'pronunciation': 0, 'grammar': 0, 'fluency': 0}
    if IS_FROZEN:
        audio_path = get_pending_audio_path(user['id'], lesson_id)
        if audio_path:
            transcript_text = transcribe_with_vercel_function(
                audio_path,
                access_token,
                audio_file_id,
            ) or ''
    elif final_audio_url:
        transcript_text = transcribe_audio(final_audio_url) or ''
        if transcript_text:
            task_3_gemini = evaluate_with_gemini(question_text, transcript_text, lesson_expected)
            task_3_groq = evaluate_with_groq(question_text, transcript_text, lesson_expected)
            task_3_result = task_3_gemini or task_3_groq or evaluate_answer(question_text, transcript_text, lesson_expected)
    if IS_FROZEN and transcript_text:
        task_3_gemini = evaluate_with_vercel_function(
            question_text,
            transcript_text,
            lesson_expected,
            access_token,
        )
        if not task_3_gemini:
            task_3_groq = evaluate_with_vercel_function(
                question_text,
                transcript_text,
                lesson_expected,
                access_token,
                'groq',
            )
        task_3_result = task_3_gemini or task_3_groq or evaluate_answer(question_text, transcript_text, lesson_expected)
    task_3_score = int(task_3_result.get('score', 0))
    ai_services_available = (
        not IS_FROZEN
        or (
            (not task_1_answer or bool(task_1_gemini or task_1_groq))
            and (not task_2_ans or bool(task_2_gemini or task_2_groq))
            and (not final_audio_url or bool(transcript_text))
        )
    )

    task_4_score = 0
    overall_score = round((task_1_result.get('score', 0) + task_2_result.get('score', 0) + task_3_score + task_4_score) / 4)

    feedback_text = (
        f"Task 1: {task_1_result.get('feedback', '')}; "
        f"Task 2: {task_2_result.get('feedback', '')}; "
        f"Task 3: {'Transcript reviewed' if transcript_text else 'No audio transcript available'}; "
        f"Task 4: Pending teacher review."
    )

    def build_ai_preview(result):
        # AI rubric is only a suggestion - the official task score belongs to the teacher.
        # Each task is worth 100 points, split into 5 criteria x 20 points.
        result = result or {}
        criteria = {
            'pronunciation': float(result.get('pronunciation', 0) or 0),
            'grammar': float(result.get('grammar', 0) or 0),
            'fluency': float(result.get('fluency', 0) or 0),
            'relevance': float(result.get('relevance', 0) or 0),
            'confidence': float(result.get('confidence', 0) or 0),
        }
        return {
            'score': int(result.get('score', 0) or 0),
            'feedback': result.get('feedback', ''),
            'criteria': criteria,
            **criteria,
        }

    evaluation_payload = {
        'task_1': {
            'gemini': task_1_gemini or {},
            'groq': task_1_groq or {},
            'ai_preview': build_ai_preview(task_1_result),
        },
        'task_2': {
            'gemini': task_2_gemini or {},
            'groq': task_2_groq or {},
            'ai_preview': build_ai_preview(task_2_result),
        },
        'task_3': {
            'gemini': task_3_gemini or {},
            'groq': task_3_groq or {},
            'ai_preview': build_ai_preview(task_3_result),
        },
    }

    user_dict = dict(user)
    conn = get_db()
    try:
        existing = conn.execute(
            'SELECT id FROM submissions WHERE student_id = ? AND lesson_id = ? ORDER BY created_at DESC LIMIT 1',
            (user['id'], lesson_id)
        ).fetchone()

        payload_data = {
            'task_1_answer': task_1_answer,
            'task_2_answer': task_2_answer,
            'task_3_transcript': transcript_text,
            'audio_url': final_audio_url,
            'video_url': final_video_url,
            'task_1_question': question_text,
            'evaluation': evaluation_payload,
            'awaiting_teacher_review': True,
        }

        row_values = (
            json.dumps(payload_data, ensure_ascii=False),
            overall_score,
            feedback_text,
            final_audio_url or audio_url,
            final_video_url if final_video_url.startswith('http') else '',
            '',
            user['id'],
            lesson_id,
        )

        if existing:
            conn.execute(
                'UPDATE submissions SET answer = ?, score = ?, feedback = ?, audio_path = ?, drive_url = ?, teacher_note = ?, created_at = CURRENT_TIMESTAMP WHERE id = ?',
                (row_values[0], row_values[1], row_values[2], row_values[3], row_values[4], row_values[5], existing['id'])
            )
            submission_id = existing['id']
        else:
            conn.execute(
                'INSERT INTO submissions (student_id, lesson_id, answer, score, feedback, audio_path, drive_url, teacher_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (user['id'], lesson_id, row_values[0], row_values[1], row_values[2], row_values[3], row_values[4], row_values[5])
            )
            submission_id = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']

        conn.commit()
    finally:
        conn.close()

    practice_saved = sync_practice_to_sheet()

    has_audio = bool(audio_url or final_audio_url)
    has_video = bool(video_url or final_video_url)
    google_drive_saved = (
        (not has_audio or audio_sync.get('uploaded'))
        and (not has_video or video_sync.get('uploaded'))
    ) if (has_audio or has_video) else True
    local_saved = True
    message = 'Submission saved successfully.'
    if (has_audio or has_video) and not google_drive_saved and not practice_saved:
        message = 'Submission saved locally, but Google Drive and Google Sheets could not be synced. Check the folder and sheet sharing configuration.'
    elif (has_audio or has_video) and not google_drive_saved:
        message = 'Submission saved locally and synced to Google Sheets, but Google Drive could not be updated.'
    elif not practice_saved:
        message = 'Submission saved locally and uploaded to Google Drive' if google_drive_saved else 'Submission saved locally, but Google Sheets could not be updated.'
    else:
        message = 'Submission saved and synced to Google Drive and Google Sheets.' if google_drive_saved else 'Submission saved and synced to Google Sheets.'
    if not ai_services_available:
        message = f'{message} AI evaluation or transcription was unavailable; local fallback scoring was used.'

    return jsonify({
        'saved': local_saved,
        'score': overall_score,
        'feedback': feedback_text,
        'task_1': {'gemini': task_1_gemini, 'groq': task_1_groq, 'teacher': task_1_result},
        'task_2': {'gemini': task_2_gemini, 'groq': task_2_groq, 'teacher': task_2_result},
        'task_3': {'gemini': task_3_gemini, 'groq': task_3_groq, 'teacher': task_3_result},
        'task_4': {'score': task_4_score, 'status': 'pending_teacher_review'},
        'score_breakdown': {
            'task_1': {'gemini': task_1_gemini, 'groq': task_1_groq, 'teacher': task_1_result},
            'task_2': {'gemini': task_2_gemini, 'groq': task_2_groq, 'teacher': task_2_result},
            'task_3': {'gemini': task_3_gemini, 'groq': task_3_groq, 'teacher': task_3_result},
            'overall': {'score': overall_score},
        },
        'evaluation': evaluation_payload,
        'local_saved': local_saved,
        'google_drive_saved': google_drive_saved,
        'google_sheet_saved': practice_saved,
        'practice_sheet_saved': practice_saved,
        'ai_services_available': ai_services_available,
        'message': message,
    })


def find_or_create_drive_folder(drive_service, folder_name='Practice', account_key=''):
    folder_id = os.getenv('GOOGLE_DRIVE_FOLDER_ID', '').strip()
    if folder_id:
        # Accept a raw folder ID or a complete Google Drive folder URL.
        if '/folders/' in folder_id:
            folder_id = folder_id.split('/folders/', 1)[1].split('/', 1)[0].split('?', 1)[0]
        return folder_id

    folder_name_env = os.getenv('GOOGLE_DRIVE_FOLDER_NAME', '').strip()
    target_name = folder_name_env or folder_name
    cache_key = (account_key, target_name)
    if account_key:
        cached_folder = _drive_folder_cache.get(cache_key)
        if cached_folder:
            cached_id, expires_at = cached_folder
            if expires_at > time.monotonic():
                return cached_id
            _drive_folder_cache.pop(cache_key, None)

    try:
        # Search for existing folder with folder_name shared with service account
        query = f"name = '{target_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        results = drive_service.files().list(
            q=query,
            fields='files(id, name)',
            spaces='drive',
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        files = results.get('files', [])
        if files:
            found_id = files[0]['id']
            if account_key:
                _drive_folder_cache[cache_key] = (found_id, time.monotonic() + DRIVE_FOLDER_CACHE_TTL)
            logger.info('Found existing Google Drive folder "%s" with ID: %s', target_name, found_id)
            return found_id
    except Exception as exc:
        logger.warning('Could not search Google Drive folder %s: %s', target_name, exc)
    return None


def upload_to_google_drive(file_path, file_name, media_type='audio', lesson_title='', student_name=''):
    if IS_FROZEN:
        path = Path(str(file_path))
        try:
            if not path.is_file() or path.stat().st_size > 18 * 1024 * 1024:
                logger.warning('Google Drive upload skipped: file is missing or larger than 18 MB.')
                return None
            tokens = get_cloud_tokens()
            access_token = tokens.get('access_token') if tokens else None
            result = upload_media_to_vercel_drive(path, file_name, media_type, access_token)
            return str((result or {}).get('drive_url') or '') or None
        except OSError as exc:
            logger.warning('Could not read media for Google Drive upload: %s', exc)
            return None

    if google_drive_build is None or MediaFileUpload is None:
        logger.warning('Google Drive upload skipped: googleapiclient is not installed.')
        return None

    credentials = get_google_credentials()
    if not credentials:
        logger.warning('Google Drive upload skipped: missing Google service account credentials.')
        return None

    if not file_name or not str(file_name).strip():
        file_name = build_drive_media_name(media_type, lesson_title, student_name, Path(str(file_path)).suffix.lstrip('.'))

    drive_service = google_drive_build('drive', 'v3', credentials=credentials)
    source_suffix = Path(str(file_path)).suffix.lower()
    if source_suffix == '.webm':
        mime_type = 'audio/webm' if str(media_type).lower() == 'audio' else 'video/webm'
    elif source_suffix == '.mp4':
        mime_type = 'video/mp4'
    elif source_suffix == '.ogg':
        mime_type = 'audio/ogg'
    elif source_suffix in ('.m4a', '.mp4a'):
        mime_type = 'audio/mp4'
    elif source_suffix == '.wav':
        mime_type = 'audio/wav'
    else:
        mime_type = 'audio/mpeg' if str(media_type).lower() == 'audio' else 'video/mp4'
    file_metadata = {'name': file_name}
    account_key = getattr(credentials, 'service_account_email', '')
    target_folder_name = os.getenv('GOOGLE_DRIVE_FOLDER_NAME', '').strip() or 'Practice'
    target_folder_id = find_or_create_drive_folder(drive_service, target_folder_name, account_key)
    if target_folder_id:
        file_metadata['parents'] = [target_folder_id]

    try:
        response = drive_service.files().create(
            body=file_metadata,
            media_body=MediaFileUpload(str(file_path), mimetype=mime_type, resumable=False),
            fields='id,webViewLink',
            supportsAllDrives=True
        ).execute()

        # Set permission to anyone with link can view so teacher can listen/watch directly
        file_id = response.get('id')
        try:
            drive_service.permissions().create(
                fileId=file_id,
                body={'type': 'anyone', 'role': 'reader'},
                fields='id',
                supportsAllDrives=True
            ).execute()
        except Exception:
            pass

        return response.get('webViewLink') or f"https://drive.google.com/file/d/{file_id}/view"
    except Exception as exc:
        if account_key:
            _drive_folder_cache.pop((account_key, target_folder_name), None)
        logger.error('Google Drive upload failed for folder=%r, account=%r, file=%r: %s', target_folder_id, account_key, file_name, exc)
        return None


def save_media_submission(student_id, lesson_id, local_url, drive_url, media_type='audio'):
    if not student_id:
        return None

    local_file_name = Path(local_url).name if local_url else ''
    conn = get_db()
    try:
        if lesson_id:
            existing = conn.execute(
                'SELECT id FROM submissions WHERE student_id = ? AND lesson_id = ? ORDER BY created_at DESC LIMIT 1',
                (student_id, int(lesson_id))
            ).fetchone()
            if existing:
                if media_type == 'audio':
                    conn.execute(
                        'UPDATE submissions SET audio_path = ?, drive_url = ? WHERE id = ?',
                        (local_file_name, drive_url, existing['id'])
                    )
                else:
                    conn.execute(
                        'UPDATE submissions SET drive_url = ? WHERE id = ?',
                        (drive_url, existing['id'])
                    )
                conn.commit()
                return existing['id']

        conn.execute(
            'INSERT INTO submissions (student_id, lesson_id, answer, score, feedback, audio_path, drive_url, teacher_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (
                student_id,
                int(lesson_id) if lesson_id else 0,
                '',
                0,
                '',
                local_file_name if media_type == 'audio' else '',
                drive_url,
                '',
            )
        )
        conn.commit()
        return conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
    finally:
        conn.close()


def get_pending_audio_path(student_id, lesson_id):
    conn = get_db()
    try:
        row = conn.execute(
            '''
            SELECT audio_path FROM submissions
            WHERE student_id = ? AND lesson_id = ?
            ORDER BY created_at DESC LIMIT 1
            ''',
            (student_id, lesson_id),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row['audio_path']:
        return None
    path = AUDIO_DIR / Path(str(row['audio_path'])).name
    return path if path.is_file() else None


@app.route('/api/upload_media', methods=['POST'])
def upload_media():
    user = require_login() if IS_FROZEN else None
    if IS_FROZEN and not user:
        return jsonify({'status': 'error', 'message': 'Please sign in before uploading media.'}), 401
    tokens = get_cloud_tokens(user['id']) if user else None
    access_token = str((tokens or {}).get('access_token') or '')
    if IS_FROZEN and not verify_cloud_request_user(user, access_token):
        return jsonify({'status': 'error', 'message': 'Your Vercel session expired. Please sign in again.'}), 401
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file provided.'}), 400

    uploaded = request.files['file']
    media_type = request.form.get('media_type', 'audio')
    if media_type not in {'audio', 'video'}:
        return jsonify({'status': 'error', 'message': 'Unsupported media type.'}), 400
    if uploaded.filename == '':
        return jsonify({'status': 'error', 'message': 'Empty file name.'}), 400

    safe_name = secure_filename(uploaded.filename)
    target_dir = AUDIO_DIR if media_type == 'audio' else VIDEO_DIR
    unique_name = f"{uuid.uuid4()}_{safe_name}"
    saved_path = target_dir / unique_name
    uploaded.save(saved_path)

    lesson_id = request.form.get('lesson_id') or request.form.get('lesson')
    lesson_title = request.form.get('lesson_title') or request.form.get('lesson_name') or 'lesson'
    if lesson_id:
        try:
            lesson = next((item for item in get_lessons() if int(item.get('id')) == int(lesson_id)), None)
            if lesson:
                lesson_title = lesson.get('title_en') or lesson.get('title_vi') or lesson_title
        except (TypeError, ValueError):
            pass
    student_name = (
        user['full_name'] or user['username']
        if user else request.form.get('student_name') or session.get('user_name') or 'student'
    )
    drive_file_name = build_drive_media_name(media_type, lesson_title, student_name, saved_path.suffix.lstrip('.'))
    local_url = f'/uploads/{media_type}/{unique_name}'
    drive_file_id = ''
    if IS_FROZEN:
        drive_result = upload_media_to_vercel_drive(
            saved_path,
            drive_file_name,
            media_type,
            access_token,
        )
        drive_url = str((drive_result or {}).get('drive_url') or '')
        drive_file_id = str((drive_result or {}).get('file_id') or '')
    else:
        drive_url = upload_to_google_drive(saved_path, drive_file_name, media_type, lesson_title, student_name)
    if drive_url:
        message = 'The file was saved locally and uploaded to Google Drive successfully.'
    else:
        message = 'Google Drive upload failed. The file was saved locally.'

    student_id = user['id'] if user else request.form.get('student_id') or session.get('user_id')
    if student_id:
        save_media_submission(student_id, lesson_id, local_url, drive_url or '', media_type)

    return jsonify({
        'status': 'ok',
        'file_name': unique_name,
        'drive_file_name': drive_file_name,
        'media_type': media_type,
        'local_url': local_url,
        'drive_url': drive_url or '',
        'drive_file_id': drive_file_id,
        'drive_saved': bool(drive_url),
        'message': message,
    })


@app.route('/uploads/<media_type>/<filename>')
def serve_upload(media_type, filename):
    folder = AUDIO_DIR if media_type == 'audio' else VIDEO_DIR
    return send_from_directory(folder, filename)


init_db()

if __name__ == '__main__':
    app.run(
        debug=not IS_FROZEN,
        host='127.0.0.1' if IS_FROZEN else '0.0.0.0',
        port=int(os.getenv('ENG_APP_PORT', '5000')),
        use_reloader=not IS_FROZEN,
    )

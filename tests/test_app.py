import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app as app_module


class AppFeatureTests(unittest.TestCase):
    def setUp(self):
        app_module.DATA_FILE = str(Path(tempfile.mkdtemp()) / 'eng_app_test.db')
        app_module.init_db()

    def test_google_credentials_can_resolve_file_from_application_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir) / 'application'
            app_dir.mkdir()
            credentials_file = app_dir / 'service-account.json'
            credentials_file.write_text('{}', encoding='utf-8')
            missing_original_path = Path(temp_dir) / 'original' / credentials_file.name

            with patch.object(app_module, 'BASE_DIR', app_dir):
                with patch.dict(os.environ, {
                    'GOOGLE_SERVICE_ACCOUNT_PATH': str(missing_original_path),
                    'GOOGLE_SERVICE_ACCOUNT_JSON': '',
                }):
                    with patch.object(
                        app_module.service_account.Credentials,
                        'from_service_account_file',
                        return_value='credentials',
                    ) as load_credentials:
                        result = app_module.get_google_credentials()

            self.assertEqual(result, 'credentials')
            self.assertEqual(load_credentials.call_args.args[0], credentials_file)

    def test_load_env_file_from_application_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir) / 'application'
            app_dir.mkdir()
            (app_dir / '.env').write_text('APPLICATION_ENV_TEST_KEY=loaded\n', encoding='utf-8')

            with patch.object(app_module, 'BASE_DIR', app_dir):
                with patch.dict(os.environ, {}, clear=False):
                    app_module.load_env_file()
                    self.assertEqual(os.environ['APPLICATION_ENV_TEST_KEY'], 'loaded')

    def test_evaluate_answer_returns_high_score_for_correct_answer(self):
        result = app_module.evaluate_answer('What is your name?', 'My name is Lan.', 'My name is Lan.')
        self.assertEqual(result['score'], 100)
        self.assertGreaterEqual(result['pronunciation'], 0)

    def test_registration_rejects_teacher_role_and_creates_only_students(self):
        original_sync = app_module.sync_students_to_sheet
        app_module.sync_students_to_sheet = lambda *args, **kwargs: True
        try:
            with app_module.app.test_client() as client:
                rejected = client.post(
                    '/register',
                    data={
                        'full_name': 'Unauthorized Teacher',
                        'username': 'unauthorizedteacher',
                        'email': 'unauthorizedteacher@example.com',
                        'password': 'secret',
                        'role': 'teacher',
                    },
                )
                self.assertEqual(rejected.status_code, 403)

                registered = client.post(
                    '/register',
                    data={
                        'full_name': 'New Student',
                        'username': 'newstudent',
                        'email': 'newstudent@example.com',
                        'password': 'secret',
                        'class_name': '7A',
                    },
                )
                self.assertEqual(registered.status_code, 302)
                login_page = client.get('/login').get_data(as_text=True)
                self.assertIn('Your student account was created successfully.', login_page)
                self.assertIn('You can now sign in.', login_page)
        finally:
            app_module.sync_students_to_sheet = original_sync

        conn = app_module.get_db()
        self.assertIsNone(conn.execute(
            'SELECT id FROM users WHERE username = ?',
            ('unauthorizedteacher',)
        ).fetchone())
        student = conn.execute(
            'SELECT role FROM users WHERE username = ?',
            ('newstudent',)
        ).fetchone()
        conn.close()
        self.assertEqual(student['role'], 'student')

    def test_two_configured_teacher_accounts_exist_and_can_log_in(self):
        conn = app_module.get_db()
        teacher_rows = conn.execute(
            "SELECT username, password, role FROM users WHERE username IN ('huynt', 'mingo') ORDER BY username"
        ).fetchall()
        conn.close()

        self.assertEqual(
            [(row['username'], row['password'], row['role']) for row in teacher_rows],
            [('huynt', '123456', 'teacher'), ('mingo', '123456', 'teacher')],
        )
        with app_module.app.test_client() as client:
            for username in ('huynt', 'mingo'):
                response = client.post(
                    '/login',
                    data={'username': username, 'password': '123456', 'role': 'teacher'},
                )
                self.assertEqual(response.status_code, 302)

    def test_google_student_sync_cannot_create_or_demote_teacher_accounts(self):
        sheet_rows = [
            {
                'full_name': 'Sheet teacher',
                'username': 'sheetteacher',
                'email': 'sheetteacher@example.com',
                'role': 'teacher',
            },
            {
                'full_name': 'Huy Nguyen',
                'username': 'huynt',
                'email': 'huynt@school.com',
                'role': 'student',
            },
        ]
        original_read_rows = app_module._read_google_worksheet_rows
        app_module._read_google_worksheet_rows = lambda title: sheet_rows
        try:
            synced = app_module.sync_google_students_into_db()
        finally:
            app_module._read_google_worksheet_rows = original_read_rows

        self.assertEqual([row['role'] for row in synced], ['student'])
        conn = app_module.get_db()
        huynt = conn.execute(
            'SELECT role FROM users WHERE username = ?',
            ('huynt',)
        ).fetchone()
        sheet_teacher = conn.execute(
            'SELECT role FROM users WHERE username = ?',
            ('sheetteacher',)
        ).fetchone()
        conn.close()
        self.assertEqual(huynt['role'], 'teacher')
        self.assertEqual(sheet_teacher['role'], 'student')

    def test_new_google_sheet_students_use_default_password(self):
        sheet_rows = [{
            'full_name': 'New Sheet Student',
            'username': 'newsheetstudent',
            'email': 'newsheetstudent@example.test',
            'class_name': '8A',
            'password': '',
        }]
        original_read_rows = app_module._read_google_worksheet_rows
        app_module._read_google_worksheet_rows = lambda title: sheet_rows
        try:
            synced = app_module.sync_google_students_into_db()
        finally:
            app_module._read_google_worksheet_rows = original_read_rows

        conn = app_module.get_db()
        student = conn.execute(
            'SELECT password FROM users WHERE username = ?',
            ('newsheetstudent',)
        ).fetchone()
        conn.close()
        self.assertEqual(len(synced), 1)
        self.assertEqual(student['password'], '123456')

    def test_legacy_default_teacher_is_migrated_to_huynt_account(self):
        conn = app_module.get_db()
        conn.execute(
            "UPDATE users SET username = 'mrhuy', email = 'teacher@school.com' WHERE username = 'huynt'"
        )
        conn.commit()
        conn.close()

        app_module.init_db()

        conn = app_module.get_db()
        migrated = conn.execute(
            'SELECT username, password, role FROM users WHERE email = ?',
            ('teacher@school.com',)
        ).fetchone()
        conn.close()
        self.assertEqual(
            (migrated['username'], migrated['password'], migrated['role']),
            ('huynt', '123456', 'teacher'),
        )

    def test_teacher_student_creation_cannot_create_teacher_role(self):
        original_sync = app_module.sync_students_to_sheet
        app_module.sync_students_to_sheet = lambda *args, **kwargs: True
        try:
            with app_module.app.test_client() as client:
                with client.session_transaction() as sess:
                    sess['user_id'] = 2
                    sess['user_role'] = 'teacher'

                response = client.post(
                    '/teacher/student/new',
                    data={
                        'full_name': 'Created as student',
                        'username': 'createdasstudent',
                        'email': 'createdasstudent@example.com',
                        'password': 'secret',
                        'role': 'teacher',
                    },
                )
                self.assertEqual(response.status_code, 302)
        finally:
            app_module.sync_students_to_sheet = original_sync

        conn = app_module.get_db()
        created = conn.execute(
            'SELECT role FROM users WHERE username = ?',
            ('createdasstudent',)
        ).fetchone()
        conn.close()
        self.assertEqual(created['role'], 'student')

    def test_submit_lesson_saves_eval_and_google_practice_record(self):
        conn = app_module.get_db()
        student = conn.execute("SELECT id FROM users WHERE role = 'student' LIMIT 1").fetchone()
        lesson = conn.execute('SELECT id FROM lessons ORDER BY id LIMIT 1').fetchone()
        conn.close()

        original_sync = app_module.sync_practice_to_sheet
        app_module.sync_practice_to_sheet = lambda *args, **kwargs: True
        try:
            with app_module.app.test_client() as client:
                with client.session_transaction() as sess:
                    sess['user_id'] = student['id']
                    sess['user_role'] = 'student'

                response = client.post(
                    '/api/submit_lesson',
                    json={
                        'lesson_id': lesson['id'],
                        'answer': 'My name is Lan.',
                        'sentence_words': ['My', 'name', 'is', 'Lan', '.'],
                        'audio_url': '',
                        'video_url': '',
                    },
                )
                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertTrue(payload['saved'])
                self.assertIn('score', payload)

                conn = app_module.get_db()
                saved = conn.execute(
                    'SELECT id, score, feedback FROM submissions WHERE student_id = ? AND lesson_id = ?',
                    (student['id'], lesson['id'])
                ).fetchone()
                conn.close()
                self.assertIsNotNone(saved)
                self.assertGreaterEqual(saved['score'], 0)
        finally:
            app_module.sync_practice_to_sheet = original_sync

    def test_submit_lesson_reports_local_only_when_google_sync_unavailable(self):
        conn = app_module.get_db()
        student = conn.execute("SELECT id FROM users WHERE role = 'student' LIMIT 1").fetchone()
        lesson = conn.execute('SELECT id FROM lessons ORDER BY id LIMIT 1').fetchone()
        conn.close()

        original_sync = app_module.sync_practice_to_sheet
        original_drive = app_module.upload_to_google_drive
        app_module.sync_practice_to_sheet = lambda *args, **kwargs: False
        app_module.upload_to_google_drive = lambda *args, **kwargs: None
        try:
            with app_module.app.test_client() as client:
                with client.session_transaction() as sess:
                    sess['user_id'] = student['id']
                    sess['user_role'] = 'student'

                response = client.post(
                    '/api/submit_lesson',
                    json={
                        'lesson_id': lesson['id'],
                        'answer': 'My name is Lan.',
                        'sentence_words': ['My', 'name', 'is', 'Lan', '.'],
                        'audio_url': '/uploads/audio/nonexistent.mp3',
                        'video_url': '/uploads/video/nonexistent.mp4',
                    },
                )
                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertTrue(payload['saved'])
                self.assertTrue(payload['local_saved'])
                self.assertFalse(payload['google_drive_saved'])
                self.assertFalse(payload['google_sheet_saved'])
                self.assertIn('local', payload['message'].lower())
        finally:
            app_module.sync_practice_to_sheet = original_sync
            app_module.upload_to_google_drive = original_drive

    def test_google_sync_sets_success_message(self):
        class DummyWorksheet:
            def get_all_values(self):
                return [['full_name', 'username', 'email', 'role', 'class_name', 'status', 'total_points', 'average_score']]

            def clear(self):
                return None

            def update(self, range_name, values, **kwargs):
                return values

            def append_row(self, row):
                return row

            def insert_row(self, row, index):
                return row

        class DummySheet:
            def get_worksheet(self, index):
                return DummyWorksheet()

            def add_worksheet(self, **kwargs):
                return DummyWorksheet()

        original_authorize = app_module.gspread.authorize
        original_credentials = app_module.get_google_credentials
        app_module.gspread.authorize = lambda credentials: type('Client', (), {'open': lambda self, name: DummySheet(), 'open_by_key': lambda self, key: DummySheet()})()
        app_module.get_google_credentials = lambda: object()
        try:
            with app_module.app.test_request_context('/') as request_ctx:
                request_ctx.session['user_id'] = 1
                request_ctx.session['user_role'] = 'teacher'
                result = app_module.sync_students_to_sheet([{'full_name': 'Test User', 'username': 'testuser', 'email': 'testuser@example.com', 'role': 'student', 'class_name': '7A', 'is_active': True, 'total_points': 0, 'average_score': 0}])
                self.assertTrue(result)
                self.assertEqual(request_ctx.session.get('google_sync_warning'), 'Google synced')
        finally:
            app_module.gspread.authorize = original_authorize
            app_module.get_google_credentials = original_credentials

    def test_sync_uses_student_and_lesson_sheet_names(self):
        sheet_titles = []

        class DummyWorksheet:
            def __init__(self):
                self.rows = []

            def clear(self):
                self.rows = []

            def get_all_values(self):
                return self.rows

            def append_row(self, row):
                self.rows.append(row)
                return row

            def update(self, range_name, values, **kwargs):
                self.rows = values
                return values

            def insert_row(self, row, index):
                self.rows.insert(index - 1, row)
                return row

        class DummySheet:
            def __init__(self):
                self._sheets = {}

            def worksheet(self, title):
                sheet_titles.append(title)
                if title not in self._sheets:
                    self._sheets[title] = DummyWorksheet()
                return self._sheets[title]

            def add_worksheet(self, title, **kwargs):
                sheet_titles.append(title)
                self._sheets[title] = DummyWorksheet()
                return self._sheets[title]

        original_authorize = app_module.gspread.authorize
        original_credentials = app_module.get_google_credentials
        app_module.gspread.authorize = lambda credentials: type('Client', (), {'open': lambda self, name: DummySheet(), 'open_by_key': lambda self, key: DummySheet()})()
        app_module.get_google_credentials = lambda: object()
        try:
            with app_module.app.test_request_context('/'):
                self.assertTrue(app_module.sync_students_to_sheet([{'full_name': 'Test User', 'username': 'testuser', 'email': 'testuser@example.com', 'role': 'student', 'class_name': '7A', 'is_active': True, 'total_points': 0, 'average_score': 0}]))
                self.assertTrue(app_module.sync_lessons_to_sheet([{'id': 1, 'title_en': 'Lesson 1', 'title_vi': 'BÃ i 1', 'class_level': '7', 'week_number': 1, 'open_date': '2026-10-20', 'close_date': '2026-10-27', 'video_url': 'https://example.com', 'sample_question': 'What?', 'speaking_question': 'What?', 'expected_answer': 'A', 'sentence_words': 'A,B', 'exercise_text_vi': 'Vi', 'exercise_text_en': 'En'}]))
                self.assertIn('Student', sheet_titles)
                self.assertIn('Lesson', sheet_titles)
        finally:
            app_module.gspread.authorize = original_authorize
            app_module.get_google_credentials = original_credentials

    def test_google_sheet_sync_batches_200_rows_per_worksheet(self):
        class DummyWorksheet:
            def __init__(self):
                self.rows = []
                self.update_calls = 0

            def clear(self):
                self.rows = []

            def update(self, range_name, values, **kwargs):
                self.update_calls += 1
                self.rows = values
                return values

        class DummySheet:
            def __init__(self):
                self.worksheets = {}

            def worksheet(self, title):
                if title not in self.worksheets:
                    raise KeyError(title)
                return self.worksheets[title]

            def add_worksheet(self, title, **kwargs):
                self.worksheets[title] = DummyWorksheet()
                return self.worksheets[title]

        class DummyClient:
            def __init__(self):
                self.sheet = DummySheet()

            def open(self, name):
                return self.sheet

            def open_by_key(self, key):
                return self.sheet

        client = DummyClient()
        original_authorize = app_module.gspread.authorize
        original_credentials = app_module.get_google_credentials
        app_module.gspread.authorize = lambda credentials: client
        app_module.get_google_credentials = lambda: object()
        try:
            students = [
                {'full_name': f'Student {index}', 'username': f'student{index}', 'email': f'student{index}@example.com'}
                for index in range(200)
            ]
            lessons = [
                {'id': index + 1, 'title_en': f'Lesson {index}', 'title_vi': f'BÃ i {index}'}
                for index in range(200)
            ]
            practice = [
                {'student_id': index + 1, 'lesson_id': index + 1, 'score': 80}
                for index in range(200)
            ]
            with app_module.app.test_request_context('/'):
                self.assertTrue(app_module.sync_students_to_sheet(students))
                self.assertTrue(app_module.sync_lessons_to_sheet(lessons))
                self.assertTrue(app_module.sync_practice_to_sheet(practice))

            self.assertEqual(set(client.sheet.worksheets), {'Student', 'Lesson', 'Practice'})
            for worksheet in client.sheet.worksheets.values():
                self.assertEqual(worksheet.update_calls, 1)
                self.assertEqual(len(worksheet.rows), 201)
        finally:
            app_module.gspread.authorize = original_authorize
            app_module.get_google_credentials = original_credentials

    def test_full_practice_sheet_sync_exports_all_submission_fields(self):
        conn = app_module.get_db()
        student = conn.execute("SELECT id, email FROM users WHERE role = 'student' LIMIT 1").fetchone()
        lesson = conn.execute('SELECT id FROM lessons ORDER BY id LIMIT 1').fetchone()
        conn.execute(
            'INSERT INTO submissions (student_id, lesson_id, answer, score, feedback, audio_path, drive_url) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (
                student['id'],
                lesson['id'],
                json.dumps({
                    'task_1_answer': 'My answer',
                    'task_2_answer': 'A sentence',
                    'task_3_transcript': 'My transcript',
                    'evaluation': {
                        'task_1': {'teacher': {'score': 91}},
                        'task_2': {'teacher': {'score': 88}},
                        'task_3': {'teacher': {'score': 85}},
                    },
                }),
                88,
                'Teacher feedback',
                'audio.mp3',
                'https://example.test/video',
            )
        )
        conn.commit()
        conn.close()

        class DummyWorksheet:
            def clear(self):
                self.rows = []

            def update(self, range_name, values, **kwargs):
                self.rows = values

        worksheet = DummyWorksheet()

        class DummySheet:
            def worksheet(self, title):
                self.title = title
                return worksheet

            def add_worksheet(self, title, **kwargs):
                self.title = title
                return worksheet

        class DummyClient:
            def open(self, name):
                return DummySheet()

            def open_by_key(self, key):
                return DummySheet()

        with patch.object(app_module.gspread, 'authorize', return_value=DummyClient()):
            with patch.object(app_module, 'get_google_credentials', return_value=object()):
                self.assertTrue(app_module.sync_practice_to_sheet())

        self.assertEqual(worksheet.rows[0][0], 'submitted_at')
        self.assertEqual(worksheet.rows[1][2], student['email'])
        self.assertEqual(worksheet.rows[1][8:14], [91, 88, 85, 0, 88, 'Teacher feedback'])
        self.assertEqual(worksheet.rows[1][14:17], ['My answer', 'A sentence', 'My transcript'])

    def test_google_sheet_import_updates_200_students_and_lessons(self):
        students = [
            {
                'full_name': f'Bulk Student {index}',
                'username': f'bulkstudent{index}',
                'email': f'bulkstudent{index}@example.test',
                'class_name': '7A',
                'status': 'active',
            }
            for index in range(200)
        ]
        lessons = [
            {
                'title_en': f'Bulk Lesson {index}',
                'title_vi': f'BÃ i há»c hÃ ng loáº¡t {index}',
                'class_level': '7',
                'week_number': '2',
            }
            for index in range(200)
        ]
        original_read_rows = app_module._read_google_worksheet_rows
        app_module._read_google_worksheet_rows = lambda title: students if title == 'Student' else lessons
        try:
            synced_students = app_module.sync_google_students_into_db()
            synced_lessons = app_module.sync_google_lessons_into_db()
            self.assertEqual(len(synced_students), 200)
            self.assertEqual(len(synced_lessons), 200)

            students[0]['full_name'] = 'Updated Bulk Student'
            lessons[0]['title_en'] = 'Updated Bulk Lesson'
            app_module.sync_google_students_into_db()
            app_module.sync_google_lessons_into_db()
        finally:
            app_module._read_google_worksheet_rows = original_read_rows

        conn = app_module.get_db()
        student = conn.execute(
            'SELECT full_name FROM users WHERE email = ?',
            ('bulkstudent0@example.test',)
        ).fetchone()
        lesson = conn.execute(
            'SELECT title_en FROM lessons WHERE title_vi = ?',
            ('BÃ i há»c hÃ ng loáº¡t 0',)
        ).fetchone()
        conn.close()
        self.assertEqual(student['full_name'], 'Updated Bulk Student')
        self.assertEqual(lesson['title_en'], 'Updated Bulk Lesson')

    def test_drive_media_name_uses_required_pattern(self):
        name = app_module.build_drive_media_name('audio', 'Unit 1', 'test user', 'mp3')
        self.assertTrue(name.startswith('Audio_Unit_1_test_user_'))
        self.assertTrue(name.endswith('.mp3'))
        self.assertRegex(name, r'^Audio_Unit_1_test_user_\d{8}-\d{6}\.mp3$')

    def test_drive_folder_lookup_reuses_recent_folder_id(self):
        class DummyFiles:
            def __init__(self):
                self.list_calls = 0

            def list(self, **kwargs):
                self.list_calls += 1
                return self

            def execute(self):
                return {'files': [{'id': 'practice-folder-id'}]}

        class DummyDriveService:
            def __init__(self):
                self.drive_files = DummyFiles()

            def files(self):
                return self.drive_files

        account_key = 'drive-cache-test@example.test'
        cache_key = (account_key, 'Practice')
        app_module._drive_folder_cache.pop(cache_key, None)
        drive_service = DummyDriveService()
        try:
            with patch.dict(app_module.os.environ, {'GOOGLE_DRIVE_FOLDER_ID': '', 'GOOGLE_DRIVE_FOLDER_NAME': ''}):
                self.assertEqual(
                    app_module.find_or_create_drive_folder(drive_service, account_key=account_key),
                    'practice-folder-id'
                )
                self.assertEqual(
                    app_module.find_or_create_drive_folder(drive_service, account_key=account_key),
                    'practice-folder-id'
                )
            self.assertEqual(drive_service.drive_files.list_calls, 1)
        finally:
            app_module._drive_folder_cache.pop(cache_key, None)

    def test_teacher_review_route_syncs_practice_sheet(self):
        conn = app_module.get_db()
        student = conn.execute("SELECT id FROM users WHERE role = 'student' LIMIT 1").fetchone()
        lesson = conn.execute('SELECT id FROM lessons ORDER BY id LIMIT 1').fetchone()
        conn.execute(
            'INSERT INTO submissions (student_id, lesson_id, answer, score, feedback, audio_path, drive_url, teacher_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (student['id'], lesson['id'], '{"evaluation": {"task_1": {"gemini": {"score": 82}, "groq": {"score": 80}}, "task_2": {"gemini": {"score": 78}, "groq": {"score": 75}}, "task_3": {"gemini": {"score": 83}, "groq": {"score": 85}}}}', 80, 'Ready', '', '', '')
        )
        conn.commit()
        submission_id = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
        conn.close()

        calls = {'count': 0}
        original_sync = app_module.sync_practice_to_sheet
        app_module.sync_practice_to_sheet = lambda *args, **kwargs: (calls.__setitem__('count', calls['count'] + 1), True)[1]
        try:
            with app_module.app.test_client() as client:
                with client.session_transaction() as sess:
                    sess['user_id'] = 2
                    sess['user_role'] = 'teacher'

                response = client.post(
                    f'/teacher/review/{submission_id}',
                    data={
                        'final_score': '88',
                        'teacher_note': 'Good work.',
                        'teacher_task_1_score': '88',
                        'teacher_task_2_score': '86',
                        'teacher_task_3_score': '90',
                    },
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(calls['count'], 1)
        finally:
            app_module.sync_practice_to_sheet = original_sync

    def test_teacher_review_route_saves_teacher_scores(self):
        conn = app_module.get_db()
        student = conn.execute("SELECT id FROM users WHERE role = 'student' LIMIT 1").fetchone()
        lesson = conn.execute('SELECT id FROM lessons ORDER BY id LIMIT 1').fetchone()
        conn.execute(
            'INSERT INTO submissions (student_id, lesson_id, answer, score, feedback, audio_path, drive_url, teacher_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (student['id'], lesson['id'], '{"evaluation": {"task_1": {"gemini": {"score": 82}, "groq": {"score": 80}}, "task_2": {"gemini": {"score": 78}, "groq": {"score": 75}}, "task_3": {"gemini": {"score": 83}, "groq": {"score": 85}}}}', 80, 'Ready', '', '', '')
        )
        conn.commit()
        submission_id = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
        conn.close()

        with patch.object(app_module, 'sync_practice_to_sheet', return_value=True) as sync_practice:
            with app_module.app.test_client() as client:
                with client.session_transaction() as sess:
                    sess['user_id'] = 2
                    sess['user_role'] = 'teacher'

                response = client.post(
                    f'/teacher/review/{submission_id}',
                    data={
                        'final_score': '88',
                        'teacher_note': 'Good work, keep speaking clearly.',
                        'teacher_pronunciation': '8.5',
                        'teacher_grammar': '9.0',
                        'teacher_fluency': '8.8',
                        'teacher_relevance': '9.2',
                        'teacher_confidence': '8.7',
                        'teacher_task_1_score': '88',
                        'teacher_task_2_score': '86',
                        'teacher_task_3_score': '90',
                    },
                    follow_redirects=True,
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn('Practice review and synced to the Google Sheets Practice sheet.', response.get_data(as_text=True))
            sync_practice.assert_called_once_with()

        conn = app_module.get_db()
        saved = conn.execute('SELECT score, teacher_note, answer FROM submissions WHERE id = ?', (submission_id,)).fetchone()
        conn.close()
        self.assertEqual(saved['score'], 88)
        self.assertIn('Good work', saved['teacher_note'])
        payload = app_module.parse_submission_payload(saved['answer'])
        self.assertIn('teacher', payload['evaluation']['task_1'])
        self.assertEqual(payload['evaluation']['task_1']['teacher']['score'], 88)

    def test_teacher_review_route_supports_decimal_scores(self):
        conn = app_module.get_db()
        student = conn.execute("SELECT id FROM users WHERE role = 'student' LIMIT 1").fetchone()
        lesson = conn.execute('SELECT id FROM lessons ORDER BY id LIMIT 1').fetchone()
        conn.execute(
            'INSERT INTO submissions (student_id, lesson_id, answer, score, feedback, audio_path, drive_url, teacher_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (student['id'], lesson['id'], '{"evaluation": {}}', 0, 'Ready', '', '', '')
        )
        conn.commit()
        submission_id = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
        conn.close()

        with app_module.app.test_client() as client:
            with client.session_transaction() as sess:
                sess['user_id'] = 2
                sess['user_role'] = 'teacher'

            response = client.post(
                f'/teacher/review/{submission_id}',
                data={
                    'final_score': '43.2',
                    'teacher_note': 'Detailed review with decimal scores.',
                    'teacher_task_1_score': '43.2',
                    'teacher_task_1_pronunciation': '10.8',
                    'teacher_task_1_grammar': '10.8',
                    'teacher_task_1_fluency': '10.8',
                    'teacher_task_1_relevance': '10.8',
                    'teacher_task_2_score': '75.5',
                    'teacher_task_3_score': '88.3',
                },
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)

        conn = app_module.get_db()
        saved = conn.execute('SELECT score, teacher_note, answer FROM submissions WHERE id = ?', (submission_id,)).fetchone()
        conn.close()
        self.assertEqual(saved['score'], 43.2)
        payload = app_module.parse_submission_payload(saved['answer'])
        self.assertEqual(payload['evaluation']['task_1']['teacher']['score'], 43.2)
        self.assertEqual(payload['evaluation']['task_1']['teacher']['pronunciation'], 10.8)

    def test_teacher_edit_lesson_route_saves_without_db_error(self):
        conn = app_module.get_db()
        teacher = conn.execute("SELECT id FROM users WHERE role = 'teacher' LIMIT 1").fetchone()
        lesson = conn.execute(
            'INSERT INTO lessons (title_en, title_vi, level, class_level, topic_en, topic_vi, goal_en, goal_vi, video_url, vocabulary, dialogue, sample_question, speaking_question, expected_answer, sentence_words, exercise_text_vi, exercise_text_en, week_number, open_date, close_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            ('Lesson 1', 'BÃ i 1', 'Basic', '6', 'Topic', 'Chá»§ Ä‘á»', 'Goal', 'Má»¥c tiÃªu', 'https://example.com', 'word,test', 'Dialogue', 'What?', 'What?', 'Answer', 'word,test', 'Exercise VI', 'Exercise EN', 1, '2026-10-20', '2026-10-27')
        )
        conn.commit()
        lesson_id = conn.execute('SELECT id FROM lessons WHERE title_en = ?', ('Lesson 1',)).fetchone()['id']
        conn.close()

        with patch.object(app_module, 'sync_lessons_to_sheet', return_value=True) as sync_lessons:
            with app_module.app.test_client() as client:
                with client.session_transaction() as sess:
                    sess['user_id'] = teacher['id']
                    sess['user_role'] = 'teacher'
                response = client.post(
                    f'/teacher/lesson/{lesson_id}',
                    data={
                        'title_vi': 'BÃ i 1 cáº­p nháº­t',
                        'title_en': 'Lesson 1 updated',
                        'video_url': 'https://example.com/video',
                        'sample_question': 'What is the topic?',
                        'speaking_question': 'What is the topic?',
                        'expected_answer': 'It is English.',
                        'sentence_words': 'It,is,English',
                        'exercise_text_vi': 'BÃ i táº­p má»›i',
                        'exercise_text_en': 'New exercise',
                        'class_level': '7',
                        'week_number': 2,
                        'open_date': '2026-10-21',
                        'close_date': '2026-10-28',
                    },
                    follow_redirects=True,
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn('Lesson updated and synced to the Google Sheets Lesson sheet.', response.get_data(as_text=True))
            sync_lessons.assert_called_once_with()

    def test_google_lesson_sync_updates_open_close_and_lesson_content(self):
        conn = app_module.get_db()
        lesson = conn.execute(
            'INSERT INTO lessons (title_en, title_vi, level, class_level, topic_en, topic_vi, goal_en, goal_vi, video_url, vocabulary, dialogue, sample_question, speaking_question, expected_answer, sentence_words, exercise_text_vi, exercise_text_en, week_number, open_date, close_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *',
            ('Old Lesson', 'BÃ i cÅ©', 'Basic', '6', 'Topic', 'Chá»§ Ä‘á»', 'Goal', 'Má»¥c tiÃªu', 'https://old.example', 'old word', 'Old dialogue', 'Old question', 'Old question', 'Old answer', 'old,word', 'Exercise VI', 'Exercise EN', 1, '2026-10-20', '2026-10-27')
        ).fetchone()
        conn.commit()
        lesson_id = lesson['id']
        conn.close()

        sheet_rows = [{
            'id': str(lesson_id),
            'Title EN': 'Updated Lesson',
            'Title VI': 'BÃ i cáº­p nháº­t',
            'Class Level': '8',
            'Week Number': '3',
            'Open Date': '2026-10-22',
            'Close Date': '2026-10-29',
            'Vocabulary': 'new,words',
            'Dialogue': 'Updated dialogue',
        }]
        original_read = app_module._read_google_worksheet_rows
        app_module._read_google_worksheet_rows = lambda title: sheet_rows if title == 'Lesson' else []
        try:
            synced = app_module.sync_google_lessons_into_db()
        finally:
            app_module._read_google_worksheet_rows = original_read

        self.assertEqual(len(synced), 1)
        conn = app_module.get_db()
        updated = conn.execute('SELECT * FROM lessons WHERE id = ?', (lesson_id,)).fetchone()
        conn.close()
        self.assertEqual(updated['title_en'], 'Updated Lesson')
        self.assertEqual(updated['title_vi'], 'BÃ i cáº­p nháº­t')
        self.assertEqual(updated['class_level'], 8)
        self.assertEqual(updated['week_number'], 3)
        self.assertEqual(updated['open_date'], '2026-10-22')
        self.assertEqual(updated['close_date'], '2026-10-29')
        self.assertEqual(updated['vocabulary'], 'new,words')
        self.assertEqual(updated['dialogue'], 'Updated dialogue')

    def test_google_student_sync_deletes_rows_removed_from_sheet(self):
        conn = app_module.get_db()
        student = conn.execute(
            "INSERT INTO users (full_name, username, email, password, role, class_name, is_active) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING *",
            ('Removed Student', 'removedstudent', 'removed@example.test', '123456', 'student', '7A', 1)
        ).fetchone()
        conn.commit()
        student_id = student['id']
        conn.close()

        original_read = app_module._read_google_worksheet_rows
        app_module._read_google_worksheet_rows = lambda title: [] if title == 'Student' else []
        app_module._GOOGLE_SHEET_READ_STATUS['student'] = True
        try:
            synced = app_module.sync_google_students_into_db()
        finally:
            app_module._read_google_worksheet_rows = original_read
            app_module._GOOGLE_SHEET_READ_STATUS.pop('student', None)

        conn = app_module.get_db()
        remaining = conn.execute('SELECT id FROM users WHERE id = ?', (student_id,)).fetchone()
        conn.close()
        self.assertEqual(synced, [])
        self.assertIsNone(remaining)

    def test_google_lesson_sync_deletes_rows_removed_from_sheet(self):
        conn = app_module.get_db()
        kept = conn.execute(
            "INSERT INTO lessons (title_en, title_vi, level, class_level, topic_en, topic_vi, goal_en, goal_vi) VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
            ('Kept Lesson', 'Bai giu lai', 'Basic', 7, 'Topic', 'Chu de', 'Goal', 'Muc tieu')
        ).fetchone()
        removed = conn.execute(
            "INSERT INTO lessons (title_en, title_vi, level, class_level, topic_en, topic_vi, goal_en, goal_vi) VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
            ('Removed Lesson', 'Bai da xoa', 'Basic', 7, 'Topic', 'Chu de', 'Goal', 'Muc tieu')
        ).fetchone()
        conn.commit()
        conn.close()

        original_read = app_module._read_google_worksheet_rows
        app_module._read_google_worksheet_rows = lambda title: [{
            'id': str(kept['id']),
            'Title EN': 'Kept Lesson',
            'Title VI': 'Bai giu lai',
            'Class Level': '7',
        }] if title == 'Lesson' else []
        try:
            app_module.sync_google_lessons_into_db()
        finally:
            app_module._read_google_worksheet_rows = original_read

        conn = app_module.get_db()
        remaining = conn.execute('SELECT id FROM lessons WHERE title_en = ?', ('Removed Lesson',)).fetchone()
        conn.close()
        self.assertIsNone(remaining)

    def test_google_practice_sync_deletes_rows_removed_from_sheet(self):
        conn = app_module.get_db()
        student = conn.execute("SELECT id FROM users WHERE role = 'student' LIMIT 1").fetchone()
        lesson = conn.execute('SELECT id FROM lessons LIMIT 1').fetchone()
        if not student or not lesson:
            conn.close()
            self.skipTest('Requires seeded student and lesson')
        submission = conn.execute(
            'INSERT INTO submissions (student_id, lesson_id, answer, score, feedback, created_at) VALUES (?, ?, ?, ?, ?, ?) RETURNING *',
            (student['id'], lesson['id'], '{}', 50, 'Old', '2026-10-28 10:00:00')
        ).fetchone()
        submission_id = submission['id']
        conn.commit()
        conn.close()

        original_read = app_module._read_google_worksheet_rows
        app_module._read_google_worksheet_rows = lambda title: [] if title == 'Practice' else []
        app_module._GOOGLE_SHEET_READ_STATUS['practice'] = True
        try:
            synced = app_module.sync_google_practice_into_db()
        finally:
            app_module._read_google_worksheet_rows = original_read
            app_module._GOOGLE_SHEET_READ_STATUS.pop('practice', None)

        conn = app_module.get_db()
        remaining = conn.execute('SELECT id FROM submissions WHERE id = ?', (submission_id,)).fetchone()
        conn.close()
        self.assertEqual(synced, [])
        self.assertIsNone(remaining)

    def test_google_lesson_sync_does_not_duplicate_by_stale_or_missing_sheet_id(self):
        conn = app_module.get_db()
        existing = conn.execute(
            "SELECT id FROM lessons WHERE title_en = 'Lesson Four' AND class_level = 7 LIMIT 1"
        ).fetchone()
        if existing:
            conn.execute('DELETE FROM lessons WHERE title_en = ? AND class_level = ?', ('Lesson Four', 7))
        lesson = conn.execute(
            '''INSERT INTO lessons (title_en, title_vi, level, class_level, topic_en, topic_vi, goal_en, goal_vi)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING *''',
            ('Lesson Four', 'Bai bon', 'Google Sheet import', 7, 'Topic', 'Chu de', 'Goal', 'Muc tieu')
        ).fetchone()
        existing_id = lesson['id']
        conn.commit()
        conn.close()

        sheet_rows = [{
            'ID': '999',
            'Title EN': 'Lesson Four',
            'Title VI': 'Bai bon',
            'Class Level': '7.0',
            'Week Number': '2',
        }]
        original_read = app_module._read_google_worksheet_rows
        app_module._read_google_worksheet_rows = lambda title: sheet_rows if title == 'Lesson' else []
        try:
            synced = app_module.sync_google_lessons_into_db()
            synced_again = app_module.sync_google_lessons_into_db()
        finally:
            app_module._read_google_worksheet_rows = original_read

        conn = app_module.get_db()
        lessons = conn.execute(
            "SELECT id, class_level, typeof(class_level) AS class_type FROM lessons WHERE title_en = 'Lesson Four' AND class_level = 7"
        ).fetchall()
        conn.close()
        self.assertEqual(len(synced), 1)
        self.assertEqual(len(synced_again), 1)
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]['id'], existing_id)
        self.assertEqual(lessons[0]['class_level'], 7)
        self.assertEqual(lessons[0]['class_type'], 'integer')

    def test_teacher_student_and_lesson_pages_do_not_wait_for_google_sheets(self):
        conn = app_module.get_db()
        teacher = conn.execute("SELECT id FROM users WHERE role = 'teacher' LIMIT 1").fetchone()
        conn.close()

        with patch.object(app_module.gspread, 'authorize', side_effect=AssertionError('page load called Google Sheets')):
            with app_module.app.test_client() as client:
                with client.session_transaction() as sess:
                    sess['user_id'] = teacher['id']
                    sess['user_role'] = 'teacher'
                self.assertEqual(client.get('/teacher/students').status_code, 200)
                self.assertEqual(client.get('/teacher/lessons').status_code, 200)

    def test_student_lesson_and_practice_lists_support_page_sizes(self):
        conn = app_module.get_db()
        teacher = conn.execute("SELECT id FROM users WHERE role = 'teacher' LIMIT 1").fetchone()
        student = conn.execute("SELECT id FROM users WHERE role = 'student' LIMIT 1").fetchone()
        lesson = conn.execute('SELECT id FROM lessons LIMIT 1').fetchone()
        for index in range(12):
            conn.execute(
                'INSERT INTO users (full_name, username, email, password, role, class_name) VALUES (?, ?, ?, ?, ?, ?)',
                (f'Pagination Student {index}', f'pagination{index}', f'pagination{index}@example.test', 'secret', 'student', '7A')
            )
            conn.execute(
                '''
                INSERT INTO lessons (title_en, title_vi, level, class_level, topic_en, topic_vi, goal_en, goal_vi)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (f'Pagination Lesson {index}', f'BÃ i phÃ¢n trang {index}', 'Test', '7', 'Topic', 'Chá»§ Ä‘á»', 'Goal', 'Má»¥c tiÃªu')
            )
            conn.execute(
                'INSERT INTO submissions (student_id, lesson_id, score, teacher_note) VALUES (?, ?, ?, ?)',
                (student['id'], lesson['id'], 80, 'Reviewed' if index % 2 == 0 else '')
            )
        conn.commit()
        conn.close()

        with app_module.app.test_client() as client:
            with client.session_transaction() as sess:
                sess['user_id'] = teacher['id']
                sess['user_role'] = 'teacher'

            for path in ('/teacher/students', '/teacher/lessons', '/teacher/practice'):
                response = client.get(f'{path}?per_page=5&page=2')
                page = response.get_data(as_text=True)
                first_table_body = page.split('<tbody>', 1)[1].split('</tbody>', 1)[0]
                self.assertEqual(response.status_code, 200)
                self.assertEqual(first_table_body.count('<tr>'), 5)
                self.assertIn('Page <strong>2</strong> of', page)
                self.assertIn('value="5" selected', page)

            practice_page = client.get('/teacher/practice?per_page=5&page=2').get_data(as_text=True)
            self.assertIn('href="/teacher/practice?page=3&amp;per_page=5&amp;search_field=student&amp;q="', practice_page)
            self.assertIn('value="20"', client.get('/teacher/students?per_page=20').get_data(as_text=True))
            student_page = client.get('/teacher/students?per_page=5').get_data(as_text=True)
            self.assertIn('Next â†’', student_page)
            self.assertIn('â† Previous', student_page)
            self.assertNotIn('Submissions to review', student_page)
            self.assertNotIn('Practice overview', student_page)

    def test_teacher_create_student_defaults_password_and_shows_success(self):
        conn = app_module.get_db()
        teacher = conn.execute("SELECT id FROM users WHERE role = 'teacher' LIMIT 1").fetchone()
        conn.close()

        with patch.object(app_module, 'sync_students_to_sheet', return_value=True):
            with app_module.app.test_client() as client:
                with client.session_transaction() as sess:
                    sess['user_id'] = teacher['id']
                    sess['user_role'] = 'teacher'

                response = client.post('/teacher/student/new', data={
                    'full_name': 'Created Student',
                    'username': 'createdstudent',
                    'email': 'createdstudent@example.test',
                    'class_name': '7A',
                    'password': '',
                })
                self.assertEqual(response.status_code, 302)
                page = client.get(response.location).get_data(as_text=True)
                self.assertIn('Student account created successfully.', page)

        conn = app_module.get_db()
        student = conn.execute(
            'SELECT password FROM users WHERE username = ?',
            ('createdstudent',)
        ).fetchone()
        conn.close()
        self.assertEqual(student['password'], '123456')

    def test_teacher_lists_search_selected_categories(self):
        conn = app_module.get_db()
        teacher = conn.execute("SELECT id FROM users WHERE role = 'teacher' LIMIT 1").fetchone()
        conn.execute(
            'INSERT INTO users (full_name, username, email, password, role, class_name) VALUES (?, ?, ?, ?, ?, ?)',
            ('Marker Student', 'markerstudent', 'markerstudent@example.test', 'secret', 'student', 'Marker Class')
        )
        student_id = conn.execute(
            'SELECT id FROM users WHERE username = ?',
            ('markerstudent',)
        ).fetchone()['id']
        conn.execute(
            '''
            INSERT INTO lessons (title_en, title_vi, level, class_level, topic_en, topic_vi, goal_en, goal_vi)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            ('Dog Search Marker', 'Marker cho chÃ³', 'Test', '8', 'Dog topic', 'Chá»§ Ä‘á» chÃ³', 'Goal', 'Má»¥c tiÃªu')
        )
        lesson_id = conn.execute(
            'SELECT id FROM lessons WHERE title_en = ?',
            ('Dog Search Marker',)
        ).fetchone()['id']
        conn.execute(
            'INSERT INTO submissions (student_id, lesson_id, answer, score) VALUES (?, ?, ?, ?)',
            (student_id, lesson_id, 'Marker answer', 90)
        )
        conn.commit()
        conn.close()

        with app_module.app.test_client() as client:
            with client.session_transaction() as sess:
                sess['user_id'] = teacher['id']
                sess['user_role'] = 'teacher'

            student_page = client.get('/teacher/students?search_field=username&q=markerstudent').get_data(as_text=True)
            self.assertIn('Marker Student', student_page)
            self.assertIn('value="username" selected', student_page)
            self.assertIn('Showing 1â€“1 of 1', student_page)

            lesson_page = client.get('/teacher/lessons?search_field=title&q=Dog').get_data(as_text=True)
            self.assertIn('Dog Search Marker', lesson_page)
            self.assertIn('value="title" selected', lesson_page)
            self.assertIn('Showing 1â€“1 of 1', lesson_page)

            practice_page = client.get('/teacher/practice?search_field=lesson&q=Dog').get_data(as_text=True)
            self.assertIn('Marker Student', practice_page)
            self.assertIn('value="lesson" selected', practice_page)
            self.assertIn('Showing 1â€“1 of 1', practice_page)

    def test_normalize_youtube_embed_url_supports_short_links(self):
        self.assertEqual(
            app_module.normalize_youtube_embed_url('https://youtu.be/abcd1234?si=abc'),
            'https://www.youtube.com/embed/abcd1234?si=abc'
        )
        self.assertEqual(
            app_module.normalize_youtube_embed_url('https://www.youtube.com/watch?v=abcd1234&t=10s'),
            'https://www.youtube.com/embed/abcd1234?t=10s'
        )

    def test_teacher_manual_google_sync_route_redirects(self):
        conn = app_module.get_db()
        teacher = conn.execute("SELECT id FROM users WHERE role = 'teacher' LIMIT 1").fetchone()
        conn.close()

        original_students = app_module.sync_google_students_into_db
        original_lessons = app_module.sync_google_lessons_into_db
        original_sync_students = app_module.sync_students_to_sheet
        original_sync_lessons = app_module.sync_lessons_to_sheet
        app_module.sync_google_students_into_db = lambda: [{'full_name': 'A'}]
        app_module.sync_google_lessons_into_db = lambda: [{'title_en': 'B'}]
        app_module.sync_students_to_sheet = lambda *args, **kwargs: True
        app_module.sync_lessons_to_sheet = lambda *args, **kwargs: True
        try:
            with app_module.app.test_client() as client:
                with client.session_transaction() as sess:
                    sess['user_id'] = teacher['id']
                    sess['user_role'] = 'teacher'
                response = client.post('/teacher/sync_google')
                self.assertEqual(response.status_code, 302)
        finally:
            app_module.sync_google_students_into_db = original_students
            app_module.sync_google_lessons_into_db = original_lessons
            app_module.sync_students_to_sheet = original_sync_students
            app_module.sync_lessons_to_sheet = original_sync_lessons

    def test_google_practice_sync_imports_sheet_records(self):
        conn = app_module.get_db()
        student = conn.execute("SELECT id FROM users WHERE role = 'student' LIMIT 1").fetchone()
        lesson = conn.execute('SELECT id FROM lessons LIMIT 1').fetchone()
        conn.close()
        self.assertIsNotNone(student)
        self.assertIsNotNone(lesson)

        sheet_rows = [{
            'submission_id': '',
            'submitted_at': '2026-10-28 14:30:00',
            'student_id': str(student['id']),
            'student_name': 'Sheet Student',
            'lesson_id': str(lesson['id']),
            'lesson_title': 'Sheet Lesson',
            'overall_score': '88',
            'task_1_score': '90',
            'task_2_score': '85',
            'task_3_score': '89',
            'feedback': 'Synced from Practice sheet',
            'answer_1': 'Hello from the sheet',
            'answer_2': 'This is a correct sentence',
            'audio_url': 'https://example.com/audio.mp3',
        }]
        original_read = app_module._read_google_worksheet_rows
        app_module._read_google_worksheet_rows = lambda title: sheet_rows if title == 'Practice' else []
        try:
            synced = app_module.sync_google_practice_into_db()
        finally:
            app_module._read_google_worksheet_rows = original_read

        self.assertEqual(len(synced), 1)
        conn = app_module.get_db()
        record = conn.execute('SELECT * FROM submissions WHERE student_id = ? AND lesson_id = ? ORDER BY id DESC LIMIT 1', (student['id'], lesson['id'])).fetchone()
        conn.close()
        self.assertIsNotNone(record)
        self.assertEqual(record['score'], 88)
        self.assertEqual(record['feedback'], 'Synced from Practice sheet')
        self.assertIn('Hello from the sheet', record['answer'])

    def test_teacher_practice_sync_reports_success(self):
        conn = app_module.get_db()
        teacher = conn.execute("SELECT id FROM users WHERE role = 'teacher' LIMIT 1").fetchone()
        conn.close()

        with patch.object(app_module, 'sync_practice_to_sheet', return_value=True) as sync_practice:
            with app_module.app.test_client() as client:
                with client.session_transaction() as sess:
                    sess['user_id'] = teacher['id']
                    sess['user_role'] = 'teacher'
                response = client.post(
                    '/teacher/sync_google',
                    data={'sheet': 'practice'},
                    headers={'Referer': '/teacher/practice'},
                    follow_redirects=True,
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn('Practice sheet synced successfully.', response.get_data(as_text=True))
            sync_practice.assert_called_once_with()

    def test_database_uses_wal_and_busy_timeout(self):
        conn = app_module.get_db()
        self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0].lower(), 'wal')
        self.assertGreaterEqual(conn.execute('PRAGMA busy_timeout').fetchone()[0], 30000)
        conn.close()

    def test_teacher_dashboard_renders_practice_table_and_png_logo(self):
        conn = app_module.get_db()
        teacher = conn.execute("SELECT id FROM users WHERE role = 'teacher' LIMIT 1").fetchone()
        conn.close()

        with app_module.app.test_client() as client:
            with client.session_transaction() as sess:
                sess['user_id'] = teacher['id']
                sess['user_role'] = 'teacher'
            response = client.get('/dashboard')
            page = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn('cva-logo.png', page)
            self.assertIn('Practice', page)
            self.assertIn('class="sidebar-toggle"', page)
            self.assertIn('class="nav-icon">ðŸ‘¥</span>', page)
            self.assertIn('class="nav-icon">ðŸ“˜</span>', page)

    def test_student_account_can_change_password(self):
        conn = app_module.get_db()
        student_id = conn.execute(
            "SELECT id FROM users WHERE role = 'student' LIMIT 1"
        ).fetchone()['id']
        conn.execute('UPDATE users SET password = ? WHERE id = ?', ('oldpass123', student_id))
        conn.commit()
        conn.close()

        with app_module.app.test_client() as client:
            with client.session_transaction() as sess:
                sess['user_id'] = student_id
                sess['user_role'] = 'student'

            page = client.get('/student/account')
            self.assertEqual(page.status_code, 200)
            self.assertIn('Change password', page.get_data(as_text=True))

            mismatch = client.post('/student/account', data={
                'current_password': 'wrongpass',
                'new_password': 'newpass123',
                'confirm_password': 'newpass123',
            })
            self.assertEqual(mismatch.status_code, 200)
            self.assertIn('Your current password is incorrect.', mismatch.get_data(as_text=True))

            updated = client.post('/student/account', data={
                'current_password': 'oldpass123',
                'new_password': 'newpass123',
                'confirm_password': 'newpass123',
            })
            self.assertEqual(updated.status_code, 302)
            self.assertIn('saved=1', updated.location)

        conn = app_module.get_db()
        password = conn.execute(
            'SELECT password FROM users WHERE id = ?',
            (student_id,)
        ).fetchone()['password']
        conn.close()
        self.assertEqual(password, 'newpass123')

    def test_teacher_dashboard_summarizes_and_pages_many_classes(self):
        conn = app_module.get_db()
        teacher = conn.execute("SELECT id FROM users WHERE role = 'teacher' LIMIT 1").fetchone()
        for index in range(32):
            class_name = '7A' if index == 0 else f'Class {index + 1:02d}'
            conn.execute(
                '''
                INSERT INTO users (full_name, username, email, password, role, class_name, is_active, average_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    f'Dashboard Student {index}',
                    f'dashboardstudent{index}',
                    f'dashboardstudent{index}@example.test',
                    'secret',
                    'student',
                    class_name,
                    1,
                    80,
                )
            )
        conn.commit()
        conn.close()

        with app_module.app.test_client() as client:
            with client.session_transaction() as sess:
                sess['user_id'] = teacher['id']
                sess['user_role'] = 'teacher'

            response = client.get('/dashboard')
            page = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn('Total classes</small><strong>32</strong>', page)
            self.assertIn('id="dashboard-class-grid" class="dashboard-class-grid" data-page-size="8"', page)
            self.assertIn('id="dashboard-class-search"', page)
            self.assertIn('id="dashboard-class-prev"', page)
            self.assertIn('id="dashboard-class-next"', page)
            self.assertEqual(page.count('class="dashboard-class-card"'), 32)

    def test_teacher_dashboard_shows_ai_scores_and_submission_time(self):
        conn = app_module.get_db()
        teacher = conn.execute("SELECT id FROM users WHERE role = 'teacher' LIMIT 1").fetchone()
        student = conn.execute("SELECT id FROM users WHERE role = 'student' LIMIT 1").fetchone()
        lesson = conn.execute('SELECT id FROM lessons LIMIT 1').fetchone()
        answer = json.dumps({
            'task_1_answer': 'The actual submitted response.',
            'task_2_answer': 'A real reordered sentence.',
            'evaluation': {
                'task_1': {'gemini': {'score': 82}, 'groq': {'score': 90}},
                'task_2': {'gemini': {'score': 78}, 'groq': {'score': 75}},
                'task_3': {'gemini': {'score': 83}, 'groq': {'score': 84}},
            }
        })
        conn.execute(
            '''
            INSERT INTO submissions (student_id, lesson_id, answer, score, created_at)
            VALUES (?, ?, ?, ?, ?)
            ''',
            (student['id'], lesson['id'], answer, 80, '2026-10-28 01:15:00')
        )
        conn.commit()
        conn.close()

        with app_module.app.test_client() as client:
            with client.session_transaction() as sess:
                sess['user_id'] = teacher['id']
                sess['user_role'] = 'teacher'

            response = client.get('/dashboard')
            page = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn('Gemini <strong>81</strong>', page)
            self.assertIn('Groq <strong>83</strong>', page)
            self.assertIn('2026-10-28 01:15', page)
            self.assertIn('<th>Submitted</th>', page)
            self.assertIn('Latest 8 submissions', page)
            self.assertNotIn('<th>Task 1</th>', page)
            self.assertNotIn('<th>Task 2</th>', page)
            self.assertNotIn('<th>Transcript</th>', page)
            self.assertNotIn('The actual submitted response.', page)

    def test_practice_history_uses_submitted_answers_before_feedback_text(self):
        practice = app_module.build_practice_history([{
            'answer': json.dumps({
                'task_1_answer': 'Student response',
                'task_2_answer': 'Reordered sentence',
            }),
            'feedback': 'Task 1: evaluator commentary; Task 2: evaluator notes.',
            'created_at': '2026-10-28 01:15:00',
        }])[0]

        self.assertEqual(practice['task_1'], 'Student response')
        self.assertEqual(practice['task_2'], 'Reordered sentence')

    def test_teacher_student_detail_uses_consistent_workspace_layout(self):
        conn = app_module.get_db()
        teacher = conn.execute("SELECT id FROM users WHERE role = 'teacher' LIMIT 1").fetchone()
        student = conn.execute("SELECT id FROM users WHERE role = 'student' LIMIT 1").fetchone()
        conn.close()

        with app_module.app.test_client() as client:
            with client.session_transaction() as sess:
                sess['user_id'] = teacher['id']
                sess['user_role'] = 'teacher'

            response = client.get(f'/teacher/student/{student["id"]}')
            page = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn('class="app-shell teacher-workspace-shell"', page)
            self.assertIn('class="student-profile-card"', page)
            self.assertIn('class="student-detail-stats"', page)
            self.assertIn('Vocabulary', page)
            self.assertIn('Lesson progress', page)
            self.assertIn('No lesson submissions yet', page)
            self.assertNotIn('overflow-x: scroll', page)

    def test_teacher_profile_changes_persist_after_database_initialization(self):
        conn = app_module.get_db()
        teacher = conn.execute("SELECT id FROM users WHERE role = 'teacher' ORDER BY id LIMIT 1").fetchone()
        conn.close()

        with app_module.app.test_client() as client:
            with client.session_transaction() as sess:
                sess['user_id'] = teacher['id']
                sess['user_role'] = 'teacher'

            response = client.post(
                '/teacher/profile',
                data={
                    'full_name': 'Updated Teacher Name',
                    'username': 'updatedteacher',
                    'email': 'updated.teacher@example.test',
                    'password': '',
                },
            )
            self.assertEqual(response.status_code, 302)
            profile_page = client.get(response.location)
            page = profile_page.get_data(as_text=True)
            self.assertEqual(profile_page.status_code, 200)
            self.assertIn('Updated Teacher Name', page)
            self.assertIn('Your account details have been saved successfully.', page)
            self.assertIn('My Account', page)
            self.assertIn('Save changes', page)
            self.assertNotIn('Há» vÃ  tÃªn', page)
            self.assertNotIn('Máº­t kháº©u má»›i', page)

        app_module.init_db()
        conn = app_module.get_db()
        updated_teacher = conn.execute(
            'SELECT full_name, username, email FROM users WHERE id = ?',
            (teacher['id'],)
        ).fetchone()
        conn.close()
        self.assertEqual(
            (updated_teacher['full_name'], updated_teacher['username'], updated_teacher['email']),
            ('Updated Teacher Name', 'updatedteacher', 'updated.teacher@example.test'),
        )

    def test_upload_media_returns_local_and_drive_status(self):
        original = app_module.upload_to_google_drive
        app_module.upload_to_google_drive = lambda *args, **kwargs: 'https://drive.google.com/file/d/test/view'
        try:
            with app_module.app.test_client() as client:
                with client.session_transaction() as sess:
                    sess['user_id'] = 1
                    sess['user_role'] = 'student'
                data = {'file': (io.BytesIO(b'hello-audio'), 'demo.wav'), 'media_type': 'audio'}
                response = client.post('/api/upload_media', data=data, content_type='multipart/form-data')
                payload = response.get_json()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(payload['status'], 'ok')
                self.assertIn('saved locally and uploaded to Google Drive successfully', payload['message'])
                self.assertTrue(payload['local_url'].startswith('/uploads/audio/'))
                self.assertIn('drive.google.com', payload['drive_url'])
        finally:
            app_module.upload_to_google_drive = original

    def test_get_lessons_for_student_handles_missing_class_name(self):
        lessons = app_module.get_lessons_for_student({'class_name': '7A'})
        self.assertTrue(lessons)
        blank_lessons = app_module.get_lessons_for_student({'class_name': ''})
        self.assertTrue(blank_lessons)

    def test_edit_student_route_does_not_crash_and_syncs_full_sheet(self):
        conn = app_module.get_db()
        teacher = conn.execute("SELECT id FROM users WHERE role = 'teacher' LIMIT 1").fetchone()
        student = conn.execute(
            "INSERT INTO users (full_name, username, email, password, role, class_name, is_active, total_points, average_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ('Alice Student', 'alicestudent', 'alice@example.com', '123456', 'student', '7A', 1, 0, 0)
        )
        conn.commit()
        student_id = conn.execute('SELECT id FROM users WHERE email = ?', ('alice@example.com',)).fetchone()['id']
        conn.close()

        with patch.object(app_module, 'sync_students_to_sheet', return_value=True) as sync_students:
            with app_module.app.test_client() as client:
                with client.session_transaction() as sess:
                    sess['user_id'] = teacher['id']
                    sess['user_role'] = 'teacher'
                response = client.post(
                    f'/teacher/student/{student_id}/edit',
                    data={
                        'full_name': 'Alice Updated',
                        'username': 'aliceupdated',
                        'email': 'aliceupdated@example.com',
                        'class_name': '8A',
                        'role': 'student',
                        'is_active': 'on',
                    },
                    follow_redirects=True,
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn('Student updated and synced to the Google Sheets Student sheet.', response.get_data(as_text=True))
            sync_students.assert_called_once_with()

    def test_teacher_delete_practice_submission_updates_database_and_sheet(self):
        conn = app_module.get_db()
        teacher = conn.execute("SELECT id FROM users WHERE role = 'teacher' ORDER BY id LIMIT 1").fetchone()
        student = conn.execute("SELECT id FROM users WHERE role = 'student' ORDER BY id LIMIT 1").fetchone()
        lesson = conn.execute('SELECT id FROM lessons ORDER BY id LIMIT 1').fetchone()
        if lesson is None:
            conn.execute('INSERT INTO lessons (title_en, title_vi, class_level) VALUES (?, ?, ?)', ('Delete test lesson', 'BÃ i kiá»ƒm tra xoÃ¡', 6))
            lesson = conn.execute('SELECT id FROM lessons ORDER BY id DESC LIMIT 1').fetchone()
        conn.execute(
            'INSERT INTO submissions (student_id, lesson_id, answer, score, created_at) VALUES (?, ?, ?, ?, ?)',
            (student['id'], lesson['id'], '{}', 75, '2026-10-29 10:00:00')
        )
        submission_id = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
        conn.commit()
        conn.close()

        with patch.object(app_module, 'sync_practice_to_sheet', return_value=True) as sync_practice:
            with app_module.app.test_client() as client:
                with client.session_transaction() as sess:
                    sess['user_id'] = teacher['id']
                    sess['user_role'] = 'teacher'
                response = client.post(
                    f'/teacher/submission/{submission_id}/delete',
                    follow_redirects=True,
                )
                self.assertEqual(response.status_code, 200)
                response_text = response.get_data(as_text=True)
                self.assertIn('Practice submission deleted', response_text)

        conn = app_module.get_db()
        remaining = conn.execute('SELECT id FROM submissions WHERE id = ?', (submission_id,)).fetchone()
        conn.close()
        self.assertIsNone(remaining)
        sync_practice.assert_called_once_with()

    def test_vercel_sign_in_stores_session_token_for_google_proxy(self):
        conn = app_module.get_db()
        conn.execute(
            'INSERT INTO users (full_name, username, email, password, role, class_name, is_active) VALUES (?, ?, ?, ?, ?, ?, ?)',
            ('Test Teacher', 'testteacher', 'testteacher@example.com', 'local-password', 'teacher', '', 1),
        )
        conn.commit()
        user_id = conn.execute(
            'SELECT id FROM users WHERE email = ?',
            ('testteacher@example.com',),
        ).fetchone()['id']
        conn.close()
        with patch.object(app_module, 'IS_FROZEN', True), patch.object(
            app_module,
            'call_vercel_api',
            return_value={
                'authenticated': True,
                'uid': 'testteacher@example.com',
                'email': 'testteacher@example.com',
                'role': 'teacher',
                'access_token': 'signed-vercel-session',
            },
        ) as remote_login:
            with app_module.app.test_client() as client:
                response = client.post(
                    '/login',
                    data={
                        'username': 'testteacher',
                        'password': 'remote-password',
                        'role': 'teacher',
                    },
                )
                self.assertEqual(response.status_code, 302)
                with client.session_transaction() as browser_session:
                    self.assertEqual(browser_session['user_id'], user_id)
                    self.assertEqual(browser_session['user_role'], 'teacher')
            remote_login.assert_called_once_with(
                'login',
                {
                    'username': 'testteacher',
                    'password': 'remote-password',
                    'role': 'teacher',
                },
                timeout=30,
            )

        tokens = app_module.get_cloud_tokens(user_id)
        self.assertEqual(tokens, {'access_token': 'signed-vercel-session'})

    def test_login_form_accepts_username_and_password(self):
        with app_module.app.test_client() as client:
            response = client.get('/login')

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="login-username" type="text" name="username"', page)
        self.assertIn('id="login-password" type="password" name="password"', page)
        self.assertNotIn('id="login-username" type="email"', page)

    def test_vercel_login_request_does_not_send_a_bearer_token(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {'access_token': 'signed-vercel-session'}
        with patch.object(app_module.requests, 'post', return_value=response) as post:
            result = app_module.call_vercel_api(
                'login',
                {'username': 'testuser', 'password': 'password', 'role': 'student'},
            )

        self.assertEqual(result['access_token'], 'signed-vercel-session')
        self.assertNotIn('Authorization', post.call_args.kwargs['headers'])
        self.assertTrue(post.call_args.args[0].endswith('/api/login'))

    def test_vercel_login_preserves_http_error_for_non_json_response(self):
        response = Mock()
        response.status_code = 500
        response.json.side_effect = ValueError('not JSON')
        with patch.object(app_module.requests, 'post', return_value=response):
            result = app_module.call_vercel_api(
                'login',
                {'username': 'testuser', 'password': 'password', 'role': 'student'},
            )

        self.assertEqual(result['status_code'], 500)
        self.assertIn('Vercel Runtime Logs', result['error'])

    def test_vercel_drive_upload_uses_resumable_url_and_returns_file_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_path = Path(temp_dir) / 'recording.webm'
            media_path.write_bytes(b'test-audio-data')
            upload_response = Mock()
            upload_response.status_code = 200
            upload_response.json.return_value = {'id': 'drive-file-123'}
            calls = []

            def fake_vercel_call(endpoint, payload, access_token=None, timeout=30):
                calls.append((endpoint, payload, access_token))
                if endpoint == 'media-upload':
                    return {'upload_url': 'https://upload.example.test/session'}
                if endpoint == 'media-complete':
                    return {'drive_url': 'https://drive.google.com/file/d/drive-file-123/view'}
                self.fail(f'Unexpected Vercel endpoint: {endpoint}')

            with patch.object(app_module, 'call_vercel_api', side_effect=fake_vercel_call):
                with patch.object(app_module.requests, 'put', return_value=upload_response) as upload:
                    result = app_module.upload_media_to_vercel_drive(
                        media_path,
                        'lesson-recording.webm',
                        'audio',
                        'signed-vercel-session',
                    )

        self.assertEqual(result, {
            'file_id': 'drive-file-123',
            'drive_url': 'https://drive.google.com/file/d/drive-file-123/view',
        })
        self.assertEqual([call[0] for call in calls], ['media-upload', 'media-complete'])
        self.assertEqual(calls[0][1]['mime_type'], 'audio/webm')
        self.assertEqual(calls[0][2], 'signed-vercel-session')
        self.assertEqual(calls[1][1], {'file_id': 'drive-file-123'})
        self.assertEqual(upload.call_args.args[0], 'https://upload.example.test/session')

    def test_student_sheet_sync_preserves_password_hashes_and_teacher_account(self):
        merged = app_module.merge_student_sheet_values(
            [
                ['full_name', 'username', 'email', 'role', 'class_name', 'status'],
                ['Student One', 'studentone', 'student@example.com', 'student', '7A', 'active'],
            ],
            [
                ['full_name', 'username', 'email', 'role', 'class_name', 'status', 'password'],
                ['Student One', 'studentone', 'student@example.com', 'student', '7A', 'active', 'scrypt$student-hash'],
                ['Teacher One', 'teacherone', 'teacher@example.com', 'teacher', '', 'active', 'scrypt$teacher-hash'],
            ],
        )

        headers = merged[0]
        rows_by_email = {row[headers.index('email')]: row for row in merged[1:]}
        self.assertEqual(rows_by_email['student@example.com'][headers.index('password')], 'scrypt$student-hash')
        self.assertEqual(rows_by_email['teacher@example.com'][headers.index('password')], 'scrypt$teacher-hash')
        self.assertEqual(len(merged), 3)

    def test_vercel_evaluation_can_select_groq_provider(self):
        response_data = {
            'score': 85,
            'feedback': 'Good answer.',
            'pronunciation': 80,
            'grammar': 85,
            'fluency': 85,
            'relevance': 90,
            'confidence': 85,
        }
        with patch.object(app_module, 'call_vercel_api', return_value=response_data) as call_api:
            result = app_module.evaluate_with_vercel_function(
                'What is your name?',
                'My name is Lan.',
                'My name is Lan.',
                'signed-session',
                'groq',
            )

        self.assertEqual(result['score'], 85)
        self.assertEqual(call_api.call_args.args[1]['provider'], 'groq')


if __name__ == '__main__':
    unittest.main()

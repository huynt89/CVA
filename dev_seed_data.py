import sqlite3


TEACHER_ACCOUNTS = (
    {
        'full_name': 'Huy Nguyen',
        'username': 'huynt',
        'email': 'huynt@school.com',
        'password': '123456',
    },
    {
        'full_name': 'Mingo',
        'username': 'mingo',
        'email': 'mingo@school.com',
        'password': '123456',
    },
)

LESSONS = [
    {
        'id': 1,
        'title': 'Greetings and introductions',
        'title_vi': 'Chào hỏi và giới thiệu bản thân',
        'class_level': '6',
        'level': 'THCS - Unit 1',
        'topic_vi': 'Chào hỏi và giới thiệu bản thân',
        'topic_en': 'Greetings and self-introduction',
        'goal_vi': 'Học sinh có thể giới thiệu tên, tuổi và hỏi tên bạn.',
        'goal_en': 'Students can introduce their name, age and ask for someone’s name.',
        'video': 'https://www.youtube.com/watch?v=YQHsXMglC9A',
        'vocabulary': ['name', 'hello', 'friend', 'school', 'introduce'],
        'dialogue': 'Hi! My name is Lan. I am 13 years old. Nice to meet you!',
        'sample_question': 'What is your name?',
        'expected_answer': 'My name is Lan.',
        'sentence_words': ['My', 'name', 'is', 'Lan', '.'],
        'exercise_text_vi': 'Sắp xếp các từ để tạo câu đúng: My name is Lan.',
        'exercise_text_en': 'Arrange the words to make the correct sentence: My name is Lan.',
        'tips': ['Use “My name is...” to introduce yourself.', 'Use “Nice to meet you!” for new friends.'],
    },
    {
        'id': 2,
        'title': 'School life',
        'title_vi': 'Cuộc sống trường học',
        'class_level': '7',
        'level': 'THCS - Unit 2',
        'topic_vi': 'Cuộc sống trường học',
        'topic_en': 'School life',
        'goal_vi': 'Mô tả môn học yêu thích và thói quen học tập.',
        'goal_en': 'Describe favorite subjects and study habits.',
        'video': 'https://www.youtube.com/watch?v=ysz5S6PUM-U',
        'vocabulary': ['favorite', 'subject', 'english', 'classroom', 'study'],
        'dialogue': 'My favorite subject is English. I study hard every day.',
        'sample_question': 'What is your favorite subject?',
        'expected_answer': 'My favorite subject is English.',
        'sentence_words': ['My', 'favorite', 'subject', 'is', 'English', '.'],
        'exercise_text_vi': 'Sắp xếp các từ để tạo câu đúng: My favorite subject is English.',
        'exercise_text_en': 'Arrange the words to make the correct sentence: My favorite subject is English.',
        'tips': ['Use “favorite subject” to talk about what you like most.', 'Add “I like...” for a shorter version.'],
    },
    {
        'id': 3,
        'title': 'Daily routines',
        'title_vi': 'Thói quen hàng ngày',
        'class_level': '8',
        'level': 'THCS - Unit 3',
        'topic_vi': 'Thói quen hằng ngày',
        'topic_en': 'Daily routines',
        'goal_vi': 'Nói về thói quen hàng ngày theo giờ.',
        'goal_en': 'Talk about daily routines and schedules.',
        'video': 'https://www.youtube.com/watch?v=E7wJTI-1dvQ',
        'vocabulary': ['wake up', 'breakfast', 'school', 'study', 'sleep'],
        'dialogue': 'I usually wake up at six and go to school at seven.',
        'sample_question': 'What time do you wake up?',
        'expected_answer': 'I wake up at six.',
        'sentence_words': ['I', 'wake', 'up', 'at', 'six', '.'],
        'exercise_text_vi': 'Sắp xếp các từ để tạo câu đúng: I wake up at six.',
        'exercise_text_en': 'Arrange the words to make the correct sentence: I wake up at six.',
        'tips': ['Use “at + time” to say the exact time.', 'Use “usually” for regular habits.'],
    },
]


def seed_demo_data(conn: sqlite3.Connection) -> None:
    user_count = conn.execute('SELECT COUNT(*) as c FROM users').fetchone()['c']
    if user_count == 0:
        conn.execute(
            'INSERT INTO users (full_name, username, email, password, role, class_name, is_active, total_points, average_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            ('Lan Nguyen', 'lannguyen', 'student@school.com', '123456', 'student', '7A', 1, 0, 0),
        )

    teacher_count = conn.execute(
        "SELECT COUNT(*) AS count FROM users WHERE role = 'teacher'"
    ).fetchone()['count']
    if teacher_count == 0:
        for teacher in TEACHER_ACCOUNTS:
            conn.execute(
                'INSERT INTO users (full_name, username, email, password, role, class_name, is_active, total_points, average_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    teacher['full_name'],
                    teacher['username'],
                    teacher['email'],
                    teacher['password'],
                    'teacher',
                    'English',
                    1,
                    0,
                    0,
                ),
            )
    else:
        legacy_teacher = conn.execute(
            "SELECT id FROM users WHERE role = 'teacher' AND username = 'mrhuy' ORDER BY id LIMIT 1"
        ).fetchone()
        if legacy_teacher:
            teacher = TEACHER_ACCOUNTS[0]
            conn.execute(
                'UPDATE users SET full_name = ?, username = ?, password = ?, class_name = ? WHERE id = ?',
                (
                    teacher['full_name'],
                    teacher['username'],
                    teacher['password'],
                    'English',
                    legacy_teacher['id'],
                ),
            )

    lesson_count = conn.execute('SELECT COUNT(*) as c FROM lessons').fetchone()['c']
    if lesson_count == 0:
        for item in LESSONS:
            conn.execute(
                '''
                INSERT INTO lessons (
                    title_en, title_vi, level, class_level, topic_en, topic_vi, goal_en, goal_vi,
                    video_url, vocabulary, dialogue, sample_question, speaking_question, expected_answer,
                    sentence_words, exercise_text_vi, exercise_text_en, task_3_text, task_4_text, week_number, open_date, close_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    item['title'],
                    item['title_vi'],
                    item['level'],
                    item.get('class_level') or '6',
                    item['topic_en'],
                    item['topic_vi'],
                    item['goal_en'],
                    item['goal_vi'],
                    item['video'],
                    ','.join(item['vocabulary']),
                    item['dialogue'],
                    item['sample_question'],
                    item['sample_question'],
                    item['expected_answer'],
                    ','.join(item['sentence_words']),
                    item['exercise_text_vi'],
                    item['exercise_text_en'],
                    'Ghi âm (Tối đa file ghi âm là 1 phút)',
                    'Quay video ngắn cuộc hội thoại giới thiệu về người bạn mới. (Video có độ dài tối đa là 30 giây)',
                    item.get('week_number') or 1,
                    item.get('open_date') or '2026-10-20',
                    item.get('close_date') or '2026-10-27',
                ),
            )

const answerBox = document.getElementById('answerBox');
const sentenceResult = document.getElementById('sentenceResult');
const aiResult = document.getElementById('aiResult');
const recordBtn = document.getElementById('recordBtn');
const answerInput = document.getElementById('answerInput');
const evaluateBtn = document.getElementById('evaluateBtn');
const uploadAudioBtn = document.getElementById('uploadAudioBtn');
const audioUpload = document.getElementById('audioUpload');
const loginRoleInputs = document.querySelectorAll('.role-input');
const loginSubmitButton = document.querySelector('.login-submit-btn');
const loginTabs = document.querySelectorAll('.role-tab');

const updateLoginButtonText = () => {
    if (!loginSubmitButton) return;

    const roleInput = document.querySelector('.role-input');
    const selectedRole = roleInput ? roleInput.value : 'student';
    const label = selectedRole === 'teacher'
        ? loginSubmitButton.dataset.teacher
        : loginSubmitButton.dataset.student;
    loginSubmitButton.textContent = label || loginSubmitButton.textContent;
};

loginTabs.forEach((tab) => {
    tab.addEventListener('click', () => {
        const selectedRole = tab.dataset.role;
        document.querySelectorAll('.role-tab').forEach((button) => {
            const isActive = button === tab;
            button.classList.toggle('active', isActive);
            button.setAttribute('aria-pressed', String(isActive));
        });
        loginRoleInputs.forEach((input) => {
            input.value = selectedRole;
        });
        updateLoginButtonText();
    });
});

if (loginSubmitButton) {
    updateLoginButtonText();
}

const setLanguage = async (lang) => {
    try {
        await fetch('/api/set_language', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ lang })
        });
        window.location.reload();
    } catch (error) {
        console.error('Language change failed', error);
    }
};

document.querySelectorAll('.lang-btn').forEach((btn) => {
    btn.addEventListener('click', () => setLanguage(btn.dataset.lang));
});

const appShell = document.querySelector('.app-shell');
const sidebarToggle = document.querySelector('.sidebar-toggle');

const syncSidebarState = (collapsed) => {
    if (!appShell) return;
    appShell.classList.toggle('sidebar-collapsed', collapsed);
    if (sidebarToggle) {
        const isMobile = window.innerWidth <= 900;
        sidebarToggle.textContent = isMobile ? (collapsed ? '⌃' : '⌄') : (collapsed ? '›' : '‹');
        sidebarToggle.style.transform = 'rotate(0deg)';
        sidebarToggle.setAttribute('aria-expanded', String(!collapsed));
        sidebarToggle.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
        sidebarToggle.title = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
    }
};

if (sidebarToggle) {
    sidebarToggle.addEventListener('click', () => {
        const collapsed = !appShell.classList.contains('sidebar-collapsed');
        syncSidebarState(collapsed);
        if (window.innerWidth > 900) {
            localStorage.setItem('sidebarCollapsed', String(collapsed));
        }
    });
}

document.querySelectorAll('.sidebar nav a').forEach((link) => {
    const label = link.querySelector('.nav-label')?.textContent?.trim();
    if (label) {
        link.setAttribute('aria-label', label);
        link.title = label;
    }

    link.addEventListener('click', () => {
        if (window.innerWidth <= 900) {
            syncSidebarState(true);
        }
    });
});

const collapseOnSmallScreens = () => {
    if (!appShell) return;
    const isMobile = window.innerWidth <= 900;
    if (isMobile) {
        syncSidebarState(true);
    } else {
        const savedState = localStorage.getItem('sidebarCollapsed');
        syncSidebarState(savedState === 'true');
    }
};

window.addEventListener('resize', collapseOnSmallScreens);
collapseOnSmallScreens();

const showStatusMessage = (message, type = 'info') => {
    const statusBox = document.getElementById('uploadStatus');
    if (!message) return;

    if (statusBox) {
        statusBox.textContent = message;
        statusBox.className = `alert-box ${type === 'warning' ? 'warning-box' : 'success-box'}`;
        return;
    }

    const box = document.createElement('div');
    box.id = 'uploadStatus';
    box.className = `alert-box ${type === 'warning' ? 'warning-box' : 'success-box'}`;
    box.textContent = message;
    const target = document.querySelector('.exercise-card, .card, .main-panel');
    if (target && target.firstChild) {
        target.insertBefore(box, target.firstChild);
    } else {
        document.body.prepend(box);
    }
};

const latestMediaUrls = { audio: '', video: '' };
const latestDriveFileIds = { audio: '', video: '' };
const pendingExerciseMedia = { audio: null, video: null };

const submitLessonBtn = document.getElementById('submitLessonBtn');

if (submitLessonBtn) {
    submitLessonBtn.addEventListener('click', async () => {
        const lessonId = getLessonId();
        const answer1 = document.getElementById('exerciseAnswerBox')?.value || '';
        const answer2 = document.getElementById('answerBox')?.textContent?.trim() || '';

        try {
            submitLessonBtn.disabled = true;
            submitLessonBtn.classList.add('loading');
            submitLessonBtn.innerHTML = '<span class="button-icon" aria-hidden="true">◌</span><span>Submitting...</span>';

            const mediaUploadResults = [];
            for (const mediaType of ['audio', 'video']) {
                const fileToUpload = pendingExerciseMedia[mediaType];
                if (fileToUpload) {
                    const mediaResult = await uploadMediaFile(fileToUpload, mediaType);
                    if (mediaResult) {
                        mediaUploadResults.push(mediaResult);
                    }
                }
            }

            const driveFailures = mediaUploadResults.filter((result) => result && !result.drive_url);
            const response = await fetch('/api/submit_lesson', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    lesson_id: lessonId,
                    answer_1: answer1,
                    answer_2: answer2,
                    audio_url: latestMediaUrls.audio,
                    video_url: latestMediaUrls.video,
                    audio_file_id: latestDriveFileIds.audio,
                    sentence_words: answer2,
                })
            });

            const data = await response.json();
            if (!response.ok || !data.saved) {
                showStatusMessage(data.message || 'Submission failed.', 'warning');
                return;
            }

            const successMessage = `Your work was submitted successfully. Total score: ${data.score}/100`;
            if (data.google_drive_saved === false || driveFailures.length) {
                showStatusMessage(`${successMessage} ${data.message || 'Google Drive sync was incomplete for one or more recordings.'}`, 'warning');
            } else if (data.google_sheet_saved === false || data.practice_sheet_saved === false) {
                showStatusMessage(`${successMessage} ${data.message || 'Google Sheets sync was incomplete.'}`, 'warning');
            } else if (data.ai_services_available === false) {
                showStatusMessage(data.message || `${successMessage} AI evaluation was unavailable.`, 'warning');
            } else {
                showStatusMessage(`${data.message || successMessage}`, 'success');
            }
            pendingExerciseMedia.audio = null;
            pendingExerciseMedia.video = null;
            latestMediaUrls.audio = '';
            latestMediaUrls.video = '';
            latestDriveFileIds.audio = '';
            latestDriveFileIds.video = '';
        } catch (error) {
            console.error('Submit lesson error', error);
            showStatusMessage('Could not save your submission. Please try again.', 'warning');
        } finally {
            submitLessonBtn.disabled = false;
            submitLessonBtn.classList.remove('loading');
            submitLessonBtn.innerHTML = '<span class="button-icon" aria-hidden="true">↗</span><span>Submit</span>';
        }
    });
}

const selectedWords = [];
const wordButtons = Array.from(document.querySelectorAll('.word-chip'));

if (wordButtons.length && answerBox) {
    const wordBank = document.querySelector('.word-bank');
    const correctSentence = document.getElementById('exerciseState')?.dataset.correct || '';

    const shuffledButtons = [...wordButtons].sort(() => Math.random() - 0.5);
    if (wordBank) {
        wordBank.innerHTML = '';
        shuffledButtons.forEach((button) => wordBank.appendChild(button));
    }

    shuffledButtons.forEach((button) => {
        button.addEventListener('click', () => {
            const word = button.dataset.word;
            selectedWords.push(word);
            const slot = document.createElement('span');
            slot.className = 'slot';
            slot.textContent = word;
            answerBox.appendChild(slot);
            button.disabled = true;
            button.style.opacity = '0.5';
        });
    });

    const resetSentence = document.getElementById('resetSentence');
    if (resetSentence) {
        resetSentence.addEventListener('click', () => {
            answerBox.innerHTML = '';
            selectedWords.length = 0;
            shuffledButtons.forEach((button) => {
                button.disabled = false;
                button.style.opacity = '1';
            });
            sentenceResult.textContent = '';
        });
    }

    const checkSentence = document.getElementById('checkSentence');
    if (checkSentence) {
        checkSentence.addEventListener('click', () => {
            const chosen = selectedWords.join(' ');
            const isCorrect = chosen.trim() === correctSentence.trim();
            sentenceResult.textContent = isCorrect
                ? 'Correct! Great job. You built the sentence correctly.'
                : 'Not yet. Try again using the pattern: ' + correctSentence;
            sentenceResult.style.color = isCorrect ? '#16a34a' : '#dc2626';
        });
    }
}


if (recordBtn) {
    recordBtn.addEventListener('click', () => {
        if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
            answerInput.value = 'My name is Lan.';
            recordBtn.textContent = 'Microphone not available';
            return;
        }

        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        const recognition = new SpeechRecognition();
        recognition.lang = 'en-US';
        recognition.interimResults = false;

        recognition.onstart = () => {
            recordBtn.textContent = 'Listening...';
        };

        recognition.onresult = (event) => {
            const transcript = event.results[0][0].transcript;
            answerInput.value = transcript;
        };

        recognition.onend = () => {
            recordBtn.textContent = 'Start recording';
        };

        recognition.start();
    });
}

const getLessonId = () => document.getElementById('exerciseState')?.dataset.lessonId || '';

const formatDuration = (seconds) => {
    const value = Math.max(0, Math.floor(seconds));
    const minutes = String(Math.floor(value / 60)).padStart(2, '0');
    const secs = String(value % 60).padStart(2, '0');
    return `${minutes}:${secs}`;
};

const getMediaDuration = (file) => new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const media = document.createElement(file.type.startsWith('video/') ? 'video' : 'audio');
    media.preload = 'metadata';
    media.onloadedmetadata = () => {
        const duration = Number.isFinite(media.duration) ? media.duration : 0;
        URL.revokeObjectURL(url);
        resolve(duration);
    };
    media.onerror = () => {
        URL.revokeObjectURL(url);
        reject(new Error('Could not read media duration'));
    };
    media.src = url;
});

const validateMediaDuration = async (file, mediaType, maxSeconds) => {
    const duration = await getMediaDuration(file);
    // WebM metadata may report Infinity/0 in some browsers. Do not reject a valid recording in that case.
    if (Number.isFinite(duration) && duration > 0 && duration > maxSeconds) {
        throw new Error(mediaType === 'audio'
            ? `Audio must be no longer than ${maxSeconds} seconds.`
            : `Video must be no longer than ${maxSeconds} seconds.`);
    }
    return duration;
};

const uploadMediaFile = async (file, mediaType) => {
    const formData = new FormData();
    const suggestedName = mediaType === 'audio' ? 'recording.webm' : 'recording.webm';
    const uploadName = file.name && file.name !== 'blob' ? file.name : suggestedName;
    formData.append('file', file, uploadName);
    formData.append('media_type', mediaType);
    const lessonId = getLessonId();
    if (lessonId) {
        formData.append('lesson_id', lessonId);
    }

try {
        const response = await fetch('/api/upload_media', {
            method: 'POST',
            body: formData
        });
        const data = await response.json();
        if (data.status === 'ok') {
            if (mediaType === 'audio') {
                latestMediaUrls.audio = data.drive_url || data.local_url || '';
            } else {
                latestMediaUrls.video = data.drive_url || data.local_url || '';
            }
            latestDriveFileIds[mediaType] = data.drive_file_id || '';
            showStatusMessage(data.message || 'Media saved successfully.', 'success');
            return data;
        }
        showStatusMessage(data.message || 'Upload failed.', 'warning');
        return null;
    } catch (error) {
        showStatusMessage('Upload failed. Please try again.', 'warning');
        return null;
    }
};

const attachTaskRecorder = (mediaType, buttonId, completeBtnId, uploadBtnId, uploadInputId, timerId, statusId, previewId, livePreviewId, maxSeconds, label) => {
    const recordBtn = document.getElementById(buttonId);
    const completeBtn = document.getElementById(completeBtnId);
    const uploadBtn = document.getElementById(uploadBtnId);
    const inputEl = document.getElementById(uploadInputId);
    const timerEl = document.getElementById(timerId);
    const statusEl = document.getElementById(statusId);
    const previewEl = document.getElementById(previewId);
    const livePreviewEl = livePreviewId ? document.getElementById(livePreviewId) : null;
    if (!recordBtn || !completeBtn || !timerEl || !statusEl || !previewEl) return;

    let mediaRecorder = null;
    let stream = null;
    let startTime = 0;
    let pendingRecording = null;
    let previewObjectUrl = '';
    let timerInterval = null;
    let recordedChunks = [];
    const recordIconHtml = '<span class="button-icon outline-button-icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="8"></circle><circle cx="12" cy="12" r="3" fill="currentColor"></circle></svg></span>';

    const stopTimer = () => {
        if (timerInterval) {
            clearInterval(timerInterval);
            timerInterval = null;
        }
    };

    const updateTimer = () => {
        const elapsed = Math.min((Date.now() - startTime) / 1000, maxSeconds);
        timerEl.hidden = false;
        timerEl.textContent = `${formatDuration(elapsed)} / ${formatDuration(maxSeconds)}`;
    };

    const resetRecordingState = () => {
        stopTimer();
        recordBtn.disabled = false;
        recordBtn.innerHTML = `${recordIconHtml}<span>${label}</span>`;
        completeBtn.hidden = true;
        timerEl.hidden = true;
        recordedChunks = [];
        pendingRecording = null;
        pendingExerciseMedia[mediaType] = null;
        previewEl.hidden = true;
        previewEl.srcObject = null;
        previewEl.removeAttribute('src');
        previewEl.load();
        if (previewObjectUrl) {
            URL.revokeObjectURL(previewObjectUrl);
            previewObjectUrl = '';
        }
        if (livePreviewEl) livePreviewEl.hidden = true;
        if (stream) {
            stream.getTracks().forEach((track) => track.stop());
            stream = null;
        }
    };

    const completeAndPreparePreview = async () => {
        if (!mediaRecorder || mediaRecorder.state === 'inactive') return;

        stopTimer();
        recordBtn.disabled = true;
        recordBtn.textContent = 'Preparing preview...';
        completeBtn.hidden = true;

        try {
            const stopped = new Promise((resolve) => {
                mediaRecorder.addEventListener('stop', resolve, { once: true });
            });
            mediaRecorder.stop();
            await stopped;

            const elapsedSeconds = (Date.now() - startTime) / 1000;
            if (elapsedSeconds < 0.5) throw new Error('Record for at least half a second.');

            const blobType = mediaRecorder.mimeType || (mediaType === 'audio' ? 'audio/webm' : 'video/webm');
            pendingRecording = new Blob(recordedChunks, { type: blobType });
            if (!pendingRecording.size) throw new Error('No recording data was captured. Please try again.');

            if (previewObjectUrl) URL.revokeObjectURL(previewObjectUrl);
            previewObjectUrl = URL.createObjectURL(pendingRecording);
            previewEl.srcObject = null;
            previewEl.src = previewObjectUrl;
            previewEl.hidden = false;
            previewEl.muted = mediaType === 'audio' ? false : true;
            previewEl.controls = true;

            pendingExerciseMedia[mediaType] = pendingRecording;
            statusEl.hidden = false;
            statusEl.textContent = 'Preview ready — it will be uploaded when you Submit';
            statusEl.classList.remove('success', 'warning');
            showStatusMessage('Recording preview ready. Upload only after you review it.', 'success');
        } catch (error) {
            showStatusMessage(error.message || 'Could not prepare recording preview.', 'warning');
            statusEl.hidden = false;
            statusEl.textContent = 'Recording failed';
            statusEl.classList.remove('success');
            statusEl.classList.add('warning');
        } finally {
            if (stream) {
                stream.getTracks().forEach((track) => track.stop());
                stream = null;
            }
            mediaRecorder = null;
            recordedChunks = [];
            recordBtn.disabled = false;
            recordBtn.innerHTML = `${recordIconHtml}<span>Record again</span>`;
            timerEl.hidden = true;
            completeBtn.hidden = true;
        }
    };

    recordBtn.addEventListener('click', async () => {
        if (mediaRecorder && mediaRecorder.state !== 'inactive') {
            return;
        }

        try {
            stream = await navigator.mediaDevices.getUserMedia(mediaType === 'audio' ? { audio: true } : { audio: true, video: true });
            const mimeType = mediaType === 'audio'
                ? 'audio/webm'
                : (MediaRecorder.isTypeSupported('video/webm;codecs=vp9') ? 'video/webm;codecs=vp9' : 'video/webm');

            recordedChunks = [];
            pendingRecording = null;
            pendingExerciseMedia[mediaType] = null;
            previewEl.hidden = true;
            previewEl.srcObject = null;
            previewEl.removeAttribute('src');
            if (livePreviewEl && mediaType === 'video') {
                previewEl.srcObject = stream;
                previewEl.hidden = false;
                previewEl.controls = false;
                previewEl.muted = true;
            }
            mediaRecorder = new MediaRecorder(stream, { mimeType });
            mediaRecorder.ondataavailable = (event) => {
                if (event.data && event.data.size > 0) {
                    recordedChunks.push(event.data);
                }
            };
            mediaRecorder.onstop = async () => {
                if (recordedChunks.length === 0) {
                    resetRecordingState();
                    return;
                }
            };

            startTime = Date.now();
            timerEl.hidden = false;
            updateTimer();
            timerInterval = setInterval(updateTimer, 250);
            recordBtn.disabled = false;
            recordBtn.innerHTML = '<span class="button-icon outline-button-icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="17" height="17" fill="currentColor"><circle cx="12" cy="12" r="7"></circle></svg></span><span>Recording</span>';
            completeBtn.hidden = false;
            statusEl.hidden = true;
            mediaRecorder.start();

            setTimeout(() => {
                if (mediaRecorder && mediaRecorder.state !== 'inactive' && !completeBtn.hidden) {
                    completeAndPreparePreview();
                }
            }, maxSeconds * 1000);
        } catch (error) {
            showStatusMessage('Could not access your microphone or camera. Please allow device access.', 'warning');
        }
    });

    completeBtn.addEventListener('click', async () => {
        await completeAndPreparePreview();
    });

    if (inputEl) {
        inputEl.addEventListener('change', async (event) => {
            const file = event.target.files?.[0];
            if (!file) return;
            try {
                await validateMediaDuration(file, mediaType, maxSeconds);
                pendingRecording = file;
                pendingExerciseMedia[mediaType] = file;
                if (previewObjectUrl) URL.revokeObjectURL(previewObjectUrl);
                previewObjectUrl = URL.createObjectURL(file);
                previewEl.srcObject = null;
                previewEl.src = previewObjectUrl;
                previewEl.hidden = false;
                previewEl.controls = true;
                statusEl.hidden = false;
                statusEl.textContent = 'File ready — it will be uploaded when you Submit';
                statusEl.classList.remove('success', 'warning');
            } catch (error) {
                showStatusMessage(error.message || 'This media file is not valid.', 'warning');
            } finally {
                inputEl.value = '';
            }
        });
    }
};

attachTaskRecorder('audio', 'recordAudioBtn', 'completeAudioBtn', 'uploadAudioTaskBtn', 'audioTaskUpload', 'audioTimer', 'audioStatus', 'audioPreview', '', 60, 'Record audio');
attachTaskRecorder('video', 'recordVideoBtn', 'completeVideoBtn', 'uploadVideoTaskBtn', 'videoTaskUpload', 'videoTimer', 'videoStatus', 'videoPreview', 'videoPreview', 30, 'Record video');

if (uploadAudioBtn && audioUpload) {
    uploadAudioBtn.addEventListener('click', () => audioUpload.click());

    audioUpload.addEventListener('change', async (event) => {
        const file = event.target.files[0];
        if (!file) return;

        const formData = new FormData();
        formData.append('file', file);
        formData.append('media_type', 'audio');
        const lessonId = getLessonId();
        if (lessonId) {
            formData.append('lesson_id', lessonId);
        }

        try {
            const response = await fetch('/api/upload_media', {
                method: 'POST',
                body: formData
            });
            const data = await response.json();
            if (data.status === 'ok') {
                const message = data.message || 'File đã lưu local';
                showStatusMessage(message, data.drive_url ? 'success' : 'warning');
                answerInput.value = message;
                if (data.drive_url) {
                    console.log('Google Drive link:', data.drive_url);
                }
            }
        } catch (error) {
            console.error('Upload failed', error);
            showStatusMessage('Lưu lên Google Drive thất bại. File đã lưu local.', 'warning');
        }
    });
}

if (evaluateBtn) {
    evaluateBtn.addEventListener('click', async () => {
        const payload = {
            question: 'What is your name?',
            answer: answerInput.value.trim() || 'My name is Lan.',
            expected: 'My name is Lan.'
        };

        try {
            const response = await fetch('/api/evaluate_speaking', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(payload)
            });

            const data = await response.json();
            aiResult.innerHTML = `
                <strong>Score:</strong> ${data.score}%<br>
                <strong>Pronunciation:</strong> ${data.pronunciation}/10<br>
                <strong>Grammar:</strong> ${data.grammar}/10<br>
                <strong>Fluency:</strong> ${data.fluency}/10<br>
                <strong>Feedback:</strong> ${data.feedback}
            `;
        } catch (error) {
            aiResult.textContent = 'Evaluation failed. Please try again.';
        }
    });
}

const dashboardClassGrid = document.getElementById('dashboard-class-grid');

if (dashboardClassGrid) {
    const classCards = Array.from(dashboardClassGrid.querySelectorAll('.dashboard-class-card'));
    const searchInput = document.getElementById('dashboard-class-search');
    const countLabel = document.getElementById('dashboard-class-count');
    const pageLabel = document.getElementById('dashboard-class-page');
    const previousButton = document.getElementById('dashboard-class-prev');
    const nextButton = document.getElementById('dashboard-class-next');
    const emptyMessage = document.getElementById('dashboard-class-empty');
    const pageSize = Number(dashboardClassGrid.dataset.pageSize) || 8;
    let currentPage = 1;

    const renderClassPage = () => {
        const query = (searchInput?.value || '').trim().toLocaleLowerCase();
        const filteredCards = classCards.filter((card) =>
            (card.dataset.className || '').toLocaleLowerCase().includes(query)
        );
        const pageCount = Math.max(1, Math.ceil(filteredCards.length / pageSize));
        currentPage = Math.min(currentPage, pageCount);
        const firstIndex = (currentPage - 1) * pageSize;
        const visibleCards = new Set(filteredCards.slice(firstIndex, firstIndex + pageSize));

        classCards.forEach((card) => {
            card.hidden = !visibleCards.has(card);
        });

        if (countLabel) {
            const first = filteredCards.length ? firstIndex + 1 : 0;
            const last = Math.min(firstIndex + pageSize, filteredCards.length);
            countLabel.textContent = `Showing ${first}-${last} of ${filteredCards.length} classes`;
        }
        if (pageLabel) {
            pageLabel.textContent = `${currentPage} / ${pageCount}`;
        }
        if (previousButton) {
            previousButton.disabled = currentPage <= 1;
        }
        if (nextButton) {
            nextButton.disabled = currentPage >= pageCount;
        }
        if (emptyMessage) {
            emptyMessage.hidden = filteredCards.length > 0;
            emptyMessage.textContent = classCards.length
                ? 'No classes match your search.'
                : 'No classes are available yet.';
        }
    };

    searchInput?.addEventListener('input', () => {
        currentPage = 1;
        renderClassPage();
    });
    previousButton?.addEventListener('click', () => {
        currentPage -= 1;
        renderClassPage();
    });
    nextButton?.addEventListener('click', () => {
        currentPage += 1;
        renderClassPage();
    });
    renderClassPage();
}

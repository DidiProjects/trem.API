let API_BASE_URL = '';

document.addEventListener('DOMContentLoaded', async function() {
    await loadConfig();
    
    const savedApiKey = localStorage.getItem('apiKey');
    if (savedApiKey) {
        document.getElementById('apiKey').value = savedApiKey;
    }

    document.getElementById('apiKey').addEventListener('change', function() {
        localStorage.setItem('apiKey', this.value);
    });

    setupMainTabs();
    setupFormHandlers();
    setupAiChat();
});

async function loadConfig() {
    try {
        const response = await fetch('/config');
        const config = await response.json();
        API_BASE_URL = config.api_url;
    } catch {
        API_BASE_URL = window.location.origin;
    }
    
    document.getElementById('baseUrlDisplay').textContent = API_BASE_URL;
    document.querySelectorAll('.base-url').forEach(el => {
        el.textContent = API_BASE_URL;
    });
}

function setupMainTabs() {
    const mainTabs = document.querySelectorAll('.main-tab');
    const sections = document.querySelectorAll('.section-content');
    const mobileSelect = document.getElementById('mobileTabSelect');

    function switchToSection(sectionName) {
        mainTabs.forEach(t => t.classList.remove('active'));
        sections.forEach(s => s.classList.remove('active'));
        
        const activeTab = document.querySelector(`.main-tab[data-section="${sectionName}"]`);
        if (activeTab) activeTab.classList.add('active');
        
        const activeSection = document.getElementById(`section-${sectionName}`);
        if (activeSection) activeSection.classList.add('active');
        
        if (mobileSelect) mobileSelect.value = sectionName;
        document.dispatchEvent(new CustomEvent('sectionchange', { detail: sectionName }));
    }

    mainTabs.forEach(tab => {
        tab.addEventListener('click', () => {
            switchToSection(tab.dataset.section);
        });
    });

    if (mobileSelect) {
        mobileSelect.addEventListener('change', () => {
            switchToSection(mobileSelect.value);
        });
    }
}

function toggleEndpoint(header) {
    const card = header.parentElement;
    card.classList.toggle('open');
}

function toggleApiKeyVisibility() {
    const input = document.getElementById('apiKey');
    input.type = input.type === 'password' ? 'text' : 'password';
}

function getApiKey() {
    const apiKey = document.getElementById('apiKey').value;
    if (!apiKey) {
        showToast('Please enter the API Key', 'error');
        return null;
    }
    return apiKey;
}

function showLoading(message = 'Processing...') {
    setLoadingMessage(message);
    document.getElementById('loading').classList.remove('hidden');
}

function setLoadingMessage(message) {
    document.querySelector('#loading p').textContent = message;
}

function hideLoading() {
    document.getElementById('loading').classList.add('hidden');
}

function showToast(message, type = 'success') {
    const toast = document.getElementById('toast');
    toast.textContent = message;
    toast.className = `toast ${type}`;
    toast.classList.remove('hidden');
    
    setTimeout(() => {
        toast.classList.add('hidden');
    }, 3000);
}

function downloadBlob(blob, filename) {
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    window.URL.revokeObjectURL(url);
    document.body.removeChild(a);
}

function formatBytes(bytes) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

function getFilenameFromResponse(response, fallback) {
    const disposition = response.headers.get('Content-Disposition');
    if (disposition) {
        let match = disposition.match(/filename\*?=(?:UTF-8'')?([^;\s]+)/i);
        if (match) {
            let filename = match[1].replace(/['"]/g, '');
            try {
                filename = decodeURIComponent(filename);
            } catch (e) {}
            return filename;
        }
    }
    return fallback;
}

const STATUS_MESSAGES = {
    413: 'Files are too large for this operation',
    429: 'Too many requests, try again in a minute',
    502: 'Server unavailable, try again shortly',
    503: 'Server unavailable, try again shortly',
    504: 'The server took too long to respond'
};

// Errors from the proxy or Cloudflare come as HTML, not the API's JSON.
async function getErrorMessage(response) {
    const fallback = STATUS_MESSAGES[response.status] || `Request error (${response.status})`;
    try {
        const error = await response.json();
        return typeof error.detail === 'string' ? error.detail : fallback;
    } catch (e) {
        return fallback;
    }
}

async function makeRequest(endpoint, formData) {
    const apiKey = getApiKey();
    if (!apiKey) return null;

    showLoading();
    
    try {
        const response = await fetch(endpoint, {
            method: 'POST',
            headers: {
                'X-API-Key': apiKey
            },
            body: formData
        });

        if (!response.ok) {
            throw new Error(await getErrorMessage(response));
        }

        return response;
    } catch (error) {
        showToast(error.message, 'error');
        hideLoading();
        return null;
    }
}

// Cloudflare drops requests over 100 MB and responses slower than 100 s, so
// files go up in parts and the transcription result is polled.
const TRANSCRIBE_POLL_MS = 3000;
const REQUEST_RETRIES = 3;

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function apiFetch(endpoint, options = {}) {
    let response;
    try {
        response = await fetch(endpoint, {
            ...options,
            headers: { 'X-API-Key': getApiKey(), ...options.headers }
        });
    } catch (error) {
        throw Object.assign(new Error('Connection lost, check your network'), { retryable: true });
    }
    if (!response.ok) {
        const error = new Error(await getErrorMessage(response));
        error.retryable = response.status >= 500 || response.status === 429;
        throw error;
    }
    return response;
}

async function withRetries(request) {
    for (let attempt = 1; ; attempt++) {
        try {
            return await request();
        } catch (error) {
            if (!error.retryable || attempt >= REQUEST_RETRIES) throw error;
            await sleep(2000 * attempt);
        }
    }
}

async function uploadForTranscription(prefix, file, language) {
    const upload = await (await withRetries(() => apiFetch(`${prefix}/transcribe/uploads`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: file.name, size: file.size, language: language || null })
    }))).json();

    const base = `${prefix}/transcribe/uploads/${upload.upload_id}`;
    try {
        for (let index = 0; index < upload.total_parts; index++) {
            const start = index * upload.part_size;
            const part = file.slice(start, start + upload.part_size);
            await withRetries(() => apiFetch(`${base}/parts/${index}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/octet-stream' },
                body: part
            }));
            setLoadingMessage(`Uploading... ${Math.round((index + 1) / upload.total_parts * 100)}%`);
        }
        const job = await (await withRetries(() => apiFetch(`${base}/complete`, { method: 'POST' }))).json();
        return job.job_id;
    } catch (error) {
        apiFetch(base, { method: 'DELETE' }).catch(() => {});
        throw error;
    }
}

async function waitForTranscription(prefix, jobId) {
    while (true) {
        await sleep(TRANSCRIBE_POLL_MS);
        const job = await (await withRetries(() => apiFetch(`${prefix}/transcribe/jobs/${jobId}`))).json();
        if (job.status === 'done') return job.result;
        if (job.status === 'failed') throw new Error(job.error);
        setLoadingMessage(job.status === 'queued' ? 'Waiting in queue...' : 'Transcribing...');
    }
}

function setupTranscribeForm({ formId, fileId, languageId, resultId, prefix, successMessage }) {
    document.getElementById(formId).addEventListener('submit', async (e) => {
        e.preventDefault();
        if (!getApiKey()) return;

        const file = document.getElementById(fileId).files[0];
        const language = document.getElementById(languageId).value;

        showLoading('Uploading... 0%');
        try {
            const jobId = await uploadForTranscription(prefix, file, language);
            setLoadingMessage('Waiting in queue...');
            const result = await waitForTranscription(prefix, jobId);
            const resultBox = document.getElementById(resultId);
            resultBox.innerHTML = `<pre>${JSON.stringify(result, null, 2)}</pre>`;
            resultBox.classList.remove('hidden');
            showToast(successMessage);
        } catch (error) {
            showToast(error.message, 'error');
        } finally {
            hideLoading();
        }
    });
}

function setupFormHandlers() {
    // PDF Split Form
    document.getElementById('splitForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const formData = new FormData();
        formData.append('file', document.getElementById('splitFile').files[0]);
        formData.append('pages', document.getElementById('splitPages').value);

        const response = await makeRequest('/pdf/split', formData);
        if (response) {
            const filename = getFilenameFromResponse(response, 'result.pdf');
            const blob = await response.blob();
            hideLoading();
            downloadBlob(blob, filename);
            showToast('PDF split successfully!');
        }
    });

    // Video Cut Form
    document.getElementById('cutMovieForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const formData = new FormData();
        formData.append('file', document.getElementById('cutMovieFile').files[0]);
        formData.append('start', document.getElementById('cutMovieStart').value);
        formData.append('end', document.getElementById('cutMovieEnd').value);

        const response = await makeRequest('/movie/cut', formData);
        if (response) {
            const filename = getFilenameFromResponse(response, 'recorte.mp4');
            const blob = await response.blob();
            hideLoading();
            downloadBlob(blob, filename);
            showToast('Vídeo recortado com sucesso!');
        }
    });

    setupTranscribeForm({
        formId: 'transcribeMovieForm',
        fileId: 'transcribeMovieFile',
        languageId: 'transcribeMovieLanguage',
        resultId: 'transcribeMovieResult',
        prefix: '/movie',
        successMessage: 'Transcrição do vídeo concluída!'
    });

    setupTranscribeForm({
        formId: 'transcribeForm',
        fileId: 'transcribeFile',
        languageId: 'transcribeLanguage',
        resultId: 'transcribeResult',
        prefix: '/audio',
        successMessage: 'Transcrição concluída!'
    });

    // Audio Cut Form
    document.getElementById('cutAudioForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const formData = new FormData();
        formData.append('file', document.getElementById('cutAudioFile').files[0]);
        formData.append('start', document.getElementById('cutAudioStart').value);
        formData.append('end', document.getElementById('cutAudioEnd').value);

        const response = await makeRequest('/audio/cut', formData);
        if (response) {
            const filename = getFilenameFromResponse(response, 'recorte.mp3');
            const blob = await response.blob();
            hideLoading();
            downloadBlob(blob, filename);
            showToast('Áudio recortado com sucesso!');
        }
    });

    document.getElementById('extractForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const formData = new FormData();
        formData.append('file', document.getElementById('extractFile').files[0]);

        const response = await makeRequest('/pdf/extract-pages', formData);
        if (response) {
            const filename = getFilenameFromResponse(response, 'pages.zip');
            const blob = await response.blob();
            hideLoading();
            downloadBlob(blob, filename);
            showToast('Pages extracted successfully!');
        }
    });

    document.getElementById('mergeForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const files = document.getElementById('mergeFiles').files;
        
        if (files.length < 2) {
            showToast('Select at least 2 files', 'error');
            return;
        }

        const formData = new FormData();
        for (let i = 0; i < files.length; i++) {
            formData.append('files', files[i]);
        }

        const response = await makeRequest('/pdf/merge', formData);
        if (response) {
            const filename = getFilenameFromResponse(response, 'merged.pdf');
            const blob = await response.blob();
            hideLoading();
            downloadBlob(blob, filename);
            showToast('PDFs merged successfully!');
        }
    });

    document.getElementById('addPasswordForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const formData = new FormData();
        formData.append('file', document.getElementById('addPwdFile').files[0]);
        formData.append('user_password', document.getElementById('userPassword').value);
        
        const ownerPwd = document.getElementById('ownerPassword').value;
        if (ownerPwd) {
            formData.append('owner_password', ownerPwd);
        }

        const response = await makeRequest('/pdf/add-password', formData);
        if (response) {
            const filename = getFilenameFromResponse(response, 'protected.pdf');
            const blob = await response.blob();
            hideLoading();
            downloadBlob(blob, filename);
            showToast('Password added successfully!');
        }
    });

    document.getElementById('removePasswordForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const formData = new FormData();
        formData.append('file', document.getElementById('removePwdFile').files[0]);
        formData.append('password', document.getElementById('currentPassword').value);

        const response = await makeRequest('/pdf/remove-password', formData);
        if (response) {
            const filename = getFilenameFromResponse(response, 'unlocked.pdf');
            const blob = await response.blob();
            hideLoading();
            downloadBlob(blob, filename);
            showToast('Password removed successfully!');
        }
    });

    document.getElementById('infoForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const formData = new FormData();
        formData.append('file', document.getElementById('infoFile').files[0]);

        const response = await makeRequest('/pdf/info', formData);
        if (response) {
            const data = await response.json();
            hideLoading();
            const resultBox = document.getElementById('infoResult');
            resultBox.innerHTML = `<pre>${JSON.stringify(data, null, 2)}</pre>`;
            resultBox.classList.remove('hidden');
            showToast('Information retrieved successfully!');
        }
    });

    document.getElementById('convertImageForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const formData = new FormData();
        formData.append('file', document.getElementById('convertImageFile').files[0]);
        formData.append('format', document.getElementById('convertImageFormat').value);
        formData.append('dpi', document.getElementById('convertImageDpi').value);
        
        const pages = document.getElementById('convertImagePages').value;
        if (pages) {
            formData.append('pages', pages);
        }

        const response = await makeRequest('/pdf/convert-to-image', formData);
        if (response) {
            const contentType = response.headers.get('Content-Type');
            const blob = await response.blob();
            hideLoading();
            
            let filename;
            if (contentType.includes('zip')) {
                filename = getFilenameFromResponse(response, 'images.zip');
            } else {
                const format = document.getElementById('convertImageFormat').value;
                const ext = format === 'jpeg' ? 'jpg' : format;
                filename = getFilenameFromResponse(response, `image.${ext}`);
            }
            
            downloadBlob(blob, filename);
            showToast('PDF converted to image successfully!');
        }
    });

    document.getElementById('convertOfxForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const formData = new FormData();
        formData.append('file', document.getElementById('convertOfxFile').files[0]);
        formData.append('bank_id', document.getElementById('convertOfxBankId').value);
        formData.append('account_id', document.getElementById('convertOfxAccountId').value);
        formData.append('account_type', document.getElementById('convertOfxAccountType').value);

        const response = await makeRequest('/pdf/convert-to-ofx', formData);
        if (response) {
            const filename = getFilenameFromResponse(response, 'statement.ofx');
            const blob = await response.blob();
            hideLoading();
            downloadBlob(blob, filename);
            showToast('PDF converted to OFX successfully!');
        }
    });

    document.getElementById('imageToPdfLayout').addEventListener('change', function() {
        const imagesPerPageGroup = document.getElementById('imagesPerPageGroup');
        imagesPerPageGroup.style.display = this.value === 'grouped' ? 'block' : 'none';
    });

    document.getElementById('imageToPdfForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const files = document.getElementById('imageToPdfFiles').files;
        
        if (files.length === 0) {
            showToast('Select at least one image', 'error');
            return;
        }

        const formData = new FormData();
        for (let i = 0; i < files.length; i++) {
            formData.append('files', files[i]);
        }
        formData.append('layout', document.getElementById('imageToPdfLayout').value);
        formData.append('images_per_page', document.getElementById('imagesPerPage').value);

        const response = await makeRequest('/image/to-pdf', formData);
        if (response) {
            const filename = getFilenameFromResponse(response, 'images.pdf');
            const blob = await response.blob();
            hideLoading();
            downloadBlob(blob, filename);
            showToast('Images converted to PDF successfully!');
        }
    });

    document.getElementById('imageConvertForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const formData = new FormData();
        formData.append('file', document.getElementById('imageConvertFile').files[0]);
        formData.append('format', document.getElementById('imageConvertFormat').value);
        formData.append('quality', document.getElementById('imageConvertQuality').value);

        const response = await makeRequest('/image/convert', formData);
        if (response) {
            const filename = getFilenameFromResponse(response, 'converted.jpg');
            const blob = await response.blob();
            hideLoading();
            downloadBlob(blob, filename);
            showToast('Image converted successfully!');
        }
    });

    document.getElementById('imageCompressQuality').addEventListener('input', function() {
        document.getElementById('qualityValue').textContent = this.value;
    });

    document.getElementById('imageCompressForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const formData = new FormData();
        formData.append('file', document.getElementById('imageCompressFile').files[0]);
        formData.append('quality', document.getElementById('imageCompressQuality').value);
        formData.append('response_type', 'json');
        
        const maxDim = document.getElementById('imageCompressMaxDim').value;
        if (maxDim) {
            formData.append('max_dimension', maxDim);
        }

        const response = await makeRequest('/image/compress', formData);
        if (response) {
            const data = await response.json();
            hideLoading();
            
            const metrics = data.metrics;
            const file = data.file;
            
            // Update metrics display
            const metricsBox = document.getElementById('compressMetrics');
            metricsBox.classList.remove('hidden');
            
            document.getElementById('metricOriginalSize').textContent = formatBytes(metrics.original_size_bytes);
            document.getElementById('metricCompressedSize').textContent = formatBytes(metrics.compressed_size_bytes);
            document.getElementById('metricReduction').textContent = `${metrics.reduction_percent}%`;
            document.getElementById('metricOriginalDim').textContent = `${metrics.original_dimensions.width} × ${metrics.original_dimensions.height}`;
            document.getElementById('metricFinalDim').textContent = `${metrics.final_dimensions.width} × ${metrics.final_dimensions.height}`;
            
            // Download file from base64
            const byteCharacters = atob(file.base64);
            const byteNumbers = new Array(byteCharacters.length);
            for (let i = 0; i < byteCharacters.length; i++) {
                byteNumbers[i] = byteCharacters.charCodeAt(i);
            }
            const byteArray = new Uint8Array(byteNumbers);
            const blob = new Blob([byteArray], { type: file.media_type });
            
            downloadBlob(blob, file.filename);
            showToast('Image compressed successfully!');
        }
    });

    // Feedback form handlers
    const feedbackMessage = document.getElementById('feedbackMessage');
    const charCount = document.getElementById('charCount');
    
    if (feedbackMessage && charCount) {
        feedbackMessage.addEventListener('input', function() {
            charCount.textContent = this.value.length;
        });
    }

    document.getElementById('feedbackForm')?.addEventListener('submit', async (e) => {
        e.preventDefault();
        
        const formData = new FormData();
        formData.append('type', document.getElementById('feedbackType').value);
        formData.append('message', document.getElementById('feedbackMessage').value);
        
        const email = document.getElementById('feedbackEmail').value;
        if (email) {
            formData.append('email', email);
        }

        const response = await makeRequest('/support/feedback', formData);
        if (response) {
            const data = await response.json();
            hideLoading();
            
            if (data.success) {
                document.getElementById('feedbackForm').classList.add('hidden');
                document.getElementById('feedbackSuccess').classList.remove('hidden');
                showToast('Feedback sent successfully!', 'success');
            }
        }
    });
}

function resetFeedbackForm() {
    document.getElementById('feedbackForm').reset();
    document.getElementById('charCount').textContent = '0';
    document.getElementById('feedbackForm').classList.remove('hidden');
    document.getElementById('feedbackSuccess').classList.add('hidden');
}

// --- AI chat ---
// Answers go through /llm/jobs and are polled: Cloudflare cuts responses at
// 100 s, and a sleeping model alone takes ~3.5 min to reload.
const AI_POLL_MS = 2500;
const AI_HEALTH_MS = 20000;
const AI_WARMUP_SECONDS = 210;  // LLM_WARMUP_SECONDS on the server
const AI_MAX_BODY = 60000;      // the server rejects /llm bodies over 64 KB
const AI_STORAGE_KEY = 'aiChats';

const ai = {
    chats: [],
    currentId: null,
    status: 'unknown',
    warmingSince: null,
    waking: false,
    visible: false,
    pollers: new Set()
};

const AI_STATUS_LABELS = {
    unknown: 'checking…',
    healthy: 'ready',
    sleeping: 'asleep',
    warming_up: 'waking up',
    offline: 'offline',
    unauthorized: 'invalid API key',
    nokey: 'enter your API key'
};

function setupAiChat() {
    ai.chats = aiLoadChats();
    ai.currentId = ai.chats[0]?.id || null;

    const input = document.getElementById('aiInput');
    document.getElementById('aiComposer').addEventListener('submit', e => {
        e.preventDefault();
        aiCurrent()?.pending ? aiStop() : aiSend();
    });
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
            e.preventDefault();
            if (!aiCurrent()?.pending) aiSend();
        }
    });
    input.addEventListener('input', () => {
        aiAutoGrow(input);
        aiRenderSize();
        // Waking takes minutes: start while the user is still typing.
        if (ai.status === 'sleeping') aiWake();
    });
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && ai.visible) aiStop();
    });
    document.getElementById('aiNewChat').addEventListener('click', aiNewChat);
    document.getElementById('aiDeleteChat').addEventListener('click', aiDeleteChat);
    document.getElementById('aiWakeBtn').addEventListener('click', aiWake);
    document.getElementById('aiSidebarToggle').addEventListener('click', () => {
        document.getElementById('aiSidebar').classList.toggle('open');
    });
    document.getElementById('aiChatList').addEventListener('click', aiOnChatListClick);
    document.getElementById('aiMessages').addEventListener('click', aiOnMessagesClick);
    document.getElementById('apiKey').addEventListener('change', aiRefreshHealth);

    document.addEventListener('sectionchange', e => {
        ai.visible = e.detail === 'ai';
        if (ai.visible) {
            aiRefreshHealth();
            input.focus();
        }
    });
    setInterval(() => {
        if (ai.visible || ai.pollers.size) aiRefreshHealth();
    }, AI_HEALTH_MS);
    setInterval(aiTick, 1000);

    // Jobs outlive the page: resume the answers that were pending on reload.
    ai.chats.filter(chat => chat.pending && !chat.pending.jobId)
        .forEach(chat => aiFail(chat, 'The page closed before the message was sent. Retry to ask again.'));
    aiRender();
    ai.chats.filter(chat => chat.pending?.jobId).forEach(aiPoll);
}

function aiLoadChats() {
    try {
        const chats = JSON.parse(localStorage.getItem(AI_STORAGE_KEY));
        return Array.isArray(chats) ? chats : [];
    } catch {
        return [];
    }
}

function aiSaveChats() {
    try {
        localStorage.setItem(AI_STORAGE_KEY, JSON.stringify(ai.chats));
    } catch {
        showToast('Could not save the chat history in this browser', 'error');
    }
}

function aiCurrent() {
    return ai.chats.find(chat => chat.id === ai.currentId) || null;
}

function aiNewId() {
    return window.crypto?.randomUUID?.() || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
}

// -- conversation flow ---------------------------------------------------

function aiSend() {
    const input = document.getElementById('aiInput');
    const text = input.value.trim();
    if (!text || !getApiKey()) return;

    let chat = aiCurrent();
    if (!chat) {
        chat = { id: aiNewId(), title: text.slice(0, 60), updatedAt: Date.now(), messages: [], pending: null };
        ai.chats.push(chat);
        ai.currentId = chat.id;
    }
    chat.messages.push({ role: 'user', content: text });
    input.value = '';
    aiAutoGrow(input);
    aiAsk(chat);
}

function aiContext(chat, draft = '') {
    const messages = chat ? chat.messages
        .filter(m => m.role === 'user' || m.role === 'assistant')
        .map(m => ({ role: m.role, content: m.content })) : [];
    if (draft) messages.push({ role: 'user', content: draft });
    const size = () => new TextEncoder().encode(JSON.stringify({ messages })).length;
    const fullSize = size();
    while (messages.length > 1 && size() > AI_MAX_BODY) messages.shift();
    while (messages.length > 1 && messages[0].role !== 'user') messages.shift();
    return { messages, fullSize, fits: size() <= AI_MAX_BODY, trimmed: size() < fullSize };
}

async function aiAsk(chat) {
    chat.messages = chat.messages.filter(m => m.role !== 'error');
    const context = aiContext(chat);
    if (!context.fits) {
        aiFail(chat, 'This message is too long: the server accepts up to 64 KB per request.');
        return;
    }
    chat.pending = { jobId: null, status: 'sending', since: Date.now() };
    chat.updatedAt = Date.now();
    aiSaveChats();
    aiRender();
    try {
        const job = await (await apiFetch('/llm/jobs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ messages: context.messages })
        })).json();
        if (!chat.pending) return;  // stopped while sending
        chat.pending.jobId = job.job_id;
        chat.pending.status = job.status;
        if (ai.status === 'sleeping') aiSetStatus('warming_up');  // the job wakes the model
        aiSaveChats();
        aiPoll(chat);
    } catch (error) {
        if (chat.pending) aiFail(chat, error.message);
    }
}

async function aiPoll(chat) {
    if (ai.pollers.has(chat.id)) return;
    ai.pollers.add(chat.id);
    try {
        while (chat.pending?.jobId && ai.chats.includes(chat)) {
            await sleep(AI_POLL_MS);
            const jobId = chat.pending?.jobId;
            if (!jobId) break;
            let job;
            try {
                job = await (await withRetries(() => apiFetch(`/llm/jobs/${jobId}`))).json();
            } catch (error) {
                if (chat.pending?.jobId === jobId) {
                    aiFail(chat, error.message === 'Job not found'
                        ? 'This answer was lost: the server restarted or it expired. Retry to ask again.'
                        : error.message);
                }
                break;
            }
            if (chat.pending?.jobId !== jobId) break;
            if (job.status === 'done') aiFinish(chat, job.result);
            else if (job.status === 'failed') aiFail(chat, job.error);
            else {
                chat.pending.status = job.status;
                aiRenderPending();
            }
        }
    } finally {
        ai.pollers.delete(chat.id);
    }
}

function aiFinish(chat, result) {
    const message = result?.choices?.[0]?.message || {};
    chat.messages.push({
        role: 'assistant',
        content: message.content || '',
        reasoning: message.reasoning_content || '',
        meta: {
            seconds: (Date.now() - chat.pending.since) / 1000,
            tokens: result?.usage?.completion_tokens,
            tps: result?.timings?.predicted_per_second
        }
    });
    chat.pending = null;
    chat.updatedAt = Date.now();
    aiSaveChats();
    aiRender();
    if (ai.status !== 'healthy') aiRefreshHealth();
    if (document.hidden || !ai.visible) showToast('The AI answered');
}

function aiFail(chat, message) {
    chat.pending = null;
    chat.messages.push({ role: 'error', content: message });
    aiSaveChats();
    aiRender();
}

function aiStop() {
    const chat = aiCurrent();
    if (!chat?.pending) return;
    // The server has no cancel: the job still runs and holds the single slot.
    aiFail(chat, 'Stopped waiting. The server still finishes this answer, so the next one may wait behind it.');
}

function aiRegenerate() {
    const chat = aiCurrent();
    if (!chat || chat.pending) return;
    while (chat.messages.length && chat.messages[chat.messages.length - 1].role !== 'user') {
        chat.messages.pop();
    }
    if (chat.messages.length) aiAsk(chat);
}

function aiNewChat() {
    ai.currentId = null;
    document.getElementById('aiSidebar').classList.remove('open');
    aiRender();
    document.getElementById('aiInput').focus();
}

function aiDeleteChat() {
    const chat = aiCurrent();
    const button = document.getElementById('aiDeleteChat');
    if (!chat) return;
    if (!button.dataset.armed) {
        button.dataset.armed = '1';
        button.textContent = 'Delete?';
        setTimeout(() => {
            delete button.dataset.armed;
            button.textContent = '🗑';
        }, 3000);
        return;
    }
    delete button.dataset.armed;
    button.textContent = '🗑';
    ai.chats = ai.chats.filter(c => c !== chat);
    ai.currentId = null;
    aiSaveChats();
    aiRender();
}

// -- model status --------------------------------------------------------

async function aiRefreshHealth() {
    const key = document.getElementById('apiKey').value;
    if (!key) return aiSetStatus('nokey');
    try {
        const response = await fetch('/llm/health', { headers: { 'X-API-Key': key } });
        if (response.status === 401 || response.status === 403) return aiSetStatus('unauthorized');
        if (!response.ok) return aiSetStatus('offline');
        aiSetStatus((await response.json()).status);
    } catch {
        aiSetStatus('offline');
    }
}

function aiSetStatus(status) {
    // A queued job wakes the model only when it reaches the slot; until then
    // /llm/health still says sleeping, but the wake is already on its way.
    const waiting = ai.waking || ai.chats.some(chat => chat.pending);
    if (status === 'sleeping' && waiting && ai.status === 'warming_up') status = 'warming_up';
    if (status === 'warming_up') ai.warmingSince = ai.warmingSince || Date.now();
    else ai.warmingSince = null;
    if (status !== 'warming_up') ai.waking = false;
    ai.status = status in AI_STATUS_LABELS ? status : 'unknown';
    aiRenderStatus();
}

async function aiWake() {
    if (ai.waking || ai.status !== 'sleeping' || !document.getElementById('apiKey').value) return;
    ai.waking = true;
    aiSetStatus('warming_up');
    try {
        await apiFetch('/llm/jobs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ messages: [{ role: 'user', content: 'ping' }], max_tokens: 1 })
        });
    } catch {
        ai.waking = false;
        aiRefreshHealth();
    }
}

// -- rendering -----------------------------------------------------------

function aiRender() {
    aiRenderSidebar();
    aiRenderMessages();
    aiRenderStatus();
    aiRenderSize();
}

function aiRenderSidebar() {
    const chats = [...ai.chats].sort((a, b) => b.updatedAt - a.updatedAt);
    document.getElementById('aiChatList').innerHTML = chats.map(chat => `
        <li class="ai-chat-item${chat.id === ai.currentId ? ' active' : ''}" data-id="${aiEscape(chat.id)}">
            ${chat.pending ? '<span class="ai-chat-busy"></span>' : ''}${aiEscape(chat.title)}
        </li>`).join('') || '<li class="ai-chat-empty">No chats yet</li>';
}

function aiRenderStatus() {
    document.getElementById('aiStatusDot').className = `ai-status-dot status-${ai.status}`;
    document.getElementById('aiStatusLabel').textContent = AI_STATUS_LABELS[ai.status];

    const banner = document.getElementById('aiWarmup');
    const asleep = ai.status === 'sleeping';
    banner.classList.toggle('hidden', !asleep && ai.status !== 'warming_up');
    document.getElementById('aiWakeBtn').classList.toggle('hidden', !asleep);
    if (asleep) {
        document.getElementById('aiWarmupText').textContent =
            'The model is asleep. Waking it takes about 3½ min, and starts as soon as you type.';
        document.getElementById('aiWarmupBar').style.width = '0%';
    } else if (ai.status === 'warming_up') {
        const elapsed = (Date.now() - ai.warmingSince) / 1000;
        const left = Math.max(AI_WARMUP_SECONDS - elapsed, 0);
        document.getElementById('aiWarmupText').textContent = left > 0
            ? `Waking up the model · ${aiClock(elapsed)} · about ${aiClock(left)} left`
            : `Waking up the model · ${aiClock(elapsed)} · almost there`;
        document.getElementById('aiWarmupBar').style.width = `${Math.min(elapsed / AI_WARMUP_SECONDS, 0.95) * 100}%`;
    }

    const chat = aiCurrent();
    const send = document.getElementById('aiSend');
    send.textContent = chat?.pending ? '■' : '↑';
    send.title = chat?.pending ? 'Stop waiting (Esc)' : 'Send (Enter)';
    send.classList.toggle('stop', Boolean(chat?.pending));
}

function aiRenderMessages() {
    const box = document.getElementById('aiMessages');
    const chat = aiCurrent();
    if (!chat || (!chat.messages.length && !chat.pending)) {
        box.innerHTML = `
            <div class="ai-empty">
                <h3>What can I help with?</h3>
                <p>gpt-oss-120b running on our own server. Answers arrive complete, not streamed, and one runs at a time.</p>
                <p>The model sleeps after 15 min idle. Waking it takes about 3½ min and starts as soon as you type.</p>
            </div>`;
        return;
    }
    const lastIndex = chat.messages.length - 1;
    box.innerHTML = chat.messages
        .map((message, index) => aiMessageHtml(message, index, index === lastIndex && !chat.pending))
        .join('') + (chat.pending ? `
            <div class="ai-msg ai-assistant">
                <div class="ai-pending">
                    <span class="ai-dots"><i></i><i></i><i></i></span>
                    <span id="aiPendingLabel">${aiEscape(aiPendingLabel(chat))}</span>
                </div>
            </div>` : '');
    box.scrollTop = box.scrollHeight;
}

function aiMessageHtml(message, index, isLast) {
    if (message.role === 'user') {
        return `<div class="ai-msg ai-user"><div class="ai-bubble">${aiEscape(message.content)}</div></div>`;
    }
    const retry = isLast ? '<button type="button" data-action="regenerate">Retry</button>' : '';
    if (message.role === 'error') {
        return `<div class="ai-msg ai-error"><span>⚠ ${aiEscape(message.content)}</span><div class="ai-meta">${retry}</div></div>`;
    }
    const meta = message.meta || {};
    const facts = [
        meta.seconds != null ? aiClock(meta.seconds) : null,
        meta.tokens != null ? `${meta.tokens} tokens` : null,
        meta.tps ? `${meta.tps.toFixed(1)} tok/s` : null
    ].filter(Boolean).join(' · ');
    const reasoning = message.reasoning ? `
        <details class="ai-reasoning">
            <summary>Reasoning</summary>
            <div>${aiEscape(message.reasoning)}</div>
        </details>` : '';
    return `
        <div class="ai-msg ai-assistant">
            ${reasoning}
            <div class="ai-content">${aiMarkdown(message.content) || '<p class="ai-muted">(empty answer)</p>'}</div>
            <div class="ai-meta">
                <span>${facts}</span>
                <button type="button" data-action="copy" data-index="${index}">Copy</button>
                ${retry}
            </div>
        </div>`;
}

function aiPendingLabel(chat) {
    const elapsed = aiClock((Date.now() - chat.pending.since) / 1000);
    if (!chat.pending.jobId) return 'Sending…';
    if (ai.status === 'warming_up' || ai.status === 'sleeping') return `Waiting for the model to wake up · ${elapsed}`;
    if (chat.pending.status === 'queued') return `Waiting in queue · ${elapsed}`;
    return `Thinking · ${elapsed}`;
}

function aiRenderPending() {
    const chat = aiCurrent();
    const label = document.getElementById('aiPendingLabel');
    if (chat?.pending && label) label.textContent = aiPendingLabel(chat);
}

function aiRenderSize() {
    const draft = document.getElementById('aiInput').value.trim();
    const context = aiContext(aiCurrent(), draft);
    const sizeBox = document.getElementById('aiSize');
    sizeBox.textContent = context.messages.length
        ? `context ${(context.fullSize / 1000).toFixed(1)} / ${AI_MAX_BODY / 1000} KB` +
          (context.trimmed ? ' · older messages left out' : '')
        : '';
    sizeBox.classList.toggle('warn', context.trimmed || !context.fits);
}

function aiTick() {
    if (ai.status === 'warming_up') aiRenderStatus();
    aiRenderPending();
}

function aiAutoGrow(input) {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
}

function aiClock(seconds) {
    const s = Math.round(seconds);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function aiOnChatListClick(e) {
    const item = e.target.closest('.ai-chat-item');
    if (!item) return;
    ai.currentId = item.dataset.id;
    document.getElementById('aiSidebar').classList.remove('open');
    aiRender();
}

function aiOnMessagesClick(e) {
    const button = e.target.closest('button[data-action]');
    if (!button) return;
    const action = button.dataset.action;
    if (action === 'regenerate') return aiRegenerate();
    const text = action === 'copy'
        ? aiCurrent()?.messages[Number(button.dataset.index)]?.content
        : button.closest('.ai-code')?.querySelector('code')?.textContent;
    if (text == null) return;
    navigator.clipboard.writeText(text).then(() => {
        button.textContent = 'Copied';
        setTimeout(() => { button.textContent = 'Copy'; }, 1500);
    }, () => showToast('Could not copy', 'error'));
}

// -- markdown ------------------------------------------------------------
// Everything is escaped first; only the tags built here reach the page.

function aiEscape(text) {
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function aiMarkdown(text) {
    const html = [];
    const fence = /```([\w+#.-]*)[^\n]*\n?([\s\S]*?)(?:\n?```|$)/g;
    let last = 0;
    let match;
    while ((match = fence.exec(text)) !== null) {
        html.push(aiBlocks(text.slice(last, match.index)));
        html.push(`
            <div class="ai-code">
                <div class="ai-code-head"><span>${aiEscape(match[1] || 'text')}</span><button type="button" data-action="copy-code">Copy</button></div>
                <pre><code>${aiEscape(match[2])}</code></pre>
            </div>`);
        last = fence.lastIndex;
    }
    html.push(aiBlocks(text.slice(last)));
    return html.join('').trim();
}

const AI_LIST_ITEM = /^\s*([-*+]|\d+[.)])\s+/;
const AI_TABLE_ROW = /^\s*\|.*\|\s*$/;

function aiBlocks(text) {
    const lines = aiEscape(text).split('\n');
    const out = [];
    let i = 0;
    while (i < lines.length) {
        const line = lines[i];
        let m;
        if (!line.trim()) {
            i++;
        } else if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
            const level = Math.min(m[1].length + 2, 6);
            out.push(`<h${level}>${aiInline(m[2])}</h${level}>`);
            i++;
        } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
            out.push('<hr>');
            i++;
        } else if (AI_TABLE_ROW.test(line) && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1] || '')) {
            const rows = [];
            while (i < lines.length && AI_TABLE_ROW.test(lines[i])) rows.push(lines[i++]);
            out.push(aiTable(rows));
        } else if (/^&gt;\s?/.test(line)) {
            const quote = [];
            while (i < lines.length && /^&gt;\s?/.test(lines[i])) quote.push(lines[i++].replace(/^&gt;\s?/, ''));
            out.push(`<blockquote>${aiInline(quote.join('<br>'))}</blockquote>`);
        } else if ((m = line.match(AI_LIST_ITEM))) {
            const tag = /\d/.test(m[1]) ? 'ol' : 'ul';
            const items = [];
            while (i < lines.length && AI_LIST_ITEM.test(lines[i])) items.push(lines[i++].replace(AI_LIST_ITEM, ''));
            out.push(`<${tag}>${items.map(item => `<li>${aiInline(item)}</li>`).join('')}</${tag}>`);
        } else {
            const paragraph = [lines[i++]];
            while (i < lines.length && lines[i].trim() &&
                   !/^(#{1,6}\s|&gt;)/.test(lines[i]) && !AI_LIST_ITEM.test(lines[i]) && !AI_TABLE_ROW.test(lines[i])) {
                paragraph.push(lines[i++]);
            }
            out.push(`<p>${aiInline(paragraph.join('<br>'))}</p>`);
        }
    }
    return out.join('');
}

function aiTable(rows) {
    const cells = row => row.trim().replace(/^\||\|$/g, '').split('|').map(cell => aiInline(cell.trim()));
    const [head, , ...body] = rows;
    return `
        <div class="ai-table"><table>
            <thead><tr>${cells(head).map(c => `<th>${c}</th>`).join('')}</tr></thead>
            <tbody>${body.map(row => `<tr>${cells(row).map(c => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody>
        </table></div>`;
}

function aiInline(text) {
    const codes = [];
    return text
        .replace(/`([^`]+)`/g, (_, code) => `\u0000${codes.push(code) - 1}\u0000`)
        .replace(/&lt;br\s*\/?&gt;/g, '<br>')
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\w)/g, '$1<em>$2</em>')
        .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
        .replace(/\u0000(\d+)\u0000/g, (_, index) => `<code>${codes[Number(index)]}</code>`);
}

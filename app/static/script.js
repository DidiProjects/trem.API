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

function showLoading() {
    document.getElementById('loading').classList.remove('hidden');
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

    // Movie Transcribe Form
    document.getElementById('transcribeMovieForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const formData = new FormData();
        formData.append('file', document.getElementById('transcribeMovieFile').files[0]);
        const language = document.getElementById('transcribeMovieLanguage').value;
        if (language) {
            formData.append('language', language);
        }

        const response = await makeRequest('/movie/transcribe', formData);
        if (response) {
            const data = await response.json();
            hideLoading();
            const resultBox = document.getElementById('transcribeMovieResult');
            resultBox.innerHTML = `<pre>${JSON.stringify(data, null, 2)}</pre>`;
            resultBox.classList.remove('hidden');
            showToast('Transcrição do vídeo concluída!');
        }
    });

    // Audio Transcribe Form
    document.getElementById('transcribeForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const formData = new FormData();
        formData.append('file', document.getElementById('transcribeFile').files[0]);
        const language = document.getElementById('transcribeLanguage').value;
        if (language) {
            formData.append('language', language);
        }

        const response = await makeRequest('/audio/transcribe', formData);
        if (response) {
            const data = await response.json();
            hideLoading();
            const resultBox = document.getElementById('transcribeResult');
            resultBox.innerHTML = `<pre>${JSON.stringify(data, null, 2)}</pre>`;
            resultBox.classList.remove('hidden');
            showToast('Transcrição concluída!');
        }
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

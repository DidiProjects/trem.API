// The playground's credential: an API key typed in the field, sent in X-API-Key. People
// log in to trem through authentik (trem.FRONT); their personal keys are made there.
// Loaded before script.js, which calls hasCredential(), authFetch() and listens to the
// 'authchange' event.

function apiKeyValue() {
    const input = document.getElementById('apiKey');
    return input ? input.value.trim() : '';
}

function hasCredential() {
    return Boolean(apiKeyValue());
}

function authHeaders() {
    const key = apiKeyValue();
    return key ? { 'X-API-Key': key } : {};
}

function authFetch(url, options = {}) {
    return fetch(url, { ...options, headers: { ...authHeaders(), ...(options.headers || {}) } });
}

function renderCredential() {
    const key = apiKeyValue();
    document.getElementById('apiKeyClear').classList.toggle('hidden', !key);
    document.getElementById('credentialStatus').textContent = key
        ? `Using: API key ••••${key.slice(-4)}`
        : 'Not authenticated: paste an API key';
}

function notifyAuthChange() {
    document.dispatchEvent(new CustomEvent('authchange'));
}

async function readError(response) {
    try {
        const body = await response.json();
        if (typeof body.detail === 'string') return body.detail;
        if (Array.isArray(body.detail)) return body.detail.map(d => d.msg.replace(/^Value error, /, '')).join(' · ');
    } catch {
        // HTML from a proxy
    }
    return (typeof STATUS_MESSAGES !== 'undefined' && STATUS_MESSAGES[response.status]) || `Request failed (${response.status})`;
}

function initCredentials() {
    const keyInput = document.getElementById('apiKey');
    keyInput.addEventListener('input', renderCredential);
    keyInput.addEventListener('change', notifyAuthChange);
    document.getElementById('apiKeyClear').addEventListener('click', () => {
        keyInput.value = '';
        renderCredential();
        notifyAuthChange();
    });
    renderCredential();
}

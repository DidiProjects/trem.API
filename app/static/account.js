// Login, sign-up, session and the Account tab. Loaded before script.js, which calls
// hasCredential(), authFetch() and listens to the 'authchange' event.
//
// The access token lives only in memory. The refresh token is an HttpOnly cookie the
// page never sees; every refresh rotates it, so refreshes are serialised across tabs
// (a second tab replaying the old cookie would look like a stolen token and end the login).

const SESSION_FLAG = 'trem_session';  // "there may be a cookie": avoids a pointless 401 on load
const CSRF = { 'X-Requested-With': 'trem' };

const auth = { token: null, me: null, timer: null, refreshing: null, challenge: null, resetToken: null };
const authChannel = 'BroadcastChannel' in window ? new BroadcastChannel('trem-auth') : null;

function storage(op, key, value) {
    try {
        if (op === 'get') return localStorage.getItem(key);
        if (op === 'set') localStorage.setItem(key, value);
        if (op === 'del') localStorage.removeItem(key);
    } catch {
        return null;
    }
}

function apiKeyValue() {
    const input = document.getElementById('apiKey');
    return input ? input.value.trim() : '';
}

function isLoggedIn() {
    return Boolean(auth.token);
}

function hasCredential() {
    return isLoggedIn() || Boolean(apiKeyValue());
}

function authHeaders() {
    if (auth.token) return { Authorization: `Bearer ${auth.token}` };
    const key = apiKeyValue();
    return key ? { 'X-API-Key': key } : {};
}

// fetch with the current credential; a 401 on a login session refreshes once and retries.
async function authFetch(url, options = {}) {
    const send = () => fetch(url, { ...options, headers: { ...authHeaders(), ...(options.headers || {}) } });
    let response = await send();
    if (response.status === 401 && isLoggedIn() && await refreshSession()) {
        response = await send();
    }
    return response;
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

async function postJson(url, body, headers = {}) {
    return fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...headers },
        body: JSON.stringify(body),
    });
}

// -- session --------------------------------------------------------------

async function startSession(tokens) {
    auth.token = tokens.access_token;
    storage('set', SESSION_FLAG, '1');
    clearTimeout(auth.timer);
    // Renew a minute before expiry, so requests never carry a dead token.
    auth.timer = setTimeout(refreshSession, Math.max(30, tokens.expires_in - 60) * 1000);
    await loadMe();
    renderAccountBar();
    notifyAuthChange();
    if (auth.me && auth.me.must_change_password) openDialog('passwordDialog');
}

function endSession(broadcast = true) {
    auth.token = null;
    auth.me = null;
    clearTimeout(auth.timer);
    storage('del', SESSION_FLAG);
    if (broadcast && authChannel) authChannel.postMessage('logout');
    renderAccountBar();
    notifyAuthChange();
}

async function refreshSession() {
    if (auth.refreshing) return auth.refreshing;
    auth.refreshing = (async () => {
        const run = async () => {
            const response = await fetch('/auth/refresh', { method: 'POST', headers: CSRF });
            if (!response.ok) return null;
            return response.json();
        };
        try {
            const tokens = navigator.locks ? await navigator.locks.request('trem-refresh', run) : await run();
            if (!tokens) {
                if (auth.token || storage('get', SESSION_FLAG)) endSession(false);
                return false;
            }
            await startSession(tokens);
            return true;
        } catch {
            return false;
        } finally {
            auth.refreshing = null;
        }
    })();
    return auth.refreshing;
}

async function loadMe() {
    const response = await fetch('/me', { headers: authHeaders() });
    auth.me = response.ok ? await response.json() : null;
}

async function logout() {
    await fetch('/auth/logout', { method: 'POST', headers: CSRF }).catch(() => {});
    endSession();
    showToast('Logged out');
}

// -- dialogs --------------------------------------------------------------

function openDialog(id) {
    const dialog = document.getElementById(id);
    dialog.querySelectorAll('.dialog-message').forEach(el => el.classList.add('hidden'));
    if (!dialog.open) dialog.showModal();
    const first = dialog.querySelector('input:not([type=hidden]):not(.hidden)');
    if (first) first.focus();
}

function closeDialog(id) {
    const dialog = document.getElementById(id);
    dialog.querySelectorAll('form').forEach(form => form.reset());
    if (dialog.open) dialog.close();
}

function dialogMessage(id, text, type = 'error') {
    const box = document.getElementById(id);
    box.textContent = text;
    box.className = `dialog-message ${type}`;
}

function showLoginStep(step) {
    document.getElementById('loginPasswordStep').classList.toggle('hidden', step !== 'password');
    document.getElementById('loginMfaStep').classList.toggle('hidden', step !== 'mfa');
}

async function submitLogin(event) {
    event.preventDefault();
    const response = await postJson('/auth/login', {
        username: document.getElementById('loginUsername').value.trim(),
        password: document.getElementById('loginPassword').value,
        session: 'cookie',
    });
    if (!response.ok) return dialogMessage('loginError', await readError(response));
    const body = await response.json();
    if (body.mfa_required) {
        auth.challenge = body.mfa_token;
        showLoginStep('mfa');
        document.getElementById('loginCode').focus();
        return;
    }
    closeDialog('loginDialog');
    await startSession(body);
    showToast(`Welcome, ${auth.me ? auth.me.username : ''}`);
}

async function submitMfa(event) {
    event.preventDefault();
    const value = document.getElementById('loginCode').value.trim();
    const isRecovery = !/^\d{6}$/.test(value);
    const response = await postJson('/auth/mfa/verify', {
        mfa_token: auth.challenge,
        [isRecovery ? 'recovery_code' : 'code']: value,
        session: 'cookie',
    });
    if (!response.ok) return dialogMessage('loginMfaError', await readError(response));
    auth.challenge = null;
    closeDialog('loginDialog');
    showLoginStep('password');
    await startSession(await response.json());
    if (isRecovery) showToast('Recovery code used. Generate new ones in Account if you are running low.');
}

async function submitSignup(event) {
    event.preventDefault();
    const password = document.getElementById('signupPassword').value;
    if (password !== document.getElementById('signupPasswordConfirm').value) {
        return dialogMessage('signupMessage', 'The passwords do not match');
    }
    const response = await postJson('/auth/register', {
        username: document.getElementById('signupUsername').value.trim(),
        email: document.getElementById('signupEmail').value.trim(),
        password,
    });
    if (!response.ok) return dialogMessage('signupMessage', await readError(response));
    document.getElementById('signupForm').classList.add('hidden');
    document.getElementById('signupDone').classList.remove('hidden');
}

async function resendVerification() {
    const email = document.getElementById('signupEmail').value.trim() || prompt('E-mail used in the sign-up');
    if (!email) return;
    const response = await postJson('/auth/resend-verification', { email });
    showToast(response.ok ? 'If there is a pending sign-up, a new link is on its way' : await readError(response),
              response.ok ? 'success' : 'error');
}

async function verifyEmailFromLink() {
    const match = location.hash.match(/^#verify-email=([A-Za-z0-9_-]+)$/);
    if (!match) return;
    // Out of the address bar and the history before anything else.
    history.replaceState(null, '', location.pathname + location.search);
    const response = await postJson('/auth/verify-email', { token: match[1] });
    const notice = document.getElementById('noticeText');
    notice.textContent = response.ok
        ? 'E-mail confirmed. Your sign-up is now waiting for the administrator; you will be able to log in once it is approved.'
        : `${await readError(response)}. Ask for a new link from the sign-up window.`;
    openDialog('noticeDialog');
}

async function submitForgot(event) {
    event.preventDefault();
    const response = await postJson('/auth/forgot-password', { email: document.getElementById('forgotEmail').value.trim() });
    if (!response.ok) return dialogMessage('forgotMessage', await readError(response));
    closeDialog('forgotDialog');
    showToast('If there is an active account with this e-mail, a reset link is on its way', 'success');
}

function resetPasswordFromLink() {
    const match = location.hash.match(/^#reset-password=([A-Za-z0-9_-]+)$/);
    if (!match) return;
    // Out of the address bar and the history before anything else; kept only in memory.
    history.replaceState(null, '', location.pathname + location.search);
    auth.resetToken = match[1];
    openDialog('resetDialog');
}

async function submitReset(event) {
    event.preventDefault();
    const password = document.getElementById('resetPassword').value;
    if (password !== document.getElementById('resetPasswordConfirm').value) {
        return dialogMessage('resetMessage', 'The passwords do not match');
    }
    const response = await postJson('/auth/reset-password', { token: auth.resetToken, new_password: password });
    if (!response.ok) {
        const message = await readError(response);
        return dialogMessage('resetMessage', response.status === 400 ? `${message}. Ask for a new link.` : message);
    }
    auth.resetToken = null;
    closeDialog('resetDialog');
    if (isLoggedIn()) endSession();
    showToast('Password changed. Log in with the new password.', 'success');
    showLoginStep('password');
    openDialog('loginDialog');
}

async function submitPasswordChange(event) {
    event.preventDefault();
    const form = event.target;
    const current = form.querySelector('[name=current]').value;
    const next = form.querySelector('[name=next]').value;
    if (next !== form.querySelector('[name=confirm]').value) {
        return dialogMessage(form.dataset.message, 'The new passwords do not match');
    }
    const response = await authFetch('/auth/change-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ current_password: current, new_password: next }),
    });
    if (!response.ok) return dialogMessage(form.dataset.message, await readError(response));
    // Every session ends with a password change: log in again with the new one.
    form.reset();
    closeDialog('passwordDialog');
    endSession();
    showToast('Password changed. Log in with the new password.');
    openDialog('loginDialog');
}

// -- header ----------------------------------------------------------------

function renderAccountBar() {
    const loggedIn = isLoggedIn() && auth.me;
    document.getElementById('accountLoggedOut').classList.toggle('hidden', loggedIn);
    document.getElementById('accountLoggedIn').classList.toggle('hidden', !loggedIn);
    document.getElementById('apiKeySection').classList.toggle('hidden', loggedIn);
    document.querySelectorAll('[data-section="account"]').forEach(el => el.classList.toggle('hidden', !loggedIn));
    const option = document.querySelector('#mobileTabSelect option[value="account"]');
    if (option) option.hidden = !loggedIn;
    if (loggedIn) {
        document.getElementById('accountName').textContent = auth.me.username;
        renderAccount();
    } else if (document.getElementById('section-account').classList.contains('active')) {
        document.querySelector('.main-tab[data-section="pdf"]').click();
    }
}

// -- account tab -------------------------------------------------------------

function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
        if (key === 'class') node.className = value;
        else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
        else node.setAttribute(key, value);
    }
    for (const child of children) node.append(child);
    return node;
}

function formatDate(value) {
    return value ? new Date(value).toLocaleString() : '—';
}

function renderAccount() {
    const me = auth.me;
    document.getElementById('profileUsername').textContent = me.username;
    document.getElementById('profileEmail').textContent = me.email || '—';
    const list = document.getElementById('profilePermissions');
    list.replaceChildren(...(me.permissions.length
        ? me.permissions.map(p => el('span', { class: 'scr-badge ok' }, p))
        : [el('span', { class: 'account-muted' }, 'No permissions yet')]));

    document.getElementById('mfaOn').classList.toggle('hidden', !me.totp_enabled);
    document.getElementById('mfaOff').classList.toggle('hidden', me.totp_enabled);
    document.getElementById('mfaRecoveryLeft').textContent = me.recovery_codes_left;

    const choices = document.getElementById('keyPermissions');
    const allowed = me.permissions.filter(p => p.startsWith('trem:') && p !== 'trem:admin');
    choices.replaceChildren(...allowed.map(p => el('label', { class: 'account-check' },
        el('input', { type: 'checkbox', value: p, checked: '' }), ` ${p}`)));
    document.getElementById('keyCreateForm').classList.toggle('hidden', allowed.length === 0);
    document.getElementById('keyNoPermissions').classList.toggle('hidden', allowed.length > 0);
    loadKeys();
}

async function loadKeys() {
    const tbody = document.getElementById('keyRows');
    const response = await authFetch('/me/api-keys');
    if (!response.ok) {
        tbody.replaceChildren(el('tr', {}, el('td', { colspan: '6' }, await readError(response))));
        return;
    }
    const keys = await response.json();
    const active = keys.filter(k => !k.revoked).length;
    document.getElementById('keyCount').textContent = `${active} of ${auth.me.api_keys_limit} active`;
    document.getElementById('keyCreateButton').disabled = active >= auth.me.api_keys_limit;
    if (!keys.length) {
        tbody.replaceChildren(el('tr', {}, el('td', { colspan: '6', class: 'account-muted' }, 'No keys yet')));
        return;
    }
    tbody.replaceChildren(...keys.map(key => el('tr', { class: key.revoked ? 'revoked' : '' },
        el('td', {}, key.name),
        el('td', {}, el('code', {}, `${key.key_hint || 'trem_'}…`)),
        el('td', {}, key.permissions.join(', ')),
        el('td', {}, formatDate(key.created_at)),
        el('td', {}, formatDate(key.last_used_at)),
        el('td', {}, key.revoked
            ? el('span', { class: 'scr-badge error' }, 'revoked')
            : el('button', { class: 'scr-link', type: 'button', onclick: () => revokeKey(key) }, 'Revoke')),
    )));
}

async function createKey(event) {
    event.preventDefault();
    const permissions = [...document.querySelectorAll('#keyPermissions input:checked')].map(i => i.value);
    const response = await authFetch('/me/api-keys', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: document.getElementById('keyName').value.trim(), permissions }),
    });
    if (!response.ok) return showToast(await readError(response), 'error');
    const created = await response.json();
    event.target.reset();
    renderAccount();
    showSecret('Your new API key',
        'Copy it now: this is the only time the full key is shown. Use it in the X-API-Key header.',
        [created.api_key]);
}

async function revokeKey(key) {
    if (!confirm(`Revoke the key "${key.name}"? Anything using it stops working.`)) return;
    const response = await authFetch(`/me/api-keys/${encodeURIComponent(key.id)}`, { method: 'DELETE' });
    if (!response.ok) return showToast(await readError(response), 'error');
    showToast('Key revoked');
    await loadMe();
    renderAccount();
}

function showSecret(title, text, lines) {
    document.getElementById('secretTitle').textContent = title;
    document.getElementById('secretText').textContent = text;
    document.getElementById('secretValue').textContent = lines.join('\n');
    openDialog('secretDialog');
}

async function copySecret() {
    try {
        await navigator.clipboard.writeText(document.getElementById('secretValue').textContent);
        showToast('Copied');
    } catch {
        showToast('Select the text and copy it', 'error');
    }
}

function closeSecret() {
    // The value must not linger in the DOM once the dialog is gone.
    document.getElementById('secretValue').textContent = '';
    closeDialog('secretDialog');
}

// -- two-factor ----------------------------------------------------------------

async function startMfaEnrol(event) {
    event.preventDefault();
    const response = await authFetch('/auth/mfa/enroll', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: document.getElementById('mfaPassword').value }),
    });
    event.target.reset();
    if (!response.ok) return showToast(await readError(response), 'error');
    const body = await response.json();
    document.getElementById('mfaSecret').textContent = body.secret.match(/.{1,4}/g).join(' ');
    document.getElementById('mfaLink').href = body.otpauth_uri;
    document.getElementById('mfaEnrolStart').classList.add('hidden');
    document.getElementById('mfaEnrolConfirm').classList.remove('hidden');
}

async function confirmMfa(event) {
    event.preventDefault();
    const response = await authFetch('/auth/mfa/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: document.getElementById('mfaConfirmCode').value.trim() }),
    });
    if (!response.ok) return showToast(await readError(response), 'error');
    const body = await response.json();
    cancelMfaEnrol();
    await loadMe();
    renderAccount();
    showSecret('Recovery codes',
        'Two-factor is on. Keep these codes somewhere safe: each one replaces a code from the app once, and they are not shown again.',
        body.recovery_codes);
}

function cancelMfaEnrol() {
    document.getElementById('mfaSecret').textContent = '';
    document.getElementById('mfaLink').removeAttribute('href');
    document.getElementById('mfaEnrolForm').reset();
    document.getElementById('mfaConfirmForm').reset();
    document.getElementById('mfaEnrolConfirm').classList.add('hidden');
    document.getElementById('mfaEnrolStart').classList.remove('hidden');
}

async function mfaAction(event, url) {
    event.preventDefault();
    const form = event.target;
    const response = await authFetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            password: form.querySelector('[name=password]').value,
            code: form.querySelector('[name=code]').value.trim(),
        }),
    });
    form.reset();
    if (!response.ok) return showToast(await readError(response), 'error');
    return response.json();
}

async function regenerateCodes(event) {
    const body = await mfaAction(event, '/auth/mfa/recovery-codes');
    if (!body) return;
    await loadMe();
    renderAccount();
    showSecret('New recovery codes', 'The previous codes no longer work. These are not shown again.', body.recovery_codes);
}

async function disableMfa(event) {
    if (!confirm('Turn two-factor off? Every session of your account ends.')) return event.preventDefault();
    if (!await mfaAction(event, '/auth/mfa/disable')) return;
    endSession();
    showToast('Two-factor turned off. Log in again.');
}

// -- wiring --------------------------------------------------------------------

function setupAccount() {
    document.getElementById('btnLogin').addEventListener('click', () => { showLoginStep('password'); openDialog('loginDialog'); });
    document.getElementById('btnSignup').addEventListener('click', () => {
        document.getElementById('signupForm').classList.remove('hidden');
        document.getElementById('signupDone').classList.add('hidden');
        openDialog('signupDialog');
    });
    document.getElementById('btnLogout').addEventListener('click', logout);
    document.getElementById('btnAccount').addEventListener('click', () => document.querySelector('.main-tab[data-section="account"]').click());
    document.getElementById('loginPasswordStep').addEventListener('submit', submitLogin);
    document.getElementById('loginMfaStep').addEventListener('submit', submitMfa);
    document.getElementById('signupForm').addEventListener('submit', submitSignup);
    document.getElementById('btnForgot').addEventListener('click', () => {
        closeDialog('loginDialog');
        openDialog('forgotDialog');
    });
    document.getElementById('forgotForm').addEventListener('submit', submitForgot);
    document.getElementById('resetForm').addEventListener('submit', submitReset);
    document.querySelectorAll('.password-form').forEach(form => form.addEventListener('submit', submitPasswordChange));
    document.querySelectorAll('[data-resend]').forEach(b => b.addEventListener('click', resendVerification));
    document.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', () => {
        if (b.dataset.close === 'secretDialog') return closeSecret();
        if (b.dataset.close === 'loginDialog') { auth.challenge = null; showLoginStep('password'); }
        if (b.dataset.close === 'resetDialog') auth.resetToken = null;
        closeDialog(b.dataset.close);
    }));
    document.getElementById('secretDialog').addEventListener('cancel', closeSecret);
    document.getElementById('secretCopy').addEventListener('click', copySecret);
    document.getElementById('keyCreateForm').addEventListener('submit', createKey);
    document.getElementById('mfaEnrolForm').addEventListener('submit', startMfaEnrol);
    document.getElementById('mfaConfirmForm').addEventListener('submit', confirmMfa);
    document.getElementById('mfaCancel').addEventListener('click', cancelMfaEnrol);
    document.getElementById('mfaCodesForm').addEventListener('submit', regenerateCodes);
    document.getElementById('mfaDisableForm').addEventListener('submit', disableMfa);
    document.getElementById('apiKey').addEventListener('change', notifyAuthChange);

    // A forced password change cannot be dismissed: the account is unusable until it is done.
    document.getElementById('passwordDialog').addEventListener('cancel', e => {
        if (auth.me && auth.me.must_change_password) e.preventDefault();
    });

    if (authChannel) authChannel.onmessage = e => { if (e.data === 'logout' && isLoggedIn()) endSession(false); };
    renderAccountBar();
}

async function initAccount() {
    setupAccount();
    await verifyEmailFromLink();
    resetPasswordFromLink();
    if (storage('get', SESSION_FLAG)) await refreshSession();
}

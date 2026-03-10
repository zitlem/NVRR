/* NVRR Admin Panel */

let adminPassword = sessionStorage.getItem('adminPassword') || '';

const loginOverlay = document.getElementById('login-overlay');
const loginBtn = document.getElementById('login-btn');
const loginPwdInput = document.getElementById('login-password');
const loginError = document.getElementById('login-error');
const adminContent = document.getElementById('admin-content');

// --- Auth ---

function authHeaders() {
    return { 'X-Admin-Password': adminPassword };
}

// Auto-login if password is saved in session
if (adminPassword) {
    fetch('/api/admin/login', { method: 'POST', headers: { 'X-Admin-Password': adminPassword } })
        .then(resp => {
            if (resp.ok) {
                loginOverlay.style.display = 'none';
                adminContent.style.display = 'block';
                loadNVRs();
                loadCameras();
                loadAdapters();
            } else {
                sessionStorage.removeItem('adminPassword');
                adminPassword = '';
            }
        });
}

loginBtn.addEventListener('click', doLogin);
loginPwdInput.addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });

async function doLogin() {
    const pwd = loginPwdInput.value;
    if (!pwd) return;

    try {
        const resp = await fetch('/api/admin/login', {
            method: 'POST',
            headers: { 'X-Admin-Password': pwd },
        });
        if (!resp.ok) throw new Error('Bad password');

        adminPassword = pwd;
        sessionStorage.setItem('adminPassword', pwd);
        loginOverlay.style.display = 'none';
        adminContent.style.display = 'block';
        loadNVRs();
        loadCameras();
        loadAdapters();
    } catch (e) {
        loginError.textContent = 'Invalid password';
        loginError.style.display = 'block';
    }
}

// --- NVRs ---

async function loadNVRs() {
    const tbody = document.getElementById('nvr-table-body');
    tbody.innerHTML = '<tr><td colspan="7" style="color:var(--text-dim)">Loading...</td></tr>';

    try {
        const resp = await fetch('/api/admin/nvrs', { headers: authHeaders() });
        const nvrs = await resp.json();

        if (nvrs.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" style="color:var(--text-dim)">No NVRs added yet</td></tr>';
            return;
        }

        tbody.innerHTML = nvrs.map(nvr => `
            <tr>
                <td>${esc(nvr.name)}</td>
                <td>
                    <input type="text" value="${esc(nvr.alias || '')}" placeholder="${esc(nvr.name)}" style="width:120px"
                        onchange="updateNVR(${nvr.id}, 'alias', this.value, this)">
                </td>
                <td>${esc(nvr.ip)}</td>
                <td>
                    <input type="number" value="${nvr.port || 80}" style="width:70px"
                        onchange="updateNVR(${nvr.id}, 'port', this.value, this)">
                </td>
                <td>
                    <input type="number" value="${nvr.sdk_port || 8000}" style="width:70px"
                        onchange="updateNVR(${nvr.id}, 'sdk_port', this.value, this)">
                    <button class="btn btn-sm btn-ghost" onclick="testSDK(${nvr.id}, this)" title="Test SDK connection">Test</button>
                </td>
                <td>${nvr.channels}</td>
                <td>
                    <button class="btn btn-sm btn-ghost" onclick="rediscoverNVR(${nvr.id})">Rediscover</button>
                    <button class="btn btn-sm btn-danger" onclick="deleteNVR(${nvr.id})">Delete</button>
                </td>
            </tr>
        `).join('');
    } catch (e) {
        tbody.innerHTML = '<tr><td colspan="7" style="color:var(--danger)">Failed to load</td></tr>';
    }
}

async function updateNVR(id, field, value, inputEl) {
    try {
        const body = {};
        body[field] = field === 'alias' ? value : parseInt(value);
        await fetch(`/api/admin/nvrs/${id}`, {
            method: 'PATCH',
            headers: { ...authHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (inputEl) {
            inputEl.style.borderColor = 'var(--success)';
            setTimeout(() => { inputEl.style.borderColor = ''; }, 1500);
        }
    } catch (e) {
        if (inputEl) {
            inputEl.style.borderColor = 'var(--danger)';
            setTimeout(() => { inputEl.style.borderColor = ''; }, 1500);
        }
        alert('Update failed: ' + e.message);
    }
}

async function testSDK(id, btn) {
    const origText = btn.textContent;
    btn.textContent = '...';
    btn.disabled = true;
    try {
        const resp = await fetch(`/api/admin/nvrs/${id}/test-sdk`, {
            method: 'POST',
            headers: authHeaders(),
        });
        const data = await resp.json();
        if (data.ok) {
            btn.textContent = 'OK';
            btn.style.color = 'var(--success)';
        } else {
            btn.textContent = 'Fail';
            btn.style.color = 'var(--danger)';
            alert(data.error);
        }
    } catch (e) {
        btn.textContent = 'Err';
        btn.style.color = 'var(--danger)';
        alert('Test failed: ' + e.message);
    }
    btn.disabled = false;
    setTimeout(() => { btn.textContent = origText; btn.style.color = ''; }, 3000);
}

document.getElementById('add-nvr-btn').addEventListener('click', async () => {
    const ip = document.getElementById('nvr-ip').value.trim();
    const port = parseInt(document.getElementById('nvr-port').value) || 80;
    const sdkRaw = document.getElementById('nvr-sdk-port').value.trim();
    // Parse as comma-separated list of ports, empty = auto
    const sdk_ports = sdkRaw ? sdkRaw.split(',').map(s => parseInt(s.trim())).filter(n => n > 0) : [];
    const username = document.getElementById('nvr-user').value.trim();
    const password = document.getElementById('nvr-pass').value;

    if (!ip || !username || !password) return;

    const status = document.getElementById('add-nvr-status');
    status.style.display = 'block';
    status.style.color = 'var(--text-dim)';
    status.textContent = 'Connecting and discovering cameras...';

    try {
        const resp = await fetch('/api/admin/nvrs', {
            method: 'POST',
            headers: { ...authHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify({ ip, port, sdk_ports, username, password }),
        });

        if (!resp.ok) {
            const err = await resp.json();
            throw new Error(err.detail || 'Failed');
        }

        const data = await resp.json();
        status.style.color = 'var(--success)';
        status.textContent = `Added "${data.name}" with ${data.cameras_found} camera(s) (SDK port: ${data.sdk_port})`;

        // Clear form
        document.getElementById('nvr-ip').value = '';
        document.getElementById('nvr-user').value = '';
        document.getElementById('nvr-pass').value = '';

        loadNVRs();
        loadCameras();
    } catch (e) {
        status.style.color = 'var(--danger)';
        status.textContent = e.message;
    }
});

async function rediscoverNVR(id) {
    try {
        const resp = await fetch(`/api/admin/nvrs/${id}/rediscover`, {
            method: 'POST',
            headers: authHeaders(),
        });
        const data = await resp.json();
        alert(`Found ${data.total_cameras} cameras (${data.new_cameras} new)`);
        loadNVRs();
        loadCameras();
    } catch (e) {
        alert('Rediscovery failed: ' + e.message);
    }
}

async function deleteNVR(id) {
    if (!confirm('Delete this NVR and all its cameras?')) return;
    try {
        await fetch(`/api/admin/nvrs/${id}`, {
            method: 'DELETE',
            headers: authHeaders(),
        });
        loadNVRs();
        loadCameras();
    } catch (e) {
        alert('Delete failed: ' + e.message);
    }
}

document.getElementById('probe-all-btn').addEventListener('click', async () => {
    const btn = document.getElementById('probe-all-btn');
    btn.disabled = true;
    btn.textContent = 'Probing...';
    try {
        const resp = await fetch('/api/admin/probe-all', { method: 'POST', headers: authHeaders() });
        if (!resp.ok) throw new Error('Probe failed');
        btn.textContent = 'Done';
        btn.style.color = 'var(--success)';
        loadCameras();
    } catch (e) {
        btn.textContent = 'Failed';
        btn.style.color = 'var(--danger)';
    }
    btn.disabled = false;
    setTimeout(() => { btn.textContent = 'Probe All'; btn.style.color = ''; }, 3000);
});

// --- Network Discovery ---

async function loadAdapters() {
    const select = document.getElementById('adapter-select');
    try {
        const resp = await fetch('/api/admin/adapters', { headers: authHeaders() });
        const adapters = await resp.json();
        select.innerHTML = '<option value="">All adapters</option>';
        adapters.forEach(a => {
            select.innerHTML += `<option value="${esc(a.ip)}">${esc(a.subnet)} (${esc(a.ip)})</option>`;
        });
    } catch (e) { /* ignore */ }
}

function deviceRow(d) {
    return `<tr>
        <td>${esc(d.name)}</td>
        <td>${esc(d.ip)}:${d.port}</td>
        <td>${esc(d.model || d.hardware || '—')}</td>
        <td>${esc(d.discovered_by || '—')}</td>
        <td>${d.already_added
            ? '<span style="color:var(--success)">Added</span>'
            : '<span style="color:var(--text-dim)">Not added</span>'}</td>
        <td>${d.already_added
            ? ''
            : `<button class="btn btn-sm btn-primary" onclick="addDiscovered('${esc(d.ip)}', ${d.port})">Add</button>`}</td>
    </tr>`;
}

let scanAbort = null;

document.getElementById('scan-btn').addEventListener('click', async () => {
    const status = document.getElementById('scan-status');
    const table = document.getElementById('discovered-table');
    const tbody = document.getElementById('discovered-table-body');
    const scanBtn = document.getElementById('scan-btn');
    const stopBtn = document.getElementById('stop-scan-btn');
    const adapter = document.getElementById('adapter-select').value;
    const adapterParam = adapter ? `adapter=${encodeURIComponent(adapter)}` : '';

    scanAbort = new AbortController();
    scanBtn.disabled = true;
    stopBtn.style.display = '';
    tbody.innerHTML = '';
    table.style.display = '';
    const devices = [];

    try {
        // Phase 1: ONVIF
        status.innerHTML = '<span class="scan-spinner"></span> ONVIF scan...';
        try {
            const resp = await fetch(`/api/admin/discover/onvif?${adapterParam}`, { headers: authHeaders(), signal: scanAbort.signal });
            if (!resp.ok) throw new Error('ONVIF scan failed');
            const onvifDevices = await resp.json();
            onvifDevices.forEach(d => {
                devices.push(d);
                tbody.innerHTML += deviceRow(d);
            });
            status.innerHTML = `<span class="scan-spinner"></span> Found ${devices.length} via ONVIF — scanning ISAPI...`;
        } catch (e) {
            if (e.name === 'AbortError') throw e;
            status.innerHTML = `<span class="scan-spinner"></span> ONVIF failed — scanning ISAPI...`;
        }

        // Phase 2: ISAPI (exclude ONVIF-found IPs)
        try {
            const excludeIps = devices.map(d => d.ip).join(',');
            const resp = await fetch(`/api/admin/discover/isapi?${adapterParam}&exclude=${encodeURIComponent(excludeIps)}`, { headers: authHeaders(), signal: scanAbort.signal });
            if (!resp.ok) throw new Error('ISAPI scan failed');
            const isapiDevices = await resp.json();
            isapiDevices.forEach(d => {
                devices.push(d);
                tbody.innerHTML += deviceRow(d);
            });
        } catch (e) {
            if (e.name === 'AbortError') throw e;
            console.warn('ISAPI scan failed:', e);
        }

        if (devices.length === 0) {
            status.textContent = 'No devices found.';
            table.style.display = 'none';
        } else {
            status.textContent = `Found ${devices.length} device(s)`;
        }
    } catch (e) {
        if (e.name === 'AbortError') {
            status.textContent = devices.length
                ? `Stopped — found ${devices.length} device(s)`
                : 'Scan stopped.';
        }
    } finally {
        scanBtn.disabled = false;
        stopBtn.style.display = 'none';
        scanAbort = null;
    }
});

document.getElementById('stop-scan-btn').addEventListener('click', () => {
    if (scanAbort) scanAbort.abort();
});

function addDiscovered(ip, port) {
    // Pre-fill the manual form and scroll to it
    document.getElementById('nvr-ip').value = ip;
    document.getElementById('nvr-port').value = port;
    document.getElementById('nvr-user').value = '';
    document.getElementById('nvr-pass').value = '';
    document.getElementById('nvr-user').focus();
    document.getElementById('nvr-ip').scrollIntoView({ behavior: 'smooth' });
}

// --- Cameras ---

async function loadCameras() {
    const tbody = document.getElementById('camera-table-body');
    tbody.innerHTML = '<tr><td colspan="6" style="color:var(--text-dim)">Loading...</td></tr>';

    try {
        const resp = await fetch('/api/admin/cameras', { headers: authHeaders() });
        const cameras = await resp.json();

        if (cameras.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" style="color:var(--text-dim)">No video feeds</td></tr>';
            return;
        }

        // Group cameras by NVR
        const groups = {};
        const order = [];
        cameras.forEach(cam => {
            const key = cam.nvr_id;
            if (!groups[key]) {
                groups[key] = { name: cam.nvr_name || 'NVR #' + key, cameras: [] };
                order.push(key);
            }
            groups[key].cameras.push(cam);
        });

        let html = '';
        order.forEach(nvrId => {
            const g = groups[nvrId];
            html += `<tr><td colspan="5" style="background:var(--bg);font-weight:600;font-size:13px;padding:10px 12px">${esc(g.name)}</td></tr>`;
            g.cameras.forEach(cam => {
                const disconnected = cam.connected === 0 || cam.connected === false;
                const rowStyle = disconnected ? 'opacity:0.4' : '';
                html += `<tr style="${rowStyle}">
                    <td style="padding-left:24px">${esc(cam.name)}</td>
                    <td>${cam.channel}</td>
                    <td>${disconnected
                        ? '<span style="color:var(--text-dim);font-size:12px">No video feed</span>'
                        : '<span style="color:var(--success);font-size:12px">Connected</span>'}</td>
                    <td>
                        <label class="toggle">
                            <input type="checkbox" ${cam.enabled ? 'checked' : ''}
                                onchange="updateCamera(${cam.id}, 'enabled', this.checked)">
                            <span class="slider"></span>
                        </label>
                    </td>
                    <td>
                        <label class="toggle">
                            <input type="checkbox" ${cam.ptz_enabled ? 'checked' : ''}
                                onchange="updateCamera(${cam.id}, 'ptz_enabled', this.checked)">
                            <span class="slider"></span>
                        </label>
                    </td>
                </tr>`;
            });
        });
        tbody.innerHTML = html;
    } catch (e) {
        tbody.innerHTML = '<tr><td colspan="6" style="color:var(--danger)">Failed to load</td></tr>';
    }
}

async function updateCamera(id, field, value) {
    try {
        await fetch(`/api/admin/cameras/${id}`, {
            method: 'PATCH',
            headers: { ...authHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify({ [field]: value }),
        });
    } catch (e) {
        alert('Update failed: ' + e.message);
    }
}

// --- Export / Import Config ---

document.getElementById('export-btn').addEventListener('click', async () => {
    try {
        const resp = await fetch('/api/admin/export', { headers: authHeaders() });
        const data = await resp.json();
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `nvrr-config-${new Date().toISOString().slice(0,10)}.json`;
        a.click();
        URL.revokeObjectURL(url);
    } catch (e) {
        alert('Export failed: ' + e.message);
    }
});

document.getElementById('import-btn').addEventListener('click', () => {
    document.getElementById('import-file').click();
});

document.getElementById('import-file').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const status = document.getElementById('config-status');
    status.style.display = 'block';
    status.style.color = 'var(--text-dim)';
    status.textContent = 'Importing...';

    try {
        const text = await file.text();
        const data = JSON.parse(text);
        const resp = await fetch('/api/admin/import', {
            method: 'POST',
            headers: { ...authHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        if (!resp.ok) throw new Error((await resp.json()).detail || 'Import failed');
        const result = await resp.json();
        status.style.color = 'var(--success)';
        status.textContent = `Imported ${result.nvrs} NVR(s), ${result.cameras} camera(s), ${result.views} view(s)`;
        loadNVRs();
        loadCameras();
    } catch (e) {
        status.style.color = 'var(--danger)';
        status.textContent = 'Import failed: ' + e.message;
    }
    e.target.value = '';
});

// --- Server Restart ---

document.getElementById('restart-btn').addEventListener('click', async () => {
    if (!confirm('Restart the NVRR server?')) return;

    const status = document.getElementById('restart-status');
    status.style.display = 'block';
    status.style.color = 'var(--text-dim)';
    status.textContent = 'Restarting...';

    try {
        await fetch('/api/admin/restart', {
            method: 'POST',
            headers: authHeaders(),
        });
    } catch (e) {
        // Expected — server goes down
    }

    // Poll until server is back
    status.textContent = 'Waiting for server...';
    const poll = setInterval(async () => {
        try {
            const resp = await fetch('/api/admin/login', {
                method: 'POST',
                headers: authHeaders(),
            });
            if (resp.ok) {
                clearInterval(poll);
                status.style.color = 'var(--success)';
                status.textContent = 'Server restarted.';
                loadNVRs();
                loadCameras();
                setTimeout(() => { status.style.display = 'none'; }, 3000);
            }
        } catch (e) { /* still down */ }
    }, 1500);
});

// --- Factory Reset ---

document.getElementById('factory-reset-btn').addEventListener('click', async () => {
    if (!confirm('This will delete ALL NVRs, cameras, and settings. Are you sure?')) return;
    if (!confirm('This cannot be undone. Really reset?')) return;

    const status = document.getElementById('reset-status');
    status.style.display = 'block';
    status.style.color = 'var(--text-dim)';
    status.textContent = 'Resetting...';

    try {
        const resp = await fetch('/api/admin/factory-reset', {
            method: 'POST',
            headers: authHeaders(),
        });
        if (!resp.ok) throw new Error('Reset failed');

        status.style.color = 'var(--success)';
        status.textContent = 'Factory reset complete.';
        loadNVRs();
        loadCameras();
    } catch (e) {
        status.style.color = 'var(--danger)';
        status.textContent = 'Reset failed: ' + e.message;
    }
});

// --- Helpers ---

function esc(str) {
    const el = document.createElement('span');
    el.textContent = str;
    return el.innerHTML;
}

/* NVRR Admin Panel */

let adminPassword = '';

const loginOverlay = document.getElementById('login-overlay');
const loginBtn = document.getElementById('login-btn');
const loginPwdInput = document.getElementById('login-password');
const loginError = document.getElementById('login-error');
const adminContent = document.getElementById('admin-content');

// --- Auth ---

function authHeaders() {
    return { 'X-Admin-Password': adminPassword };
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
        loginOverlay.style.display = 'none';
        adminContent.style.display = 'block';
        loadNVRs();
        loadCameras();
    } catch (e) {
        loginError.textContent = 'Invalid password';
        loginError.style.display = 'block';
    }
}

// --- NVRs ---

async function loadNVRs() {
    const tbody = document.getElementById('nvr-table-body');
    tbody.innerHTML = '<tr><td colspan="4" style="color:var(--text-dim)">Loading...</td></tr>';

    try {
        const resp = await fetch('/api/admin/nvrs', { headers: authHeaders() });
        const nvrs = await resp.json();

        if (nvrs.length === 0) {
            tbody.innerHTML = '<tr><td colspan="4" style="color:var(--text-dim)">No NVRs added yet</td></tr>';
            return;
        }

        tbody.innerHTML = nvrs.map(nvr => `
            <tr>
                <td>${esc(nvr.name)}</td>
                <td>${esc(nvr.ip)}:${nvr.port}</td>
                <td>${nvr.channels}</td>
                <td>
                    <button class="btn btn-sm btn-ghost" onclick="rediscoverNVR(${nvr.id})">Rediscover</button>
                    <button class="btn btn-sm btn-danger" onclick="deleteNVR(${nvr.id})">Delete</button>
                </td>
            </tr>
        `).join('');
    } catch (e) {
        tbody.innerHTML = '<tr><td colspan="4" style="color:var(--danger)">Failed to load</td></tr>';
    }
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
        status.textContent = `Added "${data.name}" with ${data.cameras_found} camera(s)`;

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

// --- Network Discovery ---

document.getElementById('scan-btn').addEventListener('click', async () => {
    const status = document.getElementById('scan-status');
    const table = document.getElementById('discovered-table');
    const tbody = document.getElementById('discovered-table-body');

    status.textContent = 'Scanning network (this takes ~5 seconds)...';
    table.style.display = 'none';

    try {
        const resp = await fetch('/api/admin/discover', { headers: authHeaders() });
        if (!resp.ok) throw new Error('Scan failed');
        const devices = await resp.json();

        if (devices.length === 0) {
            status.textContent = 'No devices found on the network.';
            return;
        }

        status.textContent = `Found ${devices.length} device(s)`;
        table.style.display = '';
        tbody.innerHTML = devices.map(d => `
            <tr>
                <td>${esc(d.name)}</td>
                <td>${esc(d.ip)}:${d.port}</td>
                <td>${esc(d.model || d.hardware || '—')}</td>
                <td>${d.already_added
                    ? '<span style="color:var(--success)">Added</span>'
                    : '<span style="color:var(--text-dim)">Not added</span>'}</td>
                <td>${d.already_added
                    ? ''
                    : `<button class="btn btn-sm btn-primary" onclick="addDiscovered('${esc(d.ip)}', ${d.port})">Add</button>`}</td>
            </tr>
        `).join('');
    } catch (e) {
        status.textContent = 'Scan failed: ' + e.message;
    }
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
    tbody.innerHTML = '<tr><td colspan="5" style="color:var(--text-dim)">Loading...</td></tr>';

    try {
        const resp = await fetch('/api/admin/cameras', { headers: authHeaders() });
        const cameras = await resp.json();

        if (cameras.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" style="color:var(--text-dim)">No cameras</td></tr>';
            return;
        }

        tbody.innerHTML = cameras.map(cam => `
            <tr>
                <td>
                    <input type="text" value="${esc(cam.name)}" style="width:160px"
                        onchange="updateCamera(${cam.id}, 'name', this.value)">
                </td>
                <td>${cam.channel}</td>
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
                <td style="color:var(--text-dim);font-size:12px">NVR #${cam.nvr_id}</td>
            </tr>
        `).join('');
    } catch (e) {
        tbody.innerHTML = '<tr><td colspan="5" style="color:var(--danger)">Failed to load</td></tr>';
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

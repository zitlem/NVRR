/* NVRR Viewer */

const grid = document.getElementById('camera-grid');
const sidebar = document.getElementById('ptz-sidebar');
const ptzName = document.getElementById('ptz-camera-name');
const presetsList = document.getElementById('presets-list');

let cameras = [];
let players = {};    // camera id -> Hls instance
let selectedCamera = null;

// --- Layout ---

document.querySelectorAll('[data-cols]').forEach(btn => {
    btn.addEventListener('click', () => {
        const cols = btn.dataset.cols;
        grid.className = `camera-grid cols-${cols}`;
        document.querySelectorAll('[data-cols]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
    });
});

// --- Load cameras ---

async function loadCameras() {
    try {
        const resp = await fetch('/api/cameras');
        cameras = await resp.json();
        renderGrid();
    } catch (e) {
        console.error('Failed to load cameras', e);
        setTimeout(loadCameras, 5000);
    }
}

function renderGrid() {
    // Destroy old players
    Object.values(players).forEach(p => p.destroy());
    players = {};
    grid.innerHTML = '';

    cameras.forEach(cam => {
        const tile = document.createElement('div');
        tile.className = 'camera-tile';
        tile.dataset.id = cam.id;

        const video = document.createElement('video');
        video.autoplay = true;
        video.muted = true;
        video.playsInline = true;
        tile.appendChild(video);

        const overlay = document.createElement('div');
        overlay.className = 'overlay';
        overlay.innerHTML = `<span class="status-dot"></span>${cam.name}`;
        tile.appendChild(overlay);

        grid.appendChild(tile);

        // Start HLS
        startStream(cam, video, overlay.querySelector('.status-dot'));

        // Click to select (PTZ)
        tile.addEventListener('click', () => selectCamera(cam, tile));

        // Double-click for fullscreen
        tile.addEventListener('dblclick', () => {
            if (tile.requestFullscreen) tile.requestFullscreen();
        });
    });
}

function startStream(cam, video, dot) {
    if (!Hls.isSupported()) {
        // Safari native HLS
        video.src = cam.stream_url;
        dot.className = 'status-dot live';
        return;
    }

    const hls = new Hls({
        liveSyncDurationCount: 2,
        liveMaxLatencyDurationCount: 5,
        enableWorker: true,
        lowLatencyMode: true,
    });

    hls.loadSource(cam.stream_url);
    hls.attachMedia(video);

    hls.on(Hls.Events.MANIFEST_PARSED, () => {
        video.play().catch(() => {});
        dot.className = 'status-dot live';
    });

    hls.on(Hls.Events.ERROR, (_, data) => {
        dot.className = 'status-dot error';
        if (data.fatal) {
            console.warn(`Stream error for ${cam.name}, retrying...`);
            setTimeout(() => {
                hls.loadSource(cam.stream_url);
            }, 3000);
        }
    });

    players[cam.id] = hls;
}

// --- PTZ ---

function selectCamera(cam, tile) {
    // Deselect previous
    document.querySelectorAll('.camera-tile.selected').forEach(t => t.classList.remove('selected'));

    if (selectedCamera === cam.id) {
        selectedCamera = null;
        sidebar.classList.remove('open');
        return;
    }

    selectedCamera = cam.id;
    tile.classList.add('selected');

    if (cam.ptz_enabled) {
        ptzName.textContent = cam.name;
        sidebar.classList.add('open');
        loadPresets(cam.id);
    } else {
        sidebar.classList.remove('open');
    }
}

async function loadPresets(cameraId) {
    presetsList.innerHTML = '';
    try {
        const resp = await fetch(`/api/ptz/${cameraId}/presets`);
        if (!resp.ok) return;
        const presets = await resp.json();
        presets.forEach(p => {
            const btn = document.createElement('button');
            btn.textContent = p.name;
            btn.addEventListener('click', () => gotoPreset(cameraId, p.token));
            presetsList.appendChild(btn);
        });
    } catch (e) {
        console.error('Failed to load presets', e);
    }
}

async function gotoPreset(cameraId, token) {
    try {
        await fetch(`/api/ptz/${cameraId}/preset/${token}`, { method: 'POST' });
    } catch (e) {
        console.error('Preset failed', e);
    }
}

// D-pad: hold to move, release to stop
document.querySelectorAll('.dpad button').forEach(btn => {
    const pan = parseFloat(btn.dataset.pan);
    const tilt = parseFloat(btn.dataset.tilt);

    const startMove = () => {
        if (!selectedCamera) return;
        fetch(`/api/ptz/${selectedCamera}/move`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pan, tilt, zoom: 0 }),
        });
    };

    const stopMovement = () => {
        if (!selectedCamera) return;
        fetch(`/api/ptz/${selectedCamera}/stop`, { method: 'POST' });
    };

    btn.addEventListener('mousedown', startMove);
    btn.addEventListener('mouseup', stopMovement);
    btn.addEventListener('mouseleave', stopMovement);
    btn.addEventListener('touchstart', e => { e.preventDefault(); startMove(); });
    btn.addEventListener('touchend', e => { e.preventDefault(); stopMovement(); });
});

// Zoom buttons
document.querySelectorAll('.zoom-controls button').forEach(btn => {
    const zoom = parseFloat(btn.dataset.zoom);

    const startZoom = () => {
        if (!selectedCamera) return;
        fetch(`/api/ptz/${selectedCamera}/move`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pan: 0, tilt: 0, zoom }),
        });
    };

    const stopZoom = () => {
        if (!selectedCamera) return;
        fetch(`/api/ptz/${selectedCamera}/stop`, { method: 'POST' });
    };

    btn.addEventListener('mousedown', startZoom);
    btn.addEventListener('mouseup', stopZoom);
    btn.addEventListener('mouseleave', stopZoom);
    btn.addEventListener('touchstart', e => { e.preventDefault(); startZoom(); });
    btn.addEventListener('touchend', e => { e.preventDefault(); stopZoom(); });
});

// Close sidebar on Escape
document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
        sidebar.classList.remove('open');
        document.querySelectorAll('.camera-tile.selected').forEach(t => t.classList.remove('selected'));
        selectedCamera = null;
    }
});

// --- Init ---
loadCameras();

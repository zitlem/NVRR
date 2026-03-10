/* NVRR Viewer */

const grid = document.getElementById('camera-grid');
const sidebar = document.getElementById('ptz-sidebar');
const ptzName = document.getElementById('ptz-camera-name');
const presetsList = document.getElementById('presets-list');
const viewsList = document.getElementById('views-list');
const addViewBtn = document.getElementById('add-view-btn');
const modal = document.getElementById('edit-view-modal');
const modalTitle = document.getElementById('modal-title');
const viewNameInput = document.getElementById('view-name-input');
const viewColsInput = document.getElementById('view-cols-input');
const cameraChecklist = document.getElementById('camera-checklist');
const modalCancel = document.getElementById('modal-cancel');
const modalSave = document.getElementById('modal-save');
const modalDelete = document.getElementById('modal-delete');

let allCameras = [];
let players = {};       // camera id -> { hls, video, dot, cam, isMain }
let selectedCamera = null;
let views = [];          // [{ name, slug, cols, cameras: [id,...] }]
let activeViewSlug = null;
let editingViewSlug = null;  // null = new view
let dragSrcId = null;

// --- Views persistence (backend API) ---

async function loadViews() {
    try {
        const resp = await fetch('/api/views');
        views = await resp.json();
    } catch { views = []; }
}

async function saveView(view) {
    if (view.id) {
        await fetch(`/api/views/${view.id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(view),
        });
    } else {
        const resp = await fetch('/api/views', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(view),
        });
        const data = await resp.json();
        view.slug = data.slug;
    }
    // Reload to get server-assigned IDs
    await loadViews();
}

async function deleteViewById(viewId) {
    await fetch(`/api/views/${viewId}`, { method: 'DELETE' });
    await loadViews();
}

function slugify(name) {
    return name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
}

function getActiveView() {
    return views.find(v => v.slug === activeViewSlug) || null;
}

// --- Render views sidebar ---

function renderViewsSidebar() {
    let html = '';
    // "All Cameras" default
    html += `<div class="view-item ${!activeViewSlug ? 'active' : ''}" data-slug="">
        <span class="view-name">All Cameras</span>
    </div>`;
    views.forEach(v => {
        html += `<div class="view-item ${activeViewSlug === v.slug ? 'active' : ''}" data-slug="${v.slug}">
            <span class="view-name">${esc(v.name)}</span>
            <button class="view-edit" data-edit="${v.slug}" title="Edit">&#9998;</button>
        </div>`;
    });
    viewsList.innerHTML = html;

    // Click handlers
    viewsList.querySelectorAll('.view-item').forEach(el => {
        el.addEventListener('click', (e) => {
            if (e.target.classList.contains('view-edit')) return;
            const slug = el.dataset.slug;
            setActiveView(slug || null);
        });
    });
    viewsList.querySelectorAll('.view-edit').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            openEditModal(btn.dataset.edit);
        });
    });
}

function setActiveView(slug) {
    activeViewSlug = slug;
    // Update URL hash
    if (slug) {
        window.location.hash = slug;
    } else {
        history.replaceState(null, '', window.location.pathname);
    }
    renderViewsSidebar();
    renderGrid();
}

// --- Modal ---

function openEditModal(slug) {
    editingViewSlug = slug || null;
    const view = slug ? views.find(v => v.slug === slug) : null;

    modalTitle.textContent = view ? 'Edit View' : 'New View';
    viewNameInput.value = view ? view.name : '';
    viewColsInput.value = view ? view.cols : 4;
    modalDelete.style.display = view ? '' : 'none';

    const selectedIds = view ? view.cameras : [];
    cameraChecklist.innerHTML = allCameras.map(cam => `
        <label class="camera-check-item">
            <input type="checkbox" value="${cam.id}" ${selectedIds.includes(cam.id) ? 'checked' : ''}>
            ${esc(cam.name)}
        </label>
    `).join('');

    modal.style.display = '';
    viewNameInput.focus();
}

function closeModal() {
    modal.style.display = 'none';
    editingViewSlug = null;
}

modalCancel.addEventListener('click', closeModal);
modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });

modalSave.addEventListener('click', async () => {
    const name = viewNameInput.value.trim();
    if (!name) { viewNameInput.focus(); return; }

    const cols = parseInt(viewColsInput.value) || 4;
    const cameraIds = [];
    cameraChecklist.querySelectorAll('input[type="checkbox"]:checked').forEach(cb => {
        cameraIds.push(parseInt(cb.value));
    });

    if (editingViewSlug) {
        // Update existing
        const view = views.find(v => v.slug === editingViewSlug);
        if (view) {
            const newSlug = slugify(name);
            await saveView({ id: view.id, name, slug: newSlug, cols, cameras: cameraIds });
            if (activeViewSlug === editingViewSlug) activeViewSlug = newSlug;
        }
    } else {
        // Create new
        const slug = slugify(name);
        await saveView({ name, slug, cols, cameras: cameraIds });
        activeViewSlug = slug;
    }

    closeModal();
    renderViewsSidebar();
    renderGrid();
    if (activeViewSlug) window.location.hash = activeViewSlug;
});

modalDelete.addEventListener('click', async () => {
    if (!editingViewSlug) return;
    if (!confirm('Delete this view?')) return;
    const view = views.find(v => v.slug === editingViewSlug);
    if (view && view.id) await deleteViewById(view.id);
    if (activeViewSlug === editingViewSlug) activeViewSlug = null;
    closeModal();
    renderViewsSidebar();
    renderGrid();
    history.replaceState(null, '', window.location.pathname);
});

addViewBtn.addEventListener('click', () => openEditModal(null));

// --- Load cameras ---

async function loadCameras() {
    try {
        const resp = await fetch('/api/cameras');
        allCameras = await resp.json();
        renderViewsSidebar();
        renderGrid();
    } catch (e) {
        console.error('Failed to load cameras', e);
        setTimeout(loadCameras, 5000);
    }
}

// --- Render grid ---

function getVisibleCameras() {
    const view = getActiveView();
    if (!view) return allCameras;
    // Return in view's camera order, skip missing
    return view.cameras
        .map(id => allCameras.find(c => c.id === id))
        .filter(Boolean);
}

function renderGrid() {
    // Destroy old players
    Object.values(players).forEach(p => { if (p.hls) p.hls.destroy(); });
    players = {};
    grid.innerHTML = '';

    const view = getActiveView();
    const cols = view ? view.cols : 4;
    grid.className = `camera-grid cols-${cols}`;

    const visible = getVisibleCameras();
    const totalCams = visible.length;
    const rows = Math.ceil(totalCams / cols) || 1;

    // Calculate tile height to fit viewport
    const gridRect = grid.parentElement.getBoundingClientRect();
    const availH = gridRect.height - 6; // padding
    const rowH = (availH - (rows - 1) * 3) / rows;

    visible.forEach(cam => {
        const tile = document.createElement('div');
        tile.className = 'camera-tile';
        tile.dataset.id = cam.id;
        tile.style.height = rowH + 'px';

        // Drag & drop for reordering (only in custom views)
        if (view) {
            tile.draggable = true;
            tile.addEventListener('dragstart', (e) => {
                dragSrcId = cam.id;
                e.dataTransfer.effectAllowed = 'move';
                tile.style.opacity = '0.5';
            });
            tile.addEventListener('dragend', () => {
                tile.style.opacity = '';
                document.querySelectorAll('.camera-tile.drag-over').forEach(t => t.classList.remove('drag-over'));
            });
            tile.addEventListener('dragover', (e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                tile.classList.add('drag-over');
            });
            tile.addEventListener('dragleave', () => tile.classList.remove('drag-over'));
            tile.addEventListener('drop', (e) => {
                e.preventDefault();
                tile.classList.remove('drag-over');
                if (dragSrcId == null || dragSrcId === cam.id) return;
                reorderCamera(dragSrcId, cam.id);
                dragSrcId = null;
            });
        }

        const video = document.createElement('video');
        video.autoplay = true;
        video.muted = true;
        video.playsInline = true;
        tile.appendChild(video);

        const overlay = document.createElement('div');
        overlay.className = 'overlay';
        overlay.innerHTML = `<span class="status-dot"></span>${esc(cam.name)}`;
        tile.appendChild(overlay);

        grid.appendChild(tile);

        const dot = overlay.querySelector('.status-dot');
        startStream(cam, video, dot, false);

        // Click to select (PTZ)
        tile.addEventListener('click', () => selectCamera(cam, tile));

        // Double-click for fullscreen + main stream
        tile.addEventListener('dblclick', () => {
            if (tile.requestFullscreen) tile.requestFullscreen();
        });
    });

    // Listen for fullscreen changes to switch streams
    document.addEventListener('fullscreenchange', onFullscreenChange);
}

async function reorderCamera(fromId, toId) {
    const view = getActiveView();
    if (!view) return;
    const arr = view.cameras;
    const fromIdx = arr.indexOf(fromId);
    const toIdx = arr.indexOf(toId);
    if (fromIdx < 0 || toIdx < 0) return;
    arr.splice(fromIdx, 1);
    arr.splice(toIdx, 0, fromId);
    await saveView({ id: view.id, cameras: arr });
    renderGrid();
}

// --- Fullscreen: switch to main stream ---

function onFullscreenChange() {
    const fsEl = document.fullscreenElement;
    if (fsEl && fsEl.classList.contains('camera-tile')) {
        const camId = parseInt(fsEl.dataset.id);
        const p = players[camId];
        if (p && !p.isMain) {
            switchStream(camId, true);
        }
    } else {
        // Exited fullscreen — switch all back to sub
        Object.keys(players).forEach(id => {
            const p = players[id];
            if (p && p.isMain) {
                switchStream(parseInt(id), false);
            }
        });
    }
}

function switchStream(camId, toMain) {
    const p = players[camId];
    if (!p) return;
    const url = toMain ? p.cam.main_stream_url : p.cam.stream_url;
    if (p.hls) {
        p.hls.loadSource(url);
        p.isMain = toMain;
    }
}

// --- HLS streaming ---

function startStream(cam, video, dot, isMain) {
    // Destroy previous if exists
    if (players[cam.id]?.hls) {
        players[cam.id].hls.destroy();
    }

    const url = isMain ? cam.main_stream_url : cam.stream_url;

    if (!Hls.isSupported()) {
        video.src = url;
        dot.className = 'status-dot live';
        players[cam.id] = { hls: null, video, dot, cam, isMain };
        return;
    }

    const hls = new Hls({
        liveSyncDurationCount: 2,
        liveMaxLatencyDurationCount: 5,
        enableWorker: true,
        lowLatencyMode: true,
    });

    hls.loadSource(url);
    hls.attachMedia(video);

    hls.on(Hls.Events.MANIFEST_PARSED, () => {
        video.play().catch(() => {});
        dot.className = 'status-dot live';
    });

    hls.on(Hls.Events.FRAG_LOADED, () => {
        dot.className = 'status-dot live';
    });

    hls.on(Hls.Events.ERROR, (_, data) => {
        if (data.fatal) {
            dot.className = 'status-dot error';
            console.warn(`Stream error for ${cam.name}, retrying...`);
            setTimeout(() => {
                hls.loadSource(url);
            }, 3000);
        }
    });

    players[cam.id] = { hls, video, dot, cam, isMain };
}

// --- PTZ ---

function selectCamera(cam, tile) {
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
        if (modal.style.display !== 'none') closeModal();
    }
});

// Handle window resize (debounced)
let resizeTimer;
window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
        if (allCameras.length) renderGrid();
    }, 200);
});

// Handle URL hash for bookmarkable views
function initFromHash() {
    const hash = window.location.hash.replace('#', '');
    if (hash && views.find(v => v.slug === hash)) {
        activeViewSlug = hash;
    }
}

window.addEventListener('hashchange', () => {
    const hash = window.location.hash.replace('#', '');
    if (hash) {
        const view = views.find(v => v.slug === hash);
        if (view && activeViewSlug !== hash) {
            activeViewSlug = hash;
            renderViewsSidebar();
            renderGrid();
        }
    } else if (activeViewSlug) {
        activeViewSlug = null;
        renderViewsSidebar();
        renderGrid();
    }
});

// --- Utility ---

function esc(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

// --- Init ---
(async () => {
    await loadViews();
    initFromHash();
    await loadCameras();
})();

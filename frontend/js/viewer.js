/* NVRR Viewer */

const grid = document.getElementById('camera-grid');
const sidebar = document.getElementById('ptz-sidebar');
const ptzName = document.getElementById('ptz-camera-name');
const presetsList = document.getElementById('presets-list');
const viewsList = document.getElementById('views-list');
const cameraListEl = document.getElementById('camera-list');
const addViewBtn = document.getElementById('add-view-btn');
const viewBar = document.getElementById('view-bar');
const viewNameInput = document.getElementById('view-name-input');
const viewColsInput = document.getElementById('view-cols');
const viewRowsInput = document.getElementById('view-rows');
const deleteViewBtn = document.getElementById('delete-view-btn');
const sidebarDivider = document.getElementById('sidebar-divider');
const sidebarTop = document.getElementById('sidebar-top');

let allCameras = [];
let players = {};       // camera id -> { hls, video, dot, cam }
let selectedCamera = null;
let views = [];
let activeViewSlug = null;
let fullscreenCamId = null;  // track which cam is fullscreen
let saveTimer = null;        // debounce view saves
let streamSyncTimer = null;  // debounce stream sync

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
    await loadViews();
}

function saveViewDebounced(view) {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => saveView(view), 500);
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
    let html = `<div class="view-item ${!activeViewSlug ? 'active' : ''}" data-slug="">
        <span class="view-name">All Cameras</span>
    </div>`;
    views.forEach(v => {
        html += `<div class="view-item ${activeViewSlug === v.slug ? 'active' : ''}" data-slug="${v.slug}">
            <span class="view-name">${esc(v.name)}</span>
        </div>`;
    });
    viewsList.innerHTML = html;

    viewsList.querySelectorAll('.view-item').forEach(el => {
        el.addEventListener('click', () => {
            const slug = el.dataset.slug;
            setActiveView(slug || null);
        });
    });
}

let collapsedNVRs = {};  // nvr_id -> bool (collapsed state)

function renderCameraList() {
    const view = getActiveView();
    const usedIds = view ? view.grid.filter(id => id != null) : [];

    // Group cameras by NVR
    const nvrGroups = {};
    const nvrOrder = [];
    allCameras.forEach(cam => {
        const nvrId = cam.nvr_id || 0;
        if (!nvrGroups[nvrId]) {
            nvrGroups[nvrId] = { name: cam.nvr_name || 'NVR', cameras: [] };
            nvrOrder.push(nvrId);
        }
        nvrGroups[nvrId].cameras.push(cam);
    });

    let html = '';
    nvrOrder.forEach(nvrId => {
        const group = nvrGroups[nvrId];
        const collapsed = !!collapsedNVRs[nvrId];
        html += `<div class="nvr-group-header" data-nvr-id="${nvrId}">
            <span class="nvr-arrow ${collapsed ? 'collapsed' : ''}">\u25BC</span>
            <span>${esc(group.name)}</span>
            <span class="nvr-cam-count">${group.cameras.length}</span>
        </div>`;
        if (!collapsed) {
            group.cameras.forEach(cam => {
                const inView = usedIds.includes(cam.id);
                html += `<div class="cam-list-item ${inView ? 'in-view' : ''}" draggable="true" data-cam-id="${cam.id}">
                    <span class="cam-dot"></span>
                    <span>${esc(cam.name)}</span>
                </div>`;
            });
        }
    });

    cameraListEl.innerHTML = html;

    // NVR group toggle
    cameraListEl.querySelectorAll('.nvr-group-header').forEach(el => {
        el.addEventListener('click', () => {
            const nvrId = el.dataset.nvrId;
            collapsedNVRs[nvrId] = !collapsedNVRs[nvrId];
            renderCameraList();
        });
    });

    // Drag start on camera items
    cameraListEl.querySelectorAll('.cam-list-item').forEach(el => {
        el.addEventListener('dragstart', (e) => {
            e.dataTransfer.setData('text/plain', el.dataset.camId);
            e.dataTransfer.effectAllowed = 'all';
        });
    });
}

function setActiveView(slug) {
    activeViewSlug = slug;
    if (slug) {
        window.location.hash = slug;
    } else {
        history.replaceState(null, '', window.location.pathname);
    }
    renderViewsSidebar();
    renderCameraList();
    updateViewBar();
    renderGrid();
}

// --- View bar (settings for custom views) ---

function updateViewBar() {
    const view = getActiveView();
    if (!view) {
        viewBar.style.display = 'none';
        return;
    }
    viewBar.style.display = '';
    viewNameInput.value = view.name;
    viewColsInput.value = view.cols;
    viewRowsInput.value = view.rows;
}

viewNameInput.addEventListener('change', async () => {
    const view = getActiveView();
    if (!view) return;
    const name = viewNameInput.value.trim();
    if (!name) return;
    const newSlug = slugify(name);
    await saveView({ id: view.id, name, slug: newSlug });
    activeViewSlug = newSlug;
    window.location.hash = newSlug;
    renderViewsSidebar();
});

viewColsInput.addEventListener('change', () => {
    const view = getActiveView();
    if (!view) return;
    const cols = Math.max(1, Math.min(8, parseInt(viewColsInput.value) || 4));
    const oldSize = view.rows * view.cols;
    view.cols = cols;
    resizeGrid(view, view.rows, cols);
    saveViewDebounced({ id: view.id, cols: view.cols, grid: view.grid });
    renderGrid();
    renderCameraList();
});

viewRowsInput.addEventListener('change', () => {
    const view = getActiveView();
    if (!view) return;
    const rows = Math.max(1, Math.min(8, parseInt(viewRowsInput.value) || 3));
    view.rows = rows;
    resizeGrid(view, rows, view.cols);
    saveViewDebounced({ id: view.id, rows: view.rows, grid: view.grid });
    renderGrid();
    renderCameraList();
});

function resizeGrid(view, newRows, newCols) {
    const newSize = newRows * newCols;
    const oldGrid = view.grid || [];
    const newGrid = [];
    for (let i = 0; i < newSize; i++) {
        newGrid.push(i < oldGrid.length ? oldGrid[i] : null);
    }
    view.grid = newGrid;
    view.rows = newRows;
    view.cols = newCols;
}

deleteViewBtn.addEventListener('click', async () => {
    const view = getActiveView();
    if (!view) return;
    if (!confirm('Delete this view?')) return;
    await deleteViewById(view.id);
    activeViewSlug = null;
    history.replaceState(null, '', window.location.pathname);
    renderViewsSidebar();
    updateViewBar();
    renderGrid();
});

addViewBtn.addEventListener('click', async () => {
    const name = prompt('View name:');
    if (!name || !name.trim()) return;
    const slug = slugify(name.trim());
    await saveView({ name: name.trim(), slug, cols: 4, rows: 3, grid: new Array(12).fill(null) });
    activeViewSlug = slug;
    window.location.hash = slug;
    renderViewsSidebar();
    renderCameraList();
    updateViewBar();
    renderGrid();
});

// --- Sidebar divider drag ---

let dividerDragging = false;
sidebarDivider.addEventListener('mousedown', (e) => {
    dividerDragging = true;
    sidebarDivider.classList.add('dragging');
    e.preventDefault();
});
document.addEventListener('mousemove', (e) => {
    if (!dividerDragging) return;
    const sidebarRect = document.getElementById('left-sidebar').getBoundingClientRect();
    const offset = e.clientY - sidebarRect.top;
    const minH = 60;
    const maxH = sidebarRect.height - 60 - 5;
    sidebarTop.style.height = Math.max(minH, Math.min(maxH, offset)) + 'px';
    sidebarTop.style.flex = 'none';
});
document.addEventListener('mouseup', () => {
    if (dividerDragging) {
        dividerDragging = false;
        sidebarDivider.classList.remove('dragging');
    }
});

// --- Load cameras ---

async function loadCameras() {
    try {
        const resp = await fetch('/api/cameras');
        allCameras = await resp.json();
        renderViewsSidebar();
        renderCameraList();
        updateViewBar();
        renderGrid();
    } catch (e) {
        console.error('Failed to load cameras', e);
        setTimeout(loadCameras, 5000);
    }
}

// --- On-demand stream management ---

function getVisibleCameraIds() {
    const view = getActiveView();
    if (!view) {
        // "All Cameras" mode: all enabled cameras
        return allCameras.map(c => c.id);
    }
    // Custom view: only cameras in the grid
    return view.grid.filter(id => id != null);
}

function syncStreams() {
    clearTimeout(streamSyncTimer);
    streamSyncTimer = setTimeout(() => {
        const ids = getVisibleCameraIds();
        fetch('/api/streams/sync', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ camera_ids: ids }),
        }).catch(e => console.warn('Stream sync failed', e));
    }, 300);
}

// --- Render grid ---

function renderGrid() {
    // Destroy old players
    Object.values(players).forEach(p => { if (p.hls) p.hls.destroy(); });
    players = {};
    grid.innerHTML = '';

    const view = getActiveView();

    if (!view) {
        // "All Cameras" mode: auto-layout
        const cols = Math.min(4, allCameras.length) || 1;
        const rows = Math.ceil(allCameras.length / cols) || 1;
        grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
        grid.style.gridTemplateRows = `repeat(${rows}, 1fr)`;

        allCameras.forEach(cam => {
            const tile = createCameraTile(cam);
            grid.appendChild(tile);
        });
    } else {
        // Custom view: exact rows x cols grid with slots
        grid.style.gridTemplateColumns = `repeat(${view.cols}, 1fr)`;
        grid.style.gridTemplateRows = `repeat(${view.rows}, 1fr)`;

        const totalSlots = view.rows * view.cols;
        for (let i = 0; i < totalSlots; i++) {
            const camId = view.grid[i];
            const cam = camId != null ? allCameras.find(c => c.id === camId) : null;

            if (cam) {
                const tile = createCameraTile(cam);
                // Allow dragging tiles within grid to rearrange
                tile.draggable = true;
                tile.addEventListener('dragstart', (e) => {
                    e.dataTransfer.setData('text/plain', String(cam.id));
                    e.dataTransfer.setData('slot-index', String(i));
                    e.dataTransfer.effectAllowed = 'all';
                    tile.style.opacity = '0.5';
                });
                tile.addEventListener('dragend', () => { tile.style.opacity = ''; });
                addDropTarget(tile, i, view);
                grid.appendChild(tile);
            } else {
                // Empty slot
                const slot = document.createElement('div');
                slot.className = 'grid-slot-empty';
                addDropTarget(slot, i, view);
                grid.appendChild(slot);
            }
        }
    }

    // Tell backend which cameras need streaming
    syncStreams();
}

function addDropTarget(el, slotIndex, view) {
    el.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'copy';
        el.classList.add('drop-target');
    });
    el.addEventListener('dragleave', () => el.classList.remove('drop-target'));
    el.addEventListener('drop', async (e) => {
        e.preventDefault();
        el.classList.remove('drop-target');

        const camId = parseInt(e.dataTransfer.getData('text/plain'));
        const fromSlot = e.dataTransfer.getData('slot-index');
        if (isNaN(camId)) return;

        if (fromSlot !== '') {
            // Rearranging within grid: swap slots
            const fromIdx = parseInt(fromSlot);
            const toIdx = slotIndex;
            if (fromIdx === toIdx) return;
            const temp = view.grid[toIdx];
            view.grid[toIdx] = view.grid[fromIdx];
            view.grid[fromIdx] = temp;
        } else {
            // Dragging from camera list: place in slot
            // Remove from any existing slot first
            for (let j = 0; j < view.grid.length; j++) {
                if (view.grid[j] === camId) view.grid[j] = null;
            }
            view.grid[slotIndex] = camId;
        }

        await saveView({ id: view.id, grid: view.grid });
        renderGrid();
        renderCameraList();
    });
}

function createCameraTile(cam) {
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
    overlay.innerHTML = `<span class="status-dot"></span>${esc(cam.name)}`;
    tile.appendChild(overlay);

    const dot = overlay.querySelector('.status-dot');
    startStream(cam, video, dot, false);

    let clickTimer = null;
    tile.addEventListener('click', () => {
        if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; return; }
        clickTimer = setTimeout(() => { clickTimer = null; selectCamera(cam, tile); }, 250);
    });

    // Double-click for fullscreen + main stream
    tile.addEventListener('dblclick', (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; }
        fullscreenCamId = cam.id;
        // Start main stream relay, then switch HLS source
        fetch(`/api/streams/${cam.id}/main/start`, { method: 'POST' })
            .then(() => {
                setTimeout(() => switchStream(cam.id, true), 1000);
            })
            .catch(() => switchStream(cam.id, true));
        if (tile.requestFullscreen) tile.requestFullscreen();
    });

    return tile;
}

// --- Fullscreen: main stream handling ---

document.addEventListener('fullscreenchange', () => {
    if (!document.fullscreenElement && fullscreenCamId != null) {
        // Exited fullscreen — switch back to sub stream and stop main relay
        switchStream(fullscreenCamId, false);
        fetch(`/api/streams/${fullscreenCamId}/main/stop`, { method: 'POST' }).catch(() => {});
        fullscreenCamId = null;
    }
});

function switchStream(camId, toMain) {
    const p = players[camId];
    if (!p || !p.hls) return;
    const url = toMain ? p.cam.main_stream_url : p.cam.stream_url;
    p.hls.loadSource(url);
}

// --- HLS streaming ---

function startStream(cam, video, dot, isMain) {
    const url = isMain ? cam.main_stream_url : cam.stream_url;

    if (!Hls.isSupported()) {
        video.src = url;
        dot.className = 'status-dot live';
        players[cam.id] = { hls: null, video, dot, cam };
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
            setTimeout(() => { hls.loadSource(url); }, 3000);
        }
    });

    players[cam.id] = { hls, video, dot, cam };
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

// D-pad
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

// Zoom
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

// Escape
document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
        sidebar.classList.remove('open');
        document.querySelectorAll('.camera-tile.selected').forEach(t => t.classList.remove('selected'));
        selectedCamera = null;
    }
});

// Resize (debounced)
let resizeTimer;
window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
        if (allCameras.length) renderGrid();
    }, 250);
});

// URL hash routing
function initFromHash() {
    const hash = window.location.hash.replace('#', '');
    if (hash && views.find(v => v.slug === hash)) {
        activeViewSlug = hash;
    }
}

window.addEventListener('hashchange', () => {
    const hash = window.location.hash.replace('#', '');
    if (hash) {
        if (views.find(v => v.slug === hash) && activeViewSlug !== hash) {
            activeViewSlug = hash;
            renderViewsSidebar();
            renderCameraList();
            updateViewBar();
            renderGrid();
        }
    } else if (activeViewSlug) {
        activeViewSlug = null;
        renderViewsSidebar();
        renderCameraList();
        updateViewBar();
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

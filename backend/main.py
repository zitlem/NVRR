"""NVRR — FastAPI backend for IP camera monitoring."""

import os
import json
import asyncio
import logging
import time as _time
import threading as _threading
from contextlib import asynccontextmanager

from pathlib import Path

# Load config
_config_path = Path(__file__).resolve().parent.parent / "config.json"
CONFIG = {}
if _config_path.exists():
    with open(_config_path) as f:
        CONFIG = json.load(f)

APP_PORT = int(CONFIG.get("port", os.environ.get("NVRR_PORT", 8000)))

from fastapi import FastAPI, Depends, HTTPException, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import aiohttp as aiohttp_lib
import httpx

from database import get_db, init_db
from discovery import discover_devices
from isapi import discover_cameras, check_nvr_connection, discover_sdk_port, fetch_camera_names, probe_isapi
from onvif_ptz import continuous_move, stop_move, goto_preset, get_presets, clear_cache
from mediamtx import sync_paths
from stream_relay import relay_manager
from hcnetsdk import sdk as sdk_mod

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
# Set to "sdk" to use HCNetSDK, or "rtsp" for direct RTSP (default)
STREAM_MODE = os.environ.get("STREAM_MODE", "sdk")

# Per-client stream tracking
# client_id -> { "camera_ids": set, "last_seen": float }
_clients: dict[str, dict] = {}
_clients_lock = _threading.Lock()
HEARTBEAT_TIMEOUT = 30  # seconds
# On-demand main streams (started via /main/start, stopped via /main/stop)
_ondemand_main: set[int] = set()


def _get_wanted_cameras() -> tuple[set[int], set[int]]:
    """Union of all active clients' camera needs. Returns (sub_ids, main_ids)."""
    now = _time.time()
    sub = set()
    main = set()
    with _clients_lock:
        for cid, info in list(_clients.items()):
            if now - info["last_seen"] > HEARTBEAT_TIMEOUT:
                del _clients[cid]  # stale client
            else:
                sub |= info["camera_ids"]
                if info.get("include_main"):
                    main |= info["camera_ids"]
    return sub, main


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _sync_camera_names()
    if STREAM_MODE != "sdk":
        await _sync_mediamtx()
    # Sync camera names every 5 minutes
    name_sync_task = asyncio.create_task(_periodic_name_sync())
    heartbeat_task = asyncio.create_task(_heartbeat_monitor())
    yield
    name_sync_task.cancel()
    heartbeat_task.cancel()
    if STREAM_MODE == "sdk":
        relay_manager.stop_all()
    # Kill MediaMTX so it doesn't linger after the script exits
    _kill_mediamtx()


async def _periodic_name_sync():
    """Sync camera names from NVRs every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        try:
            await _sync_camera_names()
        except Exception as e:
            logger.warning("Periodic name sync failed: %s", e)


async def _heartbeat_monitor():
    """Periodically prune stale clients and stop streams no one needs."""
    while True:
        await asyncio.sleep(10)
        wanted_sub, wanted_main = _get_wanted_cameras()

        for s in relay_manager.get_status():
            cam_id = s["camera_id"]
            if s["stream_type"] == 1 and cam_id not in wanted_sub:
                logger.info("Stopping sub stream no longer needed: cam%d", cam_id)
                relay_manager.stop_relay(cam_id, "")
            elif s["stream_type"] == 0 and cam_id not in wanted_main and cam_id not in _ondemand_main:
                logger.info("Stopping main stream no longer needed: cam%d", cam_id)
                relay_manager.stop_relay(cam_id, "_main")


async def _sync_camera_names():
    """Refresh camera names and connected status from all NVRs."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id, ip, port, sdk_port, username, password FROM nvrs")
        nvrs = await cursor.fetchall()
        for nvr in nvrs:
            try:
                names = await fetch_camera_names(
                    nvr["ip"], nvr["username"], nvr["password"], nvr["port"]
                )
                cursor2 = await db.execute(
                    "SELECT id, channel FROM cameras WHERE nvr_id = ?", (nvr["id"],)
                )
                cams = await cursor2.fetchall()
                channels = [cam["channel"] for cam in cams]

                # Probe SDK connected status
                connected_map = await _probe_sdk_connected(
                    nvr["ip"], nvr.get("sdk_port", 8000),
                    nvr["username"], nvr["password"], channels,
                )

                for cam in cams:
                    ch = cam["channel"]
                    is_connected = int(connected_map.get(ch, True))
                    name = names.get(ch) if names else None
                    if name:
                        await db.execute(
                            "UPDATE cameras SET name = ?, connected = ? WHERE id = ?",
                            (name, is_connected, cam["id"]),
                        )
                    else:
                        await db.execute(
                            "UPDATE cameras SET connected = ? WHERE id = ?",
                            (is_connected, cam["id"]),
                        )
                logger.info("Synced names and connected status for NVR %s", nvr["ip"])
            except Exception as e:
                logger.warning("Could not sync NVR %s: %s", nvr["ip"], e)
        await db.commit()
    finally:
        await db.close()


def _kill_mediamtx():
    """Kill MediaMTX process if running."""
    import subprocess
    try:
        subprocess.run(
            ["taskkill", "/f", "/im", "mediamtx.exe"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        logger.info("MediaMTX stopped")
    except Exception:
        pass  # Not on Windows or not running


app = FastAPI(title="NVRR", lifespan=lifespan)

# Serve frontend static files (used on Windows / dev mode without Nginx)
_frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if _frontend_dir.is_dir():
    app.mount("/css", StaticFiles(directory=_frontend_dir / "css"), name="css")
    app.mount("/js", StaticFiles(directory=_frontend_dir / "js"), name="js")

    from fastapi.responses import FileResponse

    @app.get("/admin.html")
    async def serve_admin():
        return FileResponse(_frontend_dir / "admin.html")

    @app.get("/")
    async def serve_viewer():
        return FileResponse(_frontend_dir / "index.html")


# --- HLS proxy (dev mode without Nginx) ---

MEDIAMTX_HLS = "http://127.0.0.1:8888"

@app.get("/hls/{path:path}")
async def proxy_hls(path: str, request: Request):
    """Proxy HLS requests to MediaMTX."""
    url = f"{MEDIAMTX_HLS}/{path}"
    async with aiohttp_lib.ClientSession() as session:
        try:
            async with session.get(url, timeout=aiohttp_lib.ClientTimeout(total=10)) as resp:
                headers = {
                    "Content-Type": resp.content_type or "application/octet-stream",
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": "no-cache",
                }
                body = await resp.read()
                return StreamingResponse(
                    iter([body]),
                    status_code=resp.status,
                    headers=headers,
                )
        except Exception:
            raise HTTPException(502, "MediaMTX not reachable")


# --- Debug ---

@app.get("/api/debug/rtsp-test")
async def debug_rtsp_test():
    """Test RTSP connectivity to the first camera's NVR."""
    import socket as sock
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT cameras.rtsp_url, nvrs.ip FROM cameras "
            "JOIN nvrs ON cameras.nvr_id = nvrs.id LIMIT 1"
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    if not row:
        return {"error": "No cameras in DB"}

    nvr_ip = row["ip"]
    results = {}

    # Test TCP to RTSP port 554
    for port, name in [(554, "rtsp"), (80, "http")]:
        try:
            s = sock.socket(sock.AF_INET, sock.SOCK_STREAM)
            s.settimeout(3)
            s.connect((nvr_ip, port))
            s.close()
            results[name] = f"OK — {nvr_ip}:{port} reachable"
        except Exception as e:
            results[name] = f"FAIL — {nvr_ip}:{port} — {e}"

    results["nvr_ip"] = nvr_ip
    return results


@app.get("/api/debug/mediamtx")
async def debug_mediamtx():
    """Check MediaMTX API status, path configs, and DB RTSP URLs."""
    mtx_api = MEDIAMTX_HLS.replace(":8888", ":9997")
    result = {"api_reachable": False, "paths_runtime": [], "paths_config": [], "db_cameras": [], "error": None}
    try:
        async with aiohttp_lib.ClientSession() as session:
            # Runtime state
            async with session.get(
                f"{mtx_api}/v3/paths/list",
                timeout=aiohttp_lib.ClientTimeout(total=5),
            ) as resp:
                result["api_reachable"] = True
                data = await resp.json()
                result["paths_runtime"] = data.get("items", [])

            # Path configs
            async with session.get(
                f"{mtx_api}/v3/config/paths/list",
                timeout=aiohttp_lib.ClientTimeout(total=5),
            ) as resp:
                data = await resp.json()
                result["paths_config"] = data.get("items", [])
    except Exception as e:
        result["error"] = str(e)

    # DB camera RTSP URLs
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id, name, rtsp_url, enabled FROM cameras LIMIT 3")
        rows = await cursor.fetchall()
        # Mask password in output
        for r in rows:
            r = dict(r)
            url = r["rtsp_url"]
            # Show URL structure but mask the password
            import re
            r["rtsp_url"] = re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", url)
            result["db_cameras"].append(r)
    finally:
        await db.close()

    result["stream_mode"] = STREAM_MODE
    return result


@app.get("/api/debug/relays")
async def debug_relays():
    """Check SDK relay status."""
    return {
        "stream_mode": STREAM_MODE,
        "relays": relay_manager.get_status(),
    }


@app.get("/api/debug/sdk-channels/{nvr_id}")
async def debug_sdk_channels(nvr_id: int):
    """Probe which SDK channels are valid on an NVR by attempting real_play on each."""
    nvr = await _get_nvr_credentials(nvr_id)
    ip, port = nvr["ip"], nvr.get("sdk_port", 8000)

    loop = asyncio.get_event_loop()

    def _probe():
        if not relay_manager.init_sdk():
            return {"error": "SDK init failed"}
        user_id = relay_manager._get_user_id(ip, port, nvr["username"], nvr["password"])
        if user_id < 0:
            return {"error": f"SDK login failed for {ip}:{port}"}

        start_dchan = relay_manager._start_dchans.get(f"{ip}:{port}", 0)
        results = {"ip": ip, "sdk_port": port, "startDChan": start_dchan, "channels": []}

        # Probe channels 1-64
        from hcnetsdk import REALDATACALLBACK
        @REALDATACALLBACK
        def _noop(h, t, p, s, u):
            pass

        for ch in range(1, 65):
            handle = sdk_mod.real_play(user_id, ch, _noop, stream_type=1)
            if handle >= 0:
                sdk_mod.stop_real_play(handle)
                results["channels"].append({"channel": ch, "status": "ok"})
            else:
                err = sdk_mod.get_last_error()
                results["channels"].append({"channel": ch, "status": f"error {err}"})

        return results

    return await loop.run_in_executor(None, _probe)


@app.get("/api/debug/isapi-names")
async def debug_isapi_names():
    """Test ISAPI camera name resolution for all NVRs."""
    from isapi import _detect_auth, _make_client, _fetch_video_input_names, _hik_ns, _hik_find, _hik_findall

    db = await get_db()
    try:
        cursor = await db.execute("SELECT id, ip, port, username, password FROM nvrs")
        nvrs = await cursor.fetchall()
    finally:
        await db.close()

    results = {}
    for nvr in nvrs:
        base = f"http://{nvr['ip']}:{nvr['port']}"
        nvr_result = {"auth": None, "video_input_names": {}, "streaming_channels": [], "error": None}
        try:
            auth = await _detect_auth(nvr["username"], nvr["password"], base)
            nvr_result["auth"] = "Digest" if isinstance(auth, httpx.DigestAuth) else "Basic"
            async with _make_client(auth, base) as client:

                # Get video input names
                proxy_names = await _fetch_video_input_names(client)
                nvr_result["video_input_names"] = proxy_names

                # Get streaming channels and show name resolution
                resp = await client.get("/ISAPI/Streaming/channels")
                if resp.status_code == 200:
                    import xml.etree.ElementTree as _ET
                    root = _ET.fromstring(resp.text)
                    ns = _hik_ns(root)
                    seen = set()
                    for ch in _hik_findall(root, "StreamingChannel", ns):
                        id_el = _hik_find(ch, "id", ns)
                        name_el = _hik_find(ch, "channelName", ns)
                        if id_el is None:
                            continue
                        ch_id = int(id_el.text)
                        if ch_id % 100 != 1:
                            continue
                        cam_num = ch_id // 100
                        if cam_num in seen:
                            continue
                        seen.add(cam_num)
                        ch_name = name_el.text if name_el is not None else None
                        proxy_name = proxy_names.get(cam_num)
                        final_name = proxy_name or ch_name or f"Camera {cam_num}"
                        if final_name.isdigit():
                            final_name = f"Camera {cam_num}"
                        nvr_result["streaming_channels"].append({
                            "channel_id": ch_id,
                            "camera_num": cam_num,
                            "channelName": ch_name,
                            "proxy_name": proxy_name,
                            "final_name": final_name,
                        })
                else:
                    nvr_result["error"] = f"Streaming/channels HTTP {resp.status_code}"
        except Exception as e:
            nvr_result["error"] = str(e)

        results[f"NVR {nvr['id']} ({nvr['ip']})"] = nvr_result

    return results


# --- Auth ---

async def require_admin(x_admin_password: str = Header(None)):
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")


# --- Models ---

class NVRCreate(BaseModel):
    ip: str
    username: str
    password: str
    port: int = 80
    sdk_ports: list[int] = []  # empty = auto-discover defaults; single = use directly; multiple = probe those


class NVRUpdate(BaseModel):
    port: int | None = None
    sdk_port: int | None = None
    alias: str | None = None


class CameraUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    ptz_enabled: bool | None = None


class PTZMove(BaseModel):
    pan: float = 0
    tilt: float = 0
    zoom: float = 0


class ViewCreate(BaseModel):
    name: str
    slug: str
    cols: int = 4
    rows: int = 3
    grid: list[int | None] = []  # length = rows*cols, null = empty slot


class ViewUpdate(BaseModel):
    name: str | None = None
    slug: str | None = None
    cols: int | None = None
    rows: int | None = None
    grid: list[int | None] | None = None


# --- Helpers ---

async def _sync_mediamtx():
    """Read all cameras from DB and sync to MediaMTX via API."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT cameras.id, cameras.rtsp_url, cameras.enabled "
            "FROM cameras"
        )
        rows = await cursor.fetchall()
        cameras = [dict(r) for r in rows]
        await sync_paths(cameras)
    finally:
        await db.close()


async def _probe_sdk_connected(ip: str, sdk_port: int, username: str, password: str,
                                channels: list[int]) -> dict[int, bool]:
    """Probe SDK channels and return {isapi_channel: connected} map.
    Only works in SDK mode. Returns all True if SDK is unavailable."""
    if STREAM_MODE != "sdk":
        return {ch: True for ch in channels}

    loop = asyncio.get_event_loop()

    def _probe():
        if not relay_manager.init_sdk():
            return {ch: True for ch in channels}
        user_id = relay_manager._get_user_id(ip, sdk_port, username, password)
        if user_id < 0:
            return {ch: True for ch in channels}

        start_dchan = relay_manager._start_dchans.get(f"{ip}:{sdk_port}", 33)
        from hcnetsdk import REALDATACALLBACK
        @REALDATACALLBACK
        def _noop(h, t, p, s, u):
            pass

        result = {}
        for ch in channels:
            sdk_ch = start_dchan - 1 + ch
            handle = sdk_mod.real_play(user_id, sdk_ch, _noop, stream_type=1)
            if handle >= 0:
                sdk_mod.stop_real_play(handle)
                result[ch] = True
            else:
                result[ch] = False
        return result

    return await loop.run_in_executor(None, _probe)


async def _get_nvr_credentials(nvr_id: int) -> dict:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM nvrs WHERE id = ?", (nvr_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "NVR not found")
        return dict(row)
    finally:
        await db.close()


async def _get_ptz_camera(camera_id: int) -> dict:
    """Fetch a PTZ-enabled camera with NVR credentials. Raises 404 if not found."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT cameras.*, nvrs.username, nvrs.password "
            "FROM cameras JOIN nvrs ON cameras.nvr_id = nvrs.id "
            "WHERE cameras.id = ? AND cameras.ptz_enabled = 1",
            (camera_id,),
        )
        cam = await cursor.fetchone()
    finally:
        await db.close()
    if not cam:
        raise HTTPException(404, "Camera not found or PTZ not enabled")
    return dict(cam)


# --- Public API (viewer) ---

@app.get("/api/cameras")
async def list_cameras():
    """List all enabled cameras for the viewer."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT cameras.id, cameras.name, cameras.ptz_enabled, cameras.connected, "
            "cameras.onvif_host, cameras.onvif_port, cameras.nvr_id, "
            "COALESCE(NULLIF(nvrs.alias, ''), nvrs.name) AS nvr_name "
            "FROM cameras JOIN nvrs ON cameras.nvr_id = nvrs.id "
            "WHERE cameras.enabled = 1 "
            "ORDER BY cameras.nvr_id, cameras.channel"
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "ptz_enabled": bool(r["ptz_enabled"]),
                "connected": bool(r["connected"]),
                "nvr_id": r["nvr_id"],
                "nvr_name": r["nvr_name"],
                "stream_url": f"/hls/cam{r['id']}/index.m3u8",
                "main_stream_url": f"/hls/cam{r['id']}_main/index.m3u8",
            }
            for r in rows
        ]
    finally:
        await db.close()


# --- Views API (public — no auth needed) ---

@app.get("/api/views")
async def list_views():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM views ORDER BY sort_order, id")
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "slug": r["slug"],
                "cols": r["cols"],
                "rows": r["rows"],
                "grid": json.loads(r["grid"]),
            }
            for r in rows
        ]
    finally:
        await db.close()


@app.post("/api/views")
async def create_view(body: ViewCreate):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT MAX(sort_order) FROM views")
        row = await cursor.fetchone()
        next_order = (row[0] or 0) + 1
        await db.execute(
            "INSERT INTO views (name, slug, cols, rows, grid, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
            (body.name, body.slug, body.cols, body.rows, json.dumps(body.grid), next_order),
        )
        await db.commit()
        return {"ok": True, "slug": body.slug}
    finally:
        await db.close()


@app.patch("/api/views/{view_id}")
async def update_view(view_id: int, body: ViewUpdate):
    db = await get_db()
    try:
        updates = []
        params = []
        if body.name is not None:
            updates.append("name = ?")
            params.append(body.name)
        if body.slug is not None:
            updates.append("slug = ?")
            params.append(body.slug)
        if body.cols is not None:
            updates.append("cols = ?")
            params.append(body.cols)
        if body.rows is not None:
            updates.append("rows = ?")
            params.append(body.rows)
        if body.grid is not None:
            updates.append("grid = ?")
            params.append(json.dumps(body.grid))
        if not updates:
            return {"ok": True}
        params.append(view_id)
        await db.execute(f"UPDATE views SET {', '.join(updates)} WHERE id = ?", params)
        await db.commit()
        return {"ok": True}
    finally:
        await db.close()


@app.delete("/api/views/{view_id}")
async def delete_view(view_id: int):
    db = await get_db()
    try:
        await db.execute("DELETE FROM views WHERE id = ?", (view_id,))
        await db.commit()
        return {"ok": True}
    finally:
        await db.close()


class StreamSync(BaseModel):
    client_id: str = "default"
    camera_ids: list[int]  # camera IDs this client needs
    include_main: bool = False  # also start main (HD) streams


class HeartbeatRequest(BaseModel):
    client_id: str


@app.post("/api/streams/heartbeat")
async def streams_heartbeat(body: HeartbeatRequest):
    """Viewer heartbeat — keeps this client's streams alive."""
    with _clients_lock:
        if body.client_id in _clients:
            _clients[body.client_id]["last_seen"] = _time.time()
    return {"ok": True}


@app.post("/api/streams/sync")
async def streams_sync(body: StreamSync):
    """Register this client's camera needs and reconcile running streams.
    Streams are the union of all connected clients' needs."""
    if STREAM_MODE != "sdk":
        return {"ok": True, "mode": "rtsp"}

    # Update this client's state
    with _clients_lock:
        _clients[body.client_id] = {
            "camera_ids": set(body.camera_ids),
            "include_main": body.include_main,
            "last_seen": _time.time(),
        }

    # Compute union of all clients' needs
    wanted_sub, wanted_main = _get_wanted_cameras()

    # Get currently running camera IDs by stream type
    current_sub = set()
    current_main = set()
    for status in relay_manager.get_status():
        if status["stream_type"] == 1:
            current_sub.add(status["camera_id"])
        elif status["stream_type"] == 0:
            current_main.add(status["camera_id"])

    sub_start = wanted_sub - current_sub
    sub_stop = current_sub - wanted_sub
    main_start = wanted_main - current_main
    main_stop = current_main - wanted_main

    # Stop streams no client needs
    for cam_id in sub_stop:
        relay_manager.stop_relay(cam_id, "")
    for cam_id in main_stop:
        relay_manager.stop_relay(cam_id, "_main")

    # Start newly needed streams
    all_start = sub_start | main_start
    if all_start:
        db = await get_db()
        try:
            placeholders = ",".join("?" for _ in all_start)
            cursor = await db.execute(
                f"SELECT cameras.id, cameras.channel, "
                f"nvrs.ip, nvrs.sdk_port, nvrs.username, nvrs.password "
                f"FROM cameras JOIN nvrs ON cameras.nvr_id = nvrs.id "
                f"WHERE cameras.id IN ({placeholders}) AND cameras.enabled = 1 AND cameras.connected = 1",
                list(all_start),
            )
            rows = await cursor.fetchall()
        finally:
            await db.close()

        loop = asyncio.get_event_loop()
        for row in rows:
            r = dict(row)
            if r["id"] in sub_start:
                await loop.run_in_executor(
                    None,
                    relay_manager.start_relay,
                    r["id"], r["ip"], r["sdk_port"],
                    r["username"], r["password"], r["channel"],
                    1, "",  # sub stream
                )
            if r["id"] in main_start:
                await loop.run_in_executor(
                    None,
                    relay_manager.start_relay,
                    r["id"], r["ip"], r["sdk_port"],
                    r["username"], r["password"], r["channel"],
                    0, "_main",  # main stream
                )

    return {
        "ok": True,
        "sub_started": len(sub_start),
        "main_started": len(main_start),
        "sub_stopped": len(sub_stop),
        "main_stopped": len(main_stop),
        "active_clients": len(_clients),
    }


@app.post("/api/streams/{camera_id}/main/start")
async def stream_main_start(camera_id: int):
    """Start main (high-res) stream relay for a camera. Used for fullscreen."""
    if STREAM_MODE != "sdk":
        return {"ok": True, "mode": "rtsp"}

    _ondemand_main.add(camera_id)

    # Check if already running
    for s in relay_manager.get_status():
        if s["camera_id"] == camera_id and s["stream_type"] == 0:
            return {"ok": True, "already_running": True}

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT cameras.id, cameras.channel, "
            "nvrs.ip, nvrs.sdk_port, nvrs.username, nvrs.password "
            "FROM cameras JOIN nvrs ON cameras.nvr_id = nvrs.id "
            "WHERE cameras.id = ? AND cameras.enabled = 1",
            (camera_id,),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    if not row:
        raise HTTPException(404, "Camera not found")

    r = dict(row)
    loop = asyncio.get_event_loop()
    success = await loop.run_in_executor(
        None,
        relay_manager.start_relay,
        r["id"], r["ip"], r["sdk_port"],
        r["username"], r["password"], r["channel"],
        0, "_main",  # main stream
    )
    return {"ok": success}


@app.post("/api/streams/{camera_id}/main/stop")
async def stream_main_stop(camera_id: int):
    """Stop main (high-res) stream relay for a camera."""
    if STREAM_MODE != "sdk":
        return {"ok": True, "mode": "rtsp"}
    _ondemand_main.discard(camera_id)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, relay_manager.stop_relay, camera_id, "_main")
    return {"ok": True}


@app.post("/api/ptz/{camera_id}/move")
async def ptz_move(camera_id: int, body: PTZMove):
    """Start continuous PTZ movement."""
    cam = await _get_ptz_camera(camera_id)
    await continuous_move(
        cam["onvif_host"] or cam["ip"],
        cam["onvif_port"] or 80,
        cam["username"],
        cam["password"],
        body.pan, body.tilt, body.zoom,
    )
    return {"status": "moving"}


@app.post("/api/ptz/{camera_id}/stop")
async def ptz_stop(camera_id: int):
    """Stop PTZ movement."""
    cam = await _get_ptz_camera(camera_id)
    await stop_move(
        cam["onvif_host"] or cam["ip"],
        cam["onvif_port"] or 80,
        cam["username"],
        cam["password"],
    )
    return {"status": "stopped"}


@app.post("/api/ptz/{camera_id}/preset/{preset_token}")
async def ptz_goto_preset(camera_id: int, preset_token: int):
    """Move camera to a preset."""
    cam = await _get_ptz_camera(camera_id)
    await goto_preset(
        cam["onvif_host"] or cam["ip"],
        cam["onvif_port"] or 80,
        cam["username"],
        cam["password"],
        preset_token,
    )
    return {"status": "moving to preset"}


@app.get("/api/ptz/{camera_id}/presets")
async def ptz_list_presets(camera_id: int):
    """List PTZ presets for a camera."""
    cam = await _get_ptz_camera(camera_id)
    presets = await get_presets(
        cam["onvif_host"] or cam["ip"],
        cam["onvif_port"] or 80,
        cam["username"],
        cam["password"],
    )
    return presets


# --- Admin API ---

@app.get("/api/admin/adapters", dependencies=[Depends(require_admin)])
async def admin_list_adapters():
    """List local network adapters for discovery."""
    from discovery import _get_local_ips
    local_ips = _get_local_ips()
    return [
        {"ip": ip, "subnet": ".".join(ip.split(".")[:3]) + ".x"}
        for ip in local_ips
        if len(ip.split(".")) == 4
    ]


@app.get("/api/admin/discover/onvif", dependencies=[Depends(require_admin)])
async def admin_discover_onvif(adapter: str = Query(None)):
    """Phase 1: WS-Discovery multicast probe."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT ip FROM nvrs")
        rows = await cursor.fetchall()
        known_ips = {r["ip"] for r in rows}
    finally:
        await db.close()

    devices = await discover_devices(adapter=adapter)
    return [
        {
            "ip": d.ip,
            "port": d.port,
            "name": d.name,
            "model": d.model,
            "hardware": d.hardware,
            "already_added": d.ip in known_ips,
            "discovered_by": "ONVIF",
        }
        for d in devices
    ]


@app.get("/api/admin/discover/isapi", dependencies=[Depends(require_admin)])
async def admin_discover_isapi(adapter: str = Query(None), exclude: str = Query("")):
    """Phase 2: ISAPI subnet probe, skipping IPs already found."""
    exclude_ips = set(exclude.split(",")) if exclude else set()

    db = await get_db()
    try:
        cursor = await db.execute("SELECT ip FROM nvrs")
        rows = await cursor.fetchall()
        known_ips = {r["ip"] for r in rows}
    finally:
        await db.close()

    from discovery import _get_local_ips
    local_ips = [adapter] if adapter else _get_local_ips()

    probe_ips = []
    for lip in local_ips:
        parts = lip.split(".")
        if len(parts) == 4:
            prefix = ".".join(parts[:3])
            for i in range(1, 255):
                ip = f"{prefix}.{i}"
                if ip != lip and ip not in exclude_ips:
                    probe_ips.append(ip)

    results = []
    batch_size = 50
    for i in range(0, len(probe_ips), batch_size):
        batch = probe_ips[i:i + batch_size]
        batch_results = await asyncio.gather(
            *[probe_isapi(ip, timeout=1.0) for ip in batch],
            return_exceptions=True,
        )
        for r in batch_results:
            if isinstance(r, dict) and r:
                results.append({
                    "ip": r["ip"],
                    "port": r["port"],
                    "name": r["name"],
                    "model": r["model"],
                    "hardware": r.get("hardware", ""),
                    "already_added": r["ip"] in known_ips,
                    "discovered_by": "ISAPI",
                })

    return results


@app.get("/api/admin/nvrs", dependencies=[Depends(require_admin)])
async def admin_list_nvrs():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id, name, alias, ip, port, sdk_port, channels, created_at FROM nvrs")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


@app.post("/api/admin/nvrs", dependencies=[Depends(require_admin)])
async def admin_add_nvr(body: NVRCreate):
    """Add an NVR and auto-discover its cameras."""
    # Check connection first
    try:
        info = await check_nvr_connection(body.ip, body.username, body.password, body.port)
    except Exception as e:
        raise HTTPException(400, f"Cannot connect to NVR: {e}")

    # Discover cameras
    try:
        discovered = await discover_cameras(body.ip, body.username, body.password, body.port)
    except Exception as e:
        raise HTTPException(400, f"Connected but failed to discover cameras: {e}")

    # Probe default SDK ports + any user-supplied ports
    extra_ports = body.sdk_ports if body.sdk_ports else []
    sdk_port = await discover_sdk_port(body.ip, extra_ports=extra_ports) or 8000
    logger.info("SDK port for %s: %d (auto-discovered)", body.ip, sdk_port)

    # Probe which channels actually have cameras connected
    connected_map = await _probe_sdk_connected(
        body.ip, sdk_port, body.username, body.password,
        [cam.channel for cam in discovered],
    )

    db = await get_db()
    try:
        # Insert NVR
        cursor = await db.execute(
            "INSERT INTO nvrs (name, ip, username, password, port, sdk_port, channels) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (info["name"], body.ip, body.username, body.password, body.port, sdk_port, len(discovered)),
        )
        nvr_id = cursor.lastrowid

        # Insert cameras
        for cam in discovered:
            await db.execute(
                "INSERT INTO cameras (nvr_id, channel, name, rtsp_url, connected, onvif_host, onvif_port) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (nvr_id, cam.channel, cam.name, cam.rtsp_url,
                 int(connected_map.get(cam.channel, True)), body.ip, body.port),
            )

        await db.commit()
    finally:
        await db.close()

    # Sync MediaMTX config
    await _sync_mediamtx()

    return {
        "nvr_id": nvr_id,
        "name": info["name"],
        "cameras_found": len(discovered),
        "sdk_port": sdk_port,
    }


@app.patch("/api/admin/nvrs/{nvr_id}", dependencies=[Depends(require_admin)])
async def admin_update_nvr(nvr_id: int, body: NVRUpdate):
    """Update NVR settings (e.g. HTTP port, SDK port)."""
    updates = []
    params = []
    if body.port is not None:
        updates.append("port = ?")
        params.append(body.port)
    if body.sdk_port is not None:
        updates.append("sdk_port = ?")
        params.append(body.sdk_port)
    if body.alias is not None:
        updates.append("alias = ?")
        params.append(body.alias)
    if not updates:
        raise HTTPException(400, "No fields to update")
    params.append(nvr_id)
    db = await get_db()
    try:
        await db.execute(f"UPDATE nvrs SET {', '.join(updates)} WHERE id = ?", params)
        await db.commit()
    finally:
        await db.close()
    return {"status": "updated"}


@app.post("/api/admin/nvrs/{nvr_id}/test-sdk", dependencies=[Depends(require_admin)])
async def admin_test_sdk(nvr_id: int):
    """Test SDK connection to an NVR using its stored sdk_port."""
    import asyncio as _asyncio
    nvr = await _get_nvr_credentials(nvr_id)
    sdk_port = nvr.get("sdk_port", 8000)
    ip = nvr["ip"]

    # TCP connectivity test
    try:
        _, writer = await _asyncio.wait_for(
            _asyncio.open_connection(ip, sdk_port), timeout=3.0
        )
        writer.close()
        await writer.wait_closed()
    except (OSError, _asyncio.TimeoutError):
        return {"ok": False, "sdk_port": sdk_port, "error": f"Cannot connect to {ip}:{sdk_port}"}

    # If SDK mode, try actual login
    if STREAM_MODE == "sdk":
        loop = _asyncio.get_event_loop()
        try:
            user_id, _ = await loop.run_in_executor(
                None, sdk_mod.login, ip, sdk_port, nvr["username"], nvr["password"]
            )
            if user_id >= 0:
                await loop.run_in_executor(None, sdk_mod.logout, user_id)
                return {"ok": True, "sdk_port": sdk_port, "message": f"SDK login OK on port {sdk_port}"}
            else:
                return {"ok": False, "sdk_port": sdk_port, "error": f"SDK login failed (port {sdk_port} open but auth rejected)"}
        except Exception as e:
            return {"ok": False, "sdk_port": sdk_port, "error": str(e)}

    return {"ok": True, "sdk_port": sdk_port, "message": f"Port {sdk_port} is open"}


@app.delete("/api/admin/nvrs/{nvr_id}", dependencies=[Depends(require_admin)])
async def admin_delete_nvr(nvr_id: int):
    db = await get_db()
    try:
        await db.execute("DELETE FROM nvrs WHERE id = ?", (nvr_id,))
        await db.commit()
    finally:
        await db.close()
    await _sync_mediamtx()
    return {"status": "deleted"}


@app.post("/api/admin/restart", dependencies=[Depends(require_admin)])
async def admin_restart():
    """Restart the NVRR backend process."""
    import signal

    logger.info("Server restart requested via admin panel")

    async def _do_restart():
        await asyncio.sleep(0.5)
        # Send SIGTERM to self — uvicorn will exit, and the start script relaunches
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_do_restart())
    return {"status": "restarting"}


@app.post("/api/admin/factory-reset", dependencies=[Depends(require_admin)])
async def admin_factory_reset():
    """Delete all NVRs, cameras, and reset to factory defaults."""
    # Stop all SDK relays if running
    if STREAM_MODE == "sdk":
        relay_manager.stop_all()

    db = await get_db()
    try:
        await db.execute("DELETE FROM cameras")
        await db.execute("DELETE FROM nvrs")
        await db.execute("DELETE FROM views")
        await db.execute("DELETE FROM sqlite_sequence WHERE name IN ('cameras', 'nvrs', 'views')")
        await db.commit()
    finally:
        await db.close()

    await _sync_mediamtx()
    logger.info("Factory reset completed")
    return {"status": "reset"}


@app.post("/api/admin/nvrs/{nvr_id}/rediscover", dependencies=[Depends(require_admin)])
async def admin_rediscover(nvr_id: int):
    """Re-scan an NVR for new cameras."""
    nvr = await _get_nvr_credentials(nvr_id)

    try:
        discovered = await discover_cameras(nvr["ip"], nvr["username"], nvr["password"], nvr["port"])
    except Exception as e:
        raise HTTPException(400, f"Discovery failed: {e}")

    # Probe which channels have cameras connected
    connected_map = await _probe_sdk_connected(
        nvr["ip"], nvr.get("sdk_port", 8000), nvr["username"], nvr["password"],
        [cam.channel for cam in discovered],
    )

    db = await get_db()
    added = 0
    try:
        for cam in discovered:
            is_connected = int(connected_map.get(cam.channel, True))
            try:
                await db.execute(
                    "INSERT INTO cameras (nvr_id, channel, name, rtsp_url, connected, onvif_host, onvif_port) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (nvr_id, cam.channel, cam.name, cam.rtsp_url, is_connected, nvr["ip"], nvr["port"]),
                )
                added += 1
            except Exception:
                # Already exists — update connected status and name
                await db.execute(
                    "UPDATE cameras SET connected = ?, name = ? WHERE nvr_id = ? AND channel = ?",
                    (is_connected, cam.name, nvr_id, cam.channel),
                )

        await db.execute(
            "UPDATE nvrs SET channels = ? WHERE id = ?",
            (len(discovered), nvr_id),
        )
        await db.commit()
    finally:
        await db.close()

    await _sync_mediamtx()
    return {"total_cameras": len(discovered), "new_cameras": added}


@app.get("/api/admin/cameras", dependencies=[Depends(require_admin)])
async def admin_list_cameras(nvr_id: int = Query(None)):
    db = await get_db()
    try:
        if nvr_id:
            cursor = await db.execute(
                "SELECT cameras.*, COALESCE(NULLIF(nvrs.alias, ''), nvrs.name) AS nvr_name FROM cameras "
                "JOIN nvrs ON cameras.nvr_id = nvrs.id "
                "WHERE nvr_id = ? ORDER BY channel", (nvr_id,)
            )
        else:
            cursor = await db.execute(
                "SELECT cameras.*, COALESCE(NULLIF(nvrs.alias, ''), nvrs.name) AS nvr_name FROM cameras "
                "JOIN nvrs ON cameras.nvr_id = nvrs.id "
                "ORDER BY nvr_id, channel"
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


@app.patch("/api/admin/cameras/{camera_id}", dependencies=[Depends(require_admin)])
async def admin_update_camera(camera_id: int, body: CameraUpdate):
    db = await get_db()
    try:
        updates = []
        values = []
        if body.name is not None:
            updates.append("name = ?")
            values.append(body.name)
        if body.enabled is not None:
            updates.append("enabled = ?")
            values.append(int(body.enabled))
        if body.ptz_enabled is not None:
            updates.append("ptz_enabled = ?")
            values.append(int(body.ptz_enabled))

        if not updates:
            raise HTTPException(400, "No fields to update")

        values.append(camera_id)
        await db.execute(
            f"UPDATE cameras SET {', '.join(updates)} WHERE id = ?", values
        )
        await db.commit()
    finally:
        await db.close()

    # Re-sync if enabled state changed
    if body.enabled is not None:
        await _sync_mediamtx()

    return {"status": "updated"}


@app.post("/api/admin/login")
async def admin_login(x_admin_password: str = Header(None)):
    """Verify admin password."""
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(401, "Invalid password")
    return {"status": "ok"}

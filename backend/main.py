"""NVRR — FastAPI backend for IP camera monitoring."""

import os
import asyncio
import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import aiohttp as aiohttp_lib

from database import get_db, init_db
from discovery import discover_devices
from isapi import discover_cameras, check_nvr_connection, discover_sdk_port, fetch_camera_names
from onvif_ptz import continuous_move, stop_move, goto_preset, get_presets, clear_cache
from mediamtx import sync_paths
from stream_relay import relay_manager
from hcnetsdk import sdk as sdk_mod

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
# Set to "sdk" to use HCNetSDK, or "rtsp" for direct RTSP (default)
STREAM_MODE = os.environ.get("STREAM_MODE", "sdk")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _sync_camera_names()
    if STREAM_MODE == "sdk":
        await _start_sdk_relays()
    else:
        await _sync_mediamtx()
    # Sync camera names every 5 minutes
    name_sync_task = asyncio.create_task(_periodic_name_sync())
    yield
    name_sync_task.cancel()
    if STREAM_MODE == "sdk":
        relay_manager.stop_all()


async def _periodic_name_sync():
    """Sync camera names from NVRs every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        try:
            await _sync_camera_names()
        except Exception as e:
            logger.warning("Periodic name sync failed: %s", e)


async def _sync_camera_names():
    """Refresh camera names from NVR on startup."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id, ip, port, username, password FROM nvrs")
        nvrs = await cursor.fetchall()
        for nvr in nvrs:
            try:
                names = await fetch_camera_names(
                    nvr["ip"], nvr["username"], nvr["password"], nvr["port"]
                )
                if not names:
                    continue
                cursor2 = await db.execute(
                    "SELECT id, channel FROM cameras WHERE nvr_id = ?", (nvr["id"],)
                )
                for cam in await cursor2.fetchall():
                    name = names.get(cam["channel"])
                    if name:
                        await db.execute(
                            "UPDATE cameras SET name = ? WHERE id = ?",
                            (name, cam["id"]),
                        )
                logger.info("Synced camera names for NVR %s", nvr["ip"])
            except Exception as e:
                logger.warning("Could not sync names for NVR %s: %s", nvr["ip"], e)
        await db.commit()
    finally:
        await db.close()


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


@app.get("/api/debug/isapi-names")
async def debug_isapi_names():
    """Try multiple ISAPI endpoints to find camera names from all NVRs."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id, ip, port, username, password FROM nvrs")
        nvrs = await cursor.fetchall()
    finally:
        await db.close()

    results = {}
    endpoints = [
        "/ISAPI/Streaming/channels",
        "/ISAPI/ContentMgmt/InputProxy/channels",
        "/ISAPI/System/Video/inputs/channels",
        "/ISAPI/System/Video/inputs",
    ]

    for nvr in nvrs:
        nvr_result = {}
        auth = aiohttp_lib.BasicAuth(nvr["username"], nvr["password"])
        base = f"http://{nvr['ip']}:{nvr['port']}"
        async with aiohttp_lib.ClientSession(auth=auth) as session:
            for ep in endpoints:
                try:
                    async with session.get(
                        f"{base}{ep}",
                        timeout=aiohttp_lib.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            nvr_result[ep] = await resp.text()
                        else:
                            nvr_result[ep] = f"HTTP {resp.status}"
                except Exception as e:
                    nvr_result[ep] = f"Error: {e}"
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
    sdk_port: int | None = None


class CameraUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    ptz_enabled: bool | None = None


class PTZMove(BaseModel):
    pan: float = 0
    tilt: float = 0
    zoom: float = 0


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


async def _start_sdk_relays():
    """Start HCNetSDK → FFmpeg → MediaMTX relays for all enabled cameras."""
    import asyncio
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT cameras.id, cameras.channel, cameras.enabled, "
            "nvrs.ip, nvrs.port, nvrs.sdk_port, nvrs.username, nvrs.password "
            "FROM cameras JOIN nvrs ON cameras.nvr_id = nvrs.id "
            "WHERE cameras.enabled = 1"
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    if not rows:
        logger.info("No enabled cameras to relay")
        return

    loop = asyncio.get_event_loop()
    for row in rows:
        r = dict(row)
        # Use per-NVR sdk_port from the database
        success = await loop.run_in_executor(
            None,
            relay_manager.start_relay,
            r["id"], r["ip"], r["sdk_port"],
            r["username"], r["password"], r["channel"],
        )
        if success:
            logger.info("SDK relay started for cam%d", r["id"])
        else:
            logger.error("SDK relay FAILED for cam%d (channel %d)", r["id"], r["channel"])


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


# --- Public API (viewer) ---

@app.get("/api/cameras")
async def list_cameras():
    """List all enabled cameras for the viewer."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT cameras.id, cameras.name, cameras.ptz_enabled, "
            "cameras.onvif_host, cameras.onvif_port, cameras.nvr_id "
            "FROM cameras WHERE cameras.enabled = 1 "
            "ORDER BY cameras.nvr_id, cameras.channel"
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "ptz_enabled": bool(r["ptz_enabled"]),
                "stream_url": f"/hls/cam{r['id']}/index.m3u8",
            }
            for r in rows
        ]
    finally:
        await db.close()


@app.post("/api/ptz/{camera_id}/move")
async def ptz_move(camera_id: int, body: PTZMove):
    """Start continuous PTZ movement."""
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

    cam = dict(cam)
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

    cam = dict(cam)
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

    cam = dict(cam)
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

    cam = dict(cam)
    presets = await get_presets(
        cam["onvif_host"] or cam["ip"],
        cam["onvif_port"] or 80,
        cam["username"],
        cam["password"],
    )
    return presets


# --- Admin API ---

@app.get("/api/admin/discover", dependencies=[Depends(require_admin)])
async def admin_discover_network():
    """Scan the local network for ONVIF-compatible devices via WS-Discovery."""
    # Get already-added NVR IPs to mark them
    db = await get_db()
    try:
        cursor = await db.execute("SELECT ip FROM nvrs")
        rows = await cursor.fetchall()
        known_ips = {r["ip"] for r in rows}
    finally:
        await db.close()

    devices = await discover_devices()
    return [
        {
            "ip": d.ip,
            "port": d.port,
            "name": d.name,
            "model": d.model,
            "hardware": d.hardware,
            "already_added": d.ip in known_ips,
        }
        for d in devices
    ]


@app.get("/api/admin/nvrs", dependencies=[Depends(require_admin)])
async def admin_list_nvrs():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id, name, ip, port, sdk_port, channels, created_at FROM nvrs")
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
                "INSERT INTO cameras (nvr_id, channel, name, rtsp_url, onvif_host, onvif_port) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (nvr_id, cam.channel, cam.name, cam.rtsp_url, body.ip, body.port),
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
    """Update NVR settings (e.g. SDK port)."""
    updates = []
    params = []
    if body.sdk_port is not None:
        updates.append("sdk_port = ?")
        params.append(body.sdk_port)
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
        await db.execute("DELETE FROM sqlite_sequence WHERE name IN ('cameras', 'nvrs')")
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

    db = await get_db()
    added = 0
    try:
        for cam in discovered:
            try:
                await db.execute(
                    "INSERT INTO cameras (nvr_id, channel, name, rtsp_url, onvif_host, onvif_port) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (nvr_id, cam.channel, cam.name, cam.rtsp_url, nvr["ip"], nvr["port"]),
                )
                added += 1
            except Exception:
                pass  # Already exists (UNIQUE constraint)

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
                "SELECT * FROM cameras WHERE nvr_id = ? ORDER BY channel", (nvr_id,)
            )
        else:
            cursor = await db.execute("SELECT * FROM cameras ORDER BY nvr_id, channel")
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

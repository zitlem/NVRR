"""ONVIF PTZ control for cameras."""

from onvif import ONVIFCamera
from functools import lru_cache
import asyncio
import logging

logger = logging.getLogger(__name__)

# Cache ONVIF connections to avoid reconnecting every command
_ptz_cache: dict[str, dict] = {}


async def _get_ptz_service(host: str, port: int, username: str, password: str):
    """Get or create a cached ONVIF PTZ service connection."""
    key = f"{host}:{port}"
    if key in _ptz_cache:
        return _ptz_cache[key]

    loop = asyncio.get_event_loop()

    def connect():
        cam = ONVIFCamera(host, port, username, password)
        media = cam.create_media_service()
        ptz = cam.create_ptz_service()
        profile = media.GetProfiles()[0]
        return {"ptz": ptz, "profile": profile, "token": profile.token}

    result = await loop.run_in_executor(None, connect)
    _ptz_cache[key] = result
    return result


async def continuous_move(
    host: str, port: int, username: str, password: str,
    pan: float = 0, tilt: float = 0, zoom: float = 0,
):
    """Start continuous PTZ movement. Values range from -1.0 to 1.0."""
    svc = await _get_ptz_service(host, port, username, password)
    ptz = svc["ptz"]
    token = svc["token"]

    request = ptz.create_type("ContinuousMove")
    request.ProfileToken = token
    request.Velocity = {
        "PanTilt": {"x": pan, "y": tilt},
        "Zoom": {"x": zoom},
    }

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: ptz.ContinuousMove(request))


async def stop_move(host: str, port: int, username: str, password: str):
    """Stop all PTZ movement."""
    svc = await _get_ptz_service(host, port, username, password)
    ptz = svc["ptz"]
    token = svc["token"]

    request = ptz.create_type("Stop")
    request.ProfileToken = token
    request.PanTilt = True
    request.Zoom = True

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: ptz.Stop(request))


async def goto_preset(
    host: str, port: int, username: str, password: str, preset: int
):
    """Move camera to a saved preset position."""
    svc = await _get_ptz_service(host, port, username, password)
    ptz = svc["ptz"]
    token = svc["token"]

    request = ptz.create_type("GotoPreset")
    request.ProfileToken = token
    request.PresetToken = str(preset)

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: ptz.GotoPreset(request))


async def get_presets(host: str, port: int, username: str, password: str) -> list[dict]:
    """Get all saved PTZ presets for a camera."""
    svc = await _get_ptz_service(host, port, username, password)
    ptz = svc["ptz"]
    token = svc["token"]

    loop = asyncio.get_event_loop()
    presets = await loop.run_in_executor(None, lambda: ptz.GetPresets({"ProfileToken": token}))

    return [
        {"token": p.token, "name": getattr(p, "Name", f"Preset {p.token}")}
        for p in presets
        if hasattr(p, "token")
    ]


def clear_cache(host: str = None, port: int = None):
    """Clear cached PTZ connections."""
    if host and port:
        _ptz_cache.pop(f"{host}:{port}", None)
    else:
        _ptz_cache.clear()

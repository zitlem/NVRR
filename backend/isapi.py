"""Hikvision ISAPI client for NVR discovery."""

import asyncio
import logging
import httpx
import xml.etree.ElementTree as ET
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredCamera:
    channel: int
    name: str
    rtsp_url: str


def _hik_ns(root) -> dict[str, str]:
    """Extract Hikvision XML namespace from root element."""
    return {"hik": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}


def _hik_find(el, tag: str, ns: dict):
    """Find a single child element, with or without namespace."""
    return el.find(f"hik:{tag}", ns) if ns else el.find(tag)


def _hik_findall(el, tag: str, ns: dict):
    """Find all child elements, with or without namespace."""
    return el.findall(f"hik:{tag}", ns) if ns else el.findall(tag)


async def _detect_auth(username: str, password: str, base: str):
    """Detect whether NVR needs Basic or Digest auth."""
    async with httpx.AsyncClient(auth=(username, password), base_url=base, timeout=10.0) as client:
        resp = await client.get("/ISAPI/System/deviceInfo")
        if resp.status_code != 401:
            return (username, password)  # Basic works (tuple auth)
    return httpx.DigestAuth(username, password)


def _make_client(auth, base: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(auth=auth, base_url=base, timeout=10.0)


async def _fetch_video_input_names(
    client: httpx.AsyncClient,
) -> dict[int, str]:
    """Get descriptive camera names, trying multiple ISAPI endpoints."""
    names: dict[int, str] = {}

    # Endpoint 1: /ISAPI/System/Video/inputs/channels
    try:
        resp = await client.get("/ISAPI/System/Video/inputs/channels")
        if resp.status_code == 200:
            root = ET.fromstring(resp.text)
            ns = _hik_ns(root)
            for ch in _hik_findall(root, "VideoInputChannel", ns):
                id_el = _hik_find(ch, "id", ns)
                name_el = _hik_find(ch, "name", ns)
                if id_el is not None and name_el is not None and name_el.text:
                    chan_id = int(id_el.text)
                    name = name_el.text.strip()
                    if name and not name.isdigit():
                        names[chan_id] = name
    except Exception as e:
        logger.debug("Video/inputs/channels failed: %s", e)

    if names:
        return names

    # Endpoint 2: /ISAPI/ContentMgmt/InputProxy/channels
    try:
        resp = await client.get("/ISAPI/ContentMgmt/InputProxy/channels")
        if resp.status_code == 200:
            root = ET.fromstring(resp.text)
            ns = _hik_ns(root)
            for ch in _hik_findall(root, "InputProxyChannel", ns):
                id_el = _hik_find(ch, "id", ns)
                name_el = _hik_find(ch, "name", ns)
                if id_el is not None and name_el is not None and name_el.text:
                    chan_id = int(id_el.text)
                    name = name_el.text.strip()
                    if name and not name.isdigit():
                        names[chan_id] = name
    except Exception as e:
        logger.debug("ContentMgmt/InputProxy/channels failed: %s", e)

    if names:
        return names

    # Endpoint 3: /ISAPI/Streaming/channels (use channelName, filter out numeric-only)
    try:
        resp = await client.get("/ISAPI/Streaming/channels")
        if resp.status_code == 200:
            root = ET.fromstring(resp.text)
            ns = _hik_ns(root)
            for ch in _hik_findall(root, "StreamingChannel", ns):
                id_el = _hik_find(ch, "id", ns)
                name_el = _hik_find(ch, "channelName", ns)
                if id_el is None:
                    continue
                ch_id = int(id_el.text)
                if ch_id % 100 != 1:
                    continue
                cam_num = ch_id // 100
                if name_el is not None and name_el.text:
                    name = name_el.text.strip()
                    if name and not name.isdigit():
                        names[cam_num] = name
    except Exception as e:
        logger.debug("Streaming/channels names failed: %s", e)

    return names


async def discover_cameras(
    nvr_ip: str, username: str, password: str, port: int = 80
) -> list[DiscoveredCamera]:
    """Connect to a Hikvision NVR via ISAPI and discover all connected cameras."""
    base = f"http://{nvr_ip}:{port}"
    cameras = []

    auth = await _detect_auth(username, password, base)
    async with _make_client(auth, base) as client:
        # Get descriptive names from Video inputs endpoint
        proxy_names = await _fetch_video_input_names(client)
        logger.info("Proxy names for %s: %s", nvr_ip, proxy_names)

        # Get streaming channels to find all cameras
        resp = await client.get("/ISAPI/Streaming/channels")
        resp.raise_for_status()
        text = resp.text

        root = ET.fromstring(text)
        ns = _hik_ns(root)

        channels = _hik_findall(root, "StreamingChannel", ns)

        seen = set()
        for ch in channels:
            ch_id_el = _hik_find(ch, "id", ns)
            ch_name_el = _hik_find(ch, "channelName", ns)
            if ch_id_el is None:
                continue

            ch_id = int(ch_id_el.text)
            # Hikvision uses XX01 for main stream and XX02 for sub stream
            # We want main stream only (ends in 01)
            if ch_id % 100 != 1:
                continue

            camera_num = ch_id // 100
            if camera_num in seen:
                continue
            seen.add(camera_num)

            # Prefer InputProxy name > channelName > fallback
            name = proxy_names.get(camera_num)
            ch_name = ch_name_el.text if ch_name_el is not None else None
            logger.info("Camera %d: proxy_name=%s, channelName=%s", camera_num, name, ch_name)
            if not name:
                name = ch_name
            if not name or name.isdigit():
                name = f"Camera {camera_num}"
            rtsp_url = (
                f"rtsp://{username}:{password}@{nvr_ip}:554"
                f"/Streaming/Channels/{ch_id}"
            )
            cameras.append(DiscoveredCamera(
                channel=camera_num,
                name=name,
                rtsp_url=rtsp_url,
            ))

    return cameras


async def check_nvr_connection(
    nvr_ip: str, username: str, password: str, port: int = 80
) -> dict:
    """Verify NVR is reachable and get device info."""
    base = f"http://{nvr_ip}:{port}"

    auth = await _detect_auth(username, password, base)
    async with _make_client(auth, base) as client:
        resp = await client.get("/ISAPI/System/deviceInfo")
        resp.raise_for_status()
        text = resp.text

    root = ET.fromstring(text)
    ns = _hik_ns(root)

    def find_text(tag):
        el = _hik_find(root, tag, ns)
        return el.text if el is not None else None

    return {
        "name": find_text("deviceName") or "Unknown NVR",
        "model": find_text("model"),
        "serial": find_text("serialNumber"),
    }


async def fetch_camera_names(
    nvr_ip: str, username: str, password: str, port: int = 80
) -> dict[int, str]:
    """Fetch camera names from NVR's Video inputs endpoint. Returns {channel: name}."""
    base = f"http://{nvr_ip}:{port}"
    auth = await _detect_auth(username, password, base)
    async with _make_client(auth, base) as client:
        return await _fetch_video_input_names(client)


async def probe_isapi(ip: str, port: int = 80, timeout: float = 2.0) -> dict | None:
    """Quick probe: check if an IP has a Hikvision ISAPI endpoint (no auth needed for deviceInfo on some models).
    Returns basic info dict or None."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"http://{ip}:{port}/ISAPI/System/deviceInfo")
            # 200 = open, 401 = needs auth but ISAPI exists — both mean it's a Hikvision device
            if resp.status_code in (200, 401):
                name = ""
                model = ""
                hardware = ""
                if resp.status_code == 200:
                    try:
                        root = ET.fromstring(resp.text)
                        ns = _hik_ns(root)
                        name_el = _hik_find(root, "deviceName", ns)
                        model_el = _hik_find(root, "model", ns)
                        name = name_el.text if name_el is not None else ""
                        model = model_el.text if model_el is not None else ""
                    except Exception:
                        pass
                return {
                    "ip": ip,
                    "port": port,
                    "name": name or f"Hikvision at {ip}",
                    "model": model,
                    "hardware": hardware,
                }
    except Exception:
        pass
    return None


DEFAULT_SDK_PORTS = [8000, 8001, 8002, 8003, 8004, 8005, 8200]


async def discover_sdk_port(nvr_ip: str, extra_ports: list[int] | None = None, timeout: float = 2.0) -> int | None:
    """Probe SDK ports and return the first open one. Merges defaults with any extra user-supplied ports."""
    probe_ports = list(dict.fromkeys(DEFAULT_SDK_PORTS + (extra_ports or [])))

    async def _try_port(port: int) -> int | None:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(nvr_ip, port), timeout=timeout
            )
            writer.close()
            await writer.wait_closed()
            return port
        except (OSError, asyncio.TimeoutError):
            return None

    # Probe all ports concurrently
    results = await asyncio.gather(*[_try_port(p) for p in probe_ports])
    for port in results:
        if port is not None:
            return port
    return None

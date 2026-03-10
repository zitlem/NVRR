"""Hikvision ISAPI client for NVR discovery."""

import asyncio
import aiohttp
import xml.etree.ElementTree as ET
from dataclasses import dataclass


@dataclass
class DiscoveredCamera:
    channel: int
    name: str
    rtsp_url: str


async def _fetch_video_input_names(
    session: aiohttp.ClientSession, base: str
) -> dict[int, str]:
    """Get descriptive camera names from Video/inputs/channels endpoint."""
    names: dict[int, str] = {}
    try:
        url = f"{base}/ISAPI/System/Video/inputs/channels"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return names
            text = await resp.text()

        root = ET.fromstring(text)
        ns = {"hik": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}

        def find(el, tag):
            if ns:
                return el.find(f"hik:{tag}", ns)
            return el.find(tag)

        def findall(el, tag):
            if ns:
                return el.findall(f"hik:{tag}", ns)
            return el.findall(tag)

        for ch in findall(root, "VideoInputChannel"):
            id_el = find(ch, "id")
            name_el = find(ch, "name")
            if id_el is not None and name_el is not None and name_el.text:
                chan_id = int(id_el.text)
                name = name_el.text.strip()
                if name:
                    names[chan_id] = name
    except Exception:
        pass
    return names


async def discover_cameras(
    nvr_ip: str, username: str, password: str, port: int = 80
) -> list[DiscoveredCamera]:
    """Connect to a Hikvision NVR via ISAPI and discover all connected cameras."""
    base = f"http://{nvr_ip}:{port}"
    auth = aiohttp.BasicAuth(username, password)
    cameras = []

    async with aiohttp.ClientSession(auth=auth) as session:
        # Get descriptive names from Video inputs endpoint
        proxy_names = await _fetch_video_input_names(session, base)

        # Get streaming channels to find all cameras
        url = f"{base}/ISAPI/Streaming/channels"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            text = await resp.text()

        root = ET.fromstring(text)
        ns = {"hik": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}

        def find(el, tag):
            if ns:
                return el.find(f"hik:{tag}", ns)
            return el.find(tag)

        def findall(el, tag):
            if ns:
                return el.findall(f"hik:{tag}", ns)
            return el.findall(tag)

        channels = findall(root, "StreamingChannel")

        seen = set()
        for ch in channels:
            ch_id_el = find(ch, "id")
            ch_name_el = find(ch, "channelName")
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
            if not name:
                name = ch_name_el.text if ch_name_el is not None else None
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
    auth = aiohttp.BasicAuth(username, password)

    async with aiohttp.ClientSession(auth=auth) as session:
        url = f"{base}/ISAPI/System/deviceInfo"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            text = await resp.text()

    root = ET.fromstring(text)
    ns = {"hik": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}

    def find_text(tag):
        if ns:
            el = root.find(f"hik:{tag}", ns)
        else:
            el = root.find(tag)
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
    auth = aiohttp.BasicAuth(username, password)
    async with aiohttp.ClientSession(auth=auth) as session:
        return await _fetch_video_input_names(session, base)


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

"""Hikvision ISAPI client for NVR discovery."""

import aiohttp
import xml.etree.ElementTree as ET
from dataclasses import dataclass


@dataclass
class DiscoveredCamera:
    channel: int
    name: str
    rtsp_url: str


async def discover_cameras(
    nvr_ip: str, username: str, password: str, port: int = 80
) -> list[DiscoveredCamera]:
    """Connect to a Hikvision NVR via ISAPI and discover all connected cameras."""
    base = f"http://{nvr_ip}:{port}"
    auth = aiohttp.BasicAuth(username, password)
    cameras = []

    async with aiohttp.ClientSession(auth=auth) as session:
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

            name = ch_name_el.text if ch_name_el is not None else f"Camera {camera_num}"
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

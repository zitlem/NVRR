# IPCAM Web Monitor

## What It Is

A self-hosted web application that lets you view live footage from multiple IP cameras and NVRs in a browser, with PTZ (pan/tilt/zoom) control. It runs entirely on your local network on a mini PC — no cloud, no subscriptions.

---

## How It Works

When you add an NVR through the admin panel, the backend connects to it using Hikvision's ISAPI protocol and automatically discovers all connected cameras. It then generates a streaming configuration and hands it to MediaMTX, which opens a persistent RTSP connection to each camera and converts the stream to HLS (a browser-compatible format) by re-packaging the video data without re-encoding it. This keeps CPU usage very low since the video is never decoded and re-encoded on the server — it just gets repackaged.

When a browser opens the viewer, it fetches the list of cameras from the backend and uses hls.js to connect to each stream. The browser's built-in hardware acceleration handles the actual video decoding, using the viewer's GPU automatically. PTZ commands (pan, tilt, zoom, presets) typed in the browser are sent to the backend, which forwards them to the camera using the ONVIF protocol.

---

## Components

**MediaMTX** is the streaming engine. It connects to each camera's RTSP stream and serves it as HLS. It runs as a background service and its config is automatically managed by the backend — you never edit it manually.

**FastAPI backend** is the brain of the system. It stores all camera configuration in a SQLite database, handles NVR discovery, manages PTZ connections, rewrites the MediaMTX config when cameras are added or changed, and enforces admin authentication.

**Nginx** is the web server that ties everything together. It serves the frontend HTML files, proxies API requests to FastAPI, and proxies HLS stream requests to MediaMTX — so the browser only ever talks to one host on port 80.

**hls.js** is a JavaScript library in the viewer that handles HLS playback in browsers that don't support it natively (everything except Safari).

**onvif-zeep** is the Python library used to communicate PTZ commands to cameras using the ONVIF standard protocol.

---

## Required Hardware

- Any mini PC or server running Debian 12 (headless is fine)
- Hikvision NVRs connected to the same network
- IP cameras connected to those NVRs
- Client devices (PC, phone, tablet) with a modern browser to view streams

---

## Required Software on the Server

| Software | Purpose | How to get it |
|---|---|---|
| Debian 12 | OS | — |
| Python 3.11+ | Runs the backend | apt install python3 |
| Nginx | Web server / reverse proxy | apt install nginx |
| FFmpeg | Used by MediaMTX for stream probing | apt install ffmpeg |
| MediaMTX | RTSP → HLS conversion | Binary from GitHub releases |
| FastAPI + Uvicorn | Backend API server | pip install |
| aiosqlite | Async SQLite access | pip install |
| aiohttp | HTTP calls to ISAPI | pip install |
| onvif-zeep | ONVIF PTZ control | pip install |

---

## Network Requirements

- Mini PC must be able to reach cameras/NVRs on the same LAN
- Cameras need RTSP enabled (port 554) — on by default on Hikvision
- Cameras need ONVIF enabled for PTZ control — on by default
- ISAPI (HTTP port 80) must be accessible on each NVR for auto-discovery
- No ports need to be opened to the internet — LAN only

---

## What the Admin Panel Does

- Add an NVR by entering its IP and credentials
- Automatically detects how many cameras are connected and imports them all
- Per-camera: rename, toggle PTZ on/off, enable/disable visibility in the viewer
- Rediscover button to pick up newly added cameras without re-entering credentials
- Password protected (set in the systemd service config)

---

## What the Viewer Does

- Displays all enabled cameras in a responsive grid (2×, 4×, 5×, or 6× columns)
- Click a tile to select it and show PTZ controls in the sidebar
- D-pad + zoom buttons for continuous PTZ movement (hold to move, release to stop)
- Preset buttons appear for cameras that have saved presets on the NVR
- Double-click any tile for fullscreen, Escape to exit
- Streams auto-reconnect if a camera drops

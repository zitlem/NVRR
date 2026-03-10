"""Stream relay: HCNetSDK callback → FFmpeg → RTSP publish to MediaMTX."""

import ctypes
import subprocess
import threading
import logging
import os
import time
from dataclasses import dataclass, field

from hcnetsdk import sdk, REALDATACALLBACK

logger = logging.getLogger(__name__)

MEDIAMTX_RTSP = os.environ.get("MEDIAMTX_RTSP", "rtsp://127.0.0.1:8554")
FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "ffmpeg")


@dataclass
class CameraRelay:
    camera_id: int
    channel: int
    play_handle: int = -1
    ffmpeg_proc: subprocess.Popen = None
    callback: REALDATACALLBACK = None
    running: bool = False


class StreamRelayManager:
    """Manages SDK→FFmpeg→MediaMTX relay for all cameras."""

    def __init__(self):
        self._relays: dict[int, CameraRelay] = {}  # camera_id -> relay
        self._user_ids: dict[str, int] = {}          # "ip:port" -> user_id
        self._lock = threading.Lock()
        self._initialized = False

    def init_sdk(self) -> bool:
        if self._initialized:
            return True
        if not sdk.init():
            logger.error("Failed to initialize HCNetSDK")
            return False
        sdk.set_connect_time(5000, 3)
        sdk.set_reconnect(10000, True)
        self._initialized = True
        logger.info("HCNetSDK initialized")
        return True

    def _get_user_id(self, ip: str, port: int, username: str, password: str) -> int:
        """Login to NVR, caching the session."""
        key = f"{ip}:{port}"
        if key in self._user_ids and self._user_ids[key] >= 0:
            return self._user_ids[key]

        user_id, device_info = sdk.login(ip, port, username, password)
        if user_id >= 0:
            self._user_ids[key] = user_id
        return user_id

    def start_relay(self, camera_id: int, nvr_ip: str, nvr_port: int,
                    username: str, password: str, channel: int) -> bool:
        """Start streaming a camera via SDK → FFmpeg → MediaMTX."""
        with self._lock:
            if camera_id in self._relays and self._relays[camera_id].running:
                logger.info("Relay already running for cam%d", camera_id)
                return True

        if not self.init_sdk():
            return False

        user_id = self._get_user_id(nvr_ip, nvr_port, username, password)
        if user_id < 0:
            return False

        # Start FFmpeg process: SDK sends PS (Program Stream) format data
        rtsp_url = f"{MEDIAMTX_RTSP}/cam{camera_id}"
        try:
            ffmpeg_proc = subprocess.Popen(
                [
                    FFMPEG_PATH,
                    "-loglevel", "info",
                    "-probesize", "4096",
                    "-analyzeduration", "1000000",
                    "-i", "pipe:0",
                    "-c:v", "copy",         # no transcoding
                    "-an",                  # no audio for now
                    "-f", "rtsp",
                    "-rtsp_transport", "tcp",
                    rtsp_url,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error("FFmpeg not found at '%s'. Install FFmpeg.", FFMPEG_PATH)
            return False

        relay = CameraRelay(camera_id=camera_id, channel=channel)
        relay.ffmpeg_proc = ffmpeg_proc
        relay.running = True

        # Log FFmpeg stderr in background thread
        def _log_ffmpeg(proc, cam_id):
            for line in proc.stderr:
                msg = line.decode("utf-8", errors="replace").strip()
                if msg:
                    logger.info("FFmpeg cam%d: %s", cam_id, msg)
        t = threading.Thread(target=_log_ffmpeg, args=(ffmpeg_proc, camera_id), daemon=True)
        t.start()

        # Create the callback that pipes data to FFmpeg
        def make_callback(r: CameraRelay):
            @REALDATACALLBACK
            def callback(lPlayHandle, dwDataType, pBuffer, dwBufSize, pUser):
                if not r.running or r.ffmpeg_proc is None:
                    return
                if dwDataType in (1, 2):  # NET_DVR_SYSHEAD=1, NET_DVR_STREAMDATA=2
                    try:
                        data = bytes(ctypes.cast(pBuffer, ctypes.POINTER(ctypes.c_byte * dwBufSize)).contents)
                        r.ffmpeg_proc.stdin.write(data)
                    except (OSError, ValueError):
                        pass  # pipe broken, will be cleaned up
            return callback

        relay.callback = make_callback(relay)

        # Start SDK real play
        # NVR digital channels typically start at 33, but we use the channel from ISAPI
        # The ISAPI channel 1 = NVR channel byStartDChan + 0
        # For most Hikvision NVRs, digital channels start at 33
        play_handle = sdk.real_play(user_id, channel, relay.callback, stream_type=1)  # sub stream
        if play_handle < 0:
            # Try with channel offset (NVR digital channels start at 33)
            play_handle = sdk.real_play(user_id, 32 + channel, relay.callback, stream_type=1)

        if play_handle < 0:
            logger.error("Failed to start real play for cam%d channel %d", camera_id, channel)
            ffmpeg_proc.kill()
            return False

        relay.play_handle = play_handle

        with self._lock:
            self._relays[camera_id] = relay

        logger.info("Relay started: cam%d (channel %d) → %s", camera_id, channel, rtsp_url)
        return True

    def stop_relay(self, camera_id: int):
        """Stop a single camera relay."""
        with self._lock:
            relay = self._relays.pop(camera_id, None)

        if not relay:
            return

        relay.running = False

        if relay.play_handle >= 0:
            sdk.stop_real_play(relay.play_handle)

        if relay.ffmpeg_proc:
            try:
                relay.ffmpeg_proc.stdin.close()
            except Exception:
                pass
            relay.ffmpeg_proc.kill()
            relay.ffmpeg_proc.wait(timeout=5)

        logger.info("Relay stopped: cam%d", camera_id)

    def stop_all(self):
        """Stop all relays and cleanup."""
        camera_ids = list(self._relays.keys())
        for cid in camera_ids:
            self.stop_relay(cid)

        # Logout all NVRs
        for key, uid in self._user_ids.items():
            if uid >= 0:
                sdk.logout(uid)
        self._user_ids.clear()

        if self._initialized:
            sdk.cleanup()
            self._initialized = False

        logger.info("All relays stopped and SDK cleaned up")

    def get_status(self) -> list[dict]:
        """Get status of all relays."""
        with self._lock:
            return [
                {
                    "camera_id": r.camera_id,
                    "channel": r.channel,
                    "running": r.running,
                    "ffmpeg_alive": r.ffmpeg_proc.poll() is None if r.ffmpeg_proc else False,
                }
                for r in self._relays.values()
            ]


# Singleton
relay_manager = StreamRelayManager()

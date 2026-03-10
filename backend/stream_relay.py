"""Stream relay: HCNetSDK callback → FFmpeg → RTSP publish to MediaMTX."""

import ctypes
import subprocess
import threading
import logging
import os
import time
import queue
import urllib.request
from dataclasses import dataclass, field

from hcnetsdk import sdk, REALDATACALLBACK

logger = logging.getLogger(__name__)

MEDIAMTX_RTSP = os.environ.get("MEDIAMTX_RTSP", "rtsp://127.0.0.1:8554")
MEDIAMTX_RTMP = os.environ.get("MEDIAMTX_RTMP", "rtmp://127.0.0.1:1935")
MEDIAMTX_SRT = os.environ.get("MEDIAMTX_SRT", "srt://127.0.0.1:8890")
MEDIAMTX_API = "http://127.0.0.1:9997"
FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "ffmpeg")

STREAM_TYPE_MAIN = 0
STREAM_TYPE_SUB = 1


@dataclass
class CameraRelay:
    camera_id: int
    channel: int
    stream_type: int = 1  # 0=main, 1=sub
    path_suffix: str = ""  # "" for sub, "_main" for main
    play_handle: int = -1
    ffmpeg_proc: subprocess.Popen = None
    callback: REALDATACALLBACK = None
    running: bool = False
    data_queue: queue.Queue = None
    bytes_received: int = 0
    callback_count: int = 0


def _relay_key(camera_id: int, path_suffix: str = "") -> str:
    """Build the relay/path key for a camera stream."""
    return f"cam{camera_id}{path_suffix}"


def _cleanup_ffmpeg(proc: subprocess.Popen):
    """Gracefully shut down an FFmpeg process."""
    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.kill()
    try:
        proc.wait(timeout=3)
    except Exception:
        pass


class StreamRelayManager:
    """Manages SDK→FFmpeg→MediaMTX relay for all cameras."""

    def __init__(self):
        self._relays: dict[str, CameraRelay] = {}  # "cam{id}" or "cam{id}_main" -> relay
        self._user_ids: dict[str, int] = {}          # "ip:port" -> user_id
        self._start_dchans: dict[str, int] = {}      # "ip:port" -> byStartDChan
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
        """Login to NVR, caching the session and device info."""
        key = f"{ip}:{port}"
        if key in self._user_ids and self._user_ids[key] >= 0:
            return self._user_ids[key]

        user_id, device_info = sdk.login(ip, port, username, password)
        if user_id >= 0:
            self._user_ids[key] = user_id
            self._start_dchans[key] = device_info.byStartDChan
            logger.info("NVR %s byStartDChan=%d", key, device_info.byStartDChan)
        return user_id

    def start_relay(self, camera_id: int, nvr_ip: str, nvr_port: int,
                    username: str, password: str, channel: int,
                    stream_type: int = 1, path_suffix: str = "") -> bool:
        """Start streaming a camera via SDK → FFmpeg → MediaMTX.
        stream_type: 0=main stream, 1=sub stream
        path_suffix: appended to path name (e.g. "_main")
        """
        key = _relay_key(camera_id, path_suffix)
        with self._lock:
            if key in self._relays and self._relays[key].running:
                logger.info("Relay already running for %s", key)
                return True

        if not self.init_sdk():
            return False

        user_id = self._get_user_id(nvr_ip, nvr_port, username, password)
        if user_id < 0:
            return False

        # Start FFmpeg process: SDK sends PS (Program Stream) format data
        # Use SRT+MPEGTS which supports both H.264 and HEVC (FLV only supports H.264)
        publish_url = f"{MEDIAMTX_SRT}?streamid=publish:{key}&pkt_size=1316"
        try:
            ffmpeg_proc = subprocess.Popen(
                [
                    FFMPEG_PATH,
                    "-loglevel", "info",
                    "-fflags", "+nobuffer+fastseek+flush_packets",
                    "-flags", "low_delay",
                    "-probesize", "2048",
                    "-analyzeduration", "200000",
                    "-i", "pipe:0",
                    "-c:v", "copy",         # no transcoding
                    "-an",                  # no audio for now
                    "-f", "mpegts",
                    publish_url,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error("FFmpeg not found at '%s'. Install FFmpeg.", FFMPEG_PATH)
            return False

        relay = CameraRelay(camera_id=camera_id, channel=channel,
                            stream_type=stream_type, path_suffix=path_suffix)
        relay.ffmpeg_proc = ffmpeg_proc
        relay.running = True
        relay.data_queue = queue.Queue(maxsize=500)

        # Log FFmpeg stderr in background thread
        def _log_ffmpeg(proc, cam_id):
            for line in proc.stderr:
                msg = line.decode("utf-8", errors="replace").strip()
                if msg:
                    logger.info("FFmpeg cam%d: %s", cam_id, msg)
        t = threading.Thread(target=_log_ffmpeg, args=(ffmpeg_proc, camera_id), daemon=True)
        t.start()

        # Writer thread: drains queue → FFmpeg stdin (prevents SDK callback from blocking)
        def _queue_writer(r: CameraRelay):
            while r.running:
                try:
                    data = r.data_queue.get(timeout=1)
                except queue.Empty:
                    continue
                try:
                    r.ffmpeg_proc.stdin.write(data)
                except (OSError, ValueError):
                    break  # pipe broken
            logger.info("Queue writer stopped for cam%d", r.camera_id)
        wt = threading.Thread(target=_queue_writer, args=(relay,), daemon=True)
        wt.start()

        # Create the callback that queues data (never blocks SDK thread)
        def make_callback(r: CameraRelay):
            @REALDATACALLBACK
            def callback(lPlayHandle, dwDataType, pBuffer, dwBufSize, pUser):
                if not r.running or r.ffmpeg_proc is None:
                    return
                if dwDataType in (1, 2):  # NET_DVR_SYSHEAD=1, NET_DVR_STREAMDATA=2
                    try:
                        data = bytes(ctypes.cast(pBuffer, ctypes.POINTER(ctypes.c_byte * dwBufSize)).contents)
                        r.data_queue.put_nowait(data)
                        r.bytes_received += dwBufSize
                        r.callback_count += 1
                        if r.callback_count == 1:
                            logger.info("SDK data flowing for cam%d (type=%d, size=%d)",
                                       r.camera_id, dwDataType, dwBufSize)
                    except queue.Full:
                        pass  # drop frame if queue is full
                    except (OSError, ValueError):
                        pass
            return callback

        relay.callback = make_callback(relay)

        # Start SDK real play
        # ISAPI channel 1 maps to SDK channel byStartDChan + 0 for IP cameras
        # Try the digital channel first (byStartDChan - 1 + channel), then raw channel as fallback
        nvr_key = f"{nvr_ip}:{nvr_port}"
        start_dchan = self._start_dchans.get(nvr_key, 33)
        digital_channel = start_dchan - 1 + channel  # e.g. startDChan=33, channel=1 → 33

        play_handle = sdk.real_play(user_id, digital_channel, relay.callback, stream_type=stream_type)
        if play_handle < 0 and digital_channel != channel:
            # Fallback: try raw channel number
            play_handle = sdk.real_play(user_id, channel, relay.callback, stream_type=stream_type)

        if play_handle < 0:
            logger.error("Failed to start real play for %s channel %d", key, channel)
            ffmpeg_proc.kill()
            return False

        relay.play_handle = play_handle

        with self._lock:
            self._relays[key] = relay

        logger.info("Relay started: %s (channel %d, type %d) → %s",
                     key, channel, stream_type, publish_url)
        return True

    def stop_relay(self, camera_id: int, path_suffix: str = ""):
        """Stop a single camera relay."""
        key = _relay_key(camera_id, path_suffix)
        with self._lock:
            relay = self._relays.pop(key, None)

        if not relay:
            return

        relay.running = False

        if relay.play_handle >= 0:
            sdk.stop_real_play(relay.play_handle)

        if relay.ffmpeg_proc:
            _cleanup_ffmpeg(relay.ffmpeg_proc)

        # Kick the MediaMTX path to clear stale HLS state
        try:
            req = urllib.request.Request(
                f"{MEDIAMTX_API}/v3/paths/kick/{key}",
                method="POST",
            )
            urllib.request.urlopen(req, timeout=2)
        except Exception:
            pass  # path may not exist, that's fine

        logger.info("Relay stopped: %s", key)

    def stop_camera(self, camera_id: int):
        """Stop both sub and main relays for a camera."""
        self.stop_relay(camera_id, "")
        self.stop_relay(camera_id, "_main")

    def stop_all(self):
        """Stop all relays and cleanup."""
        relay_keys = list(self._relays.keys())
        for key in relay_keys:
            with self._lock:
                relay = self._relays.pop(key, None)
            if relay:
                relay.running = False
                if relay.play_handle >= 0:
                    sdk.stop_real_play(relay.play_handle)
                if relay.ffmpeg_proc:
                    _cleanup_ffmpeg(relay.ffmpeg_proc)

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
                    "key": key,
                    "camera_id": r.camera_id,
                    "channel": r.channel,
                    "stream_type": r.stream_type,
                    "running": r.running,
                    "ffmpeg_alive": r.ffmpeg_proc.poll() is None if r.ffmpeg_proc else False,
                    "bytes_received": r.bytes_received,
                    "callback_count": r.callback_count,
                    "queue_size": r.data_queue.qsize() if r.data_queue else 0,
                }
                for key, r in self._relays.items()
            ]


# Singleton
relay_manager = StreamRelayManager()

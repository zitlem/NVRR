"""Minimal Hikvision HCNetSDK wrapper using ctypes."""

import ctypes
import os
import sys
import logging
from ctypes import (
    c_bool, c_byte, c_char, c_char_p, c_int, c_long, c_uint, c_ulong,
    c_ushort, c_void_p, POINTER, Structure, CFUNCTYPE, byref, sizeof,
)

logger = logging.getLogger(__name__)

# SDK DLL path — set via env or default to ./sdk/
SDK_DIR = os.environ.get("HCNETSDK_DIR", os.path.join(os.path.dirname(__file__), "..", "sdk"))

# --- SDK Structures ---

class NET_DVR_DEVICEINFO_V30(Structure):
    _fields_ = [
        ("sSerialNumber", c_byte * 48),
        ("byAlarmInPortNum", c_byte),
        ("byAlarmOutPortNum", c_byte),
        ("byDiskNum", c_byte),
        ("byDVRType", c_byte),
        ("byChanNum", c_byte),
        ("byStartChan", c_byte),
        ("byAudioChanNum", c_byte),
        ("byIPChanNum", c_byte),
        ("byZeroChanNum", c_byte),
        ("byMainProto", c_byte),
        ("bySubProto", c_byte),
        ("bySupport", c_byte),
        ("bySupport1", c_byte),
        ("bySupport2", c_byte),
        ("wDevType", c_ushort),
        ("bySupport3", c_byte),
        ("byMultiStreamProto", c_byte),
        ("byStartDChan", c_byte),
        ("byStartDTalkChan", c_byte),
        ("byHighDChanNum", c_byte),
        ("bySupport4", c_byte),
        ("byLanguageType", c_byte),
        ("byVoiceInChanNum", c_byte),
        ("byStartVoiceInChanNo", c_byte),
        ("bySupport5", c_byte),
        ("bySupport6", c_byte),
        ("byMirrorChanNum", c_byte),
        ("wStartMirrorChanNo", c_ushort),
        ("bySupport7", c_byte),
        ("byRes2", c_byte),
    ]


class NET_DVR_PREVIEWINFO(Structure):
    _fields_ = [
        ("lChannel", c_long),
        ("dwStreamType", c_ulong),      # 0=main, 1=sub
        ("dwLinkMode", c_ulong),         # 0=TCP, 1=UDP
        ("hPlayWnd", c_void_p),          # NULL for no preview window
        ("bBlocked", c_ulong),           # 0=non-blocking, 1=blocking
        ("bPassbackRecord", c_ulong),
        ("byPreviewMode", c_byte),
        ("byStreamID", c_byte * 32),
        ("byProtoType", c_byte),         # 0=private, 1=RTSP
        ("byRes1", c_byte),
        ("byVideoCodingType", c_byte),
        ("dwDisplayBufNum", c_ulong),
        ("byNPQMode", c_byte),
        ("byRecvMetaData", c_byte),
        ("byDataType", c_byte),
        ("byRes", c_byte * 213),
    ]


# Callback type: void(LONG lPlayHandle, DWORD dwDataType, BYTE* pBuffer, DWORD dwBufSize, void* pUser)
REALDATACALLBACK = CFUNCTYPE(None, c_long, c_ulong, POINTER(c_byte), c_ulong, c_void_p)


class HCNetSDK:
    """Wrapper around HCNetSDK (.dll on Windows, .so on Linux)."""

    def __init__(self):
        self._sdk = None
        self._loaded = False

    def load(self):
        """Load the SDK library."""
        if self._loaded:
            return True

        if sys.platform == "win32":
            lib_name = "HCNetSDK.dll"
        else:
            lib_name = "libhcnetsdk.so"

        lib_path = os.path.join(SDK_DIR, lib_name)
        if not os.path.exists(lib_path):
            logger.error("HCNetSDK not found at %s", lib_path)
            return False

        if sys.platform == "win32":
            # Add SDK dir to DLL search path
            os.environ["PATH"] = SDK_DIR + os.pathsep + os.environ.get("PATH", "")
            try:
                os.add_dll_directory(SDK_DIR)
                com_dir = os.path.join(SDK_DIR, "HCNetSDKCom")
                if os.path.isdir(com_dir):
                    os.add_dll_directory(com_dir)
            except (AttributeError, OSError):
                pass
            try:
                self._sdk = ctypes.WinDLL(lib_path)
                self._loaded = True
            except OSError as e:
                logger.error("Failed to load HCNetSDK: %s", e)
                return False
        else:
            # Linux: set LD_LIBRARY_PATH and load .so
            ld_path = os.environ.get("LD_LIBRARY_PATH", "")
            if SDK_DIR not in ld_path:
                os.environ["LD_LIBRARY_PATH"] = SDK_DIR + os.pathsep + ld_path
            com_dir = os.path.join(SDK_DIR, "HCNetSDKCom")
            if os.path.isdir(com_dir) and com_dir not in ld_path:
                os.environ["LD_LIBRARY_PATH"] = com_dir + os.pathsep + os.environ["LD_LIBRARY_PATH"]
            try:
                self._sdk = ctypes.CDLL(lib_path)
                self._loaded = True
            except OSError as e:
                logger.error("Failed to load HCNetSDK: %s", e)
                return False

        logger.info("HCNetSDK loaded from %s", lib_path)
        return True

    def init(self) -> bool:
        if not self.load():
            return False
        return bool(self._sdk.NET_DVR_Init())

    def set_connect_time(self, wait_time: int = 5000, try_times: int = 3):
        self._sdk.NET_DVR_SetConnectTime(wait_time, try_times)

    def set_reconnect(self, interval: int = 10000, enable: bool = True):
        self._sdk.NET_DVR_SetReconnect(interval, enable)

    def login(self, ip: str, port: int, username: str, password: str) -> tuple[int, NET_DVR_DEVICEINFO_V30]:
        """Login to device. Returns (user_id, device_info). user_id < 0 means failure."""
        device_info = NET_DVR_DEVICEINFO_V30()
        user_id = self._sdk.NET_DVR_Login_V30(
            ip.encode("utf-8"),
            port,
            username.encode("utf-8"),
            password.encode("utf-8"),
            byref(device_info),
        )
        if user_id < 0:
            err = self._sdk.NET_DVR_GetLastError()
            logger.error("Login failed for %s:%d — error code %d", ip, port, err)
        else:
            ip_chan_num = device_info.byIPChanNum + (device_info.byHighDChanNum << 8)
            logger.info(
                "Logged in to %s:%d — userID=%d, startDChan=%d, ipChannels=%d",
                ip, port, user_id, device_info.byStartDChan, ip_chan_num,
            )
        return user_id, device_info

    def real_play(self, user_id: int, channel: int, callback: REALDATACALLBACK,
                  stream_type: int = 0, link_mode: int = 0) -> int:
        """Start real-time preview. Returns play handle (< 0 on failure)."""
        preview = NET_DVR_PREVIEWINFO()
        preview.lChannel = channel
        preview.dwStreamType = stream_type  # 0=main, 1=sub
        preview.dwLinkMode = link_mode      # 0=TCP
        preview.hPlayWnd = None
        preview.bBlocked = 1

        handle = self._sdk.NET_DVR_RealPlay_V40(user_id, byref(preview), callback, None)
        if handle < 0:
            err = self._sdk.NET_DVR_GetLastError()
            logger.error("RealPlay failed for channel %d — error code %d", channel, err)
        else:
            logger.info("RealPlay started — channel=%d, handle=%d", channel, handle)
        return handle

    def stop_real_play(self, handle: int) -> bool:
        return bool(self._sdk.NET_DVR_StopRealPlay(handle))

    def logout(self, user_id: int) -> bool:
        return bool(self._sdk.NET_DVR_Logout(user_id))

    def cleanup(self) -> bool:
        return bool(self._sdk.NET_DVR_Cleanup())

    def get_last_error(self) -> int:
        return self._sdk.NET_DVR_GetLastError()


# Singleton
sdk = HCNetSDK()

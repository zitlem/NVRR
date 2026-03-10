"""MediaMTX configuration manager — uses the MediaMTX API for live updates."""

import aiohttp
import yaml
import os
import subprocess
import logging

logger = logging.getLogger(__name__)

MEDIAMTX_CONFIG_PATH = os.environ.get(
    "MEDIAMTX_CONFIG_PATH", "/opt/nvrr/mediamtx/mediamtx.yml"
)
MEDIAMTX_API = os.environ.get("MEDIAMTX_API", "http://127.0.0.1:9997")
STREAM_MODE = os.environ.get("STREAM_MODE", "rtsp")


async def sync_paths(cameras: list[dict]):
    """Sync camera paths to MediaMTX via its REST API."""
    if STREAM_MODE == "sdk":
        # SDK mode: FFmpeg publishes directly to MediaMTX, which auto-creates paths.
        # No path configuration needed — MediaMTX accepts any RTSP publish by default.
        logger.info("SDK mode — skipping MediaMTX path sync (auto-accept publishers)")
        return

    # Build desired state
    desired: dict[str, dict] = {}
    for cam in cameras:
        if not cam.get("enabled"):
            continue
        path_name = f"cam{cam['id']}"
        desired[path_name] = {
            "source": cam["rtsp_url"],
            "sourceProtocol": "tcp",
            "sourceOnDemand": False,
        }

    async with aiohttp.ClientSession() as session:
        # Get current paths from MediaMTX
        current_paths: set[str] = set()
        try:
            async with session.get(
                f"{MEDIAMTX_API}/v3/config/paths/list",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("items"):
                        current_paths = {item["name"] for item in data["items"]}
        except Exception as e:
            logger.warning("Could not list MediaMTX paths: %s", e)
            # Fallback to config file approach
            write_config_file(cameras)
            reload_mediamtx()
            return

        # Remove paths that shouldn't exist
        for path_name in current_paths:
            if path_name.startswith("cam") and path_name not in desired:
                try:
                    async with session.delete(
                        f"{MEDIAMTX_API}/v3/config/paths/delete/{path_name}",
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status == 200:
                            logger.info("Removed path: %s", path_name)
                except Exception as e:
                    logger.warning("Failed to remove path %s: %s", path_name, e)

        # Add or update desired paths
        for path_name, path_config in desired.items():
            if path_name in current_paths:
                # Update existing
                try:
                    async with session.patch(
                        f"{MEDIAMTX_API}/v3/config/paths/patch/{path_name}",
                        json=path_config,
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status == 200:
                            logger.info("Updated path: %s", path_name)
                except Exception as e:
                    logger.warning("Failed to update path %s: %s", path_name, e)
            else:
                # Add new
                try:
                    async with session.post(
                        f"{MEDIAMTX_API}/v3/config/paths/add/{path_name}",
                        json=path_config,
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status == 200:
                            logger.info("Added path: %s", path_name)
                        else:
                            body = await resp.text()
                            logger.warning("Failed to add path %s: %s %s", path_name, resp.status, body)
                except Exception as e:
                    logger.warning("Failed to add path %s: %s", path_name, e)

    # Also write config file so MediaMTX has paths on next restart (RTSP mode only)
    if STREAM_MODE != "sdk":
        write_config_file(cameras)


def write_config_file(cameras: list[dict]):
    """Write the MediaMTX config YAML file (used on restart)."""
    config = {
        "logLevel": "info",
        "logDestinations": ["stdout"],
        "api": True,
        "apiAddress": "127.0.0.1:9997",
        "rtsp": True,
        "rtspAddress": ":8554",
        "rtmp": True,
        "rtmpAddress": ":1935",
        "hls": True,
        "hlsAddress": ":8888",
        "hlsAlwaysRemux": True,
        "hlsSegmentCount": 7,
        "hlsSegmentDuration": "1s",
        "hlsPartDuration": "200ms",
        "hlsSegmentMaxSize": "50M",
        "hlsAllowOrigin": "*",
        "pathDefaults": {
            "source": "publisher",
            "sourceOnDemand": False,
            "overridePublisher": True,
        },
        "paths": {"all_others": None},
    }

    for cam in cameras:
        if not cam.get("enabled"):
            continue
        path_name = f"cam{cam['id']}"
        if STREAM_MODE != "sdk":
            config["paths"][path_name] = {
                "source": cam["rtsp_url"],
                "sourceProtocol": "tcp",
                "sourceOnDemand": False,
            }

    try:
        os.makedirs(os.path.dirname(MEDIAMTX_CONFIG_PATH), exist_ok=True)
        with open(MEDIAMTX_CONFIG_PATH, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        logger.info("MediaMTX config written to %s", MEDIAMTX_CONFIG_PATH)
    except Exception as e:
        logger.warning("Failed to write config file: %s", e)


def reload_mediamtx():
    """Reload MediaMTX by restarting its systemd service (Linux only)."""
    try:
        subprocess.run(
            ["systemctl", "restart", "mediamtx"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        logger.info("MediaMTX restarted successfully")
    except subprocess.CalledProcessError as e:
        logger.error("Failed to restart MediaMTX: %s", e.stderr.decode())
    except FileNotFoundError:
        pass  # Windows — API handles it

"""Network device discovery using ONVIF WS-Discovery (multicast probe)."""

import asyncio
import socket
import struct
import uuid
import xml.etree.ElementTree as ET
import re
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MULTICAST_ADDR = "239.255.255.250"
MULTICAST_PORT = 3702
PROBE_TIMEOUT = 5  # seconds

WS_DISCOVERY_PROBE = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope
    xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
    xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery"
    xmlns:wsdp="http://schemas.xmlsoap.org/ws/2006/02/devprof">
  <soap:Header>
    <wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</wsa:Action>
    <wsa:MessageID>urn:uuid:{msg_id}</wsa:MessageID>
    <wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>
  </soap:Header>
  <soap:Body>
    <wsd:Probe>
      <wsd:Types>wsdp:Device</wsd:Types>
    </wsd:Probe>
  </soap:Body>
</soap:Envelope>"""


@dataclass
class DiscoveredDevice:
    ip: str
    port: int
    name: str
    model: str
    manufacturer: str
    xaddrs: str
    hardware: str


def _parse_probe_match(data: bytes) -> DiscoveredDevice | None:
    """Parse a WS-Discovery ProbeMatch response."""
    try:
        text = data.decode("utf-8", errors="ignore")
        root = ET.fromstring(text)

        # Find XAddrs (service URLs)
        xaddrs = ""
        for el in root.iter():
            if el.tag.endswith("XAddrs") and el.text:
                xaddrs = el.text.strip()
                break

        if not xaddrs:
            return None

        # Extract IP from XAddrs
        ip_match = re.search(r"https?://([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)[:/]", xaddrs)
        if not ip_match:
            return None
        ip = ip_match.group(1)

        # Extract port
        port_match = re.search(r":(\d+)", xaddrs.split(ip)[1]) if ip in xaddrs else None
        port = int(port_match.group(1)) if port_match else 80

        # Try to find scopes for device info
        scopes_text = ""
        for el in root.iter():
            if el.tag.endswith("Scopes") and el.text:
                scopes_text = el.text.strip()
                break

        name = ""
        model = ""
        manufacturer = ""
        hardware = ""

        for scope in scopes_text.split():
            scope_lower = scope.lower()
            if "/name/" in scope_lower:
                name = scope.rsplit("/", 1)[-1].replace("%20", " ")
            elif "/hardware/" in scope_lower:
                hardware = scope.rsplit("/", 1)[-1].replace("%20", " ")
                if not model:
                    model = hardware
            elif "/location/" in scope_lower:
                pass  # skip location
            elif "onvif.org/type/" in scope_lower:
                pass  # device type
            # Some devices put model info differently
            if not name and "/profile/" not in scope_lower and "/type/" not in scope_lower:
                # Try last segment as a fallback name
                pass

        if not name:
            name = model or hardware or f"Device at {ip}"

        return DiscoveredDevice(
            ip=ip,
            port=port,
            name=name,
            model=model,
            manufacturer=manufacturer,
            xaddrs=xaddrs,
            hardware=hardware,
        )
    except Exception as e:
        logger.debug("Failed to parse probe match: %s", e)
        return None


def _get_local_ips() -> list[str]:
    """Get IP addresses of all local network interfaces."""
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass

    # Fallback: also try connecting to a public IP to find default route
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        default_ip = s.getsockname()[0]
        s.close()
        if default_ip not in ips:
            ips.append(default_ip)
    except Exception:
        pass

    if not ips:
        ips.append("0.0.0.0")

    return ips


def _create_probe_socket(local_ip: str) -> socket.socket:
    """Create a UDP multicast socket bound to a specific interface."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_MULTICAST_TTL,
        struct.pack("b", 4),
    )
    # Bind outgoing multicast to this specific interface
    sock.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_MULTICAST_IF,
        socket.inet_aton(local_ip),
    )
    sock.settimeout(0.5)
    return sock


async def discover_devices(timeout: float = PROBE_TIMEOUT) -> list[DiscoveredDevice]:
    """Send WS-Discovery multicast probe on all network interfaces and collect responses."""
    msg_id = str(uuid.uuid4())
    probe = WS_DISCOVERY_PROBE.format(msg_id=msg_id).encode("utf-8")

    loop = asyncio.get_event_loop()
    devices: dict[str, DiscoveredDevice] = {}

    local_ips = _get_local_ips()
    logger.info("Probing on %d interface(s): %s", len(local_ips), ", ".join(local_ips))

    # Create a socket per interface and send probe on each
    sockets = []
    for local_ip in local_ips:
        try:
            sock = _create_probe_socket(local_ip)
            sock.sendto(probe, (MULTICAST_ADDR, MULTICAST_PORT))
            sockets.append(sock)
            logger.info("Sent probe on %s", local_ip)
        except Exception as e:
            logger.warning("Failed to probe on %s: %s", local_ip, e)

    if not sockets:
        logger.error("No interfaces available for discovery")
        return []

    logger.info("Waiting %ss for responses...", timeout)

    # Collect responses from all sockets
    end_time = loop.time() + timeout
    while loop.time() < end_time:
        for sock in sockets:
            try:
                data, addr = await loop.run_in_executor(None, lambda s=sock: s.recvfrom(65535))
                device = _parse_probe_match(data)
                if device and device.ip not in devices:
                    devices[device.ip] = device
                    logger.info("Discovered: %s (%s)", device.name, device.ip)
            except socket.timeout:
                continue
            except Exception as e:
                logger.debug("Recv error: %s", e)
                continue

    for sock in sockets:
        sock.close()

    return list(devices.values())

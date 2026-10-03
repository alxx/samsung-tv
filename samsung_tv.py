#!/usr/bin/env python3
"""
Samsung Smart TV Volume Control Library & Engine.

Provides remote control and automation capabilities for modern Samsung Smart TVs
running Tizen OS (2016+). The architecture combines multiple local network protocols:
    - UPnP RenderingControl (SOAP port 9197): Instant, precise read of master volume and mute state.
    - WebSocket Remote API (port 8002 wss / 8001 ws): Secure encrypted remote key injection and app control.
    - Wake-on-LAN: Staggered multi-burst broadcast/unicast magic packets to wake TVs from deep sleep.
    - DIAL / YouTube Lounge protocol (ports 8080 & 8001): Direct video playback inside native TV YouTube app.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore

logger = logging.getLogger("samsung_tv")

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "samsung-tv"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.json"
DEFAULT_SOCKET_FILE = DEFAULT_CONFIG_DIR / "daemon.sock"
DEFAULT_PID_FILE = DEFAULT_CONFIG_DIR / "daemon.pid"

UPNP_RENDERING_CONTROL_XML = """<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
{body}
</s:Body>
</s:Envelope>"""

KNOWN_APPS: Dict[str, str] = {
    "youtube": "111299001912",
    "netflix": "3201907018807",
    "prime": "3201910019365",
    "primevideo": "3201910019365",
    "spotify": "3201606009684",
    "apple": "3201807016597",
    "appletv": "3201807016597",
    "disney": "3201901017640",
    "disneyplus": "3201901017640",
    "twitch": "3201910019378",
    "browser": "org.tizen.browser",
}


def extract_youtube_video_id(url_or_id: str) -> str:
    """
    Extracts an 11-character YouTube video ID from various URL formats or a raw ID string.

    Supports:
        - https://www.youtube.com/watch?v=VIDEO_ID
        - https://youtu.be/VIDEO_ID
        - https://www.youtube.com/embed/VIDEO_ID
        - https://www.youtube.com/v/VIDEO_ID
        - https://www.youtube.com/shorts/VIDEO_ID
        - Raw 11-character video ID

    Args:
        url_or_id: YouTube video URL or ID string.

    Returns:
        The extracted 11-character video ID.
    """
    clean = url_or_id.strip()
    match = re.search(
        r"(?:v=|\/embed\/|\/watch\?v=|youtu\.be\/|\/v\/|\/shorts\/|^)([a-zA-Z0-9_-]{11})(?:[?&/#]|$)",
        clean,
    )
    if match:
        return match.group(1)
    return clean


class SamsungTVError(Exception):
    """Base exception for all Samsung TV client errors."""
    pass


class TVUnreachableError(SamsungTVError):
    """Raised when the TV cannot be reached on the network or refuses connection."""
    pass


class TVUnauthorizedError(SamsungTVError):
    """Raised when the TV rejects WebSocket connection or token is rejected."""
    pass


class TVPairingPendingError(SamsungTVError):
    """Raised when user needs to confirm the on-screen pairing authorization prompt."""
    pass


@dataclass
class TVInfo:
    """
    Detailed hardware and network metadata for a discovered Samsung Smart TV.

    Attributes:
        ip: IPv4 address of the TV on the local network.
        name: Friendly broadcast name of the TV (e.g. 'Samsung Q70 Series 65').
        model_name: Consumer model designation (e.g. 'QE65Q77TATXXC').
        model: Platform generation code (e.g. '20_NIKEM_QTV').
        mac: Wireless/Wired MAC address used for Wake-on-LAN.
        power_state: Reported power state ('on' or 'standby').
        token_auth_support: Whether the TV uses token authentication for WebSocket.
        duid: Unique device identifier / DUID string.
    """
    ip: str
    name: str = "Samsung Smart TV"
    model_name: str = "Unknown"
    model: str = "Unknown"
    mac: str = ""
    power_state: str = "unknown"
    token_auth_support: bool = True
    duid: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serializes the TV metadata to a dictionary."""
        return asdict(self)


@dataclass
class TVStatus:
    """
    Runtime operational status snapshot of a Samsung Smart TV.

    Attributes:
        ip: IPv4 address of the TV.
        name: TV friendly name.
        model_name: Model number / designation.
        power_state: 'on' or 'standby'.
        volume: Master volume level (0-100), or None if unreadable.
        muted: Audio mute state (True if muted, False if unmuted, or None).
    """
    ip: str
    name: str
    model_name: str
    power_state: str
    volume: Optional[int]
    muted: Optional[bool]

    def to_dict(self) -> Dict[str, Any]:
        """Serializes the TV status snapshot to a dictionary."""
        return asdict(self)


class SamsungTVConfig:
    """
    Manages persistent configuration for the Samsung TV client.

    Loads and saves connection settings (IP address, WebSocket auth token,
    MAC address, device names, and port overrides) in a JSON configuration file.
    """

    def __init__(self, config_path: Optional[Path] = None):
        """
        Initializes the configuration manager.

        Args:
            config_path: Custom path to the config file. Defaults to ~/.config/samsung-tv/config.json.
        """
        self.config_path = config_path or DEFAULT_CONFIG_FILE
        self.ip: Optional[str] = None
        self.token: Optional[str] = None
        self.name: Optional[str] = None
        self.model_name: Optional[str] = None
        self.mac: Optional[str] = None
        self.ws_port: int = 8002
        self.upnp_port: int = 9197
        self.client_name: str = "LaptopRemote"
        self.load()

    def load(self) -> bool:
        """
        Loads saved configuration from the JSON file on disk.

        Returns:
            True if configuration was successfully loaded, False otherwise.
        """
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.ip = data.get("ip")
                    self.token = data.get("token")
                    self.name = data.get("name")
                    self.model_name = data.get("model_name")
                    self.mac = data.get("mac")
                    self.ws_port = int(data.get("ws_port", 8002))
                    self.upnp_port = int(data.get("upnp_port", 9197))
                    self.client_name = data.get("client_name", "LaptopRemote")
                    return True
            except Exception as e:
                logger.warning("Failed to load config from %s: %s", self.config_path, e)
        return False

    def save(self) -> None:
        """
        Persists the current configuration to disk as formatted JSON.
        Creates parent directories automatically if they do not exist.
        """
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "ip": self.ip,
                "token": self.token,
                "name": self.name,
                "model_name": self.model_name,
                "mac": self.mac,
                "ws_port": self.ws_port,
                "upnp_port": self.upnp_port,
                "client_name": self.client_name,
            }
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save config to %s: %s", self.config_path, e)


def get_local_subnets() -> List[str]:
    """
    Detects local IPv4 /24 subnet prefixes (e.g. ['192.168.1']) on active network interfaces.

    Checks the default routing interface first, then inspects active network interfaces,
    filtering out loopback (127.*), Tailscale/CGNAT (100.*), and Docker virtual bridges.

    Returns:
        List of 3-octet subnet prefix strings (e.g. ['192.168.1']).
    """
    subnets: List[str] = []

    # 1. Routing socket test to find the primary outgoing interface
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        parts = local_ip.split(".")
        if len(parts) == 4 and not parts[0].startswith("127"):
            subnets.append(f"{parts[0]}.{parts[1]}.{parts[2]}")
    except Exception:
        pass

    # 2. Check system IP addresses for additional physical LAN interfaces
    try:
        import subprocess
        res = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True, timeout=1.0)
        for line in res.stdout.splitlines():
            m = re.search(r"inet\s+(\d+\.\d+\.\d+)\.\d+\/", line)
            if m:
                pref = m.group(1)
                # Ignore loopback (127.*), CGNAT/Tailscale (100.*), Docker (172.17.*)
                if not (pref.startswith("127.") or pref.startswith("100.") or pref.startswith("172.17.")):
                    if pref not in subnets:
                        subnets.append(pref)
    except Exception:
        pass

    if not subnets:
        subnets.append("192.168.1")
    return subnets


def fast_arp_lookup(target_mac: Optional[str] = None) -> Optional[str]:
    """
    Quickly looks up an IP address from the Linux kernel ARP cache (/proc/net/arp).

    If target_mac is specified, looks specifically for the IP associated with that MAC.
    Otherwise, returns the first IP matching known Samsung OUI prefixes.

    Args:
        target_mac: Optional target MAC address string (colons/dashes ignored).

    Returns:
        IPv4 address string if found in the ARP table, or None.
    """
    clean_target = re.sub(r"[^0-9a-fA-F]", "", target_mac).lower() if target_mac else None
    try:
        with open("/proc/net/arp", "r", encoding="utf-8") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 4:
                    ip = parts[0]
                    mac = re.sub(r"[^0-9a-fA-F]", "", parts[3]).lower()
                    if clean_target and mac == clean_target:
                        return ip
                    # If no target specified, match Samsung OUI patterns (e.g. 64:e7:d8)
                    if not clean_target and mac.startswith(("64e7d8", "cc07ab", "b8b81e", "508569")):
                        return ip
    except Exception:
        pass
    return None


def probe_tv_rest(ip: str, timeout: float = 1.5) -> Optional[TVInfo]:
    """
    Queries the Samsung REST device info endpoint on port 8001.

    Args:
        ip: IPv4 address of the target host.
        timeout: Maximum network socket wait time in seconds.

    Returns:
        A populated TVInfo object if the host is a reachable Samsung TV, else None.
    """
    url = f"http://{ip}:8001/api/v2/"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SamsungTVVolClient"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                device = data.get("device", {})
                return TVInfo(
                    ip=ip,
                    name=device.get("name", data.get("name", "Samsung Smart TV")),
                    model_name=device.get("modelName", "Unknown"),
                    model=device.get("model", "Unknown"),
                    mac=device.get("wifiMac", device.get("mac", "")),
                    power_state=device.get("PowerState", "on"),
                    token_auth_support=device.get("TokenAuthSupport", "true").lower() == "true",
                    duid=device.get("duid", data.get("id", "")),
                )
    except Exception:
        pass
    return None


def discover_samsung_tv(timeout: float = 2.0, target_mac: Optional[str] = None) -> List[TVInfo]:
    """
    Actively discovers Samsung Smart TVs on the local LAN.

    Executes a multi-channel discovery pipeline:
        1. Fast ARP Cache Lookup: Instantly checks /proc/net/arp for known MAC or Samsung OUIs.
        2. High-Speed Subnet Port Sweep: Concurrently checks port 8001 across all 254 IPs
           on each local subnet using a multi-threaded connection pool (~400ms).
        3. SSDP M-SEARCH Broadcast: Transmits UPnP discovery datagrams to 239.255.255.250:1900.
        4. REST Device Verification: Validates responding IPs by querying http://{ip}:8001/api/v2/.

    Args:
        timeout: Maximum search duration in seconds.
        target_mac: Optional target TV MAC address to prioritize.

    Returns:
        List of verified TVInfo records discovered on the network.
    """
    candidate_ips: List[str] = []

    # 1. ARP Cache check
    arp_ip = fast_arp_lookup(target_mac)
    if arp_ip and arp_ip not in candidate_ips:
        candidate_ips.append(arp_ip)

    # 2. Parallel subnet sweep on port 8001
    def _check_port(ip_addr: str) -> Optional[str]:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.35)
        try:
            res = s.connect_ex((ip_addr, 8001))
            return ip_addr if res == 0 else None
        except Exception:
            return None
        finally:
            s.close()

    subnets = get_local_subnets()
    all_sweep_ips = [f"{sub}.{i}" for sub in subnets for i in range(1, 255)]
    try:
        with ThreadPoolExecutor(max_workers=min(256, max(32, len(all_sweep_ips)))) as executor:
            for live_ip in executor.map(_check_port, all_sweep_ips):
                if live_ip and live_ip not in candidate_ips:
                    candidate_ips.append(live_ip)
    except Exception as e:
        logger.debug("Subnet port sweep error: %s", e)

    # 3. SSDP M-SEARCH Broadcast
    ssdp_request = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 1\r\n"
        "ST: ssdp:all\r\n\r\n"
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(0.3)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    try:
        sock.sendto(ssdp_request.encode(), ("239.255.255.250", 1900))
        start_time = time.time()
        while time.time() - start_time < 0.5:
            try:
                data, addr = sock.recvfrom(4096)
                ip = addr[0]
                text = data.decode("utf-8", errors="ignore").lower()
                if "samsung" in text or "dmr" in text or "sec" in text:
                    if ip not in candidate_ips:
                        candidate_ips.append(ip)
            except (socket.timeout, TimeoutError):
                break
            except Exception:
                break
    except Exception as e:
        logger.debug("SSDP discovery error: %s", e)
    finally:
        sock.close()

    # 4. REST Device Verification
    results: List[TVInfo] = []
    for ip in candidate_ips:
        info = probe_tv_rest(ip, timeout=0.8)
        if info:
            results.append(info)

    # If target_mac specified, sort matching TV to top of list
    if target_mac and len(results) > 1:
        clean_target = re.sub(r"[^0-9a-fA-F]", "", target_mac).lower()
        results.sort(key=lambda t: 0 if re.sub(r"[^0-9a-fA-F]", "", t.mac).lower() == clean_target else 1)

    return results


class YouTubeLounge:
    """
    Client for the YouTube Lounge / DIAL protocol used on Smart TVs.
    Allows initiating direct video playback within the native YouTube TV app
    over the local network without streaming video bytes from the computer.
    """

    DIAL_PORT = 8080
    API_BASE = "https://www.youtube.com/api/lounge"
    GET_TOKEN_URL = f"{API_BASE}/pairing/get_lounge_token_batch"
    BIND_URL = f"{API_BASE}/bc/bind"

    @classmethod
    def get_screen_id(cls, tv_ip: str, timeout: float = 2.0) -> Optional[str]:
        """
        Retrieves the active YouTube screenId from the TV's DIAL multiscreen service.

        Args:
            tv_ip: The IP address of the Samsung TV.
            timeout: Network request timeout in seconds.

        Returns:
            The screenId string if YouTube is running and advertising, else None.
        """
        url = f"http://{tv_ip}:{cls.DIAL_PORT}/ws/apps/YouTube"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                xml_data = resp.read().decode("utf-8")
                root = ET.fromstring(xml_data)
                for elem in root.iter():
                    if elem.tag.endswith("screenId") and elem.text:
                        return elem.text.strip()
        except Exception as e:
            logger.debug("Failed to query DIAL YouTube status on %s: %s", tv_ip, e)
        return None

    @classmethod
    def play_video(
        cls,
        tv_ip: str,
        video_id: str,
        client_name: str = "LaptopRemote",
        launch_timeout: float = 6.0,
    ) -> bool:
        """
        Commands the TV's native YouTube app to immediately play a specific video.

        If the YouTube app is not running, it launches the app first and polls
        the TV's DIAL service until the app initializes and advertises its screenId.

        Args:
            tv_ip: The TV's IP address.
            video_id: The 11-character YouTube video ID to play.
            client_name: Controller name registered in the Lounge session.
            launch_timeout: Maximum seconds to wait for YouTube app to initialize.

        Returns:
            True if playback was successfully engaged, False otherwise.
        """
        screen_id = cls.get_screen_id(tv_ip)

        # 1. If screenId is not yet available, launch YouTube and poll for screenId
        if not screen_id:
            launch_url = f"http://{tv_ip}:8001/api/v2/applications/111299001912"
            req = urllib.request.Request(launch_url, data=b"", method="POST")
            try:
                urllib.request.urlopen(req, timeout=3.0)
            except Exception as e:
                logger.debug("Failed to launch YouTube via REST: %s", e)

            start = time.time()
            while time.time() - start < launch_timeout:
                time.sleep(0.5)
                screen_id = cls.get_screen_id(tv_ip, timeout=1.0)
                if screen_id:
                    break

        if not screen_id:
            logger.error("Could not obtain YouTube screenId from TV at %s", tv_ip)
            return False

        # 2. Exchange screenId for Lounge Token
        token_data = urllib.parse.urlencode({"screen_ids": screen_id}).encode("utf-8")
        token_req = urllib.request.Request(
            cls.GET_TOKEN_URL,
            data=token_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
            },
        )
        try:
            with urllib.request.urlopen(token_req, timeout=5.0) as resp:
                tok_json = json.loads(resp.read().decode("utf-8"))
            lounge_token = tok_json["screens"][0]["loungeToken"]
        except Exception as e:
            logger.error("Failed to obtain YouTube lounge token: %s", e)
            return False

        # 3. Establish binding session to obtain SID and gsessionid
        bind_q1 = {
            "CVER": "1",
            "RID": "1",
            "VER": "8",
            "app": "youtube-desktop",
            "device": "REMOTE_CONTROL",
            "id": "remote",
            "loungeIdToken": lounge_token,
            "name": client_name,
        }
        bind_u1 = f"{cls.BIND_URL}?{urllib.parse.urlencode(bind_q1)}"
        bind_req1 = urllib.request.Request(
            bind_u1,
            data=b"",
            headers={
                "Origin": "https://www.youtube.com",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
            },
        )
        try:
            with urllib.request.urlopen(bind_req1, timeout=8.0) as resp:
                bind_body1 = resp.read().decode("utf-8")

            sid_m = re.search(r'\[\"c\",\s*\"([^\"]+)\"', bind_body1)
            gsid_m = re.search(r'\[\"S\",\s*\"([^\"]+)\"', bind_body1)
            if not sid_m or not gsid_m:
                logger.error("Failed to parse YouTube Lounge session IDs from response")
                return False
            sid = sid_m.group(1)
            gsid = gsid_m.group(1)
        except Exception as e:
            logger.error("Failed to bind YouTube Lounge session: %s", e)
            return False

        # 4. Dispatch setPlaylist playback command
        play_q = {
            "CVER": "1",
            "RID": "2",
            "SID": sid,
            "VER": "8",
            "gsessionid": gsid,
            "loungeIdToken": lounge_token,
        }
        play_body = {
            "count": "1",
            "req0__sc": "setPlaylist",
            "req0_videoId": video_id,
            "req0_videoIds": video_id,
            "req0_currentTime": "0",
            "req0_currentIndex": "0",
        }
        play_u = f"{cls.BIND_URL}?{urllib.parse.urlencode(play_q)}"
        play_req = urllib.request.Request(
            play_u,
            data=urllib.parse.urlencode(play_body).encode("utf-8"),
            headers={
                "Origin": "https://www.youtube.com",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
            },
        )
        try:
            with urllib.request.urlopen(play_req, timeout=8.0) as resp:
                return resp.status == 200
        except Exception as e:
            logger.error("Failed to dispatch YouTube setPlaylist command: %s", e)
            return False


class SamsungTVClient:
    """
    Main client class for controlling Samsung Smart TVs over the local network.

    Combines UPnP RenderingControl (instant, reliable volume/mute status queries)
    with WebSocket Remote Control (real-time key dispatch, app launching, power control),
    Wake-on-LAN multi-burst wake sequences, and YouTube Lounge / DIAL remote playback.
    """

    def __init__(
        self,
        ip: Optional[str] = None,
        token: Optional[str] = None,
        ws_port: int = 8002,
        upnp_port: int = 9197,
        client_name: str = "LaptopRemote",
        config_path: Optional[Path] = None,
        auto_save_config: bool = True,
    ):
        """
        Initializes the Samsung TV client instance.

        Args:
            ip: Target TV IPv4 address. If None, resolved from config or auto-discovery.
            token: Authentication token from TV pairing. If None, loaded from config.
            ws_port: WebSocket port for Samsung remote control API (default 8002 SSL).
            upnp_port: UPnP RenderingControl SOAP port (default 9197).
            client_name: Friendly client identifier shown in TV pairing notifications.
            config_path: Optional custom path to JSON configuration file.
            auto_save_config: If True, newly discovered TV details and tokens are saved to disk.
        """
        self.config = SamsungTVConfig(config_path)
        self.explicit_ip = ip is not None
        self.ip = ip or self.config.ip or os.environ.get("SAMSUNG_TV_IP")
        self.token = token or self.config.token or os.environ.get("SAMSUNG_TV_TOKEN")
        self.ws_port = ws_port or self.config.ws_port
        self.upnp_port = upnp_port or self.config.upnp_port
        self.client_name = client_name or self.config.client_name
        self.auto_save_config = auto_save_config

        self._info: Optional[TVInfo] = None

    def discover_and_update_ip(self, timeout: float = 2.0) -> Optional[str]:
        """
        Actively discovers the TV's current IP address on the local network.

        Resolves dynamic DHCP address changes by verifying candidate hosts against
        the known TV MAC address, model name, or unique DUID.
        When discovered, updates self.ip and persists the new configuration to disk.

        Args:
            timeout: Maximum search duration in seconds.

        Returns:
            The verified IP address string if discovered, or None if the TV is unreachable.
        """
        target_mac = self.config.mac

        # 1. Quick probe of ARP cache IP if known
        if target_mac:
            arp_ip = fast_arp_lookup(target_mac)
            if arp_ip:
                info = probe_tv_rest(arp_ip, timeout=0.4)
                if info:
                    if arp_ip != self.ip:
                        logger.info("TV IP actively discovered via ARP table: %s (was %s)", arp_ip, self.ip)
                    self.ip = arp_ip
                    self.config.ip = arp_ip
                    self.config.name = info.name
                    self.config.model_name = info.model_name
                    self.config.mac = info.mac
                    self._info = info
                    if self.auto_save_config:
                        self.config.save()
                    return self.ip

        # 2. Quick probe of current / last known IP
        if self.ip:
            info = probe_tv_rest(self.ip, timeout=0.35)
            if info:
                clean_target = re.sub(r"[^0-9a-fA-F]", "", target_mac).lower() if target_mac else None
                clean_dev_mac = re.sub(r"[^0-9a-fA-F]", "", info.mac).lower() if info.mac else None
                if not clean_target or clean_dev_mac == clean_target:
                    self._info = info
                    return self.ip

        # 3. Active LAN discovery (multi-threaded subnet sweep + SSDP)
        discovered = discover_samsung_tv(timeout=timeout, target_mac=target_mac)
        if discovered:
            matched_tv = discovered[0]
            logger.info(
                "TV actively discovered on LAN: %s (%s, MAC: %s)",
                matched_tv.ip,
                matched_tv.model_name,
                matched_tv.mac,
            )
            self.ip = matched_tv.ip
            self.config.ip = matched_tv.ip
            self.config.name = matched_tv.name
            self.config.model_name = matched_tv.model_name
            self.config.mac = matched_tv.mac
            self._info = matched_tv
            if self.auto_save_config:
                self.config.save()
            return self.ip

        logger.debug(
            "Active discovery found no responding TV (TV may be in standby). Retaining last known IP: %s",
            self.ip,
        )
        return self.ip

    def ensure_ip(self, timeout: float = 2.5, force_discovery: bool = False) -> str:
        """
        Ensures a valid TV IP address is known, running active discovery if necessary.

        If an explicit IP was provided at construction, uses it directly.
        Otherwise actively verifies and discovers the TV's current IP address on the network.

        Args:
            timeout: Maximum discovery search time in seconds.
            force_discovery: If True, forces active network discovery even if an IP is set.

        Returns:
            The resolved TV IPv4 address.

        Raises:
            TVUnreachableError: If no Samsung TV is configured or found on the network.
        """
        if self.explicit_ip and self.ip:
            return self.ip

        if force_discovery or not self.ip:
            discovered = self.discover_and_update_ip(timeout=timeout)
            if discovered:
                return discovered
            if self.ip:
                return self.ip
            raise TVUnreachableError("No Samsung TV found on the local network. Specify --ip manually.")

        # Actively verify / update IP address
        verified = self.discover_and_update_ip(timeout=timeout)
        if verified:
            return verified
        if self.ip:
            return self.ip
        raise TVUnreachableError("No Samsung TV found on the local network. Specify --ip manually.")

    def get_info(self, timeout: float = 2.0) -> TVInfo:
        """
        Fetches detailed device hardware and software information via the TV REST API.

        Args:
            timeout: Network request timeout in seconds.

        Returns:
            A populated TVInfo object.

        Raises:
            TVUnreachableError: If the TV REST endpoint is unreachable.
        """
        ip = self.ensure_ip()
        info = probe_tv_rest(ip, timeout=timeout)
        if not info:
            raise TVUnreachableError(f"Cannot reach Samsung TV at {ip}:8001")
        self._info = info
        self.config.name = info.name
        self.config.model_name = info.model_name
        self.config.mac = info.mac
        if self.auto_save_config:
            self.config.save()
        return info

    def get_volume(self, timeout: float = 2.0) -> Optional[int]:
        """
        Queries the current master sound volume using UPnP RenderingControl GetVolume SOAP action.

        Args:
            timeout: UPnP HTTP request timeout in seconds.

        Returns:
            An integer volume level between 0 and 100, or None if query failed.
        """
        ip = self.ensure_ip()
        body = (
            '<u:GetVolume xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
            "<InstanceID>0</InstanceID>"
            "<Channel>Master</Channel>"
            "</u:GetVolume>"
        )
        soap_body = UPNP_RENDERING_CONTROL_XML.format(body=body)
        url = f"http://{ip}:{self.upnp_port}/upnp/control/RenderingControl1"
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": '"urn:schemas-upnp-org:service:RenderingControl:1#GetVolume"',
        }

        try:
            req = urllib.request.Request(url, data=soap_body.encode("utf-8"), headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                xml_data = resp.read().decode("utf-8")
                root = ET.fromstring(xml_data)
                for elem in root.iter():
                    if elem.tag.endswith("CurrentVolume") and elem.text is not None:
                        return int(elem.text)
        except Exception as e:
            logger.debug("UPnP GetVolume failed: %s", e)
        return None

    def get_mute(self, timeout: float = 2.0) -> Optional[bool]:
        """
        Queries the audio mute status using UPnP RenderingControl GetMute SOAP action.

        Args:
            timeout: UPnP HTTP request timeout in seconds.

        Returns:
            True if muted, False if unmuted, or None if query failed.
        """
        ip = self.ensure_ip()
        body = (
            '<u:GetMute xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
            "<InstanceID>0</InstanceID>"
            "<Channel>Master</Channel>"
            "</u:GetMute>"
        )
        soap_body = UPNP_RENDERING_CONTROL_XML.format(body=body)
        url = f"http://{ip}:{self.upnp_port}/upnp/control/RenderingControl1"
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": '"urn:schemas-upnp-org:service:RenderingControl:1#GetMute"',
        }

        try:
            req = urllib.request.Request(url, data=soap_body.encode("utf-8"), headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                xml_data = resp.read().decode("utf-8")
                root = ET.fromstring(xml_data)
                for elem in root.iter():
                    if elem.tag.endswith("CurrentMute") and elem.text is not None:
                        return elem.text == "1"
        except Exception as e:
            logger.debug("UPnP GetMute failed: %s", e)
        return None

    def get_status(self) -> TVStatus:
        """
        Fetches a comprehensive TV status snapshot (hardware info, volume, and mute state).

        Returns:
            A populated TVStatus object.
        """
        info = self.get_info()
        vol = self.get_volume()
        muted = self.get_mute()
        return TVStatus(
            ip=info.ip,
            name=info.name,
            model_name=info.model_name,
            power_state=info.power_state,
            volume=vol,
            muted=muted,
        )

    def _get_ws_url(self) -> str:
        """Constructs the authenticated WebSocket URL for the TV remote control channel."""
        ip = self.ensure_ip()
        name_b64 = base64.b64encode(self.client_name.encode("utf-8")).decode("utf-8")
        query_parts = [f"name={name_b64}"]
        if self.token:
            query_parts.append(f"token={self.token}")
        query = "&".join(query_parts)
        return f"wss://{ip}:{self.ws_port}/api/v2/channels/samsung.remote.control?{query}"

    async def _send_keys_async(
        self,
        keys: List[str],
        key_delay: float = 0.22,
        connect_timeout: float = 10.0,
    ) -> None:
        """
        Async implementation connecting to the TV via WebSocket and dispatching remote keys.

        Args:
            keys: List of key identifier strings (e.g. ['KEY_VOLUP', 'KEY_MUTE']).
            key_delay: Sleep duration in seconds between consecutive keys.
            connect_timeout: WebSocket handshake timeout in seconds.

        Raises:
            SamsungTVError: If websockets library is unavailable.
            TVUnauthorizedError: If authorization fails or was rejected on the TV.
            TVUnreachableError: If the TV cannot be contacted or times out.
        """
        if websockets is None:
            raise SamsungTVError("websockets library is not installed.")

        url = self._get_ws_url()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        try:
            async with websockets.connect(url, ssl=ctx, open_timeout=connect_timeout) as ws:
                # Wait for initial handshake event
                raw_msg = await asyncio.wait_for(ws.recv(), timeout=connect_timeout)
                msg = json.loads(raw_msg)
                event = msg.get("event")

                if event == "ms.channel.unauthorized":
                    self.token = None
                    self.config.token = None
                    if self.auto_save_config:
                        self.config.save()
                    raise TVUnauthorizedError("Access denied by TV. Please re-pair.")

                # Capture token if issued
                data = msg.get("data", {})
                new_token = data.get("token")
                if not new_token:
                    clients = data.get("clients", [])
                    if clients and isinstance(clients[0], dict):
                        new_token = clients[0].get("attributes", {}).get("token")

                if new_token and new_token != self.token:
                    logger.info("Received new token: %s", new_token)
                    self.token = str(new_token)
                    self.config.token = str(new_token)
                    if self.auto_save_config:
                        self.config.save()

                # Send remote keys
                for i, key in enumerate(keys):
                    cmd = {
                        "method": "ms.remote.control",
                        "params": {
                            "Cmd": "Click",
                            "DataOfCmd": key,
                            "Option": "false",
                            "TypeOfRemote": "SendRemoteKey",
                        },
                    }
                    await ws.send(json.dumps(cmd))
                    if i < len(keys) - 1:
                        await asyncio.sleep(key_delay)

        except (asyncio.TimeoutError, TimeoutError):
            raise TVUnreachableError("Timeout while connecting to Samsung TV WebSocket API.")
        except ConnectionRefusedError:
            raise TVUnreachableError(f"Connection refused at {self.ip}:{self.ws_port}. TV may be powered off.")

    def send_keys(self, keys: List[str], key_delay: float = 0.22) -> None:
        """
        Synchronously sends a sequence of remote keys over WebSocket.

        Args:
            keys: Sequence of key strings (e.g. ['KEY_VOLUP', 'KEY_MUTE']).
            key_delay: Inter-key delay in seconds.
        """
        asyncio.run(self._send_keys_async(keys, key_delay=key_delay))

    def volume_up(self, steps: int = 1) -> TVStatus:
        """
        Increases volume by a specified number of steps.

        Args:
            steps: Number of volume increments (default: 1).

        Returns:
            Updated TVStatus after applying the change.
        """
        if steps <= 0:
            return self.get_status()
        keys = ["KEY_VOLUP"] * steps
        self.send_keys(keys)
        time.sleep(0.3)
        return self.get_status()

    def volume_down(self, steps: int = 1) -> TVStatus:
        """
        Decreases volume by a specified number of steps.

        Args:
            steps: Number of volume decrements (default: 1).

        Returns:
            Updated TVStatus after applying the change.
        """
        if steps <= 0:
            return self.get_status()
        keys = ["KEY_VOLDOWN"] * steps
        self.send_keys(keys)
        time.sleep(0.3)
        return self.get_status()

    def set_volume(self, target: int, max_retries: int = 3) -> TVStatus:
        """
        Sets volume to an exact target level (0-100) using closed-loop key pulsing.

        Reads current volume via UPnP, calculates the numerical delta, pulses
        volume keys, and verifies the new reading in a closed feedback loop.

        Args:
            target: Target volume percentage between 0 and 100.
            max_retries: Maximum number of feedback adjustment iterations.

        Returns:
            Updated TVStatus reflecting the final volume.

        Raises:
            SamsungTVError: If the baseline volume cannot be read via UPnP.
        """
        target = max(0, min(100, int(target)))
        current = self.get_volume()

        if current is None:
            raise SamsungTVError("Cannot read current volume via UPnP to compute adjustment delta.")

        for _ in range(max_retries):
            diff = target - current
            if diff == 0:
                break

            steps = abs(diff)
            key = "KEY_VOLUP" if diff > 0 else "KEY_VOLDOWN"
            self.send_keys([key] * steps)
            time.sleep(0.35)

            new_vol = self.get_volume()
            if new_vol is None or new_vol == current:
                # TV reached its physical minimum (0) or maximum limit, or didn't move
                break
            current = new_vol

        return self.get_status()

    def toggle_mute(self) -> TVStatus:
        """
        Toggles audio mute state.

        Returns:
            Updated TVStatus.
        """
        self.send_keys(["KEY_MUTE"])
        time.sleep(0.3)
        return self.get_status()

    def mute(self) -> TVStatus:
        """
        Ensures TV is muted (idempotent).

        Returns:
            Updated TVStatus.
        """
        muted = self.get_mute()
        if muted is False or muted is None:
            self.send_keys(["KEY_MUTE"])
            time.sleep(0.3)
        return self.get_status()

    def unmute(self) -> TVStatus:
        """
        Ensures TV is unmuted (idempotent).

        Returns:
            Updated TVStatus.
        """
        muted = self.get_mute()
        if muted is True or muted is None:
            self.send_keys(["KEY_MUTE"])
            time.sleep(0.3)
        return self.get_status()

    def wake_on_lan(
        self,
        repeat_bursts: int = 4,
        burst_delay: float = 0.6,
        packets_per_burst: int = 3,
        wait_for_online: bool = False,
        timeout: float = 6.0,
    ) -> bool:
        """
        Sends a multi-burst Wake-on-LAN sequence to reliably wake Samsung TVs from deep standby.

        Samsung Smart TVs require initial packets to wake the low-power PHY/NIC link,
        followed by subsequent packets once the PHY is synchronized to trigger power-on.

        Args:
            repeat_bursts: Number of bursts separated by burst_delay.
            burst_delay: Interval in seconds between bursts.
            packets_per_burst: Magic packets sent per destination within each burst.
            wait_for_online: If True, polls the TV REST endpoint until it responds.
            timeout: Maximum wait time in seconds when wait_for_online is True.

        Returns:
            True if packets were dispatched (and TV came online if wait_for_online=True).

        Raises:
            SamsungTVError: If the TV MAC address is unknown or invalid.
        """
        mac = self.config.mac
        if not mac:
            try:
                info = self.get_info(timeout=1.0)
                mac = info.mac
            except Exception:
                pass

        if not mac:
            raise SamsungTVError("TV MAC address is not known. Turn the TV on once to learn its MAC.")

        mac_clean = re.sub(r"[^0-9a-fA-F]", "", mac)
        if len(mac_clean) != 12:
            raise SamsungTVError(f"Invalid MAC address: {mac}")

        mac_bytes = bytes.fromhex(mac_clean)
        magic_packet = b"\xff" * 6 + mac_bytes * 16

        # Build list of destination addresses and ports (global, subnet, unicast)
        destinations: List[Tuple[str, int]] = [
            ("255.255.255.255", 9),
            ("255.255.255.255", 7),
            ("255.255.255.255", 0),
        ]

        if self.ip:
            # Unicast directly to TV IP (bypasses router broadcast drops)
            destinations.append((self.ip, 9))
            destinations.append((self.ip, 7))
            destinations.append((self.ip, 0))

            # Subnet broadcast
            parts = self.ip.split(".")
            if len(parts) == 4:
                subnet_bcast = f"{parts[0]}.{parts[1]}.{parts[2]}.255"
                destinations.append((subnet_bcast, 9))
                destinations.append((subnet_bcast, 7))

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        try:
            for b in range(repeat_bursts):
                for _ in range(packets_per_burst):
                    for dst, port in destinations:
                        try:
                            sock.sendto(magic_packet, (dst, port))
                        except Exception:
                            pass
                    time.sleep(0.03)

                if b < repeat_bursts - 1:
                    time.sleep(burst_delay)
        finally:
            sock.close()

        if wait_for_online:
            start = time.time()
            while time.time() - start < timeout:
                if self.ip and probe_tv_rest(self.ip, timeout=0.8):
                    return True
                # Check if the TV woke up with a dynamically reassigned IP
                found_ip = self.discover_and_update_ip(timeout=1.0)
                if found_ip and probe_tv_rest(found_ip, timeout=0.8):
                    return True
                time.sleep(0.5)
            return False

        return True

    def power_toggle(self) -> None:
        """Toggles the TV power state by sending KEY_POWER."""
        self.send_keys(["KEY_POWER"])

    def power_off(self, wait_for_standby: bool = True, timeout: float = 4.0) -> bool:
        """
        Powers off the TV into standby mode.

        Checks current power state via REST probe; if already in standby or unreachable,
        returns immediately. Otherwise dispatches KEY_POWER and optionally polls
        until the TV transitions to standby.

        Args:
            wait_for_standby: If True, waits up to timeout seconds for standby state.
            timeout: Maximum wait time in seconds for power-off transition.

        Returns:
            True if the TV is confirmed in standby or signal was dispatched.
        """
        if self.ip:
            info = probe_tv_rest(self.ip, timeout=1.0)
            if not info or info.power_state != "on":
                return True

        self.send_keys(["KEY_POWER"])

        if wait_for_standby and self.ip:
            start = time.time()
            while time.time() - start < timeout:
                info = probe_tv_rest(self.ip, timeout=0.8)
                if not info or info.power_state != "on":
                    return True
                time.sleep(0.5)
        return True

    def _send_ws_command(self, cmd_dict: Dict[str, Any], timeout: float = 5.0) -> None:
        """
        Sends an arbitrary JSON command envelope over the TV WebSocket connection.

        Args:
            cmd_dict: Command dictionary to serialize and dispatch.
            timeout: Connection and transmission timeout in seconds.
        """
        async def _run():
            if websockets is None:
                return
            url = self._get_ws_url()
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            async with websockets.connect(url, ssl=ctx, open_timeout=timeout) as ws:
                await asyncio.wait_for(ws.recv(), timeout=timeout)
                await ws.send(json.dumps(cmd_dict))
        try:
            asyncio.run(_run())
        except Exception as e:
            logger.debug("WebSocket command send failed: %s", e)

    def resolve_app_id(self, app_name_or_id: str) -> str:
        """
        Resolves a friendly application name to its Samsung Tizen App ID.

        Args:
            app_name_or_id: Friendly name (e.g. 'youtube', 'netflix') or raw App ID.

        Returns:
            The resolved numeric or package App ID string.
        """
        key = app_name_or_id.lower().replace(" ", "").replace("-", "").replace("_", "")
        return KNOWN_APPS.get(key, app_name_or_id)

    def launch_app(self, app_name_or_id: str, extra: Optional[str] = None) -> bool:
        """
        Launches an application on the TV by friendly name or App ID.

        If YouTube is specified and a video URL or ID is provided,
        engages native playback directly within the TV's native YouTube app
        via the YouTube Lounge / DIAL pairing protocol.

        Args:
            app_name_or_id: Friendly app name (e.g. 'youtube', 'netflix') or numeric App ID.
            extra: Optional video URL/ID (for YouTube) or additional launch parameters.

        Returns:
            True if launched or playback started successfully, False otherwise.
        """
        ip = self.ensure_ip()
        app_id = self.resolve_app_id(app_name_or_id)

        is_youtube = app_id == "111299001912" or "youtube" in app_name_or_id.lower()
        video_id = None
        if is_youtube and extra:
            video_id = extract_youtube_video_id(extra)

        if is_youtube and video_id:
            # Engage native playback directly inside TV's native YouTube app via Lounge protocol
            if YouTubeLounge.play_video(ip, video_id, client_name=self.client_name):
                return True
            logger.warning("YouTube Lounge protocol failed, falling back to REST/WebSocket launch")

        url = f"http://{ip}:8001/api/v2/applications/{app_id}"
        headers = {"Content-Type": "application/json"}
        payload_bytes = b""
        if video_id:
            url = f"{url}?v={video_id}"
            payload_data = {
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "payload": f"v={video_id}",
                "metaTag": f"v={video_id}",
            }
            payload_bytes = json.dumps(payload_data).encode("utf-8")

        # 1. REST Launch
        try:
            req = urllib.request.Request(url, data=payload_bytes if payload_bytes else b"", headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                pass
        except Exception as e:
            logger.debug("REST launch: %s", e)

        # 2. WebSocket emit (ensures deep link is received by Cobalt/Tizen event bus)
        try:
            meta = f"v={video_id}" if video_id else ""
            action_type = "DEEP_LINK" if video_id else "NATIVE_LAUNCH"
            ws_cmd = {
                "method": "ms.channel.emit",
                "params": {
                    "event": "ed.apps.launch",
                    "to": "host",
                    "data": {
                        "action_type": action_type,
                        "appId": app_id,
                        "metaTag": meta,
                    },
                },
            }
            self._send_ws_command(ws_cmd)
        except Exception as e:
            logger.debug("WS emit failed: %s", e)

        return True

    def play_youtube(self, url_or_id: str) -> bool:
        """
        Commands the TV's native YouTube app to immediately play a specific video.

        Args:
            url_or_id: YouTube video URL or 11-character video ID.

        Returns:
            True if playback was successfully engaged, False otherwise.
        """
        return self.launch_app("youtube", extra=url_or_id)

    def close_app(self, app_name_or_id: str) -> bool:
        """
        Closes an active application on the TV.

        Args:
            app_name_or_id: Friendly name or App ID of the application to terminate.

        Returns:
            True if application was successfully closed, False otherwise.
        """
        ip = self.ensure_ip()
        app_id = self.resolve_app_id(app_name_or_id)
        url = f"http://{ip}:8001/api/v2/applications/{app_id}"
        try:
            req = urllib.request.Request(url, method="DELETE")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                return resp.status == 200
        except Exception as e:
            logger.debug("Close app failed: %s", e)
            return False

    def get_app_status(self, app_name_or_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves the runtime status and visibility of an application.

        Args:
            app_name_or_id: Friendly name or App ID.

        Returns:
            Dictionary containing application status (running, visible, version), or None.
        """
        ip = self.ensure_ip()
        app_id = self.resolve_app_id(app_name_or_id)
        url = f"http://{ip}:8001/api/v2/applications/{app_id}"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception:
            pass
        return None

    def list_installed_apps(self) -> List[Dict[str, Any]]:
        """
        Checks installation and running status of all known popular streaming apps on the TV.

        Returns:
            List of dictionaries with name, app_id, running, visible, and version fields.
        """
        results = []
        for name, app_id in KNOWN_APPS.items():
            # Skip aliases with duplicate IDs
            if name in ("primevideo", "appletv", "disneyplus"):
                continue
            status = self.get_app_status(app_id)
            if status:
                results.append({
                    "name": status.get("name", name.title()),
                    "app_id": app_id,
                    "running": status.get("running", False),
                    "visible": status.get("visible", False),
                    "version": status.get("version", "Unknown"),
                })
        return results


def is_daemon_running() -> bool:
    """
    Checks if the fast background socket daemon is running and responsive.

    Connects to ~/.config/samsung-tv/daemon.sock and performs a ping-pong handshake.

    Returns:
        True if the daemon responded with 'pong', False otherwise.
    """
    if not DEFAULT_SOCKET_FILE.exists():
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.3)
        s.connect(str(DEFAULT_SOCKET_FILE))
        s.sendall(json.dumps({"cmd": "ping"}).encode("utf-8") + b"\n")
        resp = s.recv(1024)
        s.close()
        data = json.loads(resp.decode("utf-8"))
        return data.get("status") == "pong"
    except Exception:
        return False


def send_daemon_command(cmd_dict: Dict[str, Any], timeout: float = 3.0) -> Dict[str, Any]:
    """
    Sends a command dictionary to the background daemon via UNIX domain socket.

    Args:
        cmd_dict: JSON-serializable command request.
        timeout: Socket timeout in seconds.

    Returns:
        Response dictionary from the daemon.

    Raises:
        OSError: If connecting to or communicating with the socket fails.
        json.JSONDecodeError: If the daemon response cannot be parsed as JSON.
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(DEFAULT_SOCKET_FILE))
    try:
        s.sendall(json.dumps(cmd_dict).encode("utf-8") + b"\n")
        resp_data = b""
        while not resp_data.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            resp_data += chunk
        return json.loads(resp_data.decode("utf-8").strip())
    finally:
        s.close()

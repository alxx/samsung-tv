#!/usr/bin/env python3
"""
Fast background daemon for Samsung TV volume control.
Maintains a persistent WebSocket connection to the TV for instant (<10ms) responses.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import signal
import ssl
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import websockets
except ImportError:
    websockets = None

from samsung_tv import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_PID_FILE,
    DEFAULT_SOCKET_FILE,
    SamsungTVClient,
    SamsungTVError,
    TVStatus,
    TVUnauthorizedError,
    TVUnreachableError,
)

logger = logging.getLogger("samsung_tv_daemon")


class TVDaemon:
    """
    Background daemon maintaining a persistent, warm WebSocket connection to the Samsung TV.

    Eliminates connection handshake latency, enabling instant (<10ms) remote key dispatch,
    volume adjustment, and app management over a local UNIX domain socket.
    """

    def __init__(self, client: Optional[SamsungTVClient] = None):
        """
        Initializes the background daemon instance.

        Args:
            client: Optional SamsungTVClient instance. If None, initialized with default configuration.
        """
        self.client = client or SamsungTVClient()
        self.ws: Any = None
        self.running = False
        self.socket_path = DEFAULT_SOCKET_FILE
        self.pid_path = DEFAULT_PID_FILE
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()

    async def _ensure_ws(self) -> Any:
        """
        Ensures a live, authenticated WebSocket connection to the TV is open.

        Re-establishes connection if closed or dropped, handling token validation.

        Returns:
            An active websockets client connection.

        Raises:
            TVUnauthorizedError: If the TV rejects authentication.
        """
        if self.ws and not self.ws.closed:
            return self.ws

        url = self.client._get_ws_url()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        logger.info("Connecting to TV WebSocket: %s", url)
        try:
            self.ws = await websockets.connect(url, ssl=ctx, open_timeout=6.0)
            raw_msg = await asyncio.wait_for(self.ws.recv(), timeout=6.0)
        except Exception as e:
            logger.info("WebSocket connection to %s failed (%s). Running active rediscovery...", self.client.ip, e)
            new_ip = self.client.discover_and_update_ip()
            if new_ip and new_ip != self.client.ip:
                logger.info("TV rediscovered at new IP %s. Reconnecting...", new_ip)
                url = self.client._get_ws_url()
                self.ws = await websockets.connect(url, ssl=ctx, open_timeout=6.0)
                raw_msg = await asyncio.wait_for(self.ws.recv(), timeout=6.0)
            else:
                raise

        msg = json.loads(raw_msg)
        if msg.get("event") == "ms.channel.unauthorized":
            self.ws = None
            raise TVUnauthorizedError("Access denied by TV.")

        token = msg.get("data", {}).get("token")
        if token and token != self.client.token:
            self.client.token = str(token)
            self.client.config.token = str(token)
            self.client.config.save()
            logger.info("Saved new token: %s", token)

        return self.ws

    async def _send_key_immediate(self, key: str) -> None:
        """
        Dispatches a remote control key command immediately over the persistent WebSocket.

        Args:
            key: Samsung remote key string (e.g. 'KEY_VOLUP', 'KEY_POWER').
        """
        ws = await self._ensure_ws()
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

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """
        Handles an incoming command line from a local UNIX domain socket client.

        Args:
            reader: asyncio StreamReader for reading client requests.
            writer: asyncio StreamWriter for transmitting responses.
        """
        try:
            line = await reader.readline()
            if not line:
                return

            req = json.loads(line.decode("utf-8").strip())
            cmd = req.get("cmd")
            res: Dict[str, Any] = {"status": "ok"}

            async with self._lock:
                if cmd == "ping":
                    res = {"status": "pong"}

                elif cmd == "up":
                    steps = int(req.get("steps", 1))
                    for i in range(steps):
                        await self._send_key_immediate("KEY_VOLUP")
                        if i < steps - 1:
                            await asyncio.sleep(0.18)
                    await asyncio.sleep(0.2)
                    res["volume"] = self.client.get_volume()
                    res["muted"] = self.client.get_mute()

                elif cmd == "down":
                    steps = int(req.get("steps", 1))
                    for i in range(steps):
                        await self._send_key_immediate("KEY_VOLDOWN")
                        if i < steps - 1:
                            await asyncio.sleep(0.18)
                    await asyncio.sleep(0.2)
                    res["volume"] = self.client.get_volume()
                    res["muted"] = self.client.get_mute()

                elif cmd == "set":
                    target = int(req.get("volume", 20))
                    status = self.client.set_volume(target)
                    res["volume"] = status.volume
                    res["muted"] = status.muted

                elif cmd == "mute":
                    status = self.client.mute()
                    res["volume"] = status.volume
                    res["muted"] = status.muted

                elif cmd == "unmute":
                    status = self.client.unmute()
                    res["volume"] = status.volume
                    res["muted"] = status.muted

                elif cmd == "toggle_mute":
                    status = self.client.toggle_mute()
                    res["volume"] = status.volume
                    res["muted"] = status.muted

                elif cmd == "status":
                    status = self.client.get_status()
                    res["status"] = "ok"
                    res["data"] = status.to_dict()

                elif cmd in ("power", "power_toggle"):
                    await self._send_key_immediate("KEY_POWER")
                    res["status"] = "ok"

                elif cmd in ("power_off", "off"):
                    await self._send_key_immediate("KEY_POWER")
                    res["status"] = "ok"

                elif cmd == "launch_app":
                    app = req.get("app")
                    extra = req.get("extra")
                    ok = self.client.launch_app(app, extra)
                    res["status"] = "ok" if ok else "error"

                elif cmd == "close_app":
                    app = req.get("app")
                    ok = self.client.close_app(app)
                    res["status"] = "ok" if ok else "error"

                elif cmd == "stop":
                    res = {"status": "stopping"}
                    self.running = False

                else:
                    res = {"status": "error", "message": f"Unknown command: {cmd}"}

            writer.write(json.dumps(res).encode("utf-8") + b"\n")
            await writer.drain()

            if cmd == "stop":
                asyncio.get_event_loop().call_later(0.1, self._stop_event.set)

        except Exception as e:
            logger.exception("Error handling client request: %s", e)
            try:
                err_resp = {"status": "error", "message": str(e)}
                writer.write(json.dumps(err_resp).encode("utf-8") + b"\n")
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def _keepalive_loop(self) -> None:
        """Periodically pings the TV to keep WebSocket alive."""
        while self.running:
            await asyncio.sleep(20)
            if self.ws and not self.ws.closed:
                try:
                    await self.ws.ping()
                except Exception:
                    logger.debug("Keepalive ping failed, will reconnect on next command.")
                    self.ws = None

    async def run(self) -> None:
        """
        Runs the daemon main loop.

        Creates the UNIX socket server, initiates background pre-connection
        to the TV WebSocket, spawns keepalive heartbeats, and handles graceful shutdown.
        """
        self.running = True
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()

        # Write PID
        with open(self.pid_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))

        # Actively discover TV IP at daemon start-up
        logger.info("Actively discovering Samsung TV IP on local network at daemon startup...")
        discovered_ip = self.client.discover_and_update_ip()
        if discovered_ip:
            logger.info("TV actively verified at IP %s (%s)", discovered_ip, self.client.config.model_name or "Samsung Smart TV")
        else:
            logger.warning(
                "TV not responding to discovery at daemon startup (may be in standby). Using last known IP: %s",
                self.client.ip,
            )

        server = await asyncio.start_unix_server(self.handle_client, path=str(self.socket_path))
        logger.info("Daemon listening on unix socket %s", self.socket_path)

        # Pre-connect to TV in background
        async def try_connect():
            try:
                await self._ensure_ws()
                logger.info("Connected to Samsung TV!")
            except Exception as e:
                logger.warning("Initial TV connection: %s", e)

        asyncio.create_task(try_connect())
        asyncio.create_task(self._keepalive_loop())

        try:
            await self._stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            server.close()
            await server.wait_closed()
            if self.ws and not self.ws.closed:
                await self.ws.close()
            if self.socket_path.exists():
                self.socket_path.unlink()
            if self.pid_path.exists():
                self.pid_path.unlink()
            logger.info("Daemon shut down.")


def start_daemon_background() -> int:
    """
    Spawns the TV daemon process in the background and verifies responsiveness.

    Launches a detached subprocess and polls the UNIX socket until the daemon
    answers ping requests.

    Returns:
        Process ID (PID) of the spawned background daemon process.
    """
    import subprocess
    cmd = [sys.executable, str(Path(__file__).resolve()), "--foreground"]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    
    # Wait up to 3 seconds for daemon socket to become ready
    from samsung_tv import is_daemon_running
    start = time.time()
    while time.time() - start < 3.0:
        if is_daemon_running():
            return p.pid
        time.sleep(0.1)
    return p.pid


def stop_daemon() -> bool:
    """
    Terminates the running daemon process cleanly.

    First attempts graceful shutdown via socket 'stop' command; falls back
    to SIGTERM via the stored PID file if socket communication is unresponsive.

    Returns:
        True if the daemon was found and stopped, False if not running.
    """
    if DEFAULT_SOCKET_FILE.exists():
        try:
            from samsung_tv import send_daemon_command
            res = send_daemon_command({"cmd": "stop"}, timeout=1.5)
            if res.get("status") == "stopping":
                time.sleep(0.3)
                return True
        except Exception:
            pass

    if DEFAULT_PID_FILE.exists():
        try:
            with open(DEFAULT_PID_FILE, "r") as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.3)
            return True
        except Exception:
            pass
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    daemon = TVDaemon()

    def handle_sig(sig, frame):
        logger.info("Received termination signal.")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    try:
        asyncio.run(daemon.run())
    except (KeyboardInterrupt, SystemExit):
        pass

#!/usr/bin/env python3
"""
Integration and regression test suite for Samsung TV client.
"""

import json
import subprocess
import unittest
from pathlib import Path

from samsung_tv import (
    DEFAULT_CONFIG_FILE,
    SamsungTVClient,
    SamsungTVConfig,
    discover_samsung_tv,
    is_daemon_running,
    probe_tv_rest,
)


class TestSamsungTVClient(unittest.TestCase):
    """Integration test suite verifying live connectivity and control against Samsung Smart TV."""

    def setUp(self):
        """Prepares test fixtures and loads saved configuration."""
        self.config = SamsungTVConfig()
        self.assertTrue(self.config.ip, "Config should contain discovered TV IP")
        self.client = SamsungTVClient()

    def test_config_loaded(self):
        """Verifies configuration parameters are successfully retrieved from disk."""
        self.assertEqual(self.config.ip, "192.168.1.2")
        self.assertTrue(self.config.token)
        self.assertEqual(self.config.ws_port, 8002)
        self.assertEqual(self.config.upnp_port, 9197)

    def test_rest_probe(self):
        """Verifies Samsung REST device information endpoint is queryable and valid."""
        info = probe_tv_rest(self.config.ip, timeout=3.0)
        self.assertIsNotNone(info)
        self.assertIn("Samsung", info.name)
        self.assertEqual(info.ip, "192.168.1.2")

    def test_upnp_get_volume_and_mute(self):
        """Verifies UPnP RenderingControl SOAP queries return valid volume and mute values."""
        vol = self.client.get_volume(timeout=3.0)
        self.assertIsNotNone(vol)
        self.assertIsInstance(vol, int)
        self.assertGreaterEqual(vol, 0)
        self.assertLessEqual(vol, 100)

        muted = self.client.get_mute(timeout=3.0)
        self.assertIsNotNone(muted)
        self.assertIsInstance(muted, bool)

    def test_cli_get(self):
        """Verifies CLI 'get' subcommand returns integer volume level."""
        res = subprocess.run(
            ["./samsung-tv-vol", "get"],
            capture_output=True,
            text=True,
            check=True,
        )
        val = int(res.stdout.strip())
        self.assertGreaterEqual(val, 0)
        self.assertLessEqual(val, 100)

    def test_cli_status_json(self):
        """Verifies CLI '--json status' returns valid JSON matching TV status schema."""
        res = subprocess.run(
            ["./samsung-tv-vol", "--json", "status"],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(res.stdout)
        self.assertEqual(data["ip"], "192.168.1.2")
        self.assertIn("volume", data)
        self.assertIn("muted", data)
        self.assertIn("model_name", data)

    def test_daemon_status(self):
        """Verifies background daemon process status and JSON reporting."""
        running = is_daemon_running()
        self.assertTrue(running, "Daemon should be running")

        res = subprocess.run(
            ["./samsung-tv-vol", "--json", "daemon", "status"],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(res.stdout)
        self.assertTrue(data.get("daemon_running"))

    def test_app_list(self):
        """Verifies scanning installed streaming apps on the TV."""
        apps = self.client.list_installed_apps()
        self.assertIsInstance(apps, list)
        self.assertGreater(len(apps), 0)
        app_names = [a["name"] for a in apps]
        self.assertTrue(any("YouTube" in n for n in app_names))

    def test_youtube_launch_and_close(self):
        """Verifies launching YouTube with a video deep link and closing the app."""
        # Launch YouTube with video
        self.client.launch_app("youtube", extra="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        status = self.client.get_app_status("youtube")
        self.assertIsNotNone(status)
        self.assertTrue(status.get("running"))

        # Close YouTube
        closed = self.client.close_app("youtube")
        self.assertTrue(closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)

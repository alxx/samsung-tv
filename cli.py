#!/usr/bin/env python3
"""
Command-line interface for Samsung Smart TV Volume Control.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import termios
import time
import tty
from pathlib import Path
from typing import Any, Dict, Optional

from samsung_tv import (
    DEFAULT_CONFIG_DIR,
    SamsungTVClient,
    SamsungTVConfig,
    SamsungTVError,
    TVInfo,
    TVStatus,
    TVUnauthorizedError,
    TVUnreachableError,
    discover_samsung_tv,
    is_daemon_running,
    send_daemon_command,
)

# Terminal colors
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def supports_color() -> bool:
    """
    Determines whether the standard output terminal supports ANSI color formatting.

    Returns:
        True if stdout is an interactive TTY and TERM is not 'dumb'.
    """
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


def colorize(text: str, color: str) -> str:
    """
    Wraps text with ANSI color escape codes if supported by the terminal.

    Args:
        text: Input string to decorate.
        color: ANSI escape sequence (e.g. CYAN, GREEN, RED).

    Returns:
        Colorized string if supported, otherwise plain text.
    """
    if supports_color():
        return f"{color}{text}{RESET}"
    return text


def format_volume_bar(volume: Optional[int], muted: Optional[bool] = False, width: int = 25) -> str:
    """
    Renders an ASCII/Unicode progress bar visualizing the current volume level and mute badge.

    Args:
        volume: Volume percentage (0-100), or None if unknown.
        muted: Audio mute status (True/False/None).
        width: Character width of the progress bar graph.

    Returns:
        Formatted terminal string showing progress bar and status badge.
    """
    if volume is None:
        return "[Unknown volume]"

    clamped = max(0, min(100, volume))
    filled_len = int(round(width * clamped / 100))
    empty_len = width - filled_len

    fill_char = "█"
    empty_char = "░"

    bar = filled_char_color = colorize(fill_char * filled_len, CYAN if not muted else DIM)
    bar += colorize(empty_char * empty_len, DIM)

    mute_badge = ""
    if muted:
        mute_badge = " " + colorize("[MUTED]", RED + BOLD)
    elif muted is False:
        mute_badge = " " + colorize("[UNMUTED]", GREEN)

    return f"[{bar}] {colorize(f'{clamped}%', BOLD)}{mute_badge}"


def print_status_table(status: TVStatus) -> None:
    """
    Prints a formatted summary table of TV device information and audio state.

    Args:
        status: Populated TVStatus object.
    """
    print(colorize("Samsung Smart TV Status", BOLD))
    print(colorize("─" * 40, DIM))
    print(f" {colorize('Device:', BOLD)}     {status.name} ({status.model_name})")
    print(f" {colorize('IP Address:', BOLD)} {status.ip}")
    print(f" {colorize('Power:', BOLD)}      {status.power_state}")
    vol_str = f"{status.volume}%" if status.volume is not None else "Unknown"
    mute_str = "Yes" if status.muted else ("No" if status.muted is False else "Unknown")
    print(f" {colorize('Muted:', BOLD)}      {mute_str}")
    print(f" {colorize('Volume:', BOLD)}     {format_volume_bar(status.volume, status.muted)}")
    print(colorize("─" * 40, DIM))


def get_client(args: argparse.Namespace) -> SamsungTVClient:
    """
    Constructs a SamsungTVClient instance from CLI argument overrides.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Configured SamsungTVClient instance.
    """
    return SamsungTVClient(
        ip=args.ip,
        token=args.token,
    )


def try_daemon_command(args: argparse.Namespace, cmd_dict: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Attempts to execute a command through the fast background daemon if active.

    Bypasses the daemon if --direct is passed or if the daemon is stopped.

    Args:
        args: Parsed command-line arguments containing flags like direct.
        cmd_dict: Command dictionary to dispatch to the daemon.

    Returns:
        Response dictionary from the daemon if successful, else None.
    """
    if not args.direct and is_daemon_running():
        try:
            res = send_daemon_command(cmd_dict, timeout=4.0)
            if res.get("status") == "ok":
                return res
        except Exception:
            pass
    return None


def run_interactive(client: SamsungTVClient) -> None:
    """
    Runs a live interactive keyboard session for adjusting TV volume.

    Captures raw keyboard events to provide instant feedback without requiring
    the Enter key. Supports arrow keys, vim keys (j/k), numeric keys (0-9 for 0-90%),
    mute toggling (m), refresh (r), and quit (q / Ctrl+C).

    Args:
        client: Active SamsungTVClient instance.
    """
    print(colorize("Samsung TV Interactive Volume Control", BOLD))
    print(colorize("Keys: [↑/k/+] Vol Up | [↓/j/-] Vol Down | [m] Mute/Unmute | [0-9] Set 0-90% | [q] Quit", DIM))
    print()

    # Read initial status
    try:
        status = client.get_status()
    except Exception as e:
        print(f"Failed to get TV status: {e}")
        return

    vol = status.volume or 20
    muted = status.muted or False

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    def draw():
        sys.stdout.write(f"\r  {format_volume_bar(vol, muted)}   ")
        sys.stdout.flush()

    draw()

    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\x03", "q", "Q"):  # Ctrl+C or q
                break
            elif ch == "\x1b":  # Escape sequence
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    ch3 = sys.stdin.read(1)
                    if ch3 == "A":  # Up arrow
                        client.volume_up(1)
                        vol = min(100, vol + 1)
                        draw()
                    elif ch3 == "B":  # Down arrow
                        client.volume_down(1)
                        vol = max(0, vol - 1)
                        draw()
            elif ch in ("k", "+", "="):
                client.volume_up(1)
                vol = min(100, vol + 1)
                draw()
            elif ch in ("j", "-"):
                client.volume_down(1)
                vol = max(0, vol - 1)
                draw()
            elif ch in ("m", "M"):
                client.toggle_mute()
                muted = not muted
                draw()
            elif ch in "0123456789":
                target = int(ch) * 10
                client.set_volume(target)
                vol = target
                draw()
            elif ch == "r":  # Refresh
                s = client.get_status()
                vol = s.volume or vol
                muted = s.muted or muted
                draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        print("\nExited interactive mode.")


def main() -> int:
    """
    Main CLI entry point for samsung-tv-vol.

    Parses command-line arguments, configures logging, routes subcommands
    (volume, mute, on/wake, off, app, youtube, daemon, discover, pair),
    and formats visual or machine-readable JSON output.

    Returns:
        Exit code: 0 on success, non-zero on error.
    """
    parser = argparse.ArgumentParser(
        prog="samsung-tv-vol",
        description="Command-line volume controller for Samsung Smart TVs on your local network.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  %(prog)s status               # Display TV status and volume bar
  %(prog)s up                   # Volume up by 1
  %(prog)s up 5                 # Volume up by 5
  %(prog)s down 3               # Volume down by 3
  %(prog)s set 25               # Set volume to exact value (25%%)
  %(prog)s mute / unmute        # Mute / unmute audio
  %(prog)s interactive          # Live keyboard interactive mode (arrows, m, q)
  %(prog)s on / wake            # Wake TV from standby via Wake-on-LAN
  %(prog)s off                  # Turn TV off into standby
  %(prog)s app youtube          # Launch YouTube on the TV
  %(prog)s app youtube "URL"    # Launch YouTube and directly play video
  %(prog)s youtube "URL"        # Shortcut: play YouTube video on the TV app
  %(prog)s app netflix          # Launch Netflix (or prime, spotify, twitch)
  %(prog)s app list             # List verified streaming apps installed on TV
  %(prog)s daemon start         # Start background daemon for ultra-low latency (<10ms)
""",
    )

    parser.add_argument("--ip", help="Samsung TV IP address (defaults to discovered/saved IP)")
    parser.add_argument("--token", help="WebSocket auth token (defaults to saved token)")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    parser.add_argument("--direct", action="store_true", help="Bypass background daemon even if running")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress non-essential messages")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")

    subparsers = parser.add_subparsers(dest="subcommand", help="Action to perform")

    # status
    p_status = subparsers.add_parser("status", help="Show TV status and volume")

    # get
    p_get = subparsers.add_parser("get", help="Print numeric volume level only")

    # up
    p_up = subparsers.add_parser("up", help="Increase volume")
    p_up.add_argument("steps", nargs="?", type=int, default=1, help="Number of volume steps (default: 1)")

    # down
    p_down = subparsers.add_parser("down", help="Decrease volume")
    p_down.add_argument("steps", nargs="?", type=int, default=1, help="Number of volume steps (default: 1)")

    # set
    p_set = subparsers.add_parser("set", help="Set volume to exact percentage (0-100)")
    p_set.add_argument("volume", type=int, help="Target volume level (0-100)")

    # mute
    subparsers.add_parser("mute", help="Mute TV audio")

    # unmute
    subparsers.add_parser("unmute", help="Unmute TV audio")

    # toggle-mute
    subparsers.add_parser("toggle-mute", help="Toggle mute state")

    # interactive / -i
    subparsers.add_parser("interactive", aliases=["-i"], help="Interactive keyboard control mode")

    # discover
    subparsers.add_parser("discover", help="Scan local network for Samsung TVs")

    # pair
    subparsers.add_parser("pair", help="Initiate pairing handshake with TV and save token")

    # power
    p_power = subparsers.add_parser("power", help="Toggle TV power")

    # wake / on
    p_wake = subparsers.add_parser("wake", aliases=["on"], help="Send Wake-on-LAN magic packet to TV")
    p_wake.add_argument("-w", "--wait", action="store_true", help="Wait and verify TV powers on and responds")

    # off / power-off
    subparsers.add_parser("off", aliases=["power-off"], help="Power off the TV into standby")

    # app
    p_app = subparsers.add_parser("app", help="Launch or manage TV apps (youtube, netflix, prime, etc.)")
    p_app.add_argument("name", help="App name (youtube, netflix, prime, spotify, twitch, etc.) or 'list'")
    p_app.add_argument("extra", nargs="?", default=None, help="Optional supplemental video link for YouTube, or 'close'")

    # youtube shortcut
    p_yt = subparsers.add_parser("youtube", help="Launch YouTube on TV, optionally playing a video directly")
    p_yt.add_argument("link", nargs="?", default=None, help="Optional YouTube link or video ID to play directly")

    # daemon
    p_daemon = subparsers.add_parser("daemon", help="Manage fast background daemon")
    p_daemon.add_argument(
        "action",
        choices=["start", "stop", "restart", "status"],
        help="Daemon action (start, stop, restart, status)",
    )

    args = parser.parse_args()

    # Logging setup
    log_level = logging.DEBUG if args.verbose else (logging.WARNING if args.quiet else logging.INFO)
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")

    cmd = args.subcommand or "status"

    # Daemon management command
    if cmd == "daemon":
        from daemon import start_daemon_background, stop_daemon

        action = args.action
        if action == "status":
            running = is_daemon_running()
            if args.json:
                print(json.dumps({"daemon_running": running}))
            else:
                state_str = colorize("RUNNING", GREEN + BOLD) if running else colorize("STOPPED", RED)
                print(f"Samsung TV Daemon status: {state_str}")
            return 0 if running else 1

        elif action == "stop":
            stopped = stop_daemon()
            if stopped:
                print(colorize("Daemon stopped.", GREEN))
            else:
                print("Daemon was not running.")
            return 0

        elif action == "start":
            if is_daemon_running():
                print("Daemon is already running.")
                return 0
            pid = start_daemon_background()
            time.sleep(0.5)
            if is_daemon_running():
                print(colorize(f"Daemon started successfully (PID {pid}).", GREEN))
                return 0
            else:
                print(colorize("Failed to start daemon in background.", RED))
                return 1

        elif action == "restart":
            stop_daemon()
            time.sleep(0.5)
            pid = start_daemon_background()
            time.sleep(0.5)
            print(colorize(f"Daemon restarted (PID {pid}).", GREEN))
            return 0

    # Discover command
    if cmd == "discover":
        if not args.quiet and not args.json:
            print("Scanning local network for Samsung TVs...")
        tvs = discover_samsung_tv(timeout=2.5)
        if args.json:
            print(json.dumps([tv.to_dict() for tv in tvs], indent=2))
        else:
            if not tvs:
                print(colorize("No Samsung TVs discovered.", YELLOW))
            else:
                print(colorize(f"Found {len(tvs)} Samsung TV(s):", BOLD))
                for tv in tvs:
                    print(f"  • {colorize(tv.name, BOLD)} ({tv.model_name}) at {colorize(tv.ip, CYAN)} [MAC: {tv.mac}]")
        return 0

    # Initialize client
    client = get_client(args)

    # Actively discover and verify TV IP at CLI script startup when running without daemon
    # or when using direct client commands, unless an explicit --ip was supplied.
    if not args.ip and (args.direct or not is_daemon_running() or cmd not in ("up", "down", "set", "mute", "unmute", "toggle-mute")):
        if cmd not in ("discover", "daemon"):
            try:
                client.discover_and_update_ip()
            except Exception as e:
                logging.debug("Startup active TV discovery: %s", e)

    # Wake-on-LAN command
    if cmd in ("wake", "on"):
        try:
            wait = getattr(args, "wait", False)
            if not args.quiet and not args.json:
                print(colorize("Sending Wake-on-LAN signal (staggered bursts)...", CYAN))
            online = client.wake_on_lan(
                repeat_bursts=4,
                burst_delay=0.6,
                wait_for_online=wait,
            )
            if args.json:
                print(json.dumps({"status": "wake_signal_sent", "online": online}))
            else:
                if wait:
                    if online:
                        print(colorize("✓ TV is now powered on and responsive!", GREEN + BOLD))
                    else:
                        print(colorize("Wake packets sent (TV did not respond within timeout).", YELLOW))
                else:
                    if not args.quiet:
                        print(colorize("✓ Wake-on-LAN bursts sent successfully.", GREEN))
            return 0
        except Exception as e:
            if args.json:
                print(json.dumps({"error": str(e)}))
            else:
                print(colorize(f"Error sending Wake-on-LAN: {e}", RED), file=sys.stderr)
            return 1

    # Power Off command
    if cmd in ("off", "power-off"):
        try:
            if not args.quiet and not args.json:
                print(colorize("Turning off Samsung TV...", CYAN))
            daemon_res = try_daemon_command(args, {"cmd": "off"})
            if not daemon_res:
                client.power_off()
            if args.json:
                print(json.dumps({"power": "off"}))
            else:
                if not args.quiet:
                    print(colorize("✓ TV turned off (standby).", GREEN))
            return 0
        except Exception as e:
            if args.json:
                print(json.dumps({"error": str(e)}))
            else:
                print(colorize(f"Error turning off TV: {e}", RED), file=sys.stderr)
            return 1

    # App & YouTube commands
    app_launch_name: Optional[str] = None
    app_launch_extra: Optional[str] = None

    if cmd == "youtube":
        app_launch_name = "youtube"
        app_launch_extra = getattr(args, "link", None)

    elif cmd == "app":
        target = args.name.lower()
        if target == "list":
            apps = client.list_installed_apps()
            if args.json:
                print(json.dumps(apps, indent=2))
            else:
                print(colorize("Verified Streaming Apps on TV:", BOLD))
                for a in apps:
                    run_badge = colorize("[RUNNING]", GREEN) if a["running"] else colorize("[INSTALLED]", DIM)
                    vis_badge = " " + colorize("(visible)", CYAN) if a["visible"] else ""
                    print(f"  • {colorize(a['name'], BOLD)} (ID: {a['app_id']}) {run_badge}{vis_badge}")
            return 0
        elif target == "close":
            app_to_close = args.extra
            if not app_to_close:
                print(colorize("Error: specify app to close (e.g. 'samsung-tv-vol app close youtube')", RED), file=sys.stderr)
                return 1
            client.close_app(app_to_close)
            if not args.quiet:
                print(colorize(f"✓ Closed {app_to_close}.", GREEN))
            return 0
        else:
            app_launch_name = args.name
            app_launch_extra = args.extra

    if app_launch_name is not None:
        try:
            resolved_id = client.resolve_app_id(app_launch_name)
            is_yt = resolved_id == "111299001912" or "youtube" in app_launch_name.lower()
            if not args.quiet and not args.json:
                if is_yt and app_launch_extra:
                    print(colorize(f"Launching YouTube on TV playing: {app_launch_extra}...", CYAN))
                else:
                    print(colorize(f"Launching {app_launch_name} on TV...", CYAN))

            client.launch_app(app_launch_name, extra=app_launch_extra)

            if args.json:
                res = {"app": app_launch_name, "app_id": resolved_id, "launched": True}
                if is_yt and app_launch_extra:
                    res["video"] = app_launch_extra
                print(json.dumps(res))
            else:
                if not args.quiet:
                    if is_yt and app_launch_extra:
                        print(colorize("✓ YouTube is now playing video on TV!", GREEN + BOLD))
                    else:
                        print(colorize(f"✓ Launched {app_launch_name} on TV!", GREEN))
            return 0
        except Exception as e:
            if args.json:
                print(json.dumps({"error": str(e)}))
            else:
                print(colorize(f"Error launching {app_launch_name}: {e}", RED), file=sys.stderr)
            return 1

    # Check daemon for fast command execution
    if cmd in ("up", "down", "set", "mute", "unmute", "toggle-mute"):
        daemon_cmd: Dict[str, Any] = {"cmd": cmd.replace("-", "_")}
        if cmd in ("up", "down"):
            daemon_cmd["steps"] = args.steps
        elif cmd == "set":
            daemon_cmd["volume"] = args.volume

        daemon_res = try_daemon_command(args, daemon_cmd)
        if daemon_res:
            vol = daemon_res.get("volume")
            muted = daemon_res.get("muted")
            if args.json:
                print(json.dumps({"volume": vol, "muted": muted}))
            else:
                print(format_volume_bar(vol, muted))
            return 0

    # Direct client execution
    try:
        if cmd == "status":
            status = client.get_status()
            if args.json:
                print(json.dumps(status.to_dict(), indent=2))
            else:
                print_status_table(status)
            return 0

        elif cmd == "get":
            vol = client.get_volume()
            if vol is not None:
                print(vol)
                return 0
            else:
                print("Unknown", file=sys.stderr)
                return 1

        elif cmd == "up":
            status = client.volume_up(args.steps)
            if args.json:
                print(json.dumps({"volume": status.volume, "muted": status.muted}))
            else:
                print(format_volume_bar(status.volume, status.muted))
            return 0

        elif cmd == "down":
            status = client.volume_down(args.steps)
            if args.json:
                print(json.dumps({"volume": status.volume, "muted": status.muted}))
            else:
                print(format_volume_bar(status.volume, status.muted))
            return 0

        elif cmd == "set":
            status = client.set_volume(args.volume)
            if args.json:
                print(json.dumps({"volume": status.volume, "muted": status.muted}))
            else:
                print(format_volume_bar(status.volume, status.muted))
            return 0

        elif cmd == "mute":
            status = client.mute()
            if args.json:
                print(json.dumps({"volume": status.volume, "muted": status.muted}))
            else:
                print(format_volume_bar(status.volume, status.muted))
            return 0

        elif cmd == "unmute":
            status = client.unmute()
            if args.json:
                print(json.dumps({"volume": status.volume, "muted": status.muted}))
            else:
                print(format_volume_bar(status.volume, status.muted))
            return 0

        elif cmd == "toggle-mute":
            status = client.toggle_mute()
            if args.json:
                print(json.dumps({"volume": status.volume, "muted": status.muted}))
            else:
                print(format_volume_bar(status.volume, status.muted))
            return 0

        elif cmd in ("interactive", "-i"):
            run_interactive(client)
            return 0

        elif cmd == "pair":
            print(colorize("Testing connection and pairing with TV...", CYAN))
            status = client.get_status()
            client.send_keys([])  # triggers WS connection & handshake
            print(colorize(f"Successfully paired with {status.name}!", GREEN))
            print(f"Token saved to: {client.config.config_path}")
            return 0

        elif cmd == "power":
            client.power_toggle()
            print(colorize("Toggled power.", GREEN))
            return 0

    except TVUnauthorizedError as e:
        print(colorize(f"Authentication Error: {e}", RED), file=sys.stderr)
        print("Please check the TV screen for an authorization prompt and run 'pair' again.", file=sys.stderr)
        return 2
    except TVUnreachableError as e:
        print(colorize(f"Connection Error: {e}", RED), file=sys.stderr)
        print("Check that the TV is powered on and connected to the same LAN.", file=sys.stderr)
        return 3
    except Exception as e:
        print(colorize(f"Error: {e}", RED), file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

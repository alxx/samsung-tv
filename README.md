# Samsung Smart TV Volume Control CLI

A lightweight, zero-latency command-line volume controller for modern Samsung Smart TVs (Tizen OS 2016+) on your local network.

---

## Features

- **Active Dynamic IP Discovery**: Actively verifies and discovers the TV's IP at every start-up of the daemon (or CLI when running without a daemon). Handles dynamic DHCP reassignments automatically without router admin access via kernel ARP lookups (~40ms) and multi-threaded subnet sweeps (~400ms).
- **Automatic TV Discovery**: Discovers Samsung TVs on the local LAN using SSDP M-SEARCH, ARP tables, and REST verification.
- **Accurate Volume & Mute Sensing**: Queries UPnP `RenderingControl` for exact current volume (0-100) and mute status without guesswork.
- **Closed-Loop Absolute Volume**: `samsung-tv-vol set <N>` adjusts volume to exact target values.
- **Relative Volume Control**: Fast increment/decrement (`up [N]`, `down [N]`).
- **Mute Controls**: `mute`, `unmute`, and `toggle-mute`.
- **Interactive TUI Mode**: `samsung-tv-vol interactive` (live arrow keys `↑`/`↓` and `m` with instant visual volume bar).
- **Fast Background Daemon**: Keeps a persistent WebSocket connection open to the TV for instant response times (<10ms).
- **Machine-Readable Output**: `--json` output option for status bars (Polybar, Waybar, i3blocks) and shell automation.
- **Wake-on-LAN**: Turn on or wake the TV from standby via `samsung-tv-vol wake` / `on`.
- **Global Command**: Pre-linked to `~/.local/bin/samsung-tv-vol`.

---

## Discovered Device

- **Device Name**: `[TV] Samsung Q70 Series (65)`
- **Model**: `QE65Q77TATXXC` (`20_NIKEM_QTV`)
- **IP Address**: `192.168.1.2`
- **Authentication**: Token-authenticated via secure WebSocket (`wss://192.168.1.2:8002`)
- **Configuration**: Saved in `~/.config/samsung-tv/config.json`

---

## Quick Reference

| Command | Description |
|---|---|
| `samsung-tv-vol` / `samsung-tv-vol status` | Show TV status and visual volume bar |
| `samsung-tv-vol up [N]` | Increase volume by N steps (default: 1) |
| `samsung-tv-vol down [N]` | Decrease volume by N steps (default: 1) |
| `samsung-tv-vol set <0-100>` | Set volume to an exact target percentage |
| `samsung-tv-vol get` | Output raw numeric volume (e.g. `20`) |
| `samsung-tv-vol mute` | Mute TV sound |
| `samsung-tv-vol unmute` | Unmute TV sound |
| `samsung-tv-vol toggle-mute` | Toggle mute state |
| `samsung-tv-vol interactive` (or `-i`) | Interactive keyboard volume slider |
| `samsung-tv-vol wake` (or `on`) | Send Wake-on-LAN magic packet (use `-w` to wait until TV is online) |
| `samsung-tv-vol off` | Turn off TV into standby |
| `samsung-tv-vol app <name>` | Launch TV app (e.g. `youtube`, `netflix`, `prime`, `spotify`, `twitch`) |
| `samsung-tv-vol app <name> <link>` | Launch YouTube and play a specific video directly on the TV app |
| `samsung-tv-vol youtube [link]` | Shortcut to launch YouTube or directly play video URL or video ID |
| `samsung-tv-vol app list` | List verified streaming apps installed on the TV |
| `samsung-tv-vol app close <name>` | Close an active TV app |
| `samsung-tv-vol power` | Toggle power button |
| `samsung-tv-vol discover` | Scan local network for Samsung TVs |
| `samsung-tv-vol daemon start` | Start ultra-fast background connection daemon |
| `samsung-tv-vol daemon stop` | Stop background daemon |
| `samsung-tv-vol daemon status` | Check if background daemon is active |

---

## Examples

### 1. Check Volume and Status
```bash
$ samsung-tv-vol
Samsung Smart TV Status
────────────────────────────────────────
 Device:     [TV] Samsung Q70 Series (65) (QE65Q77TATXXC)
 IP Address: 192.168.1.2
 Power:      on
 Muted:      No
 Volume:     [█████░░░░░░░░░░░░░░░░░░░░] 20% [UNMUTED]
────────────────────────────────────────
```

### 2. Adjust Volume
```bash
# Volume up by 1
samsung-tv-vol up

# Volume up by 5
samsung-tv-vol up 5

# Volume down by 3
samsung-tv-vol down 3

# Set volume directly to 25%
samsung-tv-vol set 25
```

### 3. Mute & Unmute
```bash
samsung-tv-vol mute
samsung-tv-vol unmute
samsung-tv-vol toggle-mute
```

### 4. Interactive Mode
Run `samsung-tv-vol -i` to adjust volume interactively using keyboard keys:
- **`↑` / `k` / `+`**: Volume Up
- **`↓` / `j` / `-`**: Volume Down
- **`m`**: Toggle Mute
- **`0` - `9`**: Jump directly to 0%, 10%, 20%, ... 90%
- **`q` / `Esc`**: Exit

### 5. Power On / Off
```bash
# Wake TV from standby (staggered multi-burst)
samsung-tv-vol on
samsung-tv-vol on -w          # Wait until TV responds

# Turn TV off into standby
samsung-tv-vol off
```

### 6. Launch Apps & Play YouTube Videos
```bash
# List installed streaming apps and their status
samsung-tv-vol app list

# Launch an app
samsung-tv-vol app netflix
samsung-tv-vol app prime
samsung-tv-vol app spotify

# Launch YouTube
samsung-tv-vol youtube
# or:
samsung-tv-vol app youtube

# Play a specific YouTube video directly in the TV app:
samsung-tv-vol youtube "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
# or using video ID:
samsung-tv-vol youtube dQw4w9WgXcQ

# Close an app
samsung-tv-vol app close youtube
```

---

## Ultra-Low Latency Daemon

For normal CLI usage, the client can execute directly or communicate with a lightweight background daemon via a local UNIX socket (`~/.config/samsung-tv/daemon.sock`).

When the daemon is running, the WebSocket connection to the TV remains open, reducing command latency to under 10ms.

```bash
# Start the daemon
samsung-tv-vol daemon start

# Check status
samsung-tv-vol daemon status

# Stop daemon
samsung-tv-vol daemon stop
```

*(If the daemon is not running, all commands seamlessly fallback to direct WebSocket execution).*

---

## Desktop Hotkey Integration

You can bind your laptop's audio keys or custom shortcuts to control the Samsung TV:

### GNOME / Ubuntu Desktop:
1. Open **Settings** -> **Keyboard** -> **Keyboard Shortcuts** -> **Custom Shortcuts**.
2. Add new shortcuts:
   - **Name**: TV Volume Up | **Command**: `samsung-tv-vol up` | **Shortcut**: `Ctrl+Super+Up`
   - **Name**: TV Volume Down | **Command**: `samsung-tv-vol down` | **Shortcut**: `Ctrl+Super+Down`
   - **Name**: TV Mute Toggle | **Command**: `samsung-tv-vol toggle-mute` | **Shortcut**: `Ctrl+Super+M`

### i3 / Sway (`~/.config/i3/config` or `~/.config/sway/config`):
```i3config
bindsym $mod+bracketright exec samsung-tv-vol up 2
bindsym $mod+bracketleft  exec samsung-tv-vol down 2
bindsym $mod+backslash    exec samsung-tv-vol toggle-mute
```

---

## Architecture & Technical Protocol

Samsung TVs run Tizen OS and expose multiple network interfaces:
1. **REST API (Port 8001)**: Provides device identity, model name, power state, and supported capabilities.
2. **WebSocket Remote API (Port 8002 / WSS)**: Handles encrypted remote control key injection (`KEY_VOLUP`, `KEY_VOLDOWN`, `KEY_MUTE`, `KEY_POWER`) protected by a token-based authentication handshake.
3. **UPnP RenderingControl (Port 9197)**: Provides real-time `GetVolume` (0-100) and `GetMute` (0/1) state via standard SOAP requests.

# Raspberry Pi Zero 2 W Camera Web Application

This project is a high-performance web application built with **FastAPI** to stream video and capture images from a Raspberry Pi Zero 2 W using the official `picamera2` library.

## Features
- **Daily Photo Gallery (`/`)**: Hero shot of the latest capture plus a thumbnail grid of today's photos.
- **Sunrise-to-Sunset Time-Lapse**: Automatic high-resolution captures every N seconds during daylight, computed each day for your location with [`suntime`](https://github.com/SatAgro/suntime).
- **Real-time MJPEG Streaming (`/live`)**: Low-latency video feed accessible via any web browser.
- **Dynamic Resolution Switching**: Change stream resolution (SD, HD, FHD) on-the-fly without restarting the server.
- **On-demand Capture**: Save the current frame as a JPEG download from the live UI.
- **Health Endpoint**: `/health` exposes camera status (running, FPS, resolution, error counters) for monitoring tools.
- **Prometheus Metrics**: `/metrics` exposes counters and gauges in Prometheus exposition format for scraping.
- **Asynchronous Backend**: Leverages FastAPI and Uvicorn for efficient handling of concurrent stream requests.

## Prerequisites
- **Hardware**: Raspberry Pi Zero 2 W (or newer) and a compatible Raspberry Pi Camera Module.
- **OS**: Raspberry Pi OS (Bullseye or Bookworm) with the `libcamera` stack enabled.
- **Python**: The system interpreter shipped by your OS (3.11 on Bookworm, 3.9 on Bullseye). No virtualenv is used — we run against system Python so the apt-installed `picamera2` is importable.

## Installation

This project runs against the **system Python interpreter** (no virtualenv, no `uv`) so the apt-packaged `picamera2` is importable. `picamera2` is tightly coupled to the system `libcamera` stack and does not install reliably from PyPI on the Pi.

1. **Install System Dependencies via apt**:
   ```bash
   sudo apt update
   sudo apt install python3-picamera2 python3-fastapi python3-uvicorn \
                    python3-pydantic python3-jinja2
   ```

   > **Note for Bullseye users**: `python3-fastapi` and `python3-uvicorn` are only packaged on **Bookworm**. On Bullseye, install just `python3-picamera2` and `python3-jinja2` via apt, then use the pip fallback below for the rest.

2. **Install pip-only deps** (`suntime` is not packaged in apt; on Python <3.11, also need the `tomli` shim for TOML parsing):
   ```bash
   sudo pip3 install --break-system-packages suntime
   # Bullseye / Python <3.11 only:
   # sudo pip3 install --break-system-packages tomli
   ```

3. **Fallback for any missing package** (recent Raspberry Pi OS releases enforce PEP 668; the flag below opts into a system-wide pip install anyway):
   ```bash
   sudo pip3 install --break-system-packages fastapi uvicorn pydantic jinja2
   ```

4. **Configure your location**: copy the example config and edit your latitude/longitude:
   ```bash
   cp deploy/config.toml.example config.toml
   $EDITOR config.toml   # set [location].latitude and [location].longitude
   ```

## Running the Application

Start the server by running `app.py` with the system Python:
```bash
python3 app.py
```

- `http://<your-pi-ip>:5000/` &mdash; **Today's photos**: hero (latest capture) + thumbnail gallery.
- `http://<your-pi-ip>:5000/live` &mdash; **Live stream** with on-demand capture button.

While the server is running, a background scheduler captures a still photo at the camera's **sensor maximum resolution** every `interval_seconds` (default 5 minutes) **between sunrise and sunset** for the configured location. Photos are saved under `captures/YYYY-MM-DD/HHMMSS.jpg`.

## Configuration

Runtime behavior is controlled by **two configuration sources**:

1. **`config.toml`** &mdash; static, structured settings (location, capture cadence, photo directory). Recommended path: `<project>/config.toml`. Override the path with the `WEBCAM_CONFIG` env var.
2. **Environment variables** &mdash; server-side runtime knobs (host, port, stream FPS, default stream resolution).

Both fall back to safe defaults on missing/invalid/out-of-range values, with a warning logged so misconfiguration is visible.

### `config.toml` schema

```toml
[location]
latitude  = 46.5197      # WGS84 decimal degrees, range [-90, 90]
longitude = 6.6323       # WGS84 decimal degrees, range [-180, 180]
name      = "Home"      # optional, displayed in the UI

[capture]
interval_seconds = 300   # range [5, 86400] (5s → 24h). Default 300 (5 min).
directory        = "captures"   # absolute or relative to the project root.
```

A fully-commented template lives at [`deploy/config.toml.example`](deploy/config.toml.example). The `install.sh` script seeds it for you on first run.

### Environment variables

| Variable | Default | Range | Purpose |
|----------|---------|-------|---------|
| `WEBCAM_FPS` | `15` | `1`–`60` | Caps the MJPEG stream frame rate. Lower values reduce CPU usage on the Pi Zero 2 W. |
| `WEBCAM_HOST` | `0.0.0.0` | non-empty string | Network interface uvicorn binds to. Use `127.0.0.1` to restrict to localhost. |
| `WEBCAM_PORT` | `5000` | `1`–`65535` | TCP port uvicorn listens on. |
| `WEBCAM_DEFAULT_RESOLUTION` | `640x480` | `WxH`, each `16`–`4096` | Initial **stream** resolution at startup (can be changed at runtime via the UI). Scheduled captures always use the sensor's full resolution. |
| `WEBCAM_CONFIG` | `<project>/config.toml` | path | Override the path to `config.toml`. |

Example:
```bash
WEBCAM_FPS=10 WEBCAM_PORT=8080 WEBCAM_DEFAULT_RESOLUTION=1280x720 python3 app.py
```

## Photo Gallery

The home page (`/`) shows:
- The **latest** capture as a hero image.
- A **thumbnail grid** of all of today's captures (newest first), with click-to-zoom.
- Today's **sunrise / sunset** times and the configured capture interval.
- A status chip showing whether the scheduler is currently capturing (daytime) or paused (night).

### API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/photos` | List today's captures. Query param `?date=YYYY-MM-DD` for a specific day. |
| `GET /api/dates` | List all dates that have at least one capture. |
| `GET /api/sun` | Today's sunrise/sunset (UTC), `is_daytime` flag, and `next_sunrise`. |
| `GET /photos/<date>/<file>` | Static-served captured JPEG (mounted on the capture directory). |

## Health Check

`GET /health` returns a JSON snapshot suitable for liveness/readiness probes. Example:

```json
{
  "status": "ok",
  "running": true,
  "fps": {
    "configured": 15,
    "measured": 14.0,
    "window_seconds": 1.0
  },
  "resolution": { "width": 640, "height": 480 },
  "consecutive_errors": 0,
  "total_frames": 1234,
  "last_error": null,
  "last_frame_at": 1779613000.12,
  "started_at": 1779612900.45,
  "uptime_seconds": 42.7
}
```

- `status`: `"ok"`, `"degraded"` (transient errors but recovering), or `"error"` (camera stopped or ≥50 consecutive failures).
- `fps.configured` is the cap from `WEBCAM_FPS`; `fps.measured` is the rolling-average FPS over the last `window_seconds` (1s) computed from successful frame timestamps. A measured value well below the cap typically means clients aren't pulling frames fast enough or the camera is throttled.
- HTTP **200** when healthy or degraded; HTTP **503** when in `error` state for easy alerting.

## Metrics

`GET /metrics` returns Prometheus exposition format (`text/plain; version=0.0.4`) with the following series:

| Metric | Type | Description |
|--------|------|-------------|
| `webcam_up` | gauge | `1` if the camera is running, `0` otherwise. |
| `webcam_frames_total` | counter | Successfully streamed frames since startup. |
| `webcam_consecutive_errors` | gauge | Consecutive frame-capture errors (resets on success). |
| `webcam_uptime_seconds` | gauge | Seconds since the application started (monotonic). |
| `webcam_measured_fps` | gauge | Rolling-average FPS over the last second. |
| `webcam_configured_fps` | gauge | Configured FPS cap (`WEBCAM_FPS`). |
| `webcam_resolution_width_pixels` | gauge | Current camera frame width. |
| `webcam_resolution_height_pixels` | gauge | Current camera frame height. |

Example Prometheus scrape config:
```yaml
scrape_configs:
  - job_name: webcam
    static_configs:
      - targets: ['<pi-ip>:5000']
```

## Run on Boot (systemd)

A systemd unit template ships in [`deploy/webcam.service`](deploy/webcam.service). It runs the app with the **system Python interpreter** so the apt-installed `picamera2` is available.

### Quick install (recommended)

The [`deploy/install.sh`](deploy/install.sh) script automates everything below — apt deps, pip fallback, rendering the unit file, seeding `webcam.env`, and enabling/starting the service. It's idempotent, so re-run it after updates.

```bash
git clone <repo-url> ~/webcam
cd ~/webcam
sudo ./deploy/install.sh
```

Useful flags:

```bash
sudo ./deploy/install.sh --no-start        # install but don't start yet
sudo ./deploy/install.sh --no-enable       # start, but don't autostart on boot
sudo ./deploy/install.sh --service-user pi # run service as a specific user
sudo ./deploy/install.sh --uninstall       # stop, disable, remove unit
```

After it finishes you'll see the bound URL, status, and log commands. Edit `webcam.env` (seeded from the example, all values commented out) and `sudo systemctl restart webcam` to apply changes.

### Manual install

Prefer to do it by hand? The steps below mirror what `install.sh` automates.

1. **Place the project somewhere stable**, e.g. `/home/pi/webcam`. Replace `<USER>` and `<INSTALL_DIR>` below with your values.

2. **Install dependencies system-wide** (see the [Installation](#installation) section above).

3. **Find the system Python path** — you'll paste it as `<PYTHON_BIN>`:
   ```bash
   which python3   # commonly /usr/bin/python3
   ```

4. **(Optional) Configure runtime env vars**:
   ```bash
   cp deploy/webcam.env.example <INSTALL_DIR>/webcam.env
   # edit webcam.env to override WEBCAM_FPS, WEBCAM_PORT, etc.
   ```

5. **Install the unit file and edit it in place** — copy first, then replace the placeholders in the installed copy so future `git pull`s don't clobber your edits:
   ```bash
   sudo cp deploy/webcam.service /etc/systemd/system/webcam.service
   sudo $EDITOR /etc/systemd/system/webcam.service
   # replace <USER>, <INSTALL_DIR>, <PYTHON_BIN> placeholders
   ```

6. **Enable and start the service**:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now webcam.service
   ```

   If startup fails with a camera permission error on Bookworm, also add `render` to `SupplementaryGroups=` in the unit file (some libcamera/v4l2 device nodes are owned by that group).

### Operational commands

```bash
sudo systemctl status webcam      # check status
sudo journalctl -u webcam -f      # follow logs
sudo systemctl restart webcam     # restart after pulling new code
sudo systemctl disable --now webcam   # stop and disable on boot
```

## Project Structure
- `app.py`: FastAPI application, camera control, capture scheduler.
- `templates/index.html`: Home page — today's photo gallery.
- `templates/live.html`: Live stream UI (served at `/live`).
- `config.toml` (created from the example): location & capture settings.
- `captures/YYYY-MM-DD/HHMMSS.jpg`: scheduled captures, organized by day.
- `deploy/install.sh`: automated installer (apt + pip + systemd) for the Pi.
- `deploy/webcam.service`: systemd unit template for running on boot.
- `deploy/webcam.env.example`: example environment file for the unit.
- `deploy/config.toml.example`: example TOML config (location/capture).
- `LICENSE`: MIT License.

## License

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

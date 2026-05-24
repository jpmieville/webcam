# Raspberry Pi Zero 2 W Camera Web Application

This project is a high-performance web application built with **FastAPI** to stream video and capture images from a Raspberry Pi Zero 2 W using the official `picamera2` library.

## Features
- **Real-time MJPEG Streaming**: Low-latency video feed accessible via any web browser.
- **Dynamic Resolution Switching**: Change camera resolution (SD, HD, FHD) on-the-fly without restarting the server.
- **Image Capture**: Capture high-quality JPEG images and download them directly from the interface.
- **Health Endpoint**: `/health` exposes camera status (running, FPS, resolution, error counters) for monitoring tools.
- **Prometheus Metrics**: `/metrics` exposes counters and gauges in Prometheus exposition format for scraping.
- **Asynchronous Backend**: Leverages FastAPI and Uvicorn for efficient handling of concurrent stream requests.

## Prerequisites
- **Hardware**: Raspberry Pi Zero 2 W (or newer) and a compatible Raspberry Pi Camera Module.
- **OS**: Raspberry Pi OS (Bullseye or Bookworm) with the `libcamera` stack enabled.
- **Python**: Version 3.12 (as specified in `.python-version`).

## Installation

1. **Install System Dependencies**:
   Ensure the Pi camera library is installed on your system:
   ```bash
   sudo apt update
   sudo apt install python3-picamera2
   ```

2. **Install Python Packages**:
   ```bash
   pip install fastapi uvicorn pydantic jinja2
   ```

## Running the Application

Start the server by running `app.py`:
```bash
python app.py
```
The application will be available at `http://<your-pi-ip>:5000`.

## Configuration

The following environment variables can be set to tune runtime behavior. All settings fall back to safe defaults on missing/invalid/out-of-range values, with a warning logged so misconfiguration is visible.

| Variable | Default | Range | Purpose |
|----------|---------|-------|---------|
| `WEBCAM_FPS` | `15` | `1`–`60` | Caps the MJPEG stream frame rate. Lower values reduce CPU usage on the Pi Zero 2 W. |
| `WEBCAM_HOST` | `0.0.0.0` | non-empty string | Network interface uvicorn binds to. Use `127.0.0.1` to restrict to localhost. |
| `WEBCAM_PORT` | `5000` | `1`–`65535` | TCP port uvicorn listens on. |
| `WEBCAM_DEFAULT_RESOLUTION` | `640x480` | `WxH`, each `16`–`4096` | Initial camera resolution at startup (can still be changed at runtime via the UI). |

Example:
```bash
WEBCAM_FPS=10 WEBCAM_PORT=8080 WEBCAM_DEFAULT_RESOLUTION=1280x720 python app.py
```

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

## Project Structure
- `app.py`: The main FastAPI application logic and camera control.
- `templates/index.html`: The frontend user interface.

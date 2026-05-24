import io
import logging
import os
import threading
import time
from collections import deque
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, Response, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from picamera2 import Picamera2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("webcam")

# ---------------------------------------------------------------------------
# Configuration via environment variables
# ---------------------------------------------------------------------------
# All settings fall back to safe defaults on missing/invalid values and log a
# warning so misconfiguration is visible.

# Streaming frame rate cap (frames/sec). Keep modest on Pi Zero 2 W to avoid
# pegging the CPU; the MJPEG generator will sleep for the remainder of each
# frame interval after a capture.
_DEFAULT_FPS = 15
_MIN_FPS = 1
_MAX_FPS = 60

_DEFAULT_HOST = "0.0.0.0"

_DEFAULT_PORT = 5000
_MIN_PORT = 1
_MAX_PORT = 65535

_DEFAULT_RESOLUTION = (640, 480)
_MIN_DIMENSION = 16
_MAX_DIMENSION = 4096


def _resolve_int_env(
    name: str, default: int, min_value: int, max_value: int
) -> int:
    """Read an int env var with bounds checking; fall back to default on error."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r (not an integer); using default %d.",
            name, raw, default,
        )
        return default
    if value < min_value or value > max_value:
        logger.warning(
            "%s=%d out of range [%d, %d]; using default %d.",
            name, value, min_value, max_value, default,
        )
        return default
    return value


def _resolve_stream_fps() -> int:
    return _resolve_int_env("WEBCAM_FPS", _DEFAULT_FPS, _MIN_FPS, _MAX_FPS)


def _resolve_host() -> str:
    raw = os.environ.get("WEBCAM_HOST")
    if raw is None:
        return _DEFAULT_HOST
    value = raw.strip()
    if not value:
        logger.warning(
            "Invalid WEBCAM_HOST=%r (empty); using default %r.",
            raw, _DEFAULT_HOST,
        )
        return _DEFAULT_HOST
    return value


def _resolve_port() -> int:
    return _resolve_int_env("WEBCAM_PORT", _DEFAULT_PORT, _MIN_PORT, _MAX_PORT)


def _resolve_default_resolution() -> tuple[int, int]:
    raw = os.environ.get("WEBCAM_DEFAULT_RESOLUTION")
    if raw is None or raw.strip() == "":
        return _DEFAULT_RESOLUTION
    parts = raw.strip().lower().split("x")
    if len(parts) != 2:
        logger.warning(
            "Invalid WEBCAM_DEFAULT_RESOLUTION=%r (expected WxH); using default %dx%d.",
            raw, *_DEFAULT_RESOLUTION,
        )
        return _DEFAULT_RESOLUTION
    try:
        width, height = int(parts[0]), int(parts[1])
    except ValueError:
        logger.warning(
            "Invalid WEBCAM_DEFAULT_RESOLUTION=%r (non-integer dimensions); using default %dx%d.",
            raw, *_DEFAULT_RESOLUTION,
        )
        return _DEFAULT_RESOLUTION
    if not (_MIN_DIMENSION <= width <= _MAX_DIMENSION and _MIN_DIMENSION <= height <= _MAX_DIMENSION):
        logger.warning(
            "WEBCAM_DEFAULT_RESOLUTION=%dx%d outside per-dimension range [%d, %d]; using default %dx%d.",
            width, height, _MIN_DIMENSION, _MAX_DIMENSION, *_DEFAULT_RESOLUTION,
        )
        return _DEFAULT_RESOLUTION
    return (width, height)


STREAM_FPS = _resolve_stream_fps()
_FRAME_INTERVAL = 1.0 / STREAM_FPS
HOST = _resolve_host()
PORT = _resolve_port()
DEFAULT_RESOLUTION = _resolve_default_resolution()

# After this many consecutive capture failures, log a warning so a
# permanently broken camera is visible instead of silently retrying forever.
_ERROR_LOG_THRESHOLD = 50

logger.info(
    "Config: host=%s port=%d fps=%d default_resolution=%dx%d (frame interval %.3fs).",
    HOST, PORT, STREAM_FPS, DEFAULT_RESOLUTION[0], DEFAULT_RESOLUTION[1], _FRAME_INTERVAL,
)

app = FastAPI()
templates = Jinja2Templates(directory="templates")


class CameraStatus:
    """Thread-safe shared camera status snapshot for /health."""

    # Rolling window (seconds) used to compute measured FPS from a deque of
    # recent successful-frame monotonic timestamps.
    _FPS_WINDOW_SECONDS = 1.0

    def __init__(self, resolution: tuple[int, int]) -> None:
        self._lock = threading.Lock()
        self.running: bool = False
        self.resolution: tuple[int, int] = resolution
        self.consecutive_errors: int = 0
        self.total_frames: int = 0
        self.last_error: str | None = None
        self.last_error_at: float | None = None
        self.last_frame_at: float | None = None
        # Wall-clock start (for human-readable timestamps in snapshots) and
        # a monotonic anchor (for uptime, immune to NTP jumps).
        self.started_at: float = time.time()
        self._started_monotonic: float = time.monotonic()
        # Monotonic timestamps of frames within the rolling FPS window.
        self._frame_times: deque[float] = deque()

    def mark_running(self, resolution: tuple[int, int]) -> None:
        with self._lock:
            self.running = True
            self.resolution = resolution

    def mark_stopped(self) -> None:
        with self._lock:
            self.running = False

    def record_frame(self) -> int:
        """Record a successful frame; return the prior consecutive_errors count."""
        now_mono = time.monotonic()
        with self._lock:
            prior = self.consecutive_errors
            self.total_frames += 1
            self.consecutive_errors = 0
            self.last_frame_at = time.time()
            self._frame_times.append(now_mono)
            cutoff = now_mono - self._FPS_WINDOW_SECONDS
            while self._frame_times and self._frame_times[0] < cutoff:
                self._frame_times.popleft()
            return prior

    def measured_fps(self) -> float:
        """Frames per second over the rolling window (last `_FPS_WINDOW_SECONDS`).

        Note: prune-on-read is intentional — if no new frames arrive (stalled
        stream), `record_frame` won't run, so this method must drop stale
        entries so the reported FPS correctly drops to 0.
        """
        now_mono = time.monotonic()
        cutoff = now_mono - self._FPS_WINDOW_SECONDS
        with self._lock:
            while self._frame_times and self._frame_times[0] < cutoff:
                self._frame_times.popleft()
            count = len(self._frame_times)
        return round(count / self._FPS_WINDOW_SECONDS, 1)

    def record_error(self, exc: BaseException) -> int:
        with self._lock:
            self.consecutive_errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.last_error_at = time.time()
            return self.consecutive_errors

    def uptime_seconds(self) -> float:
        return time.monotonic() - self._started_monotonic

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "resolution": {
                    "width": self.resolution[0],
                    "height": self.resolution[1],
                },
                "consecutive_errors": self.consecutive_errors,
                "total_frames": self.total_frames,
                "last_error": self.last_error,
                "last_error_at": self.last_error_at,
                "last_frame_at": self.last_frame_at,
                "started_at": self.started_at,
            }


camera_status = CameraStatus(DEFAULT_RESOLUTION)

# Initialize Camera
picam2 = Picamera2()
config = picam2.create_video_configuration(main={"size": DEFAULT_RESOLUTION})
picam2.configure(config)
picam2.start()
camera_status.mark_running(DEFAULT_RESOLUTION)

# Serializes camera access between the streaming generator,
# resolution changes, and capture requests.
camera_lock = threading.Lock()

class Resolution(BaseModel):
    width: int
    height: int

def generate_frames():
    while True:
        frame_start = time.monotonic()
        try:
            buf = io.BytesIO()
            with camera_lock:
                picam2.capture_file(buf, format="jpeg")
            prior_errors = camera_status.record_frame()
            if prior_errors:
                logger.info(
                    "Camera recovered after %d consecutive errors.",
                    prior_errors,
                )
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + buf.getvalue() + b'\r\n')
        except Exception as exc:
            consecutive_errors = camera_status.record_error(exc)
            # Log the first failure and then periodically so a permanently
            # broken camera is visible without flooding the logs.
            if consecutive_errors == 1:
                logger.warning("Frame capture failed: %s", exc)
            elif consecutive_errors % _ERROR_LOG_THRESHOLD == 0:
                logger.error(
                    "Frame capture has failed %d times in a row (last: %s).",
                    consecutive_errors,
                    exc,
                )
            time.sleep(0.1)
            continue

        # Cap FPS: sleep the remainder of the frame interval if we finished
        # early. If capture took longer than the interval, don't sleep.
        elapsed = time.monotonic() - frame_start
        remaining = _FRAME_INTERVAL - elapsed
        if remaining > 0:
            time.sleep(remaining)

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/video_feed")
async def video_feed():
    return StreamingResponse(
        generate_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )

@app.post("/change_resolution")
async def change_resolution(res: Resolution):
    try:
        with camera_lock:
            picam2.stop()
            camera_status.mark_stopped()
            new_config = picam2.create_video_configuration(main={"size": (res.width, res.height)})
            picam2.configure(new_config)
            picam2.start()
            camera_status.mark_running((res.width, res.height))
        return {"status": "success", "resolution": f"{res.width}x{res.height}"}
    except Exception as e:
        # Surface resolution-change failures via the same /health error
        # signal as frame-capture errors so monitoring can detect either.
        camera_status.record_error(e)
        raise HTTPException(status_code=500, detail=str(e))


_PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _render_prometheus_metrics() -> str:
    """Render the current camera state as Prometheus exposition format (v0.0.4)."""
    snap = camera_status.snapshot()
    measured = camera_status.measured_fps()
    uptime = camera_status.uptime_seconds()
    width, height = snap["resolution"]["width"], snap["resolution"]["height"]
    running = 1 if snap["running"] else 0

    # Each metric: HELP, TYPE, then a single sample line. Names follow
    # Prometheus conventions (snake_case, _total for counters, _seconds for
    # durations). f-string formatting yields locale-independent decimal output.
    lines = [
        "# HELP webcam_up 1 if the camera is running, 0 otherwise.",
        "# TYPE webcam_up gauge",
        f"webcam_up {running}",
        "# HELP webcam_frames_total Total number of successfully streamed frames since startup.",
        "# TYPE webcam_frames_total counter",
        f"webcam_frames_total {snap['total_frames']}",
        "# HELP webcam_consecutive_errors Consecutive frame-capture errors (resets on success).",
        "# TYPE webcam_consecutive_errors gauge",
        f"webcam_consecutive_errors {snap['consecutive_errors']}",
        "# HELP webcam_uptime_seconds Seconds since the application started (monotonic).",
        "# TYPE webcam_uptime_seconds gauge",
        f"webcam_uptime_seconds {uptime}",
        "# HELP webcam_measured_fps Frames per second over the rolling window.",
        "# TYPE webcam_measured_fps gauge",
        f"webcam_measured_fps {measured}",
        "# HELP webcam_configured_fps Configured maximum frames per second (WEBCAM_FPS).",
        "# TYPE webcam_configured_fps gauge",
        f"webcam_configured_fps {STREAM_FPS}",
        "# HELP webcam_resolution_width_pixels Current camera frame width in pixels.",
        "# TYPE webcam_resolution_width_pixels gauge",
        f"webcam_resolution_width_pixels {width}",
        "# HELP webcam_resolution_height_pixels Current camera frame height in pixels.",
        "# TYPE webcam_resolution_height_pixels gauge",
        f"webcam_resolution_height_pixels {height}",
    ]
    # Prometheus exposition format requires a trailing newline.
    return "\n".join(lines) + "\n"


@app.get("/metrics")
async def metrics():
    """Prometheus-compatible metrics endpoint."""
    return PlainTextResponse(
        content=_render_prometheus_metrics(),
        media_type=_PROMETHEUS_CONTENT_TYPE,
    )


@app.get("/health")
async def health():
    """Report camera status for monitoring tools.

    Returns HTTP 200 for healthy/degraded states and HTTP 503 when the
    camera is not running or has crossed the error threshold.
    """
    snap = camera_status.snapshot()
    consecutive = snap["consecutive_errors"]
    if not snap["running"]:
        status_label = "error"
    elif consecutive >= _ERROR_LOG_THRESHOLD:
        status_label = "error"
    elif consecutive > 0:
        status_label = "degraded"
    else:
        status_label = "ok"

    body = {
        "status": status_label,
        "running": snap["running"],
        "fps": {
            "configured": STREAM_FPS,
            "measured": camera_status.measured_fps(),
            "window_seconds": camera_status._FPS_WINDOW_SECONDS,
        },
        "resolution": snap["resolution"],
        "consecutive_errors": consecutive,
        "total_frames": snap["total_frames"],
        "last_error": (
            None
            if snap["last_error"] is None
            else {
                "message": snap["last_error"],
                "at": snap["last_error_at"],
            }
        ),
        "last_frame_at": snap["last_frame_at"],
        "started_at": snap["started_at"],
        "uptime_seconds": camera_status.uptime_seconds(),
    }
    http_status = 503 if status_label == "error" else 200
    return JSONResponse(content=body, status_code=http_status)

@app.get("/capture")
async def capture():
    buf = io.BytesIO()
    with camera_lock:
        picam2.capture_file(buf, format="jpeg")
    timestamp = int(time.time())
    filename = f"capture_{timestamp}.jpg"
    return Response(
        content=buf.getvalue(),
        media_type="image/jpeg",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)

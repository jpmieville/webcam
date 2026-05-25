import io
import logging
import os
import re
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from picamera2 import Picamera2
from suntime import Sun

# tomllib is stdlib on Python 3.11+; fall back to the `tomli` shim for older
# interpreters (e.g. Raspberry Pi OS Bullseye ships Python 3.9).
try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("webcam")

# Project root (where app.py lives). Used to resolve relative paths from the
# config file (capture directory, etc.) against a stable location regardless
# of the working directory the service is launched from.
PROJECT_ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Configuration via environment variables (server-side runtime tuning)
# ---------------------------------------------------------------------------
# All settings fall back to safe defaults on missing/invalid values and log a
# warning so misconfiguration is visible.

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


# ---------------------------------------------------------------------------
# TOML config (location + capture scheduling)
# ---------------------------------------------------------------------------

# Defaults used when config.toml is missing or a field is invalid.
_DEFAULT_LATITUDE = 46.5197      # Lausanne, CH
_DEFAULT_LONGITUDE = 6.6323
_DEFAULT_LOCATION_NAME: str | None = None
_DEFAULT_CAPTURE_INTERVAL = 300  # 5 minutes
_MIN_CAPTURE_INTERVAL = 5
_MAX_CAPTURE_INTERVAL = 86400
_DEFAULT_CAPTURE_DIRECTORY = "captures"


class AppConfig:
    """Resolved values from config.toml (with safe defaults)."""

    def __init__(
        self,
        latitude: float,
        longitude: float,
        location_name: str | None,
        capture_interval: int,
        capture_directory: Path,
        source: Path | None,
    ) -> None:
        self.latitude = latitude
        self.longitude = longitude
        self.location_name = location_name
        self.capture_interval = capture_interval
        self.capture_directory = capture_directory
        self.source = source


def _resolve_config_path() -> Path:
    """Resolve which config.toml file to load.

    Order of precedence:
      1. WEBCAM_CONFIG environment variable (absolute or relative to project root).
      2. <PROJECT_ROOT>/config.toml.
    """
    raw = os.environ.get("WEBCAM_CONFIG")
    if raw and raw.strip():
        path = Path(raw.strip()).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path
    return PROJECT_ROOT / "config.toml"


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning(
            "Failed to read %s (%s); falling back to defaults.", path, exc,
        )
        return {}


def _coerce_float(section: dict[str, Any], section_name: str, key: str,
                  default: float, *, source: str, lo: float, hi: float) -> float:
    raw = section.get(key)
    if raw is None:
        return default
    # `bool` is a subclass of `int` in Python, so we explicitly reject it here
    # to avoid silently coercing `latitude = true` into `1.0`.
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        logger.warning(
            "%s.%s=%r (expected number) in %s; using default %s.",
            section_name, key, raw, source, default,
        )
        return default
    value = float(raw)
    if not (lo <= value <= hi):
        logger.warning(
            "%s.%s=%s out of range [%s, %s] in %s; using default %s.",
            section_name, key, value, lo, hi, source, default,
        )
        return default
    return value


def _load_app_config() -> AppConfig:
    path = _resolve_config_path()
    data = _load_toml(path)
    source_str = str(path) if path.exists() else "<defaults>"

    location = data.get("location") or {}
    capture = data.get("capture") or {}

    latitude = _coerce_float(
        location, "location", "latitude",
        _DEFAULT_LATITUDE, source=source_str, lo=-90.0, hi=90.0,
    )
    longitude = _coerce_float(
        location, "location", "longitude",
        _DEFAULT_LONGITUDE, source=source_str, lo=-180.0, hi=180.0,
    )

    name_raw = location.get("name")
    location_name: str | None
    if name_raw is None:
        location_name = _DEFAULT_LOCATION_NAME
    elif isinstance(name_raw, str) and name_raw.strip():
        location_name = name_raw.strip()
    else:
        logger.warning(
            "location.name=%r (expected non-empty string) in %s; ignoring.",
            name_raw, source_str,
        )
        location_name = _DEFAULT_LOCATION_NAME

    interval_raw = capture.get("interval_seconds", _DEFAULT_CAPTURE_INTERVAL)
    if not isinstance(interval_raw, int) or isinstance(interval_raw, bool):
        logger.warning(
            "capture.interval_seconds=%r (expected integer) in %s; using default %d.",
            interval_raw, source_str, _DEFAULT_CAPTURE_INTERVAL,
        )
        capture_interval = _DEFAULT_CAPTURE_INTERVAL
    elif not (_MIN_CAPTURE_INTERVAL <= interval_raw <= _MAX_CAPTURE_INTERVAL):
        logger.warning(
            "capture.interval_seconds=%d out of range [%d, %d] in %s; using default %d.",
            interval_raw, _MIN_CAPTURE_INTERVAL, _MAX_CAPTURE_INTERVAL,
            source_str, _DEFAULT_CAPTURE_INTERVAL,
        )
        capture_interval = _DEFAULT_CAPTURE_INTERVAL
    else:
        capture_interval = interval_raw

    dir_raw = capture.get("directory", _DEFAULT_CAPTURE_DIRECTORY)
    if not isinstance(dir_raw, str) or not dir_raw.strip():
        logger.warning(
            "capture.directory=%r (expected non-empty string) in %s; using default %r.",
            dir_raw, source_str, _DEFAULT_CAPTURE_DIRECTORY,
        )
        dir_value = _DEFAULT_CAPTURE_DIRECTORY
    else:
        dir_value = dir_raw.strip()

    capture_directory = Path(dir_value).expanduser()
    if not capture_directory.is_absolute():
        capture_directory = (PROJECT_ROOT / capture_directory).resolve()

    return AppConfig(
        latitude=latitude,
        longitude=longitude,
        location_name=location_name,
        capture_interval=capture_interval,
        capture_directory=capture_directory,
        source=path if path.exists() else None,
    )


STREAM_FPS = _resolve_stream_fps()
_FRAME_INTERVAL = 1.0 / STREAM_FPS
HOST = _resolve_host()
PORT = _resolve_port()
DEFAULT_RESOLUTION = _resolve_default_resolution()
APP_CONFIG = _load_app_config()

# After this many consecutive capture failures, log a warning so a
# permanently broken camera is visible instead of silently retrying forever.
_ERROR_LOG_THRESHOLD = 50

logger.info(
    "Server config: host=%s port=%d fps=%d default_resolution=%dx%d (frame interval %.3fs).",
    HOST, PORT, STREAM_FPS, DEFAULT_RESOLUTION[0], DEFAULT_RESOLUTION[1], _FRAME_INTERVAL,
)
logger.info(
    "App config: location=(%.4f, %.4f)%s capture_interval=%ds dir=%s source=%s",
    APP_CONFIG.latitude,
    APP_CONFIG.longitude,
    f" name={APP_CONFIG.location_name!r}" if APP_CONFIG.location_name else "",
    APP_CONFIG.capture_interval,
    APP_CONFIG.capture_directory,
    APP_CONFIG.source if APP_CONFIG.source else "<defaults>",
)

# Ensure the capture directory exists up-front so the scheduler can write
# without per-loop mkdir noise. Failures here are logged but not fatal —
# the scheduler will retry the mkdir on each capture.
try:
    APP_CONFIG.capture_directory.mkdir(parents=True, exist_ok=True)
except OSError as exc:
    logger.warning(
        "Could not create capture directory %s: %s",
        APP_CONFIG.capture_directory, exc,
    )


app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Serve captured photos as static files. We mount it on a stable URL prefix
# so the templates can build links without going through Python on each hit.
app.mount(
    "/photos",
    StaticFiles(directory=str(APP_CONFIG.capture_directory), check_dir=False),
    name="photos",
)


# ---------------------------------------------------------------------------
# Camera status + lock
# ---------------------------------------------------------------------------

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
        self.started_at: float = time.time()
        self._started_monotonic: float = time.monotonic()
        self._frame_times: deque[float] = deque()

    def mark_running(self, resolution: tuple[int, int]) -> None:
        with self._lock:
            self.running = True
            self.resolution = resolution

    def mark_stopped(self) -> None:
        with self._lock:
            self.running = False

    def record_frame(self) -> int:
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

# Sensor's full resolution — used for scheduled captures. Read once after the
# camera is initialized so we don't have to hit the driver each time.
SENSOR_RESOLUTION: tuple[int, int] = tuple(picam2.sensor_resolution)  # type: ignore[assignment]
logger.info("Sensor resolution: %dx%d.", SENSOR_RESOLUTION[0], SENSOR_RESOLUTION[1])

# Serializes camera access between the streaming generator,
# resolution changes, scheduled captures, and on-demand captures.
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

        elapsed = time.monotonic() - frame_start
        remaining = _FRAME_INTERVAL - elapsed
        if remaining > 0:
            time.sleep(remaining)


# ---------------------------------------------------------------------------
# Scheduled high-resolution capture (sunrise → sunset)
# ---------------------------------------------------------------------------

_sun = Sun(APP_CONFIG.latitude, APP_CONFIG.longitude)
_shutdown_event = threading.Event()


def _today_sun_window(today: date) -> tuple[datetime, datetime]:
    """Return today's (sunrise, sunset) as UTC-aware datetimes."""
    sunrise = _sun.get_sunrise_time(today)
    sunset = _sun.get_sunset_time(today)
    # suntime returns UTC-aware datetimes; normalize defensively in case a
    # future version changes that contract.
    if sunrise.tzinfo is None:
        sunrise = sunrise.replace(tzinfo=timezone.utc)
    if sunset.tzinfo is None:
        sunset = sunset.replace(tzinfo=timezone.utc)
    return sunrise, sunset


def _capture_to_disk_at_sensor_resolution() -> Path:
    """Capture a still at the sensor's max resolution and save to <captures>/YYYY-MM-DD/HHMMSS.jpg.

    Uses Picamera2's `switch_mode_and_capture_file` which transparently swaps
    to a still configuration, captures, and switches back to the active video
    config — so the live MJPEG stream resumes automatically after each shot.
    """
    now = datetime.now()  # local time for filename clarity
    day_dir = APP_CONFIG.capture_directory / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    filepath = day_dir / now.strftime("%H%M%S.jpg")

    still_config = picam2.create_still_configuration(main={"size": SENSOR_RESOLUTION})
    with camera_lock:
        # The streaming generator will see brief failures during the swap and
        # retry — that's fine. We don't toggle camera_status.running here since
        # the camera resumes streaming on the way out.
        picam2.switch_mode_and_capture_file(still_config, str(filepath))
    return filepath


def capture_scheduler_loop() -> None:
    """Background thread: capture at sensor max during daylight on a fixed cadence."""
    interval = APP_CONFIG.capture_interval
    last_logged_window: date | None = None

    logger.info(
        "Capture scheduler started: every %ds during daylight at %dx%d.",
        interval, SENSOR_RESOLUTION[0], SENSOR_RESOLUTION[1],
    )

    while not _shutdown_event.is_set():
        now_utc = datetime.now(timezone.utc)
        try:
            sunrise, sunset = _today_sun_window(now_utc.date())
        except Exception as exc:
            logger.error("Failed to compute sunrise/sunset: %s", exc)
            # Try again next cycle.
            if _shutdown_event.wait(interval):
                break
            continue

        if last_logged_window != now_utc.date():
            logger.info(
                "Today's sun window (UTC): %s → %s.",
                sunrise.isoformat(timespec="seconds"),
                sunset.isoformat(timespec="seconds"),
            )
            last_logged_window = now_utc.date()

        if sunrise <= now_utc <= sunset:
            try:
                path = _capture_to_disk_at_sensor_resolution()
                logger.info("Captured %s", path.relative_to(PROJECT_ROOT) if path.is_relative_to(PROJECT_ROOT) else path)
            except Exception as exc:
                # Surface in /health via the same error counter as the stream.
                camera_status.record_error(exc)
                logger.error("Scheduled capture failed: %s", exc)
        else:
            logger.debug(
                "Outside sun window (now=%s, sunrise=%s, sunset=%s); skipping capture.",
                now_utc.isoformat(timespec="seconds"),
                sunrise.isoformat(timespec="seconds"),
                sunset.isoformat(timespec="seconds"),
            )

        # Sleep `interval` seconds, but wake immediately if shutdown is requested.
        if _shutdown_event.wait(interval):
            break

    logger.info("Capture scheduler stopped.")


_scheduler_thread = threading.Thread(
    target=capture_scheduler_loop,
    name="capture-scheduler",
    daemon=True,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _scheduler_thread.start()
    try:
        yield
    finally:
        _shutdown_event.set()
        # Best-effort wait so an in-flight capture can finish before the
        # process exits; bounded so a stuck capture can't block shutdown.
        _scheduler_thread.join(timeout=APP_CONFIG.capture_interval + 5)


app.router.lifespan_context = lifespan


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def index(request: Request):
    """Home: hero (latest capture) + today's gallery."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/live")
async def live(request: Request):
    """Live MJPEG stream UI (formerly served at /)."""
    return templates.TemplateResponse("live.html", {"request": request})


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
        camera_status.record_error(e)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Photo gallery API
# ---------------------------------------------------------------------------

# Captures are saved as <captures>/YYYY-MM-DD/HHMMSS.jpg. We restrict listing
# to that exact pattern so misplaced files (or path-traversal attempts via a
# crafted ?date=) don't leak into responses.
_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PHOTO_FILE_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})\.jpg$")


def _list_photos_for_date(day: date) -> list[dict[str, Any]]:
    day_dir_name = day.strftime("%Y-%m-%d")
    if not _DATE_DIR_RE.match(day_dir_name):
        return []
    day_dir = APP_CONFIG.capture_directory / day_dir_name
    if not day_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(day_dir.iterdir()):
        if not entry.is_file():
            continue
        m = _PHOTO_FILE_RE.match(entry.name)
        if not m:
            continue
        hh, mm, ss = (int(p) for p in m.groups())
        try:
            taken_at = datetime(day.year, day.month, day.day, hh, mm, ss)
        except ValueError:
            continue
        try:
            size = entry.stat().st_size
        except OSError:
            size = None
        out.append({
            "filename": entry.name,
            "date": day_dir_name,
            "taken_at": taken_at.isoformat(timespec="seconds"),
            "url": f"/photos/{day_dir_name}/{entry.name}",
            "size_bytes": size,
        })
    return out


def _list_capture_dates() -> list[str]:
    if not APP_CONFIG.capture_directory.is_dir():
        return []
    return sorted(
        d.name for d in APP_CONFIG.capture_directory.iterdir()
        if d.is_dir() and _DATE_DIR_RE.match(d.name)
    )


@app.get("/api/photos")
async def api_photos(date: str | None = None):
    """List captures for a given date (default: today, local time).

    Photos are returned in chronological order (oldest first). The caller can
    take the last entry to display the most recent capture.
    """
    if date is None:
        target = datetime.now().date()
    else:
        if not _DATE_DIR_RE.match(date):
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
        try:
            target = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid date")
    photos = _list_photos_for_date(target)
    return {
        "date": target.isoformat(),
        "count": len(photos),
        "photos": photos,
    }


@app.get("/api/dates")
async def api_dates():
    """List all dates that have at least one capture directory."""
    return {"dates": _list_capture_dates()}


@app.get("/api/sun")
async def api_sun():
    """Return today's sunrise/sunset and whether we're currently in daylight."""
    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()
    try:
        sunrise, sunset = _today_sun_window(today)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"sun computation failed: {exc}")

    is_daytime = sunrise <= now_utc <= sunset
    if now_utc < sunrise:
        next_sunrise = sunrise
    else:
        # Already past today's sunrise — next sunrise is tomorrow's.
        tomorrow = today + timedelta(days=1)
        try:
            next_sunrise, _ = _today_sun_window(tomorrow)
        except Exception:
            next_sunrise = sunrise  # fall back to today's

    return {
        "now": now_utc.isoformat(timespec="seconds"),
        "sunrise": sunrise.isoformat(timespec="seconds"),
        "sunset": sunset.isoformat(timespec="seconds"),
        "is_daytime": is_daytime,
        "next_sunrise": next_sunrise.isoformat(timespec="seconds"),
        "interval_seconds": APP_CONFIG.capture_interval,
        "location": {
            "latitude": APP_CONFIG.latitude,
            "longitude": APP_CONFIG.longitude,
            "name": APP_CONFIG.location_name,
        },
    }


# ---------------------------------------------------------------------------
# Health & metrics
# ---------------------------------------------------------------------------

_PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _render_prometheus_metrics() -> str:
    """Render the current camera state as Prometheus exposition format (v0.0.4)."""
    snap = camera_status.snapshot()
    measured = camera_status.measured_fps()
    uptime = camera_status.uptime_seconds()
    width, height = snap["resolution"]["width"], snap["resolution"]["height"]
    running = 1 if snap["running"] else 0

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
    """On-demand capture: returns a JPEG download at the current stream resolution."""
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

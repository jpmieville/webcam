#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Raspberry Pi Webcam — automated installer.
#
# Run this from the project root after cloning the repo to the target Pi:
#
#     sudo ./deploy/install.sh                    # install + enable + start
#     sudo ./deploy/install.sh --uninstall        # stop + disable + remove unit
#     sudo ./deploy/install.sh --no-start         # install but don't start
#     sudo ./deploy/install.sh --no-enable        # install + start, no autostart
#     sudo ./deploy/install.sh --service-user pi  # service runs as 'pi' (default: invoking user)
#
# The script is idempotent: re-running it will refresh the unit file and
# restart the service.
# -----------------------------------------------------------------------------
set -euo pipefail

SERVICE_NAME="webcam"
UNIT_DEST="/etc/systemd/system/${SERVICE_NAME}.service"

# ---- option parsing ---------------------------------------------------------
DO_ENABLE=1
DO_START=1
DO_UNINSTALL=0
SERVICE_USER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-enable)   DO_ENABLE=0; shift ;;
        --no-start)    DO_START=0; shift ;;
        --uninstall)   DO_UNINSTALL=1; shift ;;
        --service-user)
            [[ $# -ge 2 ]] || { echo "error: --service-user needs a value" >&2; exit 2; }
            SERVICE_USER="$2"; shift 2 ;;
        -h|--help)
            cat <<'EOF'
Raspberry Pi Webcam — automated installer.

Usage:
  sudo ./deploy/install.sh                    # install + enable + start
  sudo ./deploy/install.sh --uninstall        # stop + disable + remove unit
  sudo ./deploy/install.sh --no-start         # install but don't start
  sudo ./deploy/install.sh --no-enable        # install + start, no autostart
  sudo ./deploy/install.sh --service-user pi  # service runs as 'pi' (default: invoking user)
EOF
            exit 0 ;;
        *) echo "error: unknown argument: $1" >&2; exit 2 ;;
    esac
done

# ---- helpers ----------------------------------------------------------------
log()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[install] %s\033[0m\n' "$*" >&2; exit 1; }

require_root() {
    [[ ${EUID:-$(id -u)} -eq 0 ]] || die "must be run as root (try: sudo $0)"
}

# ---- uninstall path ---------------------------------------------------------
if [[ "$DO_UNINSTALL" -eq 1 ]]; then
    require_root
    log "Stopping ${SERVICE_NAME}.service (if running)..."
    systemctl stop  "${SERVICE_NAME}.service" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}.service" 2>/dev/null || true
    if [[ -f "$UNIT_DEST" ]]; then
        log "Removing $UNIT_DEST"
        rm -f "$UNIT_DEST"
    fi
    systemctl daemon-reload
    log "Uninstalled. (Project files and webcam.env left untouched.)"
    exit 0
fi

# ---- install path -----------------------------------------------------------
require_root

# Resolve project root (the directory containing this script's parent).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
UNIT_TEMPLATE="$SCRIPT_DIR/webcam.service"
ENV_TEMPLATE="$SCRIPT_DIR/webcam.env.example"
CONFIG_TEMPLATE="$SCRIPT_DIR/config.toml.example"

[[ -f "$UNIT_TEMPLATE" ]] || die "unit template not found at $UNIT_TEMPLATE"
[[ -f "$INSTALL_DIR/app.py" ]] || die "app.py not found at $INSTALL_DIR (is the project layout intact?)"

# Decide which user the service should run as.
# Default: the user who invoked sudo (SUDO_USER), falling back to 'pi'.
if [[ -z "$SERVICE_USER" ]]; then
    SERVICE_USER="${SUDO_USER:-}"
    [[ -n "$SERVICE_USER" && "$SERVICE_USER" != "root" ]] || SERVICE_USER="pi"
fi
id -u "$SERVICE_USER" >/dev/null 2>&1 || die "user '$SERVICE_USER' does not exist"

# Resolve absolute path to system Python 3.
PYTHON_BIN="$(command -v python3 || true)"
[[ -n "$PYTHON_BIN" ]] || die "python3 not found on PATH"

log "Project dir : $INSTALL_DIR"
log "Service user: $SERVICE_USER"
log "Python      : $PYTHON_BIN"

# ---- 1. apt dependencies ----------------------------------------------------
APT_PKGS=(python3-picamera2 python3-jinja2)

# python3-fastapi / python3-uvicorn / python3-pydantic exist on Bookworm but
# not Bullseye. Probe apt-cache and only add the ones that are available.
for opt in python3-fastapi python3-uvicorn python3-pydantic; do
    if apt-cache show "$opt" >/dev/null 2>&1; then
        APT_PKGS+=("$opt")
    fi
done

log "Updating apt index..."
apt-get update -qq

log "Installing apt packages: ${APT_PKGS[*]}"
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${APT_PKGS[@]}"

# ---- 2. pip fallback for any module the apt packages didn't cover -----------
# Ensure pip is available before we try to use it as a fallback. python3-pip
# is small and harmless to add even if no fallback ends up being needed.
if ! "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
    log "Installing python3-pip (needed for the pip fallback)..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-pip
fi

# Probe each required Python module against the system interpreter; pip-install
# the wheel for any that's missing. Uses --break-system-packages because the Pi
# OS enforces PEP 668.
declare -A PIP_FOR_MODULE=(
    [fastapi]=fastapi
    [uvicorn]=uvicorn
    [pydantic]=pydantic
    [jinja2]=jinja2
    # suntime computes daily sunrise/sunset for the scheduled-capture loop;
    # not packaged in apt, so always pip-installed if missing.
    [suntime]=suntime
)

# tomllib is stdlib on Python 3.11+ (Bookworm). On Python <3.11 (e.g. Bullseye)
# we need the `tomli` shim. Detect that and add to the pip fallback list.
if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PIP_FOR_MODULE[tomli]=tomli
fi
PIP_MISSING=()
for mod in "${!PIP_FOR_MODULE[@]}"; do
    if ! "$PYTHON_BIN" -c "import $mod" >/dev/null 2>&1; then
        PIP_MISSING+=("${PIP_FOR_MODULE[$mod]}")
    fi
done

if [[ ${#PIP_MISSING[@]} -gt 0 ]]; then
    log "pip fallback needed for: ${PIP_MISSING[*]}"
    "$PYTHON_BIN" -m pip install --break-system-packages "${PIP_MISSING[@]}"
else
    log "All required Python modules already importable from system Python."
fi

# Sanity check: picamera2 must be importable, otherwise the service will crash.
if ! "$PYTHON_BIN" -c "import picamera2" >/dev/null 2>&1; then
    die "python3 cannot import 'picamera2'. Is python3-picamera2 actually installed and is the camera enabled (sudo raspi-config -> Interface)?"
fi

# ---- 3. render systemd unit -------------------------------------------------
log "Rendering $UNIT_DEST"

# Use a safe placeholder substitution: read the template and replace the three
# placeholders with sed. Anchored on the exact placeholder tokens.
TMP_UNIT="$(mktemp)"
trap 'rm -f "$TMP_UNIT"' EXIT
sed \
    -e "s|<USER>|${SERVICE_USER}|g" \
    -e "s|<INSTALL_DIR>|${INSTALL_DIR}|g" \
    -e "s|<PYTHON_BIN>|${PYTHON_BIN}|g" \
    "$UNIT_TEMPLATE" > "$TMP_UNIT"

# Refuse to install a unit that still has unsubstituted placeholders.
if grep -E '<(USER|INSTALL_DIR|PYTHON_BIN)>' "$TMP_UNIT" >/dev/null; then
    die "rendered unit still contains placeholders; aborting"
fi

install -m 0644 "$TMP_UNIT" "$UNIT_DEST"

# ---- 4. seed env + TOML config files if not already present ----------------
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"

ENV_DEST="$INSTALL_DIR/webcam.env"
if [[ ! -f "$ENV_DEST" && -f "$ENV_TEMPLATE" ]]; then
    log "Seeding $ENV_DEST from webcam.env.example (all values commented out)"
    install -m 0644 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$ENV_TEMPLATE" "$ENV_DEST"
fi

CONFIG_DEST="$INSTALL_DIR/config.toml"
if [[ ! -f "$CONFIG_DEST" && -f "$CONFIG_TEMPLATE" ]]; then
    log "Seeding $CONFIG_DEST from config.toml.example (edit to set your latitude/longitude)"
    install -m 0644 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$CONFIG_TEMPLATE" "$CONFIG_DEST"
fi

# Ensure the captures dir exists and is owned by the service user, so the
# scheduled-capture loop can write photos without permission errors.
CAPTURES_DIR="$INSTALL_DIR/captures"
if [[ ! -d "$CAPTURES_DIR" ]]; then
    log "Creating $CAPTURES_DIR (owned by $SERVICE_USER:$SERVICE_GROUP)"
    install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$CAPTURES_DIR"
fi

# ---- 5. enable / start ------------------------------------------------------
log "Reloading systemd..."
systemctl daemon-reload

if [[ "$DO_ENABLE" -eq 1 ]]; then
    log "Enabling ${SERVICE_NAME}.service for autostart on boot..."
    systemctl enable "${SERVICE_NAME}.service" >/dev/null
fi

if [[ "$DO_START" -eq 1 ]]; then
    log "Starting ${SERVICE_NAME}.service..."
    systemctl restart "${SERVICE_NAME}.service"
    sleep 1
    if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
        log "Service is active."
    else
        warn "Service did not stay active. Recent logs:"
        journalctl -u "${SERVICE_NAME}.service" -n 30 --no-pager || true
        exit 1
    fi
fi

# Surface the bound URL by reading WEBCAM_HOST/WEBCAM_PORT from the env file
# (if any) or falling back to the app defaults.
WEBCAM_PORT_RESOLVED="5000"
WEBCAM_HOST_RESOLVED="0.0.0.0"
if [[ -f "$ENV_DEST" ]]; then
    while IFS='=' read -r key value; do
        case "$key" in
            WEBCAM_PORT) WEBCAM_PORT_RESOLVED="$value" ;;
            WEBCAM_HOST) WEBCAM_HOST_RESOLVED="$value" ;;
        esac
    done < <(grep -E '^[[:space:]]*WEBCAM_(HOST|PORT)=' "$ENV_DEST" || true)
fi

log "Done."
log "URL      : http://${WEBCAM_HOST_RESOLVED}:${WEBCAM_PORT_RESOLVED}/  (replace 0.0.0.0 with the Pi's IP)"
log "Live UI  : http://${WEBCAM_HOST_RESOLVED}:${WEBCAM_PORT_RESOLVED}/live"
log "Status   : sudo systemctl status ${SERVICE_NAME}"
log "Logs     : sudo journalctl -u ${SERVICE_NAME} -f"
log "Env file : edit $ENV_DEST then sudo systemctl restart ${SERVICE_NAME}"
log "Location : edit $CONFIG_DEST (latitude/longitude/interval) then sudo systemctl restart ${SERVICE_NAME}"

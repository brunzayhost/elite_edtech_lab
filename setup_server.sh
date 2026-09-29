#!/usr/bin/env bash
# =============================================================================
# setup_server.sh - provision the "Elite EdTech Lab" application host.
#
# WHAT : Installs the OS packages, creates an unprivileged service user, clones
#        the repo, installs Python deps with Poetry, writes .env, configures
#        the firewall, and installs a systemd service.
# WHY  : One re-runnable script = a reproducible build you can cite in the
#        methodology chapter ("setup_server.sh @ commit <hash>").
# NOT  : It does NOT install Ollama or Docker. Ollama already runs in its own
#        container, and nothing here needs Docker.
#
# Usage (as root, on Ubuntu 24.04):
#   REPO_URL=https://github.com/brunzayhost/elite_edtech_lab.git \
#   LAN_CIDR=192.168.1.0/24 ./setup_server.sh
# =============================================================================

# -e: stop on first error. -u: unset variable is an error. -o pipefail: a
# failure anywhere in a pipeline fails the pipeline. Together they stop the
# script from half-installing and reporting success.
set -euo pipefail

# ---- Tunables (override via environment variables) --------------------------
REPO_URL="${REPO_URL:-https://github.com/brunzayhost/elite_edtech_lab.git}"
BRANCH="${BRANCH:-main}"
LAB_USER="${LAB_USER:-edtech}"                 # never run the app as root
INSTALL_DIR="${INSTALL_DIR:-/opt/elite_edtech_lab}"
LAN_CIDR="${LAN_CIDR:-192.168.1.0/24}"         # who may reach the dashboard
APP_ENTRYPOINT="${APP_ENTRYPOINT:-src/app.py}" # Streamlit entry file in your repo
APP_PORT="${APP_PORT:-8501}"
POETRY_VERSION="${POETRY_VERSION:-1.8.5}"      # pinned, so builds are deterministic
ENABLE_UFW="${ENABLE_UFW:-1}"                  # set 0 inside LXC; use the Proxmox firewall
SERVICE_NAME="${SERVICE_NAME:-edtech-lab}"
SKIP_CLONE="${SKIP_CLONE:-0}"                # 1 = code is already in INSTALL_DIR (copied in), don't use Git

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

# ---- 0. Guard rails ---------------------------------------------------------
# apt, useradd and systemd need root. Fail early with a clear message.
if [[ $EUID -ne 0 ]]; then
  echo "Run as root (sudo ./setup_server.sh)" >&2
  exit 1
fi

# ---- 1. System packages -----------------------------------------------------
# python3-venv : needed to create isolated virtualenvs
# build-essential : some Python wheels compile C extensions
# fonts-noto-cjk : only matters for server-side rendering (PDF/charts). The
#                  browser renders the dashboard text itself.
# locales : so we can generate a UTF-8 Hong Kong locale
# ufw, fail2ban : firewall and SSH brute-force protection
log "Installing OS packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  git curl ca-certificates build-essential sudo \
  python3 python3-venv python3-pip \
  fonts-noto-cjk locales \
  ufw fail2ban unattended-upgrades

# Generate a UTF-8 locale so Python's default I/O encoding is UTF-8.
# (Files must still be written with encoding="utf-8" explicitly; see the Python code.)
locale-gen en_HK.UTF-8 zh_HK.UTF-8 >/dev/null
update-locale LANG=en_HK.UTF-8

# ---- 2. Service user (least privilege) -------------------------------------
# A dedicated account without a login password means a compromised dashboard
# cannot touch anything else on the host.
if ! id "$LAB_USER" &>/dev/null; then
  log "Creating service user $LAB_USER"
  useradd --system --create-home --shell /bin/bash "$LAB_USER"
fi

# ---- 3. Clone or update the repo -------------------------------------------
# Re-running the script updates the checkout instead of failing (idempotent).
# SKIP_CLONE=1 is for deployments where the code was copied into INSTALL_DIR
# (for example a private repo with no credentials on this host).
if [[ "$SKIP_CLONE" == "1" ]]; then
  log "SKIP_CLONE=1: using code already in $INSTALL_DIR"
  chown -R "$LAB_USER":"$LAB_USER" "$INSTALL_DIR"
elif [[ -d "$INSTALL_DIR/.git" ]]; then
  log "Updating existing checkout"
  sudo -u "$LAB_USER" git -C "$INSTALL_DIR" fetch --all --prune
  sudo -u "$LAB_USER" git -C "$INSTALL_DIR" checkout "$BRANCH"
  sudo -u "$LAB_USER" git -C "$INSTALL_DIR" pull --ff-only
else
  log "Cloning $REPO_URL"
  mkdir -p "$INSTALL_DIR"
  chown "$LAB_USER":"$LAB_USER" "$INSTALL_DIR"
  sudo -u "$LAB_USER" git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

# Research traceability: record the exact code state used for this deployment.
# `--dirty` appends "-dirty" if there are uncommitted local edits, which would
# otherwise make the hash misleading.
COMMIT="$(sudo -u "$LAB_USER" git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || echo no-git-checkout)"
DESCRIBE="$(sudo -u "$LAB_USER" git -C "$INSTALL_DIR" describe --always --dirty 2>/dev/null || echo no-git-checkout)"
log "Deployed commit: $COMMIT ($DESCRIBE)"

# ---- 4. Poetry (pinned) + dependencies -------------------------------------
# Installed with pip into its own venv instead of `curl | python3 -`, so we
# never execute an unpinned script downloaded from the internet as root.
POETRY_VENV="/home/$LAB_USER/.poetry-venv"
if [[ ! -x "$POETRY_VENV/bin/poetry" ]]; then
  log "Installing Poetry $POETRY_VERSION"
  sudo -u "$LAB_USER" python3 -m venv "$POETRY_VENV"
  sudo -u "$LAB_USER" "$POETRY_VENV/bin/pip" install --quiet "poetry==$POETRY_VERSION"
fi
POETRY="$POETRY_VENV/bin/poetry"

log "Installing Python dependencies from poetry.lock"
# in-project venv => stable path $INSTALL_DIR/.venv used by the systemd unit.
sudo -u "$LAB_USER" bash -c "cd '$INSTALL_DIR' && '$POETRY' config virtualenvs.in-project true && '$POETRY' install --no-interaction"

# ---- 5. Secrets file --------------------------------------------------------
# Copy the template ONLY if .env does not exist, so re-runs never overwrite
# credentials you already set. chmod 600 = only the service user can read it.
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  log "Creating .env from .env.example (edit it next!)"
  sudo -u "$LAB_USER" cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
fi
chmod 600 "$INSTALL_DIR/.env"
chown "$LAB_USER":"$LAB_USER" "$INSTALL_DIR/.env"

# Local data + spool directories (git-ignored on purpose).
sudo -u "$LAB_USER" mkdir -p "$INSTALL_DIR/data/processed" "$INSTALL_DIR/spool"

# ---- 6. Firewall ------------------------------------------------------------
# Default-deny inbound; allow SSH first so enabling the firewall cannot lock
# you out; allow the dashboard only from the LAN. Port 11434 is NOT opened
# here: this host is an Ollama client, not a server.
if [[ "$ENABLE_UFW" == "1" ]]; then
  log "Configuring UFW"
  ufw default deny incoming
  ufw default allow outgoing
  ufw allow OpenSSH
  ufw allow from "$LAN_CIDR" to any port "$APP_PORT" proto tcp
  ufw --force enable
else
  log "Skipping UFW (ENABLE_UFW=0). Make sure the Proxmox firewall protects port $APP_PORT."
fi

# fail2ban's default sshd jail is enabled out of the box on Debian/Ubuntu.
systemctl enable --now fail2ban

# ---- 7. systemd service -----------------------------------------------------
# Generated here (not a static file) so paths and user always match the
# variables above.
#   After=network-online.target : wait for networking (we need the LAN services)
#   EnvironmentFile             : load .env without exposing it on the command line
#   Restart=on-failure          : self-heal after crashes; survives reboots
#   No Requires= on Ollama      : it lives on another host, so the app handles
#                                 outages with retries rather than systemd
log "Installing systemd unit $SERVICE_NAME.service"
cat >"/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Elite EdTech Lab dashboard
After=network-online.target
Wants=network-online.target

[Service]
User=${LAB_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
ExecStart=${INSTALL_DIR}/.venv/bin/streamlit run ${APP_ENTRYPOINT} --server.port ${APP_PORT} --server.address 0.0.0.0 --server.headless true --browser.gatherUsageStats false
Restart=on-failure
RestartSec=5
# Sandboxing: the service cannot gain privileges or write outside its dirs.
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=${INSTALL_DIR}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
# The service starts only after you have edited .env; see the next-steps text.

cat <<EOF

=============================================================================
Setup complete. Deployed commit: $COMMIT

NEXT STEPS
  1. Edit secrets:       sudo -u $LAB_USER nano $INSTALL_DIR/.env
  2. Health check:       sudo -u $LAB_USER $INSTALL_DIR/scripts/healthcheck.sh
  3. Start the service:  systemctl start $SERVICE_NAME
  4. Follow the logs:    journalctl -u $SERVICE_NAME -f
  5. Snapshot in Proxmox named BASE_INSTALL_$(date +%Y%m%d)
=============================================================================
EOF

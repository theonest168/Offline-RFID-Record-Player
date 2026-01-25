#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MUSIC_DIR_DEFAULT="/home/pi/Music"

# Allow override: MUSIC_DIR=/some/path bash install/install.sh
MUSIC_DIR="${MUSIC_DIR:-$MUSIC_DIR_DEFAULT}"

echo "== RFID Record Player (Local MP3 / MPD) Installer =="
echo "Project: $PROJECT_DIR"
echo "Music dir: $MUSIC_DIR"
echo

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root:"
  echo "  sudo bash install/install.sh"
  exit 1
fi

echo "== Updating apt =="
apt-get update

echo "== Installing system packages =="
apt-get install -y \
  python3 python3-pip python3-venv \
  git \
  mpd mpc \
  pulseaudio pulseaudio-module-bluetooth \
  bluetooth bluez bluez-tools \
  lgpio

echo "== Creating music directory =="
# Create as the real user (pi) if possible
REAL_USER="${SUDO_USER:-pi}"
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6 || true)"
if [[ -z "${REAL_HOME}" ]]; then
  REAL_HOME="/home/$REAL_USER"
fi

mkdir -p "$MUSIC_DIR"
chown -R "$REAL_USER":"$REAL_USER" "$MUSIC_DIR"

echo "== Configuring MPD music directory =="
MPD_CONF="/etc/mpd.conf"

if [[ ! -f "$MPD_CONF" ]]; then
  echo "Error: $MPD_CONF not found (mpd install may have failed)."
  exit 1
fi

# Backup once
if [[ ! -f "${MPD_CONF}.bak" ]]; then
  cp "$MPD_CONF" "${MPD_CONF}.bak"
fi

# Set music_directory (replace existing line)
# Works for both quoted and unquoted formats
sed -i -E "s|^(\s*music_directory\s+).*|\1\"$MUSIC_DIR\"|g" "$MPD_CONF"

# Ensure these core paths are sane (usually already are)
# Keep MPD's database/state in default system locations.
# (No-op if already present)
grep -qE '^\s*playlist_directory' "$MPD_CONF" || echo 'playlist_directory "/var/lib/mpd/playlists"' >> "$MPD_CONF"
grep -qE '^\s*db_file' "$MPD_CONF" || echo 'db_file "/var/lib/mpd/tag_cache"' >> "$MPD_CONF"
grep -qE '^\s*state_file' "$MPD_CONF" || echo 'state_file "/var/lib/mpd/state"' >> "$MPD_CONF"

echo "== Enabling & restarting MPD =="
systemctl enable mpd
systemctl restart mpd

echo "== Updating MPD database (mpc update) =="
# Run as the real user so mpc talks to MPD cleanly in typical setups
sudo -u "$REAL_USER" mpc update || true

echo "== Installing Python packages =="
# Install Python deps system-wide (simple) OR switch to venv if you prefer.
python3 -m pip install --upgrade pip

python3 -m pip install \
  gpiozero \
  mfrc522 \
  python-dotenv \
  python-mpd2

echo "== Optional: PulseAudio auto-switch to Bluetooth sink =="
PA_DEFAULT="/home/$REAL_USER/.config/pulse/default.pa"
sudo -u "$REAL_USER" mkdir -p "$(dirname "$PA_DEFAULT")"

if [[ -f "$PA_DEFAULT" ]]; then
  if ! grep -q "module-switch-on-connect" "$PA_DEFAULT"; then
    echo "load-module module-switch-on-connect" | sudo -u "$REAL_USER" tee -a "$PA_DEFAULT" >/dev/null
  fi
else
  echo "load-module module-switch-on-connect" | sudo -u "$REAL_USER" tee "$PA_DEFAULT" >/dev/null
fi

echo
echo "== Done =="
echo "Next steps:"
echo "1) Copy MP3s into: $MUSIC_DIR"
echo "2) Run: mpc update"
echo "3) Run setup: python3 install/setup.py   (or wherever your setup.py lives)"
echo "4) Run player: python3 record_player.py"
echo
echo "If you're using Echo over Bluetooth:"
echo "- Say: 'Alexa, pair Bluetooth'"
echo "- Pair from Pi with: bluetoothctl"
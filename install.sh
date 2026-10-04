#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PLUGIN_ID="io.github.maffur-hub.youtube-music-bar"
PLUGIN_DIR="${HOME}/.config/omarchy/plugins/${PLUGIN_ID}"
DATA_DIR="${HOME}/.local/share/yt-music"
BIN_DIR="${HOME}/.local/bin"
VENV="${DATA_DIR}/venv"

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -rf "${PLUGIN_DIR}" "${DATA_DIR}" "${BIN_DIR}/yt-music-ctl"
  if command -v omarchy >/dev/null 2>&1; then
    omarchy plugin disable "${PLUGIN_ID}" >/dev/null 2>&1 || true
  fi
  printf 'Removed %s (authentication under ~/.config/yt-music was kept).\n' "${PLUGIN_ID}"
  exit 0
fi

for command in python3 mpv yt-dlp; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "${command}" >&2
    exit 1
  fi
done

# Non-fatal: mpv-mpris is what puts playback on D-Bus, which the media keys
# and Omarchy's media widget talk to. Same three script locations the
# backend's `yt-music-ctl doctor` checks.
MPRIS_FOUND=0
for mpris_path in /etc/mpv/scripts/mpris.so /usr/lib/mpv-mpris/mpris.so \
                  "${HOME}/.config/mpv/scripts/mpris.so"; do
  if [[ -e "${mpris_path}" ]]; then
    MPRIS_FOUND=1
    break
  fi
done
if [[ "${MPRIS_FOUND}" -eq 0 ]]; then
  if command -v pacman >/dev/null 2>&1; then
    printf 'Note: install mpv-mpris to enable media keys and the Omarchy media widget: sudo pacman -S mpv-mpris\n'
  else
    printf 'Note: install mpv-mpris (the mpv MPRIS D-Bus bridge) to enable media keys and the Omarchy media widget\n'
  fi
fi

mkdir -p "${BIN_DIR}" "${DATA_DIR}"
python3 -m venv "${VENV}"
"${VENV}/bin/python" -m pip install --require-hashes --only-binary=:all: \
  -r "${ROOT}/requirements.txt"

# Resolve both paths through any symlinks before the `rm -rf` below. A
# string comparison can miss that the install target is this very checkout,
# which would delete the source tree.
ROOT_REAL=$(realpath -m -- "${ROOT}")
PLUGIN_DIR_REAL=$(realpath -m -- "${PLUGIN_DIR}")

if [[ "${ROOT_REAL}" == "${PLUGIN_DIR_REAL}" ]]; then
  # Running from inside the plugin checkout (the dev/`plugin clone --edit`
  # layout): copying would mean `rm -rf`-ing our own source tree, so skip it.
  printf 'Running inside the plugin directory; skipping the copy step.\n'
else
  # Refuse when the install target is an ancestor of this checkout: `rm -rf`
  # would take the source tree with it. A differing target that is a real git
  # checkout (matching manifest.json + .git) still proceeds as before.
  case "${ROOT_REAL}/" in
    "${PLUGIN_DIR_REAL}/"*)
      printf 'Refusing to remove %s: it contains the source checkout %s\n' \
        "${PLUGIN_DIR}" "${ROOT}" >&2
      exit 1
      ;;
  esac
  rm -rf "${PLUGIN_DIR}"
  mkdir -p "${PLUGIN_DIR}"
  cp "${ROOT}/BarWidget.qml" "${ROOT}/Model.js" \
    "${ROOT}/Panel.qml" "${ROOT}/manifest.json" \
    "${ROOT}/cava.conf" "${PLUGIN_DIR}/"
fi
cp "${ROOT}/backend/yt_music.py" "${DATA_DIR}/yt_music.py"
chmod 700 "${DATA_DIR}" "${VENV}"

cat > "${BIN_DIR}/yt-music-ctl" <<EOF
#!/usr/bin/env bash
exec "${VENV}/bin/python" "${DATA_DIR}/yt_music.py" "\$@"
EOF
chmod 755 "${BIN_DIR}/yt-music-ctl"

if command -v omarchy >/dev/null 2>&1; then
  omarchy plugin enable "${PLUGIN_ID}" center >/dev/null 2>&1 || true
  omarchy restart shell >/dev/null 2>&1 || true
fi

printf 'Installed %s. Run: yt-music-ctl login\n' "${PLUGIN_ID}"

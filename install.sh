#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PLUGIN_ID="yt-music"
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

mkdir -p "${BIN_DIR}" "${DATA_DIR}"
python3 -m venv "${VENV}"
"${VENV}/bin/python" -m pip install --require-hashes --only-binary=:all: \
  -r "${ROOT}/requirements.txt"

if [[ "${ROOT}" == "${PLUGIN_DIR}" ]]; then
  # Running from inside the plugin checkout (the dev/`plugin clone --edit`
  # layout): copying would mean `rm -rf`-ing our own source tree, so skip it.
  printf 'Running inside the plugin directory; skipping the copy step.\n'
else
  rm -rf "${PLUGIN_DIR}"
  mkdir -p "${PLUGIN_DIR}"
  cp "${ROOT}/BarWidget.qml" "${ROOT}/Model.js" \
    "${ROOT}/Panel.qml" "${ROOT}/manifest.json" "${PLUGIN_DIR}/"
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

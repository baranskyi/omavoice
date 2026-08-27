#!/usr/bin/env bash
# Undo scripts/setup.sh. Removes what the setup created; leaves what is yours.
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${XDG_DATA_HOME:-$HOME/.local/share}/omavoice/venv"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/omavoice.service"
ENV_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/omavoice/env"
PW_CONF="${XDG_CONFIG_HOME:-$HOME/.config}/pipewire/pipewire.conf.d/99-omavoice-echo-cancel.conf"
LINK="$HOME/.local/bin/omavoice-ctl"

note() { printf '  %s\n' "$*"; }

systemctl --user disable --now omavoice 2>/dev/null || true
rm -f "$UNIT"
systemctl --user daemon-reload
note "service stopped and removed"

rm -rf "$(dirname "$VENV")"
note "virtualenv removed"

if [[ -L "$LINK" && "$(readlink -f "$LINK")" == "$PLUGIN_DIR/bin/omavoice-ctl" ]]; then
  rm -f "$LINK"
  note "$LINK removed"
fi

printf '\n\033[1mLeft in place on purpose:\033[0m\n'
[[ -f "$ENV_FILE" ]] && note "$ENV_FILE — your API key"
[[ -f "$PW_CONF" ]]  && note "$PW_CONF — echo cancellation, which other things may now rely on"
note "Delete either by hand if you are sure."

printf '\n\033[1mThe plugin folder itself:\033[0m\n'
note "omarchy plugin remove io.github.baranskyi.omavoice"
echo

#!/usr/bin/env bash
# omavoice — one-time setup after `omarchy plugin add`.
#
# Installing the plugin only puts QML in place. The voice itself is a daemon,
# and a daemon needs a Python environment, a systemd unit and an echo
# canceller. This script sets those up. It never uses sudo and never
# overwrites a config you already have.
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${XDG_DATA_HOME:-$HOME/.local/share}/omavoice/venv"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/omavoice.service"
ENV_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/omavoice/env"
KEY_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/omavoice/key"
PW_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/pipewire/pipewire.conf.d"
PW_CONF="$PW_DIR/99-omavoice-echo-cancel.conf"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }

# --- 1. Python environment ---------------------------------------------------
# It lives outside the plugin folder on purpose: Omarchy's validator rejects a
# plugin containing symlinks, and every virtualenv has a few.
say "Python environment"
if [[ -x "$VENV/bin/python" ]]; then
  note "already at $VENV"
else
  mkdir -p "$(dirname "$VENV")"
  if command -v uv >/dev/null; then
    uv venv "$VENV" --python 3.13 >/dev/null
  else
    python3 -m venv "$VENV"
  fi
  note "created $VENV"
fi

# Only what the lockfile names, and only if its digest matches. This used to
# resolve a lower bound at install time and, on the fallback path, upgrade pip
# to whatever was current first — two ways for an unreviewed release to end up
# inside a service that runs on every login. --require-hashes refuses a
# mismatch outright rather than warning about it, and pip is left alone.
LOCK="$PLUGIN_DIR/daemon/requirements.lock"
if [[ ! -f "$LOCK" ]]; then
  echo "missing $LOCK — refusing to install anything unpinned" >&2
  exit 1
fi

if command -v uv >/dev/null; then
  uv pip install --quiet --python "$VENV/bin/python" --require-hashes -r "$LOCK"
else
  "$VENV/bin/python" -m pip install --quiet --require-hashes --no-deps -r "$LOCK"
fi
note "dependencies installed from $LOCK, digests verified"

# --- 2. systemd unit ---------------------------------------------------------
# The PATH is captured from this shell rather than hardcoded: the daemon shells
# out to codex or claude, and version managers keep those somewhere systemd's
# own PATH will never look.
say "systemd unit"
mkdir -p "$(dirname "$UNIT")"
sed -e "s|@PLUGIN_DIR@|$PLUGIN_DIR|g" \
    -e "s|@VENV@|$VENV|g" \
    -e "s|@PATH@|$PATH|g" \
    "$PLUGIN_DIR/systemd/omavoice.service" > "$UNIT"
chmod 644 "$UNIT"
systemctl --user daemon-reload
note "installed $UNIT"

# --- 3. API key --------------------------------------------------------------
# In a file of its own, which systemd does not load. An environment variable is
# not a secret on a shared UID: every child process inherits it, and
# /proc/<pid>/environ keeps the copy the process started with for anything
# running as the same user to read — including the agent this daemon spawns on
# every question.
say "OpenAI key"
mkdir -p "$(dirname "$KEY_FILE")"
if [[ -f "$KEY_FILE" ]]; then
  chmod 600 "$KEY_FILE"
  note "kept your existing $KEY_FILE"
else
  install -m 600 /dev/null "$KEY_FILE"
  note "created $KEY_FILE (mode 600) — it is empty"
  note "the Realtime API needs a paid key: https://platform.openai.com/api-keys"
fi

if [[ -f "$ENV_FILE" ]]; then
  chmod 600 "$ENV_FILE"
  if grep -q '^OPENAI_API_KEY=' "$ENV_FILE"; then
    note "$ENV_FILE still holds a key from an older install."
    note "Save it again from the panel's settings and it will move to $KEY_FILE."
  fi
else
  mkdir -p "$(dirname "$ENV_FILE")"
  install -m 600 /dev/null "$ENV_FILE"
  {
    echo '# Settings only. The API key lives in the file next to this one, which'
    echo '# systemd does not load into the environment.'
  } >> "$ENV_FILE"
fi

# --- 4. Echo cancellation ----------------------------------------------------
# Without this the assistant hears itself through the speakers, takes that for
# your voice, and answers its own goodbye until you stop it.
say "Echo cancellation"
mkdir -p "$PW_DIR"
if [[ ! -e "$PW_CONF" ]]; then
  cp "$PLUGIN_DIR/pipewire/99-omavoice-echo-cancel.conf" "$PW_CONF"
  note "installed $PW_CONF"
  note "restart PipeWire to load it: systemctl --user restart pipewire"
elif cmp -s "$PLUGIN_DIR/pipewire/99-omavoice-echo-cancel.conf" "$PW_CONF"; then
  note "already up to date"
else
  note "$PW_CONF exists and differs — left untouched."
  note "Compare it yourself:"
  note "  diff $PW_CONF $PLUGIN_DIR/pipewire/99-omavoice-echo-cancel.conf"
fi

# --- 5. Command line ---------------------------------------------------------
say "omavoice-ctl"
BIN_DIR="$HOME/.local/bin"
LINK="$BIN_DIR/omavoice-ctl"
mkdir -p "$BIN_DIR"
if [[ -L "$LINK" && "$(readlink -f "$LINK")" == "$PLUGIN_DIR/bin/omavoice-ctl" ]]; then
  note "already linked"
elif [[ -e "$LINK" ]]; then
  note "$LINK exists and is not ours — left untouched."
  note "Call it directly instead: $PLUGIN_DIR/bin/omavoice-ctl"
else
  ln -s "$PLUGIN_DIR/bin/omavoice-ctl" "$LINK"
  note "linked $LINK"
fi

# --- Next --------------------------------------------------------------------
say "Done. Three things left, in this order:"
note "1. Put your key into $ENV_FILE"
note "2. systemctl --user enable --now omavoice"
note "3. Bind a key in ~/.config/hypr/bindings.lua:"
note "     o.bind(\"SUPER + CTRL + M\", \"Voice\", \"omarchy-shell shell toggle io.github.baranskyi.omavoice\")"
echo

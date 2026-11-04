#!/usr/bin/env bash
# Install the drift monitor, watchdog, reconciliation digest and exit engine as systemd user units (Linux).
# The execution probe is installed but never enabled here: that is the operator's decision.
# Re-run after `git pull` to pick up dependency, code and unit changes.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
env_file="$HOME/.config/papertrade/env"
units="$HOME/.config/systemd/user"

if [ ! -f "$env_file" ]; then
  echo "missing $env_file: create it with"
  echo "  mkdir -p -m 700 ~/.config/papertrade && uv run papertrade keygen --out $env_file && grep -v PRIVATE_KEY .env.example >> $env_file"
  exit 1
fi
if [ "$(stat -c %a "$env_file")" != 600 ]; then
  echo "$env_file holds the wallet key: chmod 600 it first"
  exit 1
fi

(cd "$repo" && uv sync --frozen)

mkdir -p "$units"
for unit in "$repo"/deploy/systemd/*; do
  sed "s|@REPO@|$repo|g" "$unit" > "$units/$(basename "$unit")"
done

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
systemctl --user daemon-reload
systemctl --user enable --now papertrade-drift.timer papertrade-watchdog.timer papertrade-reconcile.timer
systemctl --user enable papertrade-exits.service
systemctl --user restart papertrade-exits.service  # picks up new code and a changed env file
if ! loginctl show-user "$USER" -p Linger | grep -q yes; then
  echo "note: run 'sudo loginctl enable-linger $USER' so the timers keep running after logout"
fi
systemctl --user list-timers 'papertrade-*'
systemctl --user --no-pager status papertrade-exits.service | head -3
if ! systemctl --user is-enabled --quiet papertrade-probe.timer; then
  echo "execution probe not enabled: systemctl --user enable --now papertrade-probe.timer"
fi

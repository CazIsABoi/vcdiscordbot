#!/usr/bin/env bash
# Sets up an Oracle Cloud (OCI) Always Free Ubuntu VM to run the bot with
# Docker Compose. Run it on the VM after SSHing in, from inside a clone of
# this repo (see README.md).
#
# Usage:
#   DISCORD_TOKEN=xxx ./oracle/setup-vm.sh
#
# Safe to re-run: it skips installing Docker if it's already present and keeps
# an existing .env, so re-running after a `git pull` just rebuilds the bot.

set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi
# Make sure Docker (and so the bot, via `restart: unless-stopped`) comes back
# up after the VM reboots.
sudo systemctl enable --now docker >/dev/null

if [[ ! -f .env ]]; then
  if [[ -z "${DISCORD_TOKEN:-}" ]]; then
    echo "Error: set the DISCORD_TOKEN environment variable before running this script." >&2
    exit 1
  fi
  cp .env.example .env
  sed -i "s|^DISCORD_TOKEN=.*|DISCORD_TOKEN=${DISCORD_TOKEN}|" .env
fi

mkdir -p data
sudo docker compose up --build -d

echo "Bot is running. Tail logs with: sudo docker compose logs -f"

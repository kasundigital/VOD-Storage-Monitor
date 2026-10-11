#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${VOD_MONITOR_DIR:-/docker/vod-monitor}"
IMAGE="ghcr.io/kasundigital/vod-storage-monitor:latest"
COMPOSE_URL="https://raw.githubusercontent.com/kasundigital/VOD-Storage-Monitor/main/docker-compose.yml"

mkdir -p "$APP_DIR/data"
cd "$APP_DIR"

if [ ! -f .env ]; then
  echo "ERROR: $APP_DIR/.env not found."
  echo "Create it first from .env.example and set ADMIN_PASSWORD / SECRET_KEY."
  exit 1
fi

echo "Backing up local Compose configuration..."
if [ -f docker-compose.yml ]; then cp -a docker-compose.yml "docker-compose.yml.bak-$(date +%Y%m%d-%H%M%S)"; fi

echo "Downloading latest docker-compose.yml..."
curl -fsSL "$COMPOSE_URL" -o docker-compose.yml

echo "Pulling latest image..."
docker compose pull

echo "Starting VOD Storage Monitor..."
docker compose up -d --no-deps vod-monitor

echo
echo "Update complete."
docker exec vod-monitor sh -c 'printf "Version: "; printenv APP_VERSION; printf "Build: "; printenv BUILD_SHA' || true

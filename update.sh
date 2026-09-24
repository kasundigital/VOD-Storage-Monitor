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

echo "Removing old container..."
docker rm -f vod-monitor 2>/dev/null || true

echo "Removing old image..."
docker image rm -f "$IMAGE" 2>/dev/null || true

echo "Downloading latest docker-compose.yml..."
curl -fsSL "$COMPOSE_URL" -o docker-compose.yml

echo "Pulling latest image..."
docker compose pull

echo "Starting VOD Storage Monitor..."
docker compose up -d

echo
echo "Update complete."
docker exec vod-monitor sh -c 'printf "Version: "; printenv APP_VERSION; printf "Build: "; printenv BUILD_SHA' || true

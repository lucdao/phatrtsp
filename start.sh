#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="${PROJECT_DIR}/code"
VIDEO_FILE="${COMPOSE_DIR}/server/server_assets/0719_with_black.mp4"
NETWORK_NAME="shared_network"
IMAGE_NAME="rtspserver:latest"
RTSP_PORT="8553"
RTSP_PATH="stream"

if ! command -v docker >/dev/null 2>&1; then
  echo "Loi: chua cai Docker."
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Loi: Docker daemon chua chay hoac tai khoan khong co quyen dung Docker."
  exit 1
fi

if [[ ! -s "${VIDEO_FILE}" ]]; then
  echo "Loi: khong tim thay video ${VIDEO_FILE}"
  exit 1
fi

if ! docker network inspect "${NETWORK_NAME}" >/dev/null 2>&1; then
  docker network create "${NETWORK_NAME}" >/dev/null
fi

cd "${COMPOSE_DIR}"
if ! docker image inspect "${IMAGE_NAME}" >/dev/null 2>&1; then
  docker compose build rtsp_server
fi
docker compose up -d --no-build rtsp_server

ready=0
for _ in $(seq 1 60); do
  if (exec 3<>"/dev/tcp/127.0.0.1/${RTSP_PORT}") 2>/dev/null; then
    exec 3>&-
    exec 3<&-
    ready=1
    break
  fi
  sleep 0.5
done

if [[ "${ready}" -ne 1 ]]; then
  echo "Loi: RTSP khong san sang sau 30 giay."
  docker compose logs --tail 50 rtsp_server
  exit 1
fi

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [[ -z "${LAN_IP}" ]]; then
  LAN_IP="127.0.0.1"
fi

echo "RTSP da san sang: rtsp://${LAN_IP}:${RTSP_PORT}/${RTSP_PATH}"

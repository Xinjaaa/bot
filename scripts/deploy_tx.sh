#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-tx}"
REMOTE_DIR="${REMOTE_DIR:-/opt/wecom-callback}"
IMAGE_NAME="${IMAGE_NAME:-wecom-callback:latest}"

if [[ ! -f ".env" ]]; then
  echo ".env not found. Copy .env.example to .env and fill in real values first." >&2
  exit 1
fi

ssh "${REMOTE_HOST}" "mkdir -p ${REMOTE_DIR}"
scp Dockerfile requirements.txt .env "${REMOTE_HOST}:${REMOTE_DIR}/"
scp -r app "${REMOTE_HOST}:${REMOTE_DIR}/"

ssh "${REMOTE_HOST}" "
  set -euo pipefail
  cd ${REMOTE_DIR}
  docker build -t ${IMAGE_NAME} .
  docker rm -f wecom-callback >/dev/null 2>&1 || true
  docker run -d \
    --name wecom-callback \
    --restart unless-stopped \
    --env-file .env \
    -p 8000:8000 \
    ${IMAGE_NAME}
  docker ps --filter name=wecom-callback
"

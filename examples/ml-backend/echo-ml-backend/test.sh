#!/usr/bin/env bash

set -euo pipefail

HOST="${HOST:-http://localhost:8000}"

echo "→ GET ${HOST}/health"
curl -sS -f "${HOST}/health"
echo

echo "→ GET ${HOST}/setup"
curl -sS -f "${HOST}/setup"
echo

echo "→ POST ${HOST}/predict"
curl -sS -f -X POST "${HOST}/predict" \
  -H "Content-Type: application/json" \
  -d '{"tasks":[{"id":"task-1","file_path":"s3://bucket/img.jpg"}]}'
echo

echo
echo "✓ echo-backend 全部端点 200。把 ${HOST} 填进项目 ML Backends 即可走通预标注链路。"

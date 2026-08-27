#!/bin/sh
set -eu

cd "$(dirname "$0")"
docker compose -f compose.yaml stop neutralis
echo "Neutralis parado. Os registros em ./data foram preservados."

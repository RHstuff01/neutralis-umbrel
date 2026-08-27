#!/bin/sh
set -eu

cd "$(dirname "$0")"
docker compose -f compose.yaml ps
echo
docker compose -f compose.yaml logs --tail=30 neutralis

#!/bin/sh
set -eu

cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "Erro: Docker não foi encontrado neste Umbrel." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Erro: Docker Compose não está disponível." >&2
  exit 1
fi

mkdir -p data
chmod 700 data
docker compose -f compose.yaml config >/dev/null
docker compose -f compose.yaml up -d --build

echo
echo "Neutralis iniciado em: http://umbrel.local:8787"
echo "Se necessário, substitua umbrel.local pelo IP local do Umbrel."

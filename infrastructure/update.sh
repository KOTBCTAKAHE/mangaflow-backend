#!/bin/sh
# Update the MangaFlow backend to the latest image published by GitHub Actions.
# Usage (from the infrastructure folder):  ./update.sh
set -e
cd "$(dirname "$0")"
docker compose pull
docker compose up -d --remove-orphans
docker image prune -f >/dev/null
docker compose ps

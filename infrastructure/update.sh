#!/bin/sh
# Update the MangaFlow backend: fresh config files from GitHub + the latest image.
# Usage (from the infrastructure folder):  ./update.sh
# Your .env is never touched. Replaced files are kept as *.bak.
set -e
cd "$(dirname "$0")"
RAW="${MANGAFLOW_RAW:-https://raw.githubusercontent.com/KOTBCTAKAHE/mangaflow-backend/main/infrastructure}"
NGINX_CHANGED=0

if [ -z "$MF_NO_SELF_UPDATE" ] && command -v curl >/dev/null 2>&1; then
  tmp=$(mktemp -d)
  ok=1
  for f in update.sh docker-compose.yml nginx/nginx.conf; do
    mkdir -p "$tmp/$(dirname "$f")"
    curl -fsSL --max-time 30 "$RAW/$f" -o "$tmp/$f" || { ok=0; break; }
  done
  if [ "$ok" = 1 ]; then
    if ! cmp -s "$tmp/update.sh" update.sh; then
      chmod +x "$tmp/update.sh"
      mv "$tmp/update.sh" ./update.sh.new && mv ./update.sh.new ./update.sh  # new inode: safe while running
      echo "update.sh обновлён — запускаю новую версию"
      rm -rf "$tmp"
      exec ./update.sh
    fi
    for f in docker-compose.yml nginx/nginx.conf; do
      if [ -f "$f" ] && ! cmp -s "$tmp/$f" "$f"; then
        cp "$f" "$f.bak"
        mv "$tmp/$f" "$f"
        echo "обновлён $f (прежний сохранён как $f.bak)"
        [ "$f" = nginx/nginx.conf ] && NGINX_CHANGED=1
      fi
    done
  else
    echo "не удалось скачать свежие файлы конфигурации с GitHub — продолжаю с текущими"
  fi
  rm -rf "$tmp"
fi

docker compose pull
docker compose up -d --remove-orphans
[ "$NGINX_CHANGED" = 1 ] && docker compose restart nginx
docker image prune -f >/dev/null
docker compose ps
echo; docker compose exec -T nginx nginx -v 2>&1 || true

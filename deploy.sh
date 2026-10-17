#!/usr/bin/env bash
#
# deploy.sh -- tarik commit terbaru dari GitHub, rebuild image, restart, cek bot.
#
# Pakai:
#   ./deploy.sh            # pull + build + up
#   ./deploy.sh --test     # sekalian jalankan pytest (kalau .venv ada)
#
# Asumsi: dijalankan dari folder repo (mis. ~/agent) dan `git pull` sudah
# tidak meminta login (SSH deploy key atau PAT tersimpan).
set -euo pipefail

cd "$(dirname "$0")"

SERVICE=agent

echo "==> [1/4] git pull"
before=$(git rev-parse --short HEAD)
git pull --ff-only
after=$(git rev-parse --short HEAD)
if [ "$before" = "$after" ]; then
    echo "    tidak ada commit baru (tetap $after)"
else
    echo "    update $before -> $after"
fi

if [ "${1:-}" = "--test" ] && [ -x ./.venv/bin/python ]; then
    echo "==> pytest"
    ./.venv/bin/python -m pytest -q
fi

echo "==> [2/4] docker compose up -d --build"
docker compose up -d --build --force-recreate

echo "==> [3/4] tunggu bot siap"
cid=$(docker compose ps -q "$SERVICE")
ok=0
for _ in $(seq 1 20); do
    if docker logs --since 3m "$cid" 2>&1 | grep -q 'polling_started'; then
        ok=1
        break
    fi
    sleep 2
done

echo "==> [4/4] status & log"
docker ps --filter "id=$cid" --format '    {{.Names}}  {{.Status}}'
docker logs --since 3m "$cid" 2>&1 | grep -E 'bot_started|ERROR|Traceback' || true

if [ "$ok" -eq 1 ]; then
    echo "OK: deploy selesai, bot jalan (polling_started)."
else
    echo "GAGAL: 'polling_started' belum muncul. Cek: docker logs $cid"
    exit 1
fi

#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите через sudo bash deploy/install.sh"
  exit 1
fi
cd "$(dirname "$0")/.."
/usr/bin/python3 -c 'import sys; assert sys.version_info >= (3,11), "Нужен Python 3.11+"; from zoneinfo import ZoneInfo; ZoneInfo("Asia/Dubai")'
if [ ! -f .env ] && [ ! -f /opt/padel-escape/.env ]; then
  echo "Сначала выполните python3 configure.py в папке проекта."
  exit 1
fi
if ! id padelbot >/dev/null 2>&1; then
  useradd --system --home-dir /opt/padel-escape --shell /usr/sbin/nologin padelbot
fi
install -d -m 0755 /opt/padel-escape
install -d -o padelbot -g padelbot -m 0700 /opt/padel-escape/data
systemctl stop padel-escape.service 2>/dev/null || true
install -m 0644 core.py bot.py backup.py /opt/padel-escape/
if [ ! -f /opt/padel-escape/.env ]; then
  install -o padelbot -g padelbot -m 0600 .env /opt/padel-escape/.env
fi
install -m 0644 deploy/padel-escape.service /etc/systemd/system/padel-escape.service
cd /opt/padel-escape
runuser -u padelbot -- /usr/bin/python3 bot.py --check
systemctl daemon-reload
systemctl enable --now padel-escape.service
systemctl --no-pager status padel-escape.service

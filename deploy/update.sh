#!/usr/bin/env bash
# Pull the latest code and restart the site. Run on the server as root:
#   sudo /opt/stonkflyrh/deploy/update.sh            # site only (safe while trading)
#   sudo /opt/stonkflyrh/deploy/update.sh --worker   # also restart the trading worker
#
# The site reads index.html from disk on every request, so a pure HTML change
# is live the moment the pull lands; the restart covers server.py changes.
set -euo pipefail
cd /opt/stonkflyrh
sudo -u stonkfly git pull --ff-only
sudo -u stonkfly .venv/bin/pip install -q -e '.[test]'
systemctl restart stonkflyrh-site
echo "site updated: $(sudo -u stonkfly git rev-parse --short HEAD)"
if [ "${1:-}" = "--worker" ]; then
  echo "stopping the worker at the next safe point..."
  mode=$(grep -E '^STONKFLYRH_MODE=' .env | cut -d= -f2 | tr -d ' ' || echo paper)
  touch "runs/${mode:-paper}/STOP"
  systemctl stop stonkflyrh-worker
  rm -f "runs/${mode:-paper}/STOP"
  systemctl start stonkflyrh-worker
  echo "worker restarted; if the trading code changed it will ask for a new run directory"
fi

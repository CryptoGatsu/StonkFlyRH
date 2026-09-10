#!/usr/bin/env bash
# One-shot installer for Ubuntu 22.04/24.04. Run as root on a 16 GB machine:
#   curl -fsSL https://raw.githubusercontent.com/CryptoGatsu/StonkFlyRH/main/deploy/install.sh | sudo bash
# Then edit /opt/stonkflyrh/.env and:  sudo systemctl start stonkflyrh-worker
set -euo pipefail

REPO="${STONKFLYRH_REPO:-https://github.com/CryptoGatsu/StonkFlyRH}"
BRANCH="${STONKFLYRH_BRANCH:-main}"
HOME_DIR=/opt/stonkflyrh

echo "==> packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq build-essential git nginx curl >/dev/null
# Any Python 3.11+ the OS ships (24.04 has 3.12); fall back to deadsnakes 3.11.
PY=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 \
     && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    PY="$candidate"; break
  fi
done
if [ -z "$PY" ]; then
  apt-get install -y -qq software-properties-common >/dev/null
  add-apt-repository -y ppa:deadsnakes/ppa >/dev/null
  apt-get update -qq
  PY=python3.11
fi
PKG="$PY"; [ "$PY" = python3 ] && PKG=python3
apt-get install -y -qq "${PKG}" "${PKG}-venv" "${PKG}-dev" >/dev/null
echo "    using $("$PY" --version)"

echo "==> swap"
# The connectome's build step spikes above 8 GB. A machine under 12 GB gets an
# 8 GB swapfile so `prepare` finishes instead of being killed.
total_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
if [ "$total_kb" -lt 12000000 ] && [ ! -f /swapfile ]; then
  fallocate -l 8G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -q vm.swappiness=10 && echo 'vm.swappiness=10' > /etc/sysctl.d/90-stonkflyrh.conf
  echo "    8 GB swapfile added (machine has $((total_kb / 1024 / 1024)) GB RAM)"
fi

echo "==> user and checkout"
id -u stonkfly >/dev/null 2>&1 || adduser --system --group --home "$HOME_DIR" stonkfly
if [ ! -d "$HOME_DIR/.git" ]; then
  sudo -u stonkfly git clone --branch "$BRANCH" "$REPO" "$HOME_DIR"
else
  sudo -u stonkfly git -C "$HOME_DIR" pull --ff-only
fi
cd "$HOME_DIR"

echo "==> python"
[ -d .venv ] || sudo -u stonkfly "$PY" -m venv .venv
sudo -u stonkfly .venv/bin/pip install -q --upgrade pip
sudo -u stonkfly .venv/bin/pip install -q -e '.[test]'

echo "==> config"
[ -f .env ] || { sudo -u stonkfly cp .env.example .env; chmod 600 .env; }
[ -f tokens.json ] || sudo -u stonkfly cp tokens.example.json tokens.json
sudo -u stonkfly mkdir -p runs keystore data
chmod 700 keystore

echo "==> services"
cp deploy/stonkflyrh-worker.service deploy/stonkflyrh-site.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now stonkflyrh-site
systemctl enable stonkflyrh-worker

echo "==> tests"
sudo -u stonkfly OPENBLAS_NUM_THREADS=1 .venv/bin/python -m pytest -q

cat <<MSG

Installed. Two things left, both in $HOME_DIR/.env:

  1. STONKFLYRH_MODE      paper to start; live when you mean it
  2. STONKFLYRH_PRIVATE_KEY   the fly wallet key, for the first live start only
     (start imports it into keystore/ and tells you to delete the line)

Then:  sudo systemctl start stonkflyrh-worker
       journalctl -fu stonkflyrh-worker
Site:  http://127.0.0.1:8787  (deploy/nginx.conf to make it public)

The first start downloads the 1.1 GB connectome and compiles the kernel.
MSG

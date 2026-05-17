#!/usr/bin/env bash
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
pyinstaller --clean --noconfirm relay.spec

OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
case "$OS" in
  darwin) OS="darwin" ;;
  linux) OS="linux" ;;
  msys*|mingw*|cygwin*) OS="windows" ;;
esac
case "$ARCH" in
  x86_64|amd64) ARCH="amd64" ;;
  arm64|aarch64) ARCH="arm64" ;;
esac

mkdir -p dist/release
if [ "$OS" = "windows" ]; then
  cp dist/relay.exe "dist/release/relay-${OS}-${ARCH}.exe"
else
  cp dist/relay "dist/release/relay-${OS}-${ARCH}"
fi

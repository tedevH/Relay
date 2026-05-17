#!/usr/bin/env bash
set -euo pipefail

OWNER="${RELAY_GITHUB_OWNER:-tedevH}"
REPO="${RELAY_GITHUB_REPO:-Relay}"
INSTALL_DIR="${RELAY_INSTALL_DIR:-$HOME/.local/bin}"
BINARY_NAME="relay"

say() {
  printf '%s\n' "$*"
}

fail() {
  say "Relay install failed: $*" >&2
  exit 1
}

need() {
  command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

sha256() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    fail "missing checksum command: install shasum or sha256sum"
  fi
}

detect_platform() {
  local os arch
  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  arch="$(uname -m)"

  case "$os" in
    darwin) os="darwin" ;;
    linux) os="linux" ;;
    msys*|mingw*|cygwin*) os="windows" ;;
    *) fail "unsupported OS: $os" ;;
  esac

  case "$arch" in
    x86_64|amd64) arch="amd64" ;;
    arm64|aarch64) arch="arm64" ;;
    *) fail "unsupported CPU architecture: $arch" ;;
  esac

  if [ "$os" = "windows" ]; then
    BINARY_NAME="relay.exe"
    printf 'relay-%s-%s.exe' "$os" "$arch"
  else
    printf 'relay-%s-%s' "$os" "$arch"
  fi
}

download() {
  local url="$1"
  local dest="$2"
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    curl -fL --retry 3 -H "Authorization: Bearer $GITHUB_TOKEN" "$url" -o "$dest"
  else
    curl -fL --retry 3 "$url" -o "$dest"
  fi
}

main() {
  need curl
  need uname
  need chmod
  need mkdir

  local asset tmp checksums actual expected url latest_url checksum_url target
  asset="$(detect_platform)"
  tmp="$(mktemp -t relay.XXXXXX)"
  checksums="$(mktemp -t relay-checksums.XXXXXX)"
  latest_url="https://github.com/${OWNER}/${REPO}/releases/latest/download/${asset}"
  checksum_url="https://github.com/${OWNER}/${REPO}/releases/latest/download/SHA256SUMS"
  url="${RELAY_DOWNLOAD_URL:-$latest_url}"

  say "Installing Relay from:"
  say "  $url"
  download "$url" "$tmp" || fail "could not download ${asset}. Publish a GitHub Release first, or set GITHUB_TOKEN for private repos."

  if [ -z "${RELAY_SKIP_CHECKSUM:-}" ]; then
    download "${RELAY_CHECKSUM_URL:-$checksum_url}" "$checksums" || fail "could not download SHA256SUMS"
    expected="$(awk -v file="$asset" '$2 == file {print $1}' "$checksums")"
    [ -n "$expected" ] || fail "SHA256SUMS does not contain ${asset}"
    actual="$(sha256 "$tmp")"
    [ "$actual" = "$expected" ] || fail "checksum mismatch for ${asset}"
  fi

  mkdir -p "$INSTALL_DIR"
  target="$INSTALL_DIR/$BINARY_NAME"
  chmod +x "$tmp"
  mv "$tmp" "$target"

  say "Relay installed to $target"
  case ":$PATH:" in
    *":$INSTALL_DIR:"*) ;;
    *)
      say ""
      say "Add Relay to your PATH:"
      say "  export PATH=\"$INSTALL_DIR:\$PATH\""
      ;;
  esac

  say ""
  "$target" --version || true
  say "Run 'relay doctor' inside any terminal to check your setup."
}

main "$@"

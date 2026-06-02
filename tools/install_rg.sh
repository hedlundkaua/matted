#!/usr/bin/env bash
set -euo pipefail

VERSION="${MATTED_RG_VERSION:-14.1.1}"
INSTALL_DIR="${MATTED_TOOLS_DIR:-$HOME/.local/bin}"

case "$(uname -m)" in
  x86_64|amd64)
    TARGET="x86_64-unknown-linux-musl"
    ;;
  aarch64|arm64)
    TARGET="aarch64-unknown-linux-gnu"
    ;;
  *)
    echo "Arquitetura nao suportada para instalacao automatica: $(uname -m)" >&2
    exit 1
    ;;
esac

ARCHIVE="ripgrep-${VERSION}-${TARGET}.tar.gz"
URL="https://github.com/BurntSushi/ripgrep/releases/download/${VERSION}/${ARCHIVE}"
WORK_DIR="${TMPDIR:-/tmp}/matted-rg-${VERSION}-${TARGET}"

mkdir -p "$WORK_DIR" "$INSTALL_DIR"

if command -v curl >/dev/null 2>&1; then
  curl -L "$URL" -o "$WORK_DIR/$ARCHIVE"
elif command -v wget >/dev/null 2>&1; then
  wget -O "$WORK_DIR/$ARCHIVE" "$URL"
else
  echo "curl ou wget e necessario para baixar ripgrep." >&2
  exit 1
fi

tar -xzf "$WORK_DIR/$ARCHIVE" -C "$WORK_DIR"
install -m 755 "$WORK_DIR/ripgrep-${VERSION}-${TARGET}/rg" "$INSTALL_DIR/rg"

echo "rg instalado em: $INSTALL_DIR/rg"
"$INSTALL_DIR/rg" --version | head -n 1

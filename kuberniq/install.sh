#!/usr/bin/env bash
# =============================================================================
# kuberniq installer
# =============================================================================
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/oluwaTG/kuberniq/main/install.sh | bash
#
# To install a specific version:
#   curl -fsSL .../install.sh | KUBEAI_VERSION=kuberniq/v1.0.0 bash
# =============================================================================
set -euo pipefail

REPO="oluwaTG/kuberniq"
BINARY="kuberniq"
INSTALL_DIR="${KUBEAI_INSTALL_DIR:-/usr/local/bin}"

# ── Detect OS and architecture ────────────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS" in
  Darwin) OS_KEY="macos"  ;;
  Linux)  OS_KEY="linux"  ;;
  *)
    echo "❌  Unsupported OS: $OS"
    echo "    Download manually from: https://github.com/$REPO/releases"
    exit 1
    ;;
esac

case "$ARCH" in
  arm64|aarch64) ARCH_KEY="arm64" ;;
  x86_64)        ARCH_KEY="x64"   ;;
  *)
    echo "❌  Unsupported architecture: $ARCH"
    echo "    Download manually from: https://github.com/$REPO/releases"
    exit 1
    ;;
esac

RID="${OS_KEY}-${ARCH_KEY}"

# ── Resolve version ───────────────────────────────────────────────────────────
if [[ -z "${KUBEAI_VERSION:-}" ]]; then
  echo "Fetching latest kuberniq release..."
  KUBEAI_VERSION=$(
    curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
    | grep '"tag_name"' \
    | sed 's/.*"tag_name": "\(.*\)".*/\1/'
  )
  if [[ -z "$KUBEAI_VERSION" ]]; then
    echo "❌  Could not determine the latest release tag."
    echo "    Set KUBEAI_VERSION manually, e.g.:"
    echo "    KUBEAI_VERSION=kuberniq/v1.0.0 bash install.sh"
    exit 1
  fi
fi

echo "Installing kuberniq ${KUBEAI_VERSION} for ${RID}..."

# ── Download ──────────────────────────────────────────────────────────────────
DOWNLOAD_URL="https://github.com/${REPO}/releases/download/${KUBEAI_VERSION}/kuberniq-${RID}"
TMP_FILE="$(mktemp)"

if ! curl -fsSL "$DOWNLOAD_URL" -o "$TMP_FILE"; then
  echo "❌  Download failed: $DOWNLOAD_URL"
  echo "    Check https://github.com/$REPO/releases for available versions."
  rm -f "$TMP_FILE"
  exit 1
fi

chmod +x "$TMP_FILE"

# ── Install ───────────────────────────────────────────────────────────────────
if [[ -w "$INSTALL_DIR" ]]; then
  mv "$TMP_FILE" "$INSTALL_DIR/$BINARY"
else
  echo "    $INSTALL_DIR requires sudo — you may be prompted for your password."
  sudo mv "$TMP_FILE" "$INSTALL_DIR/$BINARY"
fi

# ── Verify ────────────────────────────────────────────────────────────────────
echo ""
echo "✅  kuberniq installed to $INSTALL_DIR/$BINARY"
echo ""
kuberniq --version
echo ""
echo "Get started:"
echo "  kuberniq login <mcp-server-url>"
echo "  kuberniq cluster add <name>"

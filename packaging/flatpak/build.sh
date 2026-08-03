#!/usr/bin/env bash
# Build the single-file Linux installer: dist/Niksnaks-Hosting-<version>.flatpak
#
# Must run on Linux (or WSL) with flatpak and flatpak-builder installed:
#   Debian/Ubuntu: sudo apt install flatpak flatpak-builder
#   Fedora:        sudo dnf install flatpak flatpak-builder
#
# Install the result with:
#   flatpak install --user ./dist/Niksnaks-Hosting-<version>.flatpak
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"

APP_ID="com.niksnakshosting.NiksnaksHosting"
MANIFEST="packaging/flatpak/$APP_ID.yml"
VERSION="$(python3 -c 'exec(open("niksnaks_hosting/version.py").read()); print(__version__)')"
BUNDLE="dist/Niksnaks-Hosting-$VERSION.flatpak"

command -v flatpak >/dev/null || { echo "flatpak is not installed."; exit 1; }
command -v flatpak-builder >/dev/null || { echo "flatpak-builder is not installed."; exit 1; }

echo "==> Ensuring Flathub remote and GNOME runtime"
flatpak remote-add --if-not-exists --user flathub https://dl.flathub.org/repo/flathub.flatpakrepo

echo "==> Building $APP_ID $VERSION"
mkdir -p dist
flatpak-builder \
  --user \
  --force-clean \
  --install-deps-from=flathub \
  --repo=build/flatpak-repo \
  build/flatpak-build \
  "$MANIFEST"

echo "==> Packing bundle"
flatpak build-bundle build/flatpak-repo "$BUNDLE" "$APP_ID"

echo
echo "Done: $BUNDLE"
echo "Install with: flatpak install --user ./$BUNDLE"

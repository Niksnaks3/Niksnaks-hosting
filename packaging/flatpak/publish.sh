#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"

APP_ID="com.niksnakshosting.NiksnaksHosting"
MANIFEST="packaging/flatpak/$APP_ID.yml"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' niksnaks_hosting/version.py)"
REPO="${REPO:-build/flatpak-repo}"
REPO_URL="${REPO_URL:?Set REPO_URL to the address the Flatpak repo is served from}"
BUNDLE="dist/Niksnaks-Hosting-$VERSION.flatpak"
FLATHUB="https://dl.flathub.org/repo/flathub.flatpakrepo"

command -v flatpak >/dev/null || { echo "flatpak is not installed."; exit 1; }
command -v flatpak-builder >/dev/null || { echo "flatpak-builder is not installed."; exit 1; }

sign=()
keys=()
if [ -n "${GPG_KEY_ID:-}" ]; then
  sign=(--gpg-sign="$GPG_KEY_ID")
  if [ -n "${GPG_HOMEDIR:-}" ]; then
    sign+=(--gpg-homedir="$GPG_HOMEDIR")
  fi
  mkdir -p build
  gpg ${GPG_HOMEDIR:+--homedir "$GPG_HOMEDIR"} --export "$GPG_KEY_ID" > build/flatpak-key.gpg
  keys=(--gpg-keys=build/flatpak-key.gpg)
fi

if [ -f "$REPO/config" ]; then
  mkdir -p "$REPO/refs/heads" "$REPO/refs/remotes" "$REPO/refs/mirrors" "$REPO/tmp" "$REPO/state" "$REPO/extensions"
fi

echo "==> Ensuring Flathub remote and GNOME runtime"
flatpak remote-add --if-not-exists --user flathub "$FLATHUB"

echo "==> Building $APP_ID $VERSION into $REPO"
mkdir -p dist
flatpak-builder \
  --user \
  --force-clean \
  --disable-rofiles-fuse \
  --install-deps-from=flathub \
  --repo="$REPO" \
  "${sign[@]}" \
  build/flatpak-build \
  "$MANIFEST"

echo "==> Updating repo summary and deltas"
flatpak build-update-repo \
  "${sign[@]}" \
  --title="Niksnaks-Hosting" \
  --generate-static-deltas \
  --prune \
  --prune-depth=5 \
  "$REPO"

echo "==> Packing bundle that updates from $REPO_URL"
flatpak build-bundle \
  "${keys[@]}" \
  --repo-url="$REPO_URL" \
  --runtime-repo="$FLATHUB" \
  "$REPO" \
  "$BUNDLE" \
  "$APP_ID"

echo
echo "Done: $BUNDLE"

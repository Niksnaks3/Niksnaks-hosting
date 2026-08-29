#!/usr/bin/env bash
# Build the app into the local repo and update the installed copy in place.
#
# The counterpart to build.sh: that one packs a bundle to hand to somebody else, this
# one is the loop you run while working. It skips the bundle and lets ostree send only
# what actually changed, which for a Python edit is close to nothing.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"

APP_ID="com.niksnakshosting.NiksnaksHosting"
MANIFEST="packaging/flatpak/$APP_ID.yml"
REPO="$PROJECT_ROOT/build/flatpak-repo"
REMOTE="niksnaks-local"

command -v flatpak >/dev/null || { echo "flatpak is not installed."; exit 1; }
command -v flatpak-builder >/dev/null || { echo "flatpak-builder is not installed."; exit 1; }

echo "==> Ensuring Flathub remote and GNOME runtime"
flatpak remote-add --if-not-exists --user flathub https://dl.flathub.org/repo/flathub.flatpakrepo

echo "==> Building into $REPO"
flatpak-builder \
  --user \
  --force-clean \
  --install-deps-from=flathub \
  --repo="$REPO" \
  build/flatpak-build \
  "$MANIFEST"

# An unsigned repo sitting in the working tree, so no GPG and no enumerating it as if
# it were somewhere to browse for apps.
flatpak remote-add --user --if-not-exists --no-gpg-verify --no-enumerate "$REMOTE" "$REPO"
flatpak remote-modify --user --no-gpg-verify --url="file://$REPO" "$REMOTE"

if flatpak info --user "$APP_ID" >/dev/null 2>&1; then
  origin="$(flatpak info --user --show-origin "$APP_ID")"
  if [ "$origin" != "$REMOTE" ]; then
    # Installed from a bundle, whose origin has no URL and so can never update.
    # Point it at the repo just built rather than making them reinstall.
    echo "==> Repointing origin '$origin' at the local repo"
    flatpak remote-modify --user --no-gpg-verify --url="file://$REPO" "$origin"
  fi
  echo "==> Updating the installed app"
  flatpak update --user -y "$APP_ID"
else
  echo "==> Not installed yet, installing"
  flatpak install --user -y "$REMOTE" "$APP_ID"
fi

echo
echo "Done. Run it with: flatpak run $APP_ID"

#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"

PREFIX="${MSYS_PREFIX:-/ucrt64}"
ICO="build/Niksnaks-Hosting.ico"

if [[ -z "${ISCC:-}" ]]; then
  for candidate in \
    "/c/Program Files (x86)/Inno Setup 6/ISCC.exe" \
    "/c/Program Files/Inno Setup 6/ISCC.exe" \
    "$(cygpath -u "${LOCALAPPDATA:-C:/}")/Programs/Inno Setup 6/ISCC.exe"
  do
    [[ -x "$candidate" ]] && ISCC="$candidate" && break
  done
fi
ISCC="${ISCC:-/c/Program Files (x86)/Inno Setup 6/ISCC.exe}"

echo "==> Checking build environment"
python -c "import gi; gi.require_version('Gtk', '4.0'); gi.require_version('Adw', '1'); from gi.repository import Gtk, Adw" \
  || { echo "GTK4/libadwaita/PyGObject not available in this Python. Use the MSYS2 UCRT64 shell."; exit 1; }
python -m PyInstaller --version >/dev/null \
  || { echo "PyInstaller missing: python -m pip install pyinstaller"; exit 1; }

echo "==> Rendering application icon"
mkdir -p build
python packaging/windows/make_icon.py packaging/linux/com.niksnakshosting.NiksnaksHosting.svg "$ICO"

echo "==> Compiling translations"
rm -rf build/share/locale
while read -r lang; do
  [[ -z "$lang" || "$lang" == \#* ]] && continue
  mkdir -p "build/share/locale/$lang/LC_MESSAGES"
  msgfmt "po/$lang.po" -o "build/share/locale/$lang/LC_MESSAGES/niksnaks-hosting.mo"
done < po/LINGUAS

echo "==> Building app bundle with PyInstaller"
MSYS_PREFIX="$(cygpath -m "$PREFIX")" python -m PyInstaller packaging/windows/niksnaks-hosting.spec --noconfirm --clean

echo "==> Compiling GSettings schemas in the bundle"
glib-compile-schemas dist/Niksnaks-Hosting/share/glib-2.0/schemas >/dev/null 2>&1 || true

if [[ -x "$ISCC" ]]; then
  echo "==> Building installer with Inno Setup"
  "$ISCC" "$(cygpath -w packaging/windows/niksnaks-hosting.iss)"
else
  echo "!! Inno Setup not found at: $ISCC"
  echo "   Install it (winget install JRSoftware.InnoSetup) or set ISCC=<path to ISCC.exe>."
  exit 1
fi

echo
echo "Done:"
ls -1 dist/*.exe

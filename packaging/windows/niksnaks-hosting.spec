# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Windows build of Niksnaks-Hosting.

Must be run from the MSYS2 UCRT64 environment, where GTK4, libadwaita and
PyGObject live under /ucrt64. Everything the app needs at runtime (typelibs,
GSettings schemas, pixbuf loaders, icon themes) is bundled next to the exe.

    pyinstaller packaging/windows/niksnaks-hosting.spec --noconfirm
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

PROJECT = Path(SPECPATH).resolve().parents[1]
PREFIX = Path(os.environ.get("MSYS_PREFIX", "C:/msys64/ucrt64"))

# Read straight from version.py rather than duplicating the number here, the same
# way the .iss installer script does -- one source of truth for the version that
# actually ships, instead of a metadata field that quietly drifts from it.
_version_line = (PROJECT / "niksnaks_hosting" / "version.py").read_text(encoding="utf-8")
APP_VERSION = _version_line.split('"')[1]
_version_tuple = tuple(int(part) for part in APP_VERSION.split(".")) + (0, 0, 0, 0)
_version_tuple = _version_tuple[:4]

# Without this, Windows' file Properties -> Details tab is blank: no company, no
# description, no product version -- just whatever PyInstaller leaves default.
VERSION_INFO = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=_version_tuple,
        prodvers=_version_tuple,
        mask=0x3F,
        flags=0x0,
        OS=0x40004,  # VOS_NT_WINDOWS32
        fileType=0x1,  # VFT_APP
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",
                    [
                        StringStruct("CompanyName", "niksnaks3"),
                        StringStruct("FileDescription", "Niksnaks-Hosting"),
                        StringStruct("FileVersion", APP_VERSION),
                        StringStruct("InternalName", "Niksnaks-Hosting"),
                        StringStruct("LegalCopyright", "Copyright (C) niksnaks3. Licensed under GPL-3.0-or-later."),
                        StringStruct("OriginalFilename", "Niksnaks-Hosting.exe"),
                        StringStruct("ProductName", "Niksnaks-Hosting"),
                        StringStruct("ProductVersion", APP_VERSION),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

# Console builds are useful when debugging startup failures.
CONSOLE = os.environ.get("NIKSNAKS_CONSOLE") == "1"

binaries = []
datas = []
# PyGObject loads gi.overrides.* through its own dynamic importer, so PyInstaller
# never sees the imports and leaves them out. Without them the frozen app is
# missing API that exists when running from source -- Gtk.TextBuffer.create_tag,
# the GLib.Variant helpers, and so on -- which fails at runtime, not at build time.
hiddenimports = (
    collect_submodules("niksnaks_hosting")
    + collect_submodules("gi.overrides")
    + [
        "gi",
        # Bridges pycairo and PyGObject by registering the foreign-struct
        # converter for cairo.Context. Nothing imports it by name, so without
        # this every Gtk.DrawingArea draw callback fails and renders blank.
        "gi._gi_cairo",
        "cairo",
        "PIL._imaging",
    ]
    # paramiko loads its key and cipher backends by name at runtime, so PyInstaller
    # cannot see them from the imports alone and SSH would fail only once frozen.
    + collect_submodules("paramiko")
    + collect_submodules("cryptography")
    + ["nacl", "bcrypt", "cffi", "_cffi_backend"]
)

# --- GTK / GObject shared libraries -----------------------------------------
# PyInstaller follows the PE imports of each DLL listed here, so naming the
# top-level libraries pulls in the rest of the GTK stack transitively.
for name in (
    "libgtk-4-1.dll",
    "libadwaita-1-0.dll",
    "libgdk_pixbuf-2.0-0.dll",
    "librsvg-2-2.dll",
    "libgirepository-1.0-1.dll",
    "libgirepository-2.0-0.dll",
    "libpangowin32-1.0-0.dll",
):
    dll = PREFIX / "bin" / name
    if dll.exists():
        binaries.append((str(dll), "."))

# gdk-pixbuf image loaders (PNG/JPEG/SVG) plus their cache.
pixbuf_root = PREFIX / "lib" / "gdk-pixbuf-2.0" / "2.10.0"
if (pixbuf_root / "loaders").exists():
    for loader in (pixbuf_root / "loaders").glob("*.dll"):
        binaries.append((str(loader), "lib/gdk-pixbuf-2.0/2.10.0/loaders"))
if (pixbuf_root / "loaders.cache").exists():
    datas.append((str(pixbuf_root / "loaders.cache"), "lib/gdk-pixbuf-2.0/2.10.0"))

# GIO modules (TLS support for glib-side networking).
gio_modules = PREFIX / "lib" / "gio" / "modules"
if gio_modules.exists():
    for module in gio_modules.glob("*.dll"):
        binaries.append((str(module), "lib/gio/modules"))

# --- Typelibs ----------------------------------------------------------------
typelib_dir = PREFIX / "lib" / "girepository-1.0"
for typelib in typelib_dir.glob("*.typelib"):
    datas.append((str(typelib), "lib/girepository-1.0"))

# --- Shared data -------------------------------------------------------------
schemas = PREFIX / "share" / "glib-2.0" / "schemas" / "gschemas.compiled"
if schemas.exists():
    datas.append((str(schemas), "share/glib-2.0/schemas"))

for theme in ("Adwaita", "hicolor"):
    theme_dir = PREFIX / "share" / "icons" / theme
    if theme_dir.exists():
        datas.append((str(theme_dir), f"share/icons/{theme}"))

# --- Application resources ---------------------------------------------------
datas.append((str(PROJECT / "niksnaks_hosting" / "gtk_ui" / "style.css"), "niksnaks_hosting/gtk_ui"))

# App/symbolic icons: the dev-layout path (parents[3]/packaging/linux) is kept so
# the same lookups work frozen, and a hicolor copy makes them theme-visible.
for svg in (PROJECT / "packaging" / "linux").glob("*.svg"):
    datas.append((str(svg), "packaging/linux"))
    datas.append((str(svg), "share/icons/hicolor/scalable/apps"))

# Compiled translations, if the build script produced them.
locale_root = PROJECT / "build" / "share" / "locale"
for mo in locale_root.rglob("*.mo") if locale_root.exists() else []:
    dest = mo.parent.relative_to(PROJECT / "build").as_posix()
    datas.append((str(mo), dest))

icon_file = PROJECT / "build" / "Niksnaks-Hosting.ico"

a = Analysis(
    [str(PROJECT / "niksnaks_hosting.py")],
    pathex=[str(PROJECT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc_data"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Niksnaks-Hosting",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=CONSOLE,
    icon=str(icon_file) if icon_file.exists() else None,
    contents_directory=".",
    version=VERSION_INFO,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Niksnaks-Hosting",
)

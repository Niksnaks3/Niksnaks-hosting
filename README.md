<p align="center">
  <img src="packaging/linux/com.niksnakshosting.NiksnaksHosting.svg" alt="Niksnaks-Hosting icon" width="128" />
</p>

<h1 align="center">Niksnaks-Hosting</h1>

<p align="center">
  Host Minecraft servers
  <br><br>
  <a href="https://flathub.org/en/apps/com.niksnakshosting.NiksnaksHosting"><img src="https://img.shields.io/flathub/downloads/com.niksnakshosting.NiksnaksHosting?label=Flathub%20Downloads&color=brightgreen"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0--or--later-blue"></a>
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux-blue">
</p>

Niksnaks-Hosting is a desktop app for creating, running, and managing Minecraft Fabric servers with a clean, native-style UI.

It keeps the full server workflow in one app: setup, start/stop, monitoring, mod management, backups, and player access controls.

## Why Niksnaks-Hosting?


- Easy to use: set up and run a Fabric server without juggling scripts, terminals, or scattered tools.
- Auto-downloads dependencies: Niksnaks-Hosting fetches what your server needs, including Java when it is missing.
- All in the app: setup, start/stop, live monitoring, mod management, backups, and access controls in one place.
- Built for real hosting: stream logs, send commands, tweak settings, and manage worlds without leaving Niksnaks-Hosting.
- Less manual maintenance: practical backup and restore tools help you recover quickly when something goes wrong.


## Run Niksnaks-Hosting
[![Download on Flathub](https://flathub.org/api/badge?svg&locale=en)](https://flathub.org/en/apps/com.niksnakshosting.NiksnaksHosting)

- Linux: use the Flatpak release from [Flathub](https://flathub.org/en/apps/com.niksnakshosting.NiksnaksHosting).
- Windows: Download the Windows installer.

<details>
<summary>Run from source (Python)</summary>

### Linux

1. Install GTK4/libadwaita and PyGObject system packages.
2. Install Python dependencies:

```bash
python3 -m pip install requests psutil Pillow
```

3. Run Niksnaks-Hosting:

```bash
python3 niksnaks_hosting.py
```

### Windows

The Windows build uses the same GTK/libadwaita UI as Linux, so it needs a Python environment that already has PyGObject (`gi`), GTK4, and libadwaita.

Recommended MSYS2 UCRT64 setup:

```bash
pacman -Suy
pacman -S mingw-w64-ucrt-x86_64-python mingw-w64-ucrt-x86_64-python-gobject mingw-w64-ucrt-x86_64-python-requests mingw-w64-ucrt-x86_64-python-psutil mingw-w64-ucrt-x86_64-python-pillow mingw-w64-ucrt-x86_64-gtk4 mingw-w64-ucrt-x86_64-libadwaita
```

Then run from the MSYS2 UCRT64 shell:

```bash
python niksnaks_hosting.py
```

Conda is also supported if the environment includes `pygobject` and `gtk4`.

</details>

## Demo




## Screenshots

<p align="center">
	<img src="packaging/linux/screenshots/console.png" alt="Console view" width="900" />
</p>

- Console: stream logs and send commands.

<p align="center">
	<img src="packaging/linux/screenshots/performance.png" alt="Performance view" width="900" />
</p>

- Performance: monitor live server performance.

<p align="center">
	<img src="packaging/linux/screenshots/properties.png" alt="Properties view" width="900" />
</p>

- Properties: edit server settings with autosave.

<p align="center">
	<img src="packaging/linux/screenshots/files.png" alt="Files and worlds view" width="900" />
</p>

- Files: manage worlds, dimensions, and backups.

<p align="center">
	<img src="packaging/linux/screenshots/mods.png" alt="Mods view" width="900" />
</p>

- Mods: browse Modrinth, install mods, and auto-manage dependencies.

<p align="center">
	<img src="packaging/linux/screenshots/connect.png" alt="Connect view" width="900" />
</p>

- Connect: configure Playit and manage whitelist/ban lists.

<p align="center">
	<img src="packaging/linux/screenshots/backups.png" alt="Backups view" width="900" />
</p>

- Backups: create and restore world backups.

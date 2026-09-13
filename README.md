<div align="center">

<img src="packaging/linux/com.niksnakshosting.NiksnaksHosting.svg" width="120" alt="Niksnaks-Hosting icon" />

# Niksnaks-Hosting

### Host your own Minecraft server. No command line required.

Create a Minecraft server in a few clicks — Java or Bedrock, any version — and let the app
handle the downloads, the Java runtime, the mods, and the backups.

[![License](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.3.2-brightgreen.svg)](https://github.com/Niksnaks3/Niksnaks-hosting/releases)
[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Windows-lightgrey.svg)](#-installation)
[![Python](https://img.shields.io/badge/python-3.13%2B-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![GTK4](https://img.shields.io/badge/GTK-4%20%2F%20libadwaita-4A86CF.svg?logo=gnome&logoColor=white)](https://gtk.org/)
[![Built with Claude Code](https://img.shields.io/badge/built%20with-Claude%20Code-D97757.svg)](#-development)

</div>

---

## ✨ Why Niksnaks-Hosting?

Running a Minecraft server normally means hunting down the right server `.jar`, installing
a matching Java version, editing config files by hand, and remembering which port you used.
Niksnaks-Hosting does all of that for you behind a native GNOME-style interface — and keeps
going once the server is up, with a live console, mod browsers, backups, and performance
graphs in the same window.

## 📸 Screenshots

<div align="center">

| Console & logs | Install mods | Server properties |
| :---: | :---: | :---: |
| <img src="packaging/linux/screenshots/console.png" width="260" alt="Server console and logs" /> | <img src="packaging/linux/screenshots/mods.png" width="260" alt="Install and manage mods" /> | <img src="packaging/linux/screenshots/properties.png" width="260" alt="Edit server properties" /> |

| Performance | Backups | Connect |
| :---: | :---: | :---: |
| <img src="packaging/linux/screenshots/performance.png" width="260" alt="Monitor server performance" /> | <img src="packaging/linux/screenshots/backups.png" width="260" alt="Create and restore backups" /> | <img src="packaging/linux/screenshots/connect.png" width="260" alt="Share your server" /> |

</div>

## 🚀 Features

### Getting a server running

- **One-click setup** — the app downloads the server files and the matching Java runtime for
  you. Nothing to install by hand.
- **Java or Bedrock** — pick the edition when you create the server. Bedrock uses Mojang's
  Bedrock Dedicated Server and gets the same console, backups, allow list, and properties
  editor, tuned to Bedrock's own settings.
- **Version & loader picker** — choose a Minecraft version, then any **Fabric**, **Forge**, or
  **NeoForge** build published for it, with the recommended one preselected.
- **Choose your port** — every server gets its own port, so you can run several side by side.
  The app suggests the next free one and warns you before two servers collide.
- **Optional performance mods** — flip one switch at creation time to install compatible
  optimisation mods.
- **Start from a backup** — build a brand-new server straight out of a full backup, which
  doubles as a way to clone an existing one.
- **Run on another machine** — add a host over SSH right from the create-server wizard,
  Windows or Linux, and give its password once. The app installs a dedicated key so it
  never asks again, then installs Java and the loader on the host and runs the server
  there — as a systemd user service on Linux, or a Scheduled Task on Windows — and
  streams its console back. Servers keep running on the host after you close the app —
  quitting here never shuts down a machine over there — and are picked up again next time
  you open it.
- **Adopt an existing install** — if Niksnaks-Hosting is already managing servers on that
  host, the app asks it what those are as soon as you add the machine, and offers to
  bring them under this app's control too. Nothing has to be typed in by hand.
- **Either end can drive a server** — a server folder carries its own console, so a
  server started by the copy of Niksnaks-Hosting on the other computer still shows up
  here as running, still streams its log, still takes console commands, and still stops
  from here. It works the other way round too: a server this app started on another
  machine shows up as running in the app on that machine, whose console reads it and
  whose stop button stops it — even though the server belongs to no process that app
  can see. Server pictures live in the server folder as well, so both ends show the
  same one.
- **A server made on a host belongs to that host** — create one over SSH and it is put
  where the copy of Niksnaks-Hosting on that machine keeps its own, and written into
  that machine's server list, so it appears over there like any other server it runs.
  Rename it, give it a picture or delete it from here and the other end follows.
- **Removing a host cleans up after itself** — its servers drop out of the local list
  instead of being left stranded, without touching anything on the machine itself. A
  **Clear all hosts** button does the same for every host at once, revoking its login
  key where the machine is reachable.

### Mods, modpacks & worlds

- **Modrinth browser** — search, filter, and install mods, modpacks, and datapacks from
  inside the app, with required dependencies resolved and installed for you.
- **CurseForge modpacks** — install Fabric, Forge, and NeoForge packs from CurseForge in one
  click, preferring the author's server pack when one was published.
- **NeoForge tab** — its own browser for NeoForge packs, whatever loader the server runs. A
  pack never installs onto the wrong loader: the app asks first, and switches the server
  over only if you say so.
- **One-click updates** — check every installed mod, modpack, and datapack for newer builds
  and apply them together.
- **Progress you can see** — installs and restores show a real progress bar with per-file
  detail, so you always know how far along a long download is.
- **World management** — import existing worlds, browse world files, and read the seed.
- **Mod helpers** — Geyser, Floodgate, and Simple Voice Chat get configured automatically
  when you install them.

### Running & sharing

- **Live console** — watch server output as it happens and send commands straight back.
- **Performance monitoring** — CPU, memory, and uptime at a glance.
- **Properties editor** — edit `server.properties` through a proper form instead of a text
  file, with comments and unknown keys preserved.
- **Players** — manage the whitelist and ban list (Bedrock's allow list included).
- **Playit.gg tunnels** — share your server with friends without touching your router.
- **Backups** — world-only or full-server backups, restore from any of them, and optional
  auto-cleanup of ones older than 30 days.
- **Safe quit** — closing the app stops every server running **on that computer**, waits
  for each world to finish saving, and takes the auto backup before it exits. No more
  killing a server mid-write.
- **Quitting never touches another machine** — a server you run on a host over SSH keeps
  going when you close the app, exactly as it does when you close the lid. That is the
  whole point of putting it there. It is the copy of Niksnaks-Hosting **on the host** that
  saves and backs it up, when that machine's app is closed — and it does so for every
  server running there, including the ones you started on it from over here.
- **Per-server Java control** — pick a Java version and set custom JVM arguments.
- **Reset the app** — **Settings → Reset** puts this install back to the day you got
  it: every server on this computer goes, with its world, its mods and all of its
  backups, and so do the settings, the downloaded Java runtimes and the cache. It is
  permanent, there is no undo, and the button says so in red before you touch it.
  Servers on a host over SSH are **not** deleted — they keep running exactly as they
  are, nothing on those machines is touched, and the app just stops keeping track.
  Hosts are forgotten too and the app's login key is taken back off each one it can
  still reach, so add a machine again later and its servers come back.

### Polish

- **Server icons** — give each server its own picture.
- **Keyboard shortcuts** — listed in the in-app **Shortcuts** dialog.
- **Translations** — ships with English and Polski (Polish). More are very welcome; see
  [CONTRIBUTING.md](CONTRIBUTING.md).

## 📦 Installation

### Windows

Grab the installer from the [Releases page](https://github.com/Niksnaks3/Niksnaks-hosting/releases)
and run it:

```
Niksnaks-Hosting-<version>-Setup.exe
```

<details>
<summary>Building the Windows installer yourself</summary>

Windows builds are frozen with **PyInstaller** and packaged with **Inno Setup 6**. GTK4,
libadwaita, and PyGObject must all come from the same Python environment — the **MSYS2
UCRT64** shell is the supported one.

```bash
./packaging/windows/build.sh
```

The finished installer lands in `dist/`.

</details>

### Linux — Flatpak (recommended)

```bash
./packaging/flatpak/build.sh
flatpak install --user dist/Niksnaks-Hosting-*.flatpak
```

This pulls in the GNOME runtime and the required Python modules, then produces a bundle in
`dist/`.

<details>
<summary>Linux — build from source with Meson</summary>

```bash
meson setup build
ninja -C build
meson install -C build      # installs to the configured prefix
```

That installs the `niksnaks-hosting` command, the desktop entry, icons, metainfo, and
compiled translations.

</details>

### Run from source (development)

```bash
pip install requests psutil pillow
python niksnaks_hosting.py
```

> [!NOTE]
> On Windows, use the Python that has GTK4, libadwaita, and PyGObject available — the MSYS2
> UCRT64 environment. The app auto-detects a bundled GTK/GObject runtime in frozen builds.

### Requirements

| | |
| --- | --- |
| **Python** | 3.13 or newer |
| **UI toolkit** | GTK 4, libadwaita, PyGObject |
| **Python packages** | `requests`, `psutil`, `Pillow` |
| **Java** | downloaded and managed by the app, per server |

## 🎮 Getting started

Launch **Niksnaks-Hosting** from your application menu, or run `niksnaks-hosting` in a
terminal.

1. Click **Create Server**.
2. Choose **Java** or **Bedrock**, then a Minecraft version and loader.
3. Pick a port, or accept the free one the app suggests.
4. Press **Create** — the app downloads everything and tells you when the server is ready.

From there: install mods from Modrinth or CurseForge, set up a Playit tunnel, watch the
console, edit properties, and take backups.

> [!TIP]
> **Bedrock is not Java.** Bedrock servers run Mojang's native Bedrock Dedicated Server, so
> there is no loader, no heap size, and no Modrinth mods — the app hides those options for
> you. Bedrock players connect over **UDP**, using the server's own port (19132 by default)
> plus the next port up for IPv6. The **Connect** view spells out the exact address.

## 🛠 Development

### Tooling

The project is checked with **Ruff** (line length 120, targeting `py313`) and **mypy**.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow and expectations before opening
a patch.

### Built with the help of Claude Code

Parts of this project were developed with [**Claude Code**](https://claude.com/claude-code),
Anthropic's agentic coding tool, used as a pair-programming assistant for implementing
features, profiling and optimising hot paths, and writing the test suites that verify them.
Every change was reviewed and tested before landing.

### Project layout

```
niksnaks_hosting.py        # Entry point; sets up the GTK environment for frozen builds
niksnaks_hosting/          # Application package
├── factory.py             # App factory
├── i18n.py                # gettext setup and language switching
├── version.py             # Single source of the version number
├── gtk_ui/                # GTK4/libadwaita UI
│   ├── dialogs/           # create_server, preferences, progress, icon picker, …
│   └── views/             # console, performance, properties, files, connect, sidebar
└── shared/
    ├── backend/           # server_manager, java_manager, modrinth_client,
    │                      # curseforge_client, download_manager, playit_manager,
    │                      # bedrock_manager, server_process, config_manager, …
    ├── remote/            # SSH hosts: key auth, Windows/Linux ops, RCON,
    │                      # remote install adoption
    └── core/              # event bus
po/                        # Translations (.pot, .po, LINGUAS, POTFILES)
packaging/                 # Flatpak, Linux (desktop/metainfo/icons), Windows
bin/                       # Installed launcher
meson.build                # Build definition
```

## 🌍 Translations

Niksnaks-Hosting uses Python's standard `gettext`; translatable strings are marked with
`_("...")` in the source. To add or update a language, generate the `.pot`, create or update
a `.po`, add your locale to `po/LINGUAS`, and submit it with your patch — the steps are in
[CONTRIBUTING.md](CONTRIBUTING.md).

## 🤝 Contributing

Contributions are welcome! Bug reports, translations, and patches all help. Please read
[CONTRIBUTING.md](CONTRIBUTING.md) first.

## 📄 License

Free software, licensed under **GPL-3.0-or-later**. See [LICENSE](LICENSE) for the full text.

## 💙 Acknowledgements

Developed by **[niksnaks3](https://github.com/Niksnaks3)**.

Thanks to [Modrinth](https://modrinth.com), [CurseForge](https://www.curseforge.com), and
[Playit.gg](https://playit.gg), whose APIs make key features possible — and to the GTK and
GNOME projects for the toolkit this is built on.

Minecraft is a trademark of Mojang Studios. This project is not affiliated with or endorsed
by Mojang Studios or Microsoft.

<div align="center">
<br />
<sub>Built for people who just want to play with their friends.</sub>
</div>

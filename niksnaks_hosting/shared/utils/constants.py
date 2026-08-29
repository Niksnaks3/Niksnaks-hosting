import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

from niksnaks_hosting.version import __version__

APP_ID = "com.niksnakshosting.NiksnaksHosting"
APP_NAME = "Niksnaks-Hosting"
APP_VERSION = __version__
APP_WEBSITE = ""

def _default_data_dir() -> Path:
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "Niksnaks-Hosting"
        return Path.home() / "AppData" / "Local" / "Niksnaks-Hosting"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Niksnaks-Hosting"

    return Path.home() / ".local" / "share" / "niksnaks-hosting"

_DATA_DIR_OVERRIDE = os.environ.get("NIKSNAKS_HOSTING_DATA_DIR")

DATA_DIR = Path(_DATA_DIR_OVERRIDE or _default_data_dir())
SERVERS_DIR = DATA_DIR / "servers"
JRES_DIR = DATA_DIR / "jres"
CACHE_DIR = DATA_DIR / "cache"
TEMP_DIR = DATA_DIR / "tmp"
CONFIG_FILE = DATA_DIR / "servers.json"

for d in [DATA_DIR, SERVERS_DIR, JRES_DIR, CACHE_DIR, TEMP_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Under Flatpak /tmp is a RAM-backed tmpfs of about half the machine's memory, which a
# modpack fills long before the disk does. Point child processes at the data folder too.
if sys.platform != "win32":
    os.environ["TMPDIR"] = str(TEMP_DIR)

if not _DATA_DIR_OVERRIDE:
    try:
        from niksnaks_hosting.shared.utils.migration import migrate_legacy_data

        migrate_legacy_data(DATA_DIR)
    except Exception:
        pass

FABRIC_META_BASE = "https://meta.fabricmc.net/v2/versions"
FABRIC_GAME_VERSIONS_URL = f"{FABRIC_META_BASE}/game"
FABRIC_LOADER_VERSIONS_URL = f"{FABRIC_META_BASE}/loader"
FABRIC_INSTALLER_VERSIONS_URL = f"{FABRIC_META_BASE}/installer"

def get_fabric_loader_versions_url(mc_version: str) -> str:
    return f"{FABRIC_LOADER_VERSIONS_URL}/{quote(str(mc_version or ''), safe='')}"

FORGE_MAVEN_BASE = "https://maven.minecraftforge.net/net/minecraftforge/forge"
FORGE_VERSIONS_URL = f"{FORGE_MAVEN_BASE}/maven-metadata.xml"
FORGE_PROMOTIONS_URL = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"

def get_forge_installer_url(full_version: str) -> str:
    return f"{FORGE_MAVEN_BASE}/{full_version}/forge-{full_version}-installer.jar"

def get_forge_full_version(mc_version: str, build: str) -> str:
    return f"{mc_version}-{build}"

# NeoForge publishes two artifacts. Everything from Minecraft 1.20.2 onward lives under
# net/neoforged/neoforge, while 1.20.1 — the fork's starting point — kept Forge's old
# net/neoforged/forge coordinates and its mc-prefixed version numbers.
NEOFORGE_MAVEN_BASE = "https://maven.neoforged.net/releases/net/neoforged/neoforge"
NEOFORGE_VERSIONS_URL = f"{NEOFORGE_MAVEN_BASE}/maven-metadata.xml"
NEOFORGE_LEGACY_MAVEN_BASE = "https://maven.neoforged.net/releases/net/neoforged/forge"
NEOFORGE_LEGACY_VERSIONS_URL = f"{NEOFORGE_LEGACY_MAVEN_BASE}/maven-metadata.xml"
NEOFORGE_LEGACY_MC_VERSION = "1.20.1"

# A NeoForge version is the Minecraft version it targets with the build number tacked on:
# 21.1.72 is 1.21.1, 26.2.0.66 is 26.2. Betas are ordinary releases for older Minecraft
# versions and stay in the list; the alpha and snapshot builds target no released game.
_NEOFORGE_VERSION_RE = re.compile(r"^(?P<nums>\d+(?:\.\d+){2,3})(?:-beta)?$")

def is_legacy_neoforge_version(version: str) -> bool:
    return str(version or "").strip().startswith(f"{NEOFORGE_LEGACY_MC_VERSION}-")

def get_neoforge_installer_url(version: str) -> str:
    version = str(version or "").strip()
    if is_legacy_neoforge_version(version):
        return f"{NEOFORGE_LEGACY_MAVEN_BASE}/{version}/forge-{version}-installer.jar"
    return f"{NEOFORGE_MAVEN_BASE}/{version}/neoforge-{version}-installer.jar"

def neoforge_mc_version(version: str) -> str:
    """The Minecraft version a NeoForge build targets, read out of its own version number."""
    version = str(version or "").strip()
    if is_legacy_neoforge_version(version):
        return NEOFORGE_LEGACY_MC_VERSION

    match = _NEOFORGE_VERSION_RE.match(version)
    if not match:
        return ""

    parts = [int(part) for part in match.group("nums").split(".")]
    if len(parts) == 3:
        # The 1.x era dropped the leading "1.", so 21.1.72 means Minecraft 1.21.1.
        major, minor, patch = 1, parts[0], parts[1]
    else:
        major, minor, patch = parts[0], parts[1], parts[2]
    return f"{major}.{minor}.{patch}" if patch else f"{major}.{minor}"

def is_neoforge_beta(version: str) -> bool:
    return str(version or "").strip().endswith("-beta")

LOADER_FABRIC = "fabric"
LOADER_FORGE = "forge"
LOADER_NEOFORGE = "neoforge"
SUPPORTED_LOADERS = [LOADER_FABRIC, LOADER_FORGE, LOADER_NEOFORGE]
LOADER_DISPLAY_NAMES = {
    LOADER_FABRIC: "Fabric",
    LOADER_FORGE: "Forge",
    LOADER_NEOFORGE: "NeoForge",
}

# Forge and NeoForge both ship a Java installer that writes an args file into libraries/
# instead of a runnable jar. They share every install, launch and cleanup path.
INSTALLER_LOADERS = frozenset({LOADER_FORGE, LOADER_NEOFORGE})

def uses_installer(value: str | None) -> bool:
    return normalize_loader(value) in INSTALLER_LOADERS

def normalize_loader(value: str | None) -> str:
    loader = str(value or "").strip().lower()
    return loader if loader in SUPPORTED_LOADERS else LOADER_FABRIC

def get_loader_display_name(value: str | None) -> str:
    return LOADER_DISPLAY_NAMES[normalize_loader(value)]

EDITION_JAVA = "java"
EDITION_BEDROCK = "bedrock"
SUPPORTED_EDITIONS = [EDITION_JAVA, EDITION_BEDROCK]
EDITION_DISPLAY_NAMES = {
    EDITION_JAVA: "Java Edition",
    EDITION_BEDROCK: "Bedrock Edition",
}

def normalize_edition(value: str | None) -> str:
    edition = str(value or "").strip().lower()
    return edition if edition in SUPPORTED_EDITIONS else EDITION_JAVA

def get_edition_display_name(value: str | None) -> str:
    return EDITION_DISPLAY_NAMES[normalize_edition(value)]

def is_bedrock(value: str | None) -> bool:
    return normalize_edition(value) == EDITION_BEDROCK

BEDROCK_DOWNLOAD_LINKS_URL = "https://net-secondary.web.minecraft-services.net/api/v1.0/download/links"
BEDROCK_DOWNLOAD_TYPES = {
    "win32": ("serverBedrockWindows", "serverBedrockPreviewWindows"),
    "darwin": ("serverBedrockLinux", "serverBedrockPreviewLinux"),
    "linux": ("serverBedrockLinux", "serverBedrockPreviewLinux"),
}
BEDROCK_SERVER_BINARY = "bedrock_server.exe" if sys.platform == "win32" else "bedrock_server"
BEDROCK_BINARY_NAMES = ("bedrock_server", "bedrock_server.exe")
BEDROCK_DEFAULT_PORT = 19132
BEDROCK_DEFAULT_PORT_V6 = 19133
BEDROCK_WORLDS_DIR = "worlds"

def get_bedrock_download_types() -> tuple[str, str]:
    return BEDROCK_DOWNLOAD_TYPES.get(sys.platform, BEDROCK_DOWNLOAD_TYPES["linux"])

def get_bedrock_download_types_for(platform: str) -> tuple[str, str]:
    """The download keys for a named platform, for machines other than this one.

    The same reason get_adoptium_jre_url_for exists: a Windows desktop managing a Linux
    box needs the Linux build, and the build this app happens to be running on is no
    guide at all.
    """
    key = "win32" if platform == "windows" else "linux"
    return BEDROCK_DOWNLOAD_TYPES[key]

ADOPTIUM_API_BASE = "https://api.adoptium.net/v3/binary/latest"

ADOPTIUM_ARCH_MAP = {
    "x86_64": "x64",
    "AMD64": "x64",
    "amd64": "x64",
    "arm64": "aarch64",
    "aarch64": "aarch64",
    "armv7l": "arm",
}

def get_adoptium_jre_url_for(java_version: int, os_name: str, machine: str) -> tuple[str, str]:
    """The runtime for a named platform, for machines other than this one.

    A server on another host needs that host's build, which is rarely the same as the
    one this app is running on: a Windows desktop managing a Linux box, or an x86
    laptop managing an ARM board.
    """
    arch = ADOPTIUM_ARCH_MAP.get(machine, "x64")
    archive_type = "zip" if os_name == "windows" else "tar.gz"
    url = f"{ADOPTIUM_API_BASE}/{java_version}/ga/{os_name}/{arch}/jre/hotspot/normal/eclipse"
    return url, archive_type

def get_adoptium_jre_download_info(java_version: int) -> tuple[str, str]:
    import platform

    machine = platform.machine()
    arch_map = ADOPTIUM_ARCH_MAP
    arch = arch_map.get(machine, "x64")

    if sys.platform == "win32":
        os_name = "windows"
        image_type = "jre"
        archive_type = "zip"
    elif sys.platform == "darwin":
        os_name = "mac"
        image_type = "jre"
        archive_type = "tar.gz"
    else:
        os_name = "linux"
        image_type = "jre"
        archive_type = "tar.gz"

    url = f"{ADOPTIUM_API_BASE}/{java_version}/ga/{os_name}/{arch}/{image_type}/hotspot/normal/eclipse"
    return url, archive_type

DEFAULT_JAVA_VERSION = 21

def _parse_mc_version_tuple(mc_version: str) -> tuple[int, int, int] | None:
    match = re.match(r"^(\d+(?:\.\d+){0,2})", mc_version or "")
    if not match:
        return None

    nums = [int(part) for part in match.group(1).split(".")]
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]

def parse_mc_version(mc_version: str) -> tuple[int, int, int] | None:
    return _parse_mc_version_tuple(mc_version)

def get_required_java_version(mc_version: str) -> int:
    parsed = _parse_mc_version_tuple(mc_version)
    if not parsed:
        return DEFAULT_JAVA_VERSION

    if parsed >= (26, 1, 0):
        return 25
    if (1, 20, 5) <= parsed <= (1, 21, 11):
        return 21
    if (1, 18, 0) <= parsed <= (1, 20, 4):
        return 17
    if (1, 17, 0) <= parsed <= (1, 17, 1):
        return 16
    if (1, 12, 0) <= parsed <= (1, 16, 5):
        return 8

    return DEFAULT_JAVA_VERSION

# A server folder carries its own console, so any copy of this app that can see the
# folder can read what the server is saying and type back to it -- whichever machine
# started it, and whichever copy of the app. console.log is the transcript of the run;
# console.in is the inbox commands are appended to. On a Linux host running a server
# through our own systemd unit console.in is a FIFO held open as the process's stdin,
# which a plain append writes into just the same, so writers never have to know which.
CONSOLE_LOG_FILE = "console.log"
CONSOLE_IN_FILE = "console.in"

# Windows has no FIFO, so a server a Scheduled Task runs there is started by a launcher
# script that watches console.in and types what appears into the process. This file is
# what that launcher writes while it is doing so -- the wrapper's pid, the server's, and
# the server's image name -- and it removes it on the way out. Its wrapper being alive is
# how any copy of this app tells an inbox that is being read from one that is not.
LAUNCHER_PID_FILE = "niksnaks.pid"

# A server folder holds two pictures: icon.png, the app's own copy that the sidebar
# draws, and server-icon.png, the one Minecraft shows in the multiplayer list. Both are
# written from the picture the user picks, so a modpack is not free to paint over them.
USER_ICON_FILE = "icon.png"
MINECRAFT_ICON_FILE = "server-icon.png"
ICON_FILES = frozenset({USER_ICON_FILE, MINECRAFT_ICON_FILE})

def has_user_server_icon(server_dir: Path | str) -> bool:
    # Only the icon picker and the create dialog write icon.png, and no modpack is
    # allowed to, so the file being there means the user picked a picture. The folder is
    # asked as it was handed over, since a server's may well be on another computer.
    root = server_dir if hasattr(server_dir, "is_file") else Path(server_dir)
    try:
        return (root / USER_ICON_FILE).is_file()
    except Exception:
        return False

def is_pack_protected_icon(server_dir: Path | str, target: Path) -> bool:
    # icon.png is the app's alone. A pack may still give a server its picture through
    # server-icon.png, but only while the user has not chosen one of their own.
    leaf = target.name.lower()
    if leaf not in ICON_FILES or target.parent.resolve() != Path(server_dir).resolve():
        return False
    return leaf != MINECRAFT_ICON_FILE or has_user_server_icon(server_dir)

# Only one server can listen on a port at a time, so hosting several side by side means
# giving each its own. Java Edition binds a single TCP port; Bedrock binds server-port for
# IPv4 and server-portv6 right after it, so Bedrock ports move in pairs.
DEFAULT_SERVER_PORT = 25565
MIN_SERVER_PORT = 1024
MAX_SERVER_PORT = 65535

def default_port_for_edition(edition: str | None) -> int:
    return BEDROCK_DEFAULT_PORT if is_bedrock(edition) else DEFAULT_SERVER_PORT

def ports_bound_by(port: int, edition: str | None) -> set[int]:
    """Every port a server on *port* occupies once it starts."""
    return {port, port + 1} if is_bedrock(edition) else {port}

DEFAULT_SERVER_PROPERTIES = {
    "motd": "a Niksnaks-Hosting server",
    "max-players": "20",
    "difficulty": "easy",
    "gamemode": "survival",
    "pvp": "true",
    "online-mode": "true",
    "white-list": "false",
    "allow-flight": "false",
    "view-distance": "10",
    "simulation-distance": "10",
    "server-port": str(DEFAULT_SERVER_PORT),
    "level-seed": "",
    "level-type": "minecraft\\:normal",
    "spawn-protection": "16",
    "enable-command-block": "false",
    "allow-nether": "true",
    "hardcore": "false",
    "enable-rcon": "false",
    "max-world-size": "29999984",
    "enable-query": "false",
}

DIFFICULTIES = ["peaceful", "easy", "normal", "hard"]

GAMEMODES = ["survival", "creative", "adventure", "spectator"]

# Bedrock Dedicated Server ships its own server.properties; these are the values
# Niksnaks-Hosting sets on a fresh install. Key names and accepted values differ
# from Java Edition (no eula.txt, no level-type, UDP port 19132).
BEDROCK_DEFAULT_SERVER_PROPERTIES = {
    "server-name": "a Niksnaks-Hosting server",
    "gamemode": "survival",
    "force-gamemode": "false",
    "difficulty": "easy",
    "allow-cheats": "false",
    "max-players": "10",
    "online-mode": "true",
    "allow-list": "false",
    "server-port": str(BEDROCK_DEFAULT_PORT),
    "server-portv6": str(BEDROCK_DEFAULT_PORT_V6),
    "enable-lan-visibility": "true",
    "view-distance": "32",
    "tick-distance": "4",
    "player-idle-timeout": "30",
    "max-threads": "8",
    "level-name": "Bedrock level",
    "level-seed": "",
    "default-player-permission-level": "member",
    "texturepack-required": "false",
    "disable-player-interaction": "false",
    "disable-custom-skins": "false",
    "chat-restriction": "None",
}

BEDROCK_GAMEMODES = ["survival", "creative", "adventure"]

BEDROCK_PERMISSION_LEVELS = ["visitor", "member", "operator"]

BEDROCK_CHAT_RESTRICTIONS = ["None", "Dropped", "Disabled"]

LEVEL_TYPES = [
    "minecraft\\:normal",
    "minecraft\\:flat",
    "minecraft\\:large_biomes",
    "minecraft\\:amplified",
    "minecraft\\:single_biome_surface",
]

LEVEL_TYPE_NAMES = {
    "minecraft\\:normal": _("Default"),
    "minecraft\\:flat": _("Flat"),
    "minecraft\\:large_biomes": _("Large Biomes"),
    "minecraft\\:amplified": _("Amplified"),
    "minecraft\\:single_biome_surface": _("Single Biome"),
}

class ServerStatus:
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"

MIN_RAM_MB = 512

def get_system_ram_mb() -> int:
    try:
        import psutil

        return int(psutil.virtual_memory().total / (1024 * 1024))
    except Exception:
        if sys.platform == "win32":
            try:
                import ctypes

                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]

                stat = MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
                return int(stat.ullTotalPhys / (1024 * 1024))
            except Exception:
                pass
        else:
            try:
                return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 * 1024))
            except Exception:
                pass
    return 16384

def _get_max_ram_mb() -> int:
    sys_ram = get_system_ram_mb()

    if sys_ram <= 4096:
        headroom = 1024
    elif sys_ram <= 8192:
        headroom = 1536
    else:
        headroom = 2048
    return max(MIN_RAM_MB, sys_ram - headroom)

MAX_RAM_MB = _get_max_ram_mb()
DEFAULT_RAM_MB = min(2048, MAX_RAM_MB)

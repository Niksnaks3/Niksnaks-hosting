"""
Central constants for Niksnaks-Hosting application.
"""

import os
import re
import sys
from pathlib import Path

from niksnaks_hosting.version import __version__

# Application identity
APP_ID = "com.niksnakshosting.NiksnaksHosting"
APP_NAME = "Niksnaks-Hosting"
APP_VERSION = __version__
APP_WEBSITE = ""

# Directories


def _default_data_dir() -> Path:
    """Return a sensible per-user data directory for the current platform."""
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "Hosty"
        return Path.home() / "AppData" / "Local" / "Hosty"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Hosty"

    return Path.home() / ".local" / "share" / "hosty"


DATA_DIR = Path(os.environ.get("HOSTY_DATA_DIR", _default_data_dir()))
SERVERS_DIR = DATA_DIR / "servers"
JRES_DIR = DATA_DIR / "jres"
CACHE_DIR = DATA_DIR / "cache"
CONFIG_FILE = DATA_DIR / "servers.json"

# Ensure directories exist
for d in [DATA_DIR, SERVERS_DIR, JRES_DIR, CACHE_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Fabric Meta API
FABRIC_META_BASE = "https://meta.fabricmc.net/v2/versions"
FABRIC_GAME_VERSIONS_URL = f"{FABRIC_META_BASE}/game"
FABRIC_LOADER_VERSIONS_URL = f"{FABRIC_META_BASE}/loader"
FABRIC_INSTALLER_VERSIONS_URL = f"{FABRIC_META_BASE}/installer"

# Forge maven / promotions
# The Forge maven serves only maven-metadata.xml (no .json), listing every
# '<mc>-<forgebuild>' version string (e.g. "1.21.4-54.1.6").
FORGE_MAVEN_BASE = "https://maven.minecraftforge.net/net/minecraftforge/forge"
FORGE_VERSIONS_URL = f"{FORGE_MAVEN_BASE}/maven-metadata.xml"
FORGE_PROMOTIONS_URL = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"


def get_forge_installer_url(full_version: str) -> str:
    """Return the Forge installer JAR URL for a full '<mc>-<build>' version string."""
    return f"{FORGE_MAVEN_BASE}/{full_version}/forge-{full_version}-installer.jar"


def get_forge_full_version(mc_version: str, build: str) -> str:
    """Combine a Minecraft version and a Forge build into the '<mc>-<build>' string."""
    return f"{mc_version}-{build}"


# Mod loaders
LOADER_FABRIC = "fabric"
LOADER_FORGE = "forge"
SUPPORTED_LOADERS = [LOADER_FABRIC, LOADER_FORGE]
LOADER_DISPLAY_NAMES = {
    LOADER_FABRIC: "Fabric",
    LOADER_FORGE: "Forge",
}


def normalize_loader(value: str | None) -> str:
    """Coerce any stored/user-supplied loader id to a supported one (defaults to Fabric)."""
    loader = str(value or "").strip().lower()
    return loader if loader in SUPPORTED_LOADERS else LOADER_FABRIC


def get_loader_display_name(value: str | None) -> str:
    """Human-readable name for a loader id."""
    return LOADER_DISPLAY_NAMES[normalize_loader(value)]

# Adoptium JRE API
ADOPTIUM_API_BASE = "https://api.adoptium.net/v3/binary/latest"


def get_adoptium_jre_download_info(java_version: int) -> tuple[str, str]:
    """
    Return a platform-specific Adoptium JRE download URL and archive type.

    Returns:
        (url, archive_type) where archive_type is "zip" or "tar.gz".
    """
    import platform

    machine = platform.machine()
    arch_map = {
        "x86_64": "x64",
        "AMD64": "x64",
        "arm64": "aarch64",
        "aarch64": "aarch64",
    }
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


def get_adoptium_jre_url(java_version: int) -> str:
    """Backward-compatible helper that returns only the Adoptium JRE URL."""
    return get_adoptium_jre_download_info(java_version)[0]


DEFAULT_JAVA_VERSION = 21


def _parse_mc_version_tuple(mc_version: str) -> tuple[int, int, int] | None:
    """Parse a Minecraft version string into (major, minor, patch)."""
    match = re.match(r"^(\d+(?:\.\d+){0,2})", mc_version or "")
    if not match:
        return None

    nums = [int(part) for part in match.group(1).split(".")]
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def parse_mc_version(mc_version: str) -> tuple[int, int, int] | None:
    """Parse a Minecraft version string into (major, minor, patch), or None if unparseable."""
    return _parse_mc_version_tuple(mc_version)


def get_required_java_version(mc_version: str) -> int:
    """Determine required Java version for a Minecraft version (same ranges for Fabric and Forge)."""
    parsed = _parse_mc_version_tuple(mc_version)
    if not parsed:
        return DEFAULT_JAVA_VERSION

    # Loader requirements:
    # 26.1+ -> Java 25+
    # 1.20.5 - 1.21.11 -> Java 21+
    # 1.18 - 1.20.4 -> Java 17+
    # 1.17 - 1.17.1 -> Java 16+
    # 1.12 - 1.16.5 -> Java 8+
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


# Default server.properties values
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
    "server-port": "25565",
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


# Difficulty options
DIFFICULTIES = ["peaceful", "easy", "normal", "hard"]

# Gamemode options
GAMEMODES = ["survival", "creative", "adventure", "spectator"]

# Level types
LEVEL_TYPES = [
    "minecraft\\:normal",
    "minecraft\\:flat",
    "minecraft\\:large_biomes",
    "minecraft\\:amplified",
    "minecraft\\:single_biome_surface",
]

# Display names for level types
LEVEL_TYPE_NAMES = {
    "minecraft\\:normal": _("Default"),
    "minecraft\\:flat": _("Flat"),
    "minecraft\\:large_biomes": _("Large Biomes"),
    "minecraft\\:amplified": _("Amplified"),
    "minecraft\\:single_biome_surface": _("Single Biome"),
}


# Server status
class ServerStatus:
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


# Default RAM allocation in MB
MIN_RAM_MB = 512


def get_system_ram_mb() -> int:
    """Return the total system RAM in Megabytes."""
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
    return 16384  # Default fallback if all fails (16GB)


def _get_max_ram_mb() -> int:
    sys_ram = get_system_ram_mb()
    # Determine OS headroom to leave system responsive:
    # <= 4GB system: leave 1GB
    # <= 8GB system: leave 1.5GB
    # > 8GB system: leave 2GB
    if sys_ram <= 4096:
        headroom = 1024
    elif sys_ram <= 8192:
        headroom = 1536
    else:
        headroom = 2048
    return max(MIN_RAM_MB, sys_ram - headroom)


MAX_RAM_MB = _get_max_ram_mb()
DEFAULT_RAM_MB = min(2048, MAX_RAM_MB)

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
CONFIG_FILE = DATA_DIR / "servers.json"

for d in [DATA_DIR, SERVERS_DIR, JRES_DIR, CACHE_DIR]:
    d.mkdir(parents=True, exist_ok=True)

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

LOADER_FABRIC = "fabric"
LOADER_FORGE = "forge"
SUPPORTED_LOADERS = [LOADER_FABRIC, LOADER_FORGE]
LOADER_DISPLAY_NAMES = {
    LOADER_FABRIC: "Fabric",
    LOADER_FORGE: "Forge",
}

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

ADOPTIUM_API_BASE = "https://api.adoptium.net/v3/binary/latest"

def get_adoptium_jre_download_info(java_version: int) -> tuple[str, str]:
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
    return get_adoptium_jre_download_info(java_version)[0]

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

from __future__ import annotations

import json
import re
import tempfile
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from niksnaks_hosting.shared.utils.constants import (
    APP_VERSION,
    LOADER_FABRIC,
    LOADER_FORGE,
    normalize_loader,
)
from niksnaks_hosting.shared.utils.net import make_ssl_context

USER_AGENT = f"Niksnaks-Hosting/{APP_VERSION}"

# api.curseforge.com refuses anonymous callers, so the app reads through a public
# mirror of it by default and only talks to CurseForge directly once the user has
# supplied a key of their own.
OFFICIAL_API = "https://api.curseforge.com/v1"
PROXY_API = "https://api.curse.tools/v1/cf"

GAME_ID_MINECRAFT = 432
CLASS_ID_MODPACKS = 4471

# CurseForge numbers its loaders; only the two this app can install are mapped.
LOADER_TYPE_IDS = {LOADER_FORGE: 1, LOADER_FABRIC: 4}

SORT_POPULARITY = 2
SORT_LAST_UPDATED = 3
SORT_NAME = 4
SORT_TOTAL_DOWNLOADS = 6

RELEASE_TYPE_NAMES = {1: "release", 2: "beta", 3: "alpha"}

# Files that record who may play and whether the terms were accepted. A modpack has
# no business rewriting those, so they survive an install untouched.
PROTECTED_FILES = frozenset(
    {
        "eula.txt",
        "ops.json",
        "whitelist.json",
        "banned-players.json",
        "banned-ips.json",
        "usercache.json",
        "server.properties",
    }
)

# server.properties is merged rather than skipped, because a pack often needs its own
# gameplay settings. These keys still stay as they are: they decide where the server
# listens and what it is called, which is the app's and the user's business, not the pack's.
PRESERVED_PROPERTIES = frozenset(
    {
        "server-port",
        "server-ip",
        "query.port",
        "rcon.port",
        "rcon.password",
        "enable-rcon",
        "enable-query",
        "level-name",
        "level-seed",
        "motd",
        "white-list",
        "online-mode",
        "max-players",
    }
)

_SSL_CONTEXT = make_ssl_context()


@dataclass
class CurseForgeHit:
    mod_id: int
    slug: str
    name: str
    summary: str
    logo_url: str
    downloads: int
    author: str
    categories: list[str]
    website_url: str
    date_modified: str

    @property
    def project_id(self) -> str:
        """The id as the install state and the UI rows spell it."""
        return str(self.mod_id)


@dataclass
class CurseForgeFile:
    file_id: int
    mod_id: int
    display_name: str
    file_name: str
    download_url: str
    game_versions: list[str]
    release_type: int
    file_date: str
    file_length: int
    server_pack_file_id: int
    is_server_pack: bool

    @property
    def version_id(self) -> str:
        return str(self.file_id)

    @property
    def release_label(self) -> str:
        return RELEASE_TYPE_NAMES.get(int(self.release_type or 1), "release")

    @property
    def mc_versions(self) -> list[str]:
        """Only the Minecraft versions; CurseForge mixes loader names into the same list."""
        return [v for v in self.game_versions if v and v[0].isdigit()]

    @property
    def loaders(self) -> list[str]:
        return [v.lower() for v in self.game_versions if v and not v[0].isdigit()]


@dataclass
class ModpackInstallResult:
    downloaded_files: int = 0
    extracted_files: int = 0
    managed_mod_files: list[str] = field(default_factory=list)
    # Mods whose author opted out of third-party downloads; the pack is incomplete without them.
    manual_downloads: list[tuple[str, str]] = field(default_factory=list)
    used_server_pack: bool = False
    pack_mc_version: str = ""
    pack_loader: str = ""
    pack_loader_version: str = ""


def _api_key() -> str:
    try:
        from niksnaks_hosting.shared.backend.preferences_manager import PreferencesManager

        return PreferencesManager().curseforge_api_key
    except Exception:
        return ""


def _api_bases() -> list[tuple[str, dict[str, str]]]:
    """Endpoints to try in order, each with the headers it needs."""
    bases: list[tuple[str, dict[str, str]]] = []
    key = _api_key()
    if key:
        bases.append((OFFICIAL_API, {"x-api-key": key}))
    bases.append((PROXY_API, {}))
    return bases


def _request_json(path: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
    query = ""
    if params:
        cleaned = {k: v for k, v in params.items() if v is not None and v != ""}
        query = "?" + urllib.parse.urlencode(cleaned)

    last_error: Exception = RuntimeError("No CurseForge endpoint configured")
    for base, extra_headers in _api_bases():
        req = urllib.request.Request(
            f"{base}{path}{query}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json", **extra_headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_error = e
            continue

    raise last_error


def _hit_to_model(raw: dict[str, Any]) -> CurseForgeHit:
    logo = raw.get("logo") or {}
    authors = raw.get("authors") or []
    links = raw.get("links") or {}
    return CurseForgeHit(
        mod_id=int(raw.get("id") or 0),
        slug=str(raw.get("slug") or ""),
        name=str(raw.get("name") or ""),
        summary=str(raw.get("summary") or "")[:280],
        logo_url=str(logo.get("thumbnailUrl") or logo.get("url") or ""),
        downloads=int(raw.get("downloadCount") or 0),
        author=str(authors[0].get("name") or "") if authors else "",
        categories=[str(c.get("name") or "") for c in (raw.get("categories") or []) if c.get("name")],
        website_url=str(links.get("websiteUrl") or ""),
        date_modified=str(raw.get("dateModified") or ""),
    )


def _file_to_model(raw: dict[str, Any]) -> CurseForgeFile:
    return CurseForgeFile(
        file_id=int(raw.get("id") or 0),
        mod_id=int(raw.get("modId") or 0),
        display_name=str(raw.get("displayName") or raw.get("fileName") or ""),
        file_name=str(raw.get("fileName") or ""),
        download_url=str(raw.get("downloadUrl") or ""),
        game_versions=[str(v) for v in (raw.get("gameVersions") or [])],
        release_type=int(raw.get("releaseType") or 1),
        file_date=str(raw.get("fileDate") or ""),
        file_length=int(raw.get("fileLength") or 0),
        server_pack_file_id=int(raw.get("serverPackFileId") or 0),
        is_server_pack=bool(raw.get("isServerPack")),
    )


def search_modpacks(
    query: str = "",
    game_version: str = "",
    loader: str = LOADER_FORGE,
    category_id: int = 0,
    sort_field: int = SORT_POPULARITY,
    index: int = 0,
    page_size: int = 20,
) -> tuple[list[CurseForgeHit], int]:
    """Modpacks that run on the given Minecraft version and loader, newest-first by sort."""
    params: dict[str, Any] = {
        "gameId": GAME_ID_MINECRAFT,
        "classId": CLASS_ID_MODPACKS,
        "sortField": int(sort_field),
        "sortOrder": "desc",
        "index": max(0, int(index)),
        "pageSize": max(1, min(50, int(page_size))),
    }
    text = (query or "").strip()
    if text:
        params["searchFilter"] = text
    if game_version:
        params["gameVersion"] = game_version
    loader_id = LOADER_TYPE_IDS.get(normalize_loader(loader))
    if loader_id:
        params["modLoaderType"] = loader_id
    if category_id:
        params["categoryId"] = int(category_id)

    data = _request_json("/mods/search", params)
    hits = [_hit_to_model(raw) for raw in (data.get("data") or []) if isinstance(raw, dict)]
    pagination = data.get("pagination") or {}
    total = int(pagination.get("totalCount") or len(hits))
    return hits, total


def list_categories() -> list[tuple[str, int]]:
    """Modpack categories as (name, id), alphabetically."""
    try:
        data = _request_json("/categories", {"gameId": GAME_ID_MINECRAFT, "classId": CLASS_ID_MODPACKS})
    except Exception:
        return []

    out = [
        (str(c.get("name") or ""), int(c.get("id") or 0))
        for c in (data.get("data") or [])
        if isinstance(c, dict) and c.get("name") and c.get("id")
    ]
    return sorted(out, key=lambda item: item[0].lower())


def get_mod(mod_id: int | str) -> dict[str, Any] | None:
    try:
        data = _request_json(f"/mods/{int(mod_id)}")
    except Exception:
        return None
    payload = data.get("data")
    return payload if isinstance(payload, dict) else None


def get_mod_description(mod_id: int | str) -> str:
    """The project's long description, as HTML."""
    try:
        data = _request_json(f"/mods/{int(mod_id)}/description")
    except Exception:
        return ""
    return str(data.get("data") or "")


def get_files(
    mod_id: int | str,
    game_version: str = "",
    loader: str = "",
    index: int = 0,
    page_size: int = 30,
) -> list[CurseForgeFile]:
    """Downloadable files for a project, newest first, optionally narrowed to what fits a server."""
    params: dict[str, Any] = {
        "index": max(0, int(index)),
        "pageSize": max(1, min(50, int(page_size))),
    }
    if game_version:
        params["gameVersion"] = game_version
    if loader:
        loader_id = LOADER_TYPE_IDS.get(normalize_loader(loader))
        if loader_id:
            params["modLoaderType"] = loader_id

    try:
        data = _request_json(f"/mods/{int(mod_id)}/files", params)
    except Exception:
        return []

    return [_file_to_model(raw) for raw in (data.get("data") or []) if isinstance(raw, dict)]


def get_file(mod_id: int | str, file_id: int | str) -> CurseForgeFile | None:
    try:
        data = _request_json(f"/mods/{int(mod_id)}/files/{int(file_id)}")
    except Exception:
        return None
    payload = data.get("data")
    return _file_to_model(payload) if isinstance(payload, dict) else None


def find_compatible_file(mod_id: int | str, game_version: str, loader: str) -> CurseForgeFile | None:
    """The newest file for this server, preferring a full release over a beta."""
    files = get_files(mod_id, game_version=game_version, loader=loader, page_size=20)
    if not files:
        return None

    release = next((f for f in files if int(f.release_type or 1) == 1), None)
    return release or files[0]


def resolve_server_pack(pack_file: CurseForgeFile) -> CurseForgeFile | None:
    """The author's ready-to-run server build of a pack, when they published one."""
    if not pack_file.server_pack_file_id:
        return None
    server_file = get_file(pack_file.mod_id, pack_file.server_pack_file_id)
    if server_file and server_file.download_url:
        return server_file
    return None


def _safe_target(root: Path, relative_path: str) -> Path | None:
    rel = str(relative_path or "").replace("\\", "/").lstrip("/")
    if not rel:
        return None

    target = (root / rel).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target


def _download_to_file(
    url: str,
    dest: Path,
    timeout: float = 300.0,
    on_bytes: Callable[[int, int], None] | None = None,
) -> None:
    """Stream a download to disk; pack archives run to hundreds of megabytes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with dest.open("wb") as fh:
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if on_bytes:
                    on_bytes(done, total)


def _merge_server_properties(server_root: Path, incoming: bytes) -> None:
    """Take the pack's gameplay settings but keep the server where the user put it."""
    from niksnaks_hosting.shared.backend.config_manager import ConfigManager

    config = ConfigManager(server_root)
    current = config.load()
    if not current:
        return

    changed = False
    for line in incoming.decode("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        # Preserved keys belong to the app and the user. Keys the current file does not
        # have are skipped as well: a pack's properties can target a different build.
        if key in PRESERVED_PROPERTIES or key not in current:
            continue
        config.set_value(key, value.strip())
        changed = True

    if changed:
        config.save()


def _extract_archive(
    archive: Path,
    server_root: Path,
    result: ModpackInstallResult,
    strip_prefix: str = "",
    progress: Callable[[float, str], None] | None = None,
    progress_base: float = 0.0,
    progress_span: float = 1.0,
) -> None:
    """Unpack an archive over the server, guarding the app-owned files and against zip slip."""
    with zipfile.ZipFile(archive) as zf:
        entries = [zi for zi in zf.infolist() if not zi.is_dir()]
        total = len(entries) or 1

        for index, zinfo in enumerate(entries, start=1):
            name = zinfo.filename.replace("\\", "/")
            if strip_prefix:
                if not name.startswith(strip_prefix):
                    continue
                name = name[len(strip_prefix) :]
            if not name:
                continue

            target = _safe_target(server_root, name)
            if not target:
                continue

            leaf = target.name.lower()
            if leaf in PROTECTED_FILES and target.parent.resolve() == server_root.resolve():
                if leaf == "server.properties" and target.is_file():
                    _merge_server_properties(server_root, zf.read(zinfo))
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(zinfo, "r") as src:
                target.write_bytes(src.read())
            result.extracted_files += 1

            relative = name.lstrip("/").lower()
            if relative.startswith("mods/") and relative.endswith(".jar"):
                result.managed_mod_files.append(Path(relative).name)

            if progress and index % 25 == 0:
                progress(progress_base + progress_span * (index / total), Path(name).name)


def _parse_manifest(zf: zipfile.ZipFile) -> dict[str, Any] | None:
    try:
        return json.loads(zf.read("manifest.json").decode("utf-8"))
    except (KeyError, ValueError, OSError):
        return None


_FORGE_INSTALLER_RE = re.compile(r"^forge-(?P<mc>[\d.]+)-(?P<loader>[\d.]+)-installer\.jar$", re.IGNORECASE)


def _detect_versions_from_installer(server_root: Path) -> tuple[str, str]:
    """A server pack usually ships the loader installer it expects; its name says which."""
    try:
        for jar in server_root.glob("*-installer.jar"):
            match = _FORGE_INSTALLER_RE.match(jar.name)
            if match:
                return match.group("mc"), match.group("loader")
    except OSError:
        pass
    return "", ""


def _apply_manifest_metadata(result: ModpackInstallResult, manifest: dict[str, Any]) -> None:
    """Read the Minecraft version and loader a pack declares for itself."""
    minecraft = manifest.get("minecraft") or {}
    result.pack_mc_version = str(minecraft.get("version") or "")

    loaders = minecraft.get("modLoaders") or []
    primary = next((entry for entry in loaders if entry.get("primary")), loaders[0] if loaders else {})
    loader_id = str(primary.get("id") or "")
    if "-" in loader_id:
        result.pack_loader, result.pack_loader_version = loader_id.split("-", 1)
    else:
        result.pack_loader = loader_id


def _install_from_manifest(
    archive: Path,
    server_root: Path,
    result: ModpackInstallResult,
    timeout: float,
    progress: Callable[[float, str], None] | None,
) -> None:
    """Install a client-style pack: fetch every mod the manifest lists, then its overrides."""
    with zipfile.ZipFile(archive) as zf:
        manifest = _parse_manifest(zf)
        if not manifest:
            raise RuntimeError("Invalid modpack: missing manifest.json")

        _apply_manifest_metadata(result, manifest)

        wanted = [
            (int(entry["projectID"]), int(entry["fileID"]))
            for entry in (manifest.get("files") or [])
            if isinstance(entry, dict) and entry.get("projectID") and entry.get("fileID")
        ]
        overrides_dir = str(manifest.get("overrides") or "overrides").strip("/")

    if progress:
        progress(0.10, "")

    # Each mod needs its own metadata call, so resolve them a few at a time; the
    # downloads themselves stay sequential to keep the mirror happy.
    def resolve(pair: tuple[int, int]) -> CurseForgeFile | None:
        return get_file(pair[0], pair[1])

    resolved: list[CurseForgeFile | None] = []
    if wanted:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for position, entry in enumerate(pool.map(resolve, wanted), start=1):
                resolved.append(entry)
                if progress and position % 10 == 0:
                    progress(0.10 + 0.25 * (position / len(wanted)), "")

    mods_dir = server_root / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)

    total = len(resolved) or 1
    for position, entry in enumerate(resolved, start=1):
        span_start = 0.35 + 0.50 * ((position - 1) / total)
        span_end = 0.35 + 0.50 * (position / total)

        if entry is None:
            continue

        if not entry.download_url:
            # The author opted out of third-party downloads, so the app cannot fetch it.
            result.manual_downloads.append(
                (entry.display_name or entry.file_name or str(entry.file_id), str(entry.mod_id))
            )
            continue

        target = _safe_target(mods_dir, entry.file_name)
        if not target:
            continue

        def report_bytes(done: int, size: int, start: float = span_start, end: float = span_end) -> None:
            if progress:
                progress((start + (end - start) * (done / size)) if size else start, target.name)

        _download_to_file(entry.download_url, target, timeout=timeout, on_bytes=report_bytes)
        result.downloaded_files += 1
        if target.suffix.lower() == ".jar":
            result.managed_mod_files.append(target.name)

    if progress:
        progress(0.88, "")

    _extract_archive(
        archive,
        server_root,
        result,
        strip_prefix=f"{overrides_dir}/",
        progress=progress,
        progress_base=0.88,
        progress_span=0.10,
    )


def install_modpack(
    mod_id: int | str,
    file_id: int | str,
    server_dir: Path | str,
    timeout: float = 300.0,
    progress_callback: Callable[[float, str], None] | None = None,
) -> ModpackInstallResult:
    """Install a CurseForge modpack into a server folder.

    Prefers the author's server pack, which is already stripped of client-only mods.
    Falls back to the client pack's manifest when no server build was published.
    """
    server_root = Path(server_dir)
    server_root.mkdir(parents=True, exist_ok=True)

    result = ModpackInstallResult()

    def progress(fraction: float, message: str = "") -> None:
        if progress_callback:
            progress_callback(max(0.0, min(1.0, fraction)), message)

    progress(0.01, "")

    pack_file = get_file(mod_id, file_id)
    if not pack_file:
        raise RuntimeError("Could not read the selected modpack file.")

    chosen = resolve_server_pack(pack_file) or pack_file
    result.used_server_pack = chosen.file_id != pack_file.file_id

    if not chosen.download_url:
        raise RuntimeError(
            "CurseForge does not offer this pack for automatic download. "
            "Download it from the project page and import it manually."
        )

    with tempfile.TemporaryDirectory(prefix="niksnaks-hosting-cf-") as tmp:
        archive = Path(tmp) / (chosen.file_name or "modpack.zip")

        def on_bytes(done: int, size: int) -> None:
            share = (done / size) if size else 0.0
            # Downloading the archive is most of a server-pack install and only the
            # opening act of a manifest install.
            progress(0.02 + share * (0.55 if result.used_server_pack else 0.06), "")

        _download_to_file(chosen.download_url, archive, timeout=timeout, on_bytes=on_bytes)

        try:
            zipfile.ZipFile(archive).close()
        except zipfile.BadZipFile as e:
            raise RuntimeError("The downloaded modpack file is not a valid archive.") from e

        if result.used_server_pack:
            with zipfile.ZipFile(archive) as zf:
                manifest = _parse_manifest(zf)
            if manifest:
                _apply_manifest_metadata(result, manifest)

            _extract_archive(
                archive,
                server_root,
                result,
                progress=progress,
                progress_base=0.58,
                progress_span=0.40,
            )
        else:
            _install_from_manifest(archive, server_root, result, timeout, progress)

    # Not every server pack carries a manifest. The installer it ships knows the exact
    # loader build; failing that, the catalogue entry the search already matched on does.
    if not result.pack_loader_version:
        installer_mc, installer_loader = _detect_versions_from_installer(server_root)
        result.pack_loader_version = installer_loader
        if not result.pack_mc_version:
            result.pack_mc_version = installer_mc
    if not result.pack_mc_version:
        result.pack_mc_version = next(iter(pack_file.mc_versions), "")
    if not result.pack_loader:
        result.pack_loader = next(iter(pack_file.loaders), "")

    result.managed_mod_files = sorted({name.lower() for name in result.managed_mod_files})
    progress(1.0, "")
    return result

import gzip
import stat
import struct
import threading
from collections import OrderedDict
from pathlib import Path

_INFO_CACHE_SIZE = 64
_info_cache: OrderedDict = OrderedDict()
_info_cache_lock = threading.Lock()

class _NbtReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def _read(self, size: int) -> bytes:
        chunk = self.data[self.pos : self.pos + size]
        if len(chunk) != size:
            raise ValueError("Unexpected end of NBT data")
        self.pos += size
        return chunk

    def byte(self) -> int:
        return struct.unpack(">b", self._read(1))[0]

    def ubyte(self) -> int:
        return self._read(1)[0]

    def short(self) -> int:
        return struct.unpack(">h", self._read(2))[0]

    def int(self) -> int:
        return struct.unpack(">i", self._read(4))[0]

    def long(self) -> int:
        return struct.unpack(">q", self._read(8))[0]

    def string(self) -> str:
        length = struct.unpack(">H", self._read(2))[0]
        return self._read(length).decode("utf-8", errors="replace")

    def payload(self, tag_type: int):
        if tag_type == 1:
            return self.byte()
        if tag_type == 2:
            return self.short()
        if tag_type == 3:
            return self.int()
        if tag_type == 4:
            return self.long()
        if tag_type in (5, 6):
            self._read(4 if tag_type == 5 else 8)
            return None
        if tag_type == 7:
            self._read(max(0, self.int()))
            return None
        if tag_type == 8:
            return self.string()
        if tag_type == 9:
            item_type = self.ubyte()
            length = max(0, self.int())
            return [self.payload(item_type) for _ in range(length)]
        if tag_type == 10:
            out = {}
            while True:
                child_type = self.ubyte()
                if child_type == 0:
                    return out
                name = self.string()
                out[name] = self.payload(child_type)
        if tag_type == 11:
            self._read(max(0, self.int()) * 4)
            return None
        if tag_type == 12:
            self._read(max(0, self.int()) * 8)
            return None
        raise ValueError(f"Unsupported NBT tag {tag_type}")

def _read_nbt_file(nbt_file: Path) -> dict | None:
    if not nbt_file.is_file():
        return None
    return _load_nbt_file(nbt_file)

def _load_nbt_file(nbt_file: Path) -> dict | None:
    try:
        raw = nbt_file.read_bytes()
        data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
        reader = _NbtReader(data)
        if reader.ubyte() != 10:
            return None
        reader.string()
        root = reader.payload(10)
        data_tag = root.get("Data", root) if isinstance(root, dict) else {}
        if not isinstance(data_tag, dict):
            return None
        return data_tag
    except Exception:
        return None

def _extract_seed(node) -> str:
    if isinstance(node, dict):
        lower_map = {str(key).casefold(): value for key, value in node.items()}
        worldgen = lower_map.get("worldgensettings")
        if isinstance(worldgen, dict):
            seed = _extract_seed(worldgen)
            if seed:
                return seed

        for key in ("seed", "randomseed", "level-seed"):
            value = lower_map.get(key)
            if isinstance(value, (str, int, float)):
                text = str(value).strip()
                if text:
                    return text

        for value in node.values():
            seed = _extract_seed(value)
            if seed:
                return seed
    elif isinstance(node, list):
        for value in node:
            seed = _extract_seed(value)
            if seed:
                return seed
    return ""

def _extract_type(node) -> str:
    if not isinstance(node, dict):
        return ""

    lower_map = {str(k).casefold(): v for k, v in node.items()}

    if "generatorname" in lower_map:
        gn = str(lower_map["generatorname"]).lower()
        if gn == "flat":
            return "minecraft\\:flat"
        if gn == "largebiomes":
            return "minecraft\\:large_biomes"
        if gn == "amplified":
            return "minecraft\\:amplified"
        if gn == "default":
            return "minecraft\\:normal"

    dims = lower_map.get("dimensions")
    if isinstance(dims, dict):
        dims_lower = {str(k).casefold(): v for k, v in dims.items()}
        overworld = dims_lower.get("minecraft:overworld") or dims_lower.get("overworld")
        if isinstance(overworld, dict):
            ow_lower = {str(k).casefold(): v for k, v in overworld.items()}
            gen = ow_lower.get("generator")
            if isinstance(gen, dict):
                gen_lower = {str(k).casefold(): v for k, v in gen.items()}
                t = str(gen_lower.get("type", "")).lower()
                if t == "minecraft:flat" or t == "flat":
                    return "minecraft\\:flat"
                if t == "minecraft:noise" or t == "noise":
                    s = str(gen_lower.get("settings", "")).lower()
                    if "amplified" in s:
                        return "minecraft\\:amplified"
                    if "large_biomes" in s:
                        return "minecraft\\:large_biomes"

                    bs = gen_lower.get("biome_source", {})
                    if isinstance(bs, dict):
                        bs_lower = {str(k).casefold(): v for k, v in bs.items()}
                        p = str(bs_lower.get("preset", "")).lower()
                        if "large_biomes" in p:
                            return "minecraft\\:large_biomes"
                        if "amplified" in p:
                            return "minecraft\\:amplified"
                        bst = str(bs_lower.get("type", "")).lower()
                        if "fixed" in bst or "single" in bst:
                            return "minecraft\\:single_biome_surface"

                    return "minecraft\\:normal"

    for value in node.values():
        if isinstance(value, dict):
            res = _extract_type(value)
            if res:
                return res
    return ""

def _world_files(world_dir: Path) -> list[Path]:
    return [
        world_dir / "level.dat",
        world_dir / "level.dat_old",
        world_dir / "data" / "minecraft" / "world_gen_settings.dat",
        world_dir / "data" / "world_gen_settings.dat",
    ]

def _info_from_nbt(nbt: dict | None) -> tuple[str, str]:
    if not nbt:
        return "", ""
    return _extract_seed(nbt), _extract_type(nbt)

def _cache_key(nbt_file: Path, info) -> tuple:
    mtime_ns = getattr(info, "st_mtime_ns", None)
    if mtime_ns is None:
        mtime_ns = int(info.st_mtime * 1_000_000_000)
    return nbt_file, mtime_ns, int(info.st_size)

def _file_info(nbt_file: Path) -> tuple[str, str]:
    try:
        info = nbt_file.stat()
    except FileNotFoundError:
        return "", ""
    except Exception:
        return _info_from_nbt(_read_nbt_file(nbt_file))

    try:
        if not stat.S_ISREG(info.st_mode):
            return "", ""
        key = _cache_key(nbt_file, info)
    except Exception:
        return _info_from_nbt(_read_nbt_file(nbt_file))

    with _info_cache_lock:
        cached = _info_cache.get(key)
        if cached is not None:
            _info_cache.move_to_end(key)
            return cached

    result = _info_from_nbt(_load_nbt_file(nbt_file))
    with _info_cache_lock:
        _info_cache[key] = result
        _info_cache.move_to_end(key)
        while len(_info_cache) > _INFO_CACHE_SIZE:
            _info_cache.popitem(last=False)
    return result

def get_world_seed(world_dir: Path) -> str:
    for target in _world_files(world_dir):
        seed = _file_info(target)[0]
        if seed:
            return seed
    return ""

def get_world_type(world_dir: Path) -> str:
    for target in _world_files(world_dir):
        wtype = _file_info(target)[1]
        if wtype:
            return wtype
    return ""

def get_world_info(world_dir: Path) -> tuple[str, str]:
    seed = ""
    wtype = ""
    for target in _world_files(world_dir):
        file_seed, file_type = _file_info(target)
        seed = seed or file_seed
        wtype = wtype or file_type
        if seed and wtype:
            break
    return seed, wtype

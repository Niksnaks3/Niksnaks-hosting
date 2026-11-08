import gzip
import os
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from niksnaks_hosting.shared.remote.paths import RemotePath
from niksnaks_hosting.shared.utils import nbt_utils
from niksnaks_hosting.shared.utils.nbt_utils import get_world_info, get_world_seed, get_world_type

SEED = -4172144997902289642

MODERN_WORLDGEN = {
    "seed": SEED,
    "generate_features": True,
    "dimensions": {
        "minecraft:overworld": {
            "type": "minecraft:overworld",
            "generator": {
                "type": "minecraft:noise",
                "settings": "minecraft:overworld",
                "biome_source": {"type": "minecraft:multi_noise", "preset": "minecraft:overworld"},
            },
        },
        "minecraft:the_nether": {
            "type": "minecraft:the_nether",
            "generator": {"type": "minecraft:noise", "settings": "minecraft:nether"},
        },
    },
}


def _string(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def _encode(value) -> tuple[int, bytes]:
    if isinstance(value, dict):
        body = b""
        for key, item in value.items():
            tag, data = _encode(item)
            body += bytes([tag]) + _string(key) + data
        return 10, body + b"\x00"
    if isinstance(value, list):
        items = [_encode(item) for item in value]
        item_tag = items[0][0] if items else 0
        return 9, bytes([item_tag]) + struct.pack(">i", len(items)) + b"".join(data for _tag, data in items)
    if isinstance(value, str):
        return 8, _string(value)
    if isinstance(value, bool):
        return 1, struct.pack(">b", int(value))
    if isinstance(value, int):
        return 4, struct.pack(">q", value)
    raise TypeError(value)


def nbt_bytes(root: dict, compress: bool = True) -> bytes:
    _tag, body = _encode(root)
    data = b"\x0a" + _string("") + body
    return gzip.compress(data) if compress else data


def level_dat(**data) -> bytes:
    return nbt_bytes({"Data": {"LevelName": "world", "version": 19133, **data}})


class _FolderFs:
    label = "test-host"

    def __init__(self, root: Path):
        self.root = root
        self.reads = 0

    def _local(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def stat(self, path: str):
        try:
            info = os.stat(self._local(path))
        except (FileNotFoundError, NotADirectoryError):
            return None
        return SimpleNamespace(st_mode=info.st_mode, st_size=info.st_size, st_mtime=int(info.st_mtime))

    def read_bytes(self, path: str) -> bytes:
        self.reads += 1
        return self._local(path).read_bytes()


class WorldInfoTests(unittest.TestCase):
    def setUp(self):
        nbt_utils._info_cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.world = self.root / "world"
        self.world.mkdir()

    def write(self, relative: str, data: bytes, mtime_s: int | None = None) -> Path:
        target = self.world / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if mtime_s is not None:
            os.utime(target, ns=(mtime_s * 1_000_000_000, mtime_s * 1_000_000_000))
        return target

    def count_reads(self):
        return mock.patch.object(Path, "read_bytes", autospec=True, side_effect=Path.read_bytes)

    def test_seed_and_type_from_modern_level_dat(self):
        self.write("level.dat", level_dat(WorldGenSettings=MODERN_WORLDGEN))
        self.assertEqual(get_world_info(self.world), (str(SEED), "minecraft\\:normal"))
        self.assertEqual(get_world_seed(self.world), str(SEED))
        self.assertEqual(get_world_type(self.world), "minecraft\\:normal")

    def test_world_types(self):
        cases = {
            "minecraft\\:flat": {"type": "minecraft:flat", "settings": {"layers": []}},
            "minecraft\\:amplified": {"type": "minecraft:noise", "settings": "minecraft:amplified"},
            "minecraft\\:large_biomes": {"type": "minecraft:noise", "settings": "minecraft:large_biomes"},
            "minecraft\\:single_biome_surface": {
                "type": "minecraft:noise",
                "settings": "minecraft:overworld",
                "biome_source": {"type": "minecraft:fixed", "biome": "minecraft:desert"},
            },
        }
        for expected, generator in cases.items():
            with self.subTest(expected=expected):
                nbt_utils._info_cache.clear()
                worldgen = {"seed": 7, "dimensions": {"minecraft:overworld": {"generator": generator}}}
                self.write("level.dat", level_dat(WorldGenSettings=worldgen))
                self.assertEqual(get_world_info(self.world), ("7", expected))

    def test_legacy_level_dat(self):
        self.write("level.dat", level_dat(RandomSeed=1234567, generatorName="largeBiomes"))
        self.assertEqual(get_world_info(self.world), ("1234567", "minecraft\\:large_biomes"))

    def test_uncompressed_level_dat(self):
        self.write("level.dat", nbt_bytes({"Data": {"RandomSeed": 99, "generatorName": "flat"}}, compress=False))
        self.assertEqual(get_world_info(self.world), ("99", "minecraft\\:flat"))

    def test_root_used_when_data_is_missing(self):
        self.write("level.dat", nbt_bytes({"WorldGenSettings": MODERN_WORLDGEN}))
        self.assertEqual(get_world_info(self.world), (str(SEED), "minecraft\\:normal"))

    def test_world_gen_settings_file_fills_in_what_level_dat_lacks(self):
        self.write("level.dat", level_dat(Time=100))
        self.write("level.dat_old", level_dat(Time=90))
        self.write(
            "data/minecraft/world_gen_settings.dat",
            nbt_bytes({"DataVersion": 4500, "data": MODERN_WORLDGEN}),
        )
        with self.count_reads() as reads:
            self.assertEqual(get_world_info(self.world), (str(SEED), "minecraft\\:normal"))
        self.assertEqual(reads.call_count, 3)

    def test_seed_and_type_may_come_from_different_files(self):
        self.write("level.dat", level_dat(RandomSeed=55))
        self.write("data/world_gen_settings.dat", nbt_bytes({"data": {"generatorName": "amplified"}}))
        self.assertEqual(get_world_info(self.world), ("55", "minecraft\\:amplified"))
        self.assertEqual(get_world_seed(self.world), "55")
        self.assertEqual(get_world_type(self.world), "minecraft\\:amplified")

    def test_broken_and_cut_off_files(self):
        good = level_dat(WorldGenSettings=MODERN_WORLDGEN)
        raw = nbt_bytes({"Data": {"RandomSeed": 31337, "generatorName": "flat"}}, compress=False)
        cases = {
            "empty": b"",
            "not nbt": b"definitely not an nbt file",
            "bad gzip": b"\x1f\x8b" + b"\x00" * 40,
            "cut-off gzip": good[: len(good) // 2],
            "cut-off nbt": raw[: len(raw) - 12],
            "not a compound": b"\x08" + _string("") + _string("text"),
            "unknown tag": b"\x0a" + _string("") + b"\x63" + _string("x") + b"\x00",
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                nbt_utils._info_cache.clear()
                for leftover in ("level.dat", "level.dat_old"):
                    (self.world / leftover).unlink(missing_ok=True)
                self.write("level.dat", data)
                self.assertEqual(get_world_info(self.world), ("", ""))
                self.write("level.dat_old", level_dat(RandomSeed=8, generatorName="default"))
                self.assertEqual(get_world_info(self.world), ("8", "minecraft\\:normal"))

    def test_missing_world(self):
        self.assertEqual(get_world_info(self.root / "nowhere"), ("", ""))
        self.assertEqual(get_world_seed(self.root / "nowhere"), "")
        self.assertEqual(get_world_type(self.root / "nowhere"), "")

    def test_folder_named_like_level_dat_is_skipped(self):
        (self.world / "level.dat").mkdir()
        self.write("level.dat_old", level_dat(RandomSeed=4, generatorName="flat"))
        self.assertEqual(get_world_info(self.world), ("4", "minecraft\\:flat"))

    def test_unchanged_file_is_read_once(self):
        self.write("level.dat", level_dat(WorldGenSettings=MODERN_WORLDGEN))
        with self.count_reads() as reads:
            first = get_world_info(self.world)
            second = get_world_info(self.world)
            self.assertEqual(get_world_seed(self.world), str(SEED))
            self.assertEqual(get_world_type(self.world), "minecraft\\:normal")
        self.assertEqual(first, second)
        self.assertEqual(reads.call_count, 1)

    def test_changed_file_is_read_again(self):
        self.write("level.dat", level_dat(RandomSeed=1, generatorName="flat"), mtime_s=1_700_000_000)
        self.assertEqual(get_world_info(self.world), ("1", "minecraft\\:flat"))
        self.write("level.dat", level_dat(RandomSeed=2, generatorName="amplified"), mtime_s=1_700_000_100)
        with self.count_reads() as reads:
            self.assertEqual(get_world_info(self.world), ("2", "minecraft\\:amplified"))
        self.assertEqual(reads.call_count, 1)

    def test_same_size_rewrite_with_new_mtime_is_read_again(self):
        self.write("level.dat", level_dat(RandomSeed=11111, generatorName="flat"), mtime_s=1_700_000_000)
        self.assertEqual(get_world_info(self.world), ("11111", "minecraft\\:flat"))
        self.write("level.dat", level_dat(RandomSeed=22222, generatorName="flat"), mtime_s=1_700_000_001)
        self.assertEqual(get_world_info(self.world), ("22222", "minecraft\\:flat"))

    def test_file_that_appears_later_is_read(self):
        self.assertEqual(get_world_info(self.world), ("", ""))
        self.write("level.dat", level_dat(RandomSeed=3, generatorName="default"))
        self.assertEqual(get_world_info(self.world), ("3", "minecraft\\:normal"))

    def test_cache_is_capped(self):
        target = self.write("level.dat", level_dat(RandomSeed=1, generatorName="flat"))
        for index in range(nbt_utils._INFO_CACHE_SIZE + 10):
            stamp = (1_700_000_000 + index) * 1_000_000_000
            os.utime(target, ns=(stamp, stamp))
            self.assertEqual(get_world_info(self.world), ("1", "minecraft\\:flat"))
        self.assertEqual(len(nbt_utils._info_cache), nbt_utils._INFO_CACHE_SIZE)
        os.utime(target, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        with self.count_reads() as reads:
            self.assertEqual(get_world_info(self.world), ("1", "minecraft\\:flat"))
        self.assertEqual(reads.call_count, 1)


class RemoteWorldInfoTests(unittest.TestCase):
    def setUp(self):
        nbt_utils._info_cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "srv" / "world").mkdir(parents=True)
        self.fs = _FolderFs(self.root)
        self.world = RemotePath(self.fs, "/srv/world")

    def write(self, data: bytes, mtime_s: int) -> None:
        target = self.root / "srv" / "world" / "level.dat"
        target.write_bytes(data)
        os.utime(target, ns=(mtime_s * 1_000_000_000, mtime_s * 1_000_000_000))

    def test_remote_world_is_cached_and_reread_after_a_change(self):
        self.write(level_dat(WorldGenSettings=MODERN_WORLDGEN), 1_700_000_000)
        self.assertEqual(get_world_info(self.world), (str(SEED), "minecraft\\:normal"))
        self.assertEqual(get_world_info(self.world), (str(SEED), "minecraft\\:normal"))
        self.assertEqual(self.fs.reads, 1)
        self.write(level_dat(RandomSeed=6, generatorName="flat"), 1_700_000_050)
        self.assertEqual(get_world_info(self.world), ("6", "minecraft\\:flat"))
        self.assertEqual(self.fs.reads, 2)

    def test_same_path_on_another_host_is_not_shared(self):
        self.write(level_dat(RandomSeed=12, generatorName="flat"), 1_700_000_000)
        other_fs = _FolderFs(self.root)
        self.assertEqual(get_world_info(self.world), ("12", "minecraft\\:flat"))
        self.assertEqual(get_world_info(RemotePath(other_fs, "/srv/world")), ("12", "minecraft\\:flat"))
        self.assertEqual(other_fs.reads, 1)

    def test_stat_failure_skips_the_cache(self):
        self.write(level_dat(RandomSeed=21, generatorName="amplified"), 1_700_000_000)
        with mock.patch.object(RemotePath, "stat", side_effect=OSError("stat is not available")):
            self.assertEqual(get_world_info(self.world), ("21", "minecraft\\:amplified"))
            self.assertEqual(get_world_info(self.world), ("21", "minecraft\\:amplified"))
        self.assertEqual(self.fs.reads, 2)
        self.assertEqual(len(nbt_utils._info_cache), 0)

    def test_stat_without_a_modification_time_skips_the_cache(self):
        self.write(level_dat(RandomSeed=22, generatorName="flat"), 1_700_000_000)
        original_stat = self.fs.stat

        def stat_without_mtime(path):
            info = original_stat(path)
            if info is not None:
                info.st_mtime = None
            return info

        self.fs.stat = stat_without_mtime
        self.assertEqual(get_world_info(self.world), ("22", "minecraft\\:flat"))
        self.assertEqual(get_world_info(self.world), ("22", "minecraft\\:flat"))
        self.assertEqual(self.fs.reads, 2)
        self.assertEqual(len(nbt_utils._info_cache), 0)


if __name__ == "__main__":
    unittest.main()

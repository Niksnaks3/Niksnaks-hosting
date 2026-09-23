"""The web panel's login, grants and mod routes, driven over real HTTP against a throwaway data folder.

Run with:  python -m unittest
"""

from __future__ import annotations

import http.cookiejar
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from PIL import Image

# Every module below reads DATA_DIR at import, so the folder has to be chosen first.
_TMP = tempfile.TemporaryDirectory()
os.environ["NIKSNAKS_HOSTING_DATA_DIR"] = _TMP.name

import niksnaks_hosting.i18n  # noqa: E402,F401  installs the _() builtin
from niksnaks_hosting.shared.backend import mod_installer  # noqa: E402
from niksnaks_hosting.shared.backend.bedrock_manager import BedrockVersionOption  # noqa: E402
from niksnaks_hosting.shared.backend.server_manager import ServerManager  # noqa: E402
from niksnaks_hosting.version import __version__  # noqa: E402
from niksnaks_hosting.web.auth import LOGIN_FAILURES_ALLOWED, SessionStore, UserStore  # noqa: E402
from niksnaks_hosting.web.panel import WebPanel  # noqa: E402


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.store = UserStore(path=__import__("pathlib").Path(_TMP.name) / "auth_test_users.json")

    def test_password_round_trip_and_persistence(self):
        self.store.set_user("alice", "correct horse", ["s1"], False)
        self.assertIsNotNone(self.store.verify("alice", "correct horse"))
        self.assertIsNone(self.store.verify("alice", "wrong"))
        self.assertIsNone(self.store.verify("nobody", "x"))
        reloaded = UserStore(path=self.store._path)
        self.assertEqual(reloaded.get("alice").servers, ["s1"])

    def test_update_keeps_password_when_none_given(self):
        self.store.set_user("bob", "secret-one", [], False)
        self.store.set_user("bob", None, ["s2"], True)
        self.assertIsNotNone(self.store.verify("bob", "secret-one"))
        self.assertTrue(self.store.get("bob").admin)
        with self.assertRaises(ValueError):
            self.store.set_user("new", None, [], False)

    def test_lockout_after_repeated_failures(self):
        self.store.set_user("carol", "right-password", [], False)
        sessions = SessionStore(self.store)
        for _ in range(LOGIN_FAILURES_ALLOWED):
            self.assertIsNone(sessions.login("carol", "nope", "10.0.0.1"))
        self.assertIsNone(sessions.login("carol", "right-password", "10.0.0.1"))
        self.assertIsNotNone(sessions.login("carol", "right-password", "10.0.0.2"))

    def test_username_rules(self):
        self.assertTrue(UserStore.valid_username("jan.de_vries-2"))
        self.assertFalse(UserStore.valid_username(""))
        self.assertFalse(UserStore.valid_username("has space"))
        self.assertFalse(UserStore.valid_username("x" * 33))


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager = ServerManager()
        cls.mine = cls.manager.add_server("Mine", "1.21.1", loader_version="0.16.0")
        cls.theirs = cls.manager.add_server("Theirs", "1.21.1")
        cls.bedrock = cls.manager.add_server("Rock", "1.21.0", edition="bedrock")
        cls.panel = WebPanel(cls.manager)
        cls.panel.users.set_user("alice", "hunter22", [cls.mine.id, cls.bedrock.id], False)
        cls.panel.users.set_user("boss", "adminpass", [], True)
        ok, msg = cls.panel.start(0)
        assert ok, msg
        cls.port = cls.panel._server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.panel.stop()

    def setUp(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method, path, body=None, header=True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        if header:
            req.add_header("X-Requested-With", "fetch")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def post_bytes(self, path, payload, content_type="image/png"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=payload, method="POST")
        req.add_header("X-Requested-With", "fetch")
        req.add_header("Content-Type", content_type)
        try:
            with self.opener.open(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def login(self, user="alice", password="hunter22"):
        status, _ = self.call("POST", "/api/login", {"username": user, "password": password})
        self.assertEqual(status, 200)

    def test_static_and_traversal(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as r:
            page = r.read()
        self.assertIn(b"<title>Niksnaks-Hosting</title>", page)
        # The script and stylesheet are asked for by version, so an update cannot leave
        # a browser on the old ones.
        self.assertIn(f'src="/app.js?v={__version__}"'.encode(), page)
        self.assertNotIn(b"__APP_VERSION__", page)
        self.assertEqual(self.call("GET", "/../pyproject.toml")[0], 404)

    def test_answers_carry_the_app_version(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/me")
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            self.assertEqual(e.headers["X-App-Version"], __version__)

    def test_login_needs_header_and_cookie_gates_routes(self):
        self.assertEqual(self.call("GET", "/api/me")[0], 401)
        creds = {"username": "alice", "password": "hunter22"}
        self.assertEqual(self.call("POST", "/api/login", creds, header=False)[0], 403)
        self.assertEqual(self.call("POST", "/api/login", {"username": "alice", "password": "bad"})[0], 401)
        self.login()
        self.assertEqual(self.call("GET", "/api/me")[1]["username"], "alice")
        self.call("POST", "/api/logout")
        self.assertEqual(self.call("GET", "/api/me")[0], 401)

    def test_grants_hide_other_servers(self):
        self.login()
        names = {s["name"] for s in self.call("GET", "/api/servers")[1]}
        self.assertEqual(names, {"Mine", "Rock"})
        self.assertEqual(self.call("GET", f"/api/servers/{self.theirs.id}")[0], 404)
        self.assertEqual(self.call("POST", f"/api/servers/{self.theirs.id}/start")[0], 404)
        self.assertEqual(self.call("GET", f"/api/servers/{self.mine.id}")[0], 200)

    def test_admin_sees_everything(self):
        self.login("boss", "adminpass")
        names = {s["name"] for s in self.call("GET", "/api/servers")[1]}
        self.assertEqual(names, {"Mine", "Theirs", "Rock"})

    def test_console_and_stats_on_stopped_server(self):
        self.login()
        self.assertEqual(self.call("GET", f"/api/servers/{self.mine.id}/console")[1], {"lines": []})
        self.assertEqual(self.call("POST", f"/api/servers/{self.mine.id}/console", {"command": "list"})[0], 409)
        stats = self.call("GET", f"/api/servers/{self.mine.id}/stats")[1]
        self.assertEqual(stats["status"], "stopped")
        self.assertIsNone(stats["cpu_percent"])

    def test_dashboard_stats_cover_granted_servers_only(self):
        self.assertEqual(self.call("GET", "/api/stats")[0], 401)
        self.login()
        data = self.call("GET", "/api/stats")[1]
        self.assertEqual(set(data["servers"]), {self.mine.id, self.bedrock.id})
        self.assertEqual(data["servers"][self.mine.id]["status"], "stopped")
        self.assertIsNone(data["servers"][self.bedrock.id]["tps"])
        if data["host"] is not None:
            self.assertGreater(data["host"]["mem_total_mb"], 0)
        mine = self.call("GET", f"/api/servers/{self.mine.id}")[1]
        self.assertIn("port", mine)
        self.assertTrue(mine["port"] is None or 1 <= mine["port"] <= 65535)

    def test_mod_routes(self):
        self.login()
        self.assertEqual(self.call("GET", f"/api/servers/{self.bedrock.id}/mods")[0], 409)
        mods_dir = self.mine.server_dir / "mods"
        mods_dir.mkdir(exist_ok=True)
        (mods_dir / "example.jar").write_bytes(b"jar")
        listed = self.call("GET", f"/api/servers/{self.mine.id}/mods")[1]
        self.assertEqual([m["filename"] for m in listed], ["example.jar"])
        self.assertEqual(self.call("DELETE", f"/api/servers/{self.mine.id}/mods/..%2Fserver.properties")[0], 400)
        self.assertEqual(self.call("DELETE", f"/api/servers/{self.mine.id}/mods/missing.jar")[0], 404)
        self.assertEqual(self.call("DELETE", f"/api/servers/{self.mine.id}/mods/EXAMPLE.jar")[0], 200)
        self.assertFalse((mods_dir / "example.jar").exists())
        self.assertEqual(self.call("POST", f"/api/servers/{self.mine.id}/mods/install", {})[0], 400)

    def test_settings_and_motd(self):
        self.login()
        (self.mine.server_dir / "server.properties").write_text("motd=old\nmax-players=20\n", encoding="utf-8")
        settings = self.call("GET", f"/api/servers/{self.mine.id}/settings")[1]
        self.assertEqual(settings["motd"], "old")
        self.assertTrue(settings["properties_readable"])
        self.assertTrue(settings["can_update"])
        motd_path = f"/api/servers/{self.mine.id}/settings/motd"
        self.assertEqual(self.call("POST", motd_path, {"motd": "two\nlines"})[0], 400)
        self.assertEqual(self.call("POST", motd_path, {"motd": "x" * 201})[0], 400)
        self.assertEqual(self.call("POST", motd_path, {"motd": "hello there"})[0], 200)
        written = (self.mine.server_dir / "server.properties").read_text(encoding="utf-8")
        self.assertIn("motd=hello there", written)
        self.assertIn("max-players=20", written)   # the rest of the file is left alone
        self.assertEqual(self.call("GET", f"/api/servers/{self.theirs.id}/settings")[0], 404)

    def test_bedrock_server_has_settings_too(self):
        self.login()
        base = f"/api/servers/{self.bedrock.id}"
        (self.bedrock.server_dir / "server.properties").write_text("server-name=old\ngamemode=survival\n", encoding="utf-8")
        status, settings = self.call("GET", f"{base}/settings")
        self.assertEqual(status, 200)
        self.assertEqual(settings["edition"], "bedrock")
        self.assertEqual(settings["motd"], "old")   # Bedrock's list line is server-name
        self.assertTrue(settings["can_update"])
        self.assertEqual(self.call("POST", f"{base}/settings/motd", {"motd": "Rocky"})[0], 200)
        written = (self.bedrock.server_dir / "server.properties").read_text(encoding="utf-8")
        self.assertIn("server-name=Rocky", written)
        self.assertNotIn("motd=", written)
        self.assertIn("gamemode=survival", written)

        buffer = io.BytesIO()
        Image.new("RGB", (64, 64), (200, 120, 10)).save(buffer, "PNG")
        self.assertEqual(self.post_bytes(f"{base}/icon", buffer.getvalue())[0], 200)
        self.assertTrue(self.call("GET", f"{base}/settings")[1]["has_icon"])

        offered = [
            BedrockVersionOption("1.21.90.3", "https://example.invalid/a.zip"),
            BedrockVersionOption("1.20.0.1", "https://example.invalid/b.zip", preview=True),
        ]
        original = self.manager.bedrock_versions
        self.manager.bedrock_versions = lambda info: offered
        try:
            versions = self.call("GET", f"{base}/update/versions")[1]
            self.assertEqual(versions["mc_versions"], ["1.21.90.3"])   # the older preview is not an update
            self.assertIn("1.21.90.3", versions["labels"])
            self.assertEqual(self.call("GET", f"{base}/update/loader-versions?mc_version=1.21.90.3")[0], 409)
            self.assertEqual(self.call("POST", f"{base}/update", {"mc_version": "9.9.9"})[0], 409)
        finally:
            self.manager.bedrock_versions = original

    def test_server_picture_round_trip(self):
        self.login()
        self.assertEqual(self.call("GET", f"/api/servers/{self.mine.id}/icon")[0], 404)
        self.assertEqual(self.post_bytes(f"/api/servers/{self.mine.id}/icon", b"")[0], 400)
        self.assertEqual(self.post_bytes(f"/api/servers/{self.mine.id}/icon", b"not an image")[0], 400)
        buffer = io.BytesIO()
        Image.new("RGB", (40, 90), (10, 120, 200)).save(buffer, "PNG")
        self.assertEqual(self.post_bytes(f"/api/servers/{self.mine.id}/icon", buffer.getvalue())[0], 200)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/servers/{self.mine.id}/icon")
        with self.opener.open(req, timeout=5) as r:
            self.assertEqual(r.headers["Content-Type"], "image/png")
            served = r.read()
        self.assertEqual(Image.open(io.BytesIO(served)).size, (128, 128))   # squared off on the way in
        self.assertTrue(self.call("GET", f"/api/servers/{self.mine.id}/settings")[1]["has_icon"])
        self.assertEqual(self.post_bytes(f"/api/servers/{self.theirs.id}/icon", buffer.getvalue())[0], 404)

    def test_update_route_checks_its_arguments(self):
        self.login()
        self.assertEqual(self.call("POST", f"/api/servers/{self.mine.id}/update", {})[0], 400)
        self.assertEqual(self.call("GET", f"/api/servers/{self.mine.id}/update/loader-versions")[0], 400)
        self.assertEqual(self.call("POST", f"/api/servers/{self.theirs.id}/update", {"mc_version": "1.21.4"})[0], 404)

    def test_event_stream_relays_console_output(self):
        self.login()
        got = []

        def reader():
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/servers/{self.mine.id}/events")
            with self.opener.open(req, timeout=10) as r:
                for raw in r:
                    got.append(raw.decode().strip())
                    if any(line.startswith("data:") for line in got):
                        break

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        process = self.manager.get_process(self.mine.id)
        import time

        time.sleep(0.3)
        process._emit_output("hello\n")
        thread.join(5)
        self.assertIn('data: {"event": "output", "text": "hello\\n"}', got)

    def test_event_stream_ends_when_the_login_is_removed(self):
        self.login()
        got = []

        def reader():
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/servers/{self.mine.id}/events")
            with self.opener.open(req, timeout=10) as r:
                for raw in r:
                    got.append(raw.decode().strip())

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        import time

        time.sleep(0.3)
        self.panel.sessions.logout_user("alice")
        self.manager.get_process(self.mine.id)._emit_output("secret\n")
        thread.join(5)
        self.assertFalse(thread.is_alive(), "the stream should have been closed")
        self.assertFalse(any("secret" in line for line in got))


class ModInstallerTests(unittest.TestCase):
    def setUp(self):
        self.root = __import__("pathlib").Path(tempfile.mkdtemp(dir=_TMP.name))
        (self.root / "mods").mkdir()
        for name in ("sodium.jar", "lithium.jar", "fabric-api.jar"):
            (self.root / "mods" / name).write_bytes(b"x")
        from niksnaks_hosting.shared.backend.modrinth_client import ModrinthVersion

        self.dep = ModrinthVersion("v9", "P3", "Fabric API", "fapi", "0.9", [], [], "", "http://x", "fabric-api.jar")
        state = mod_installer.ModState(self.root)
        state.record_mod("A1", "Sodium", "v1", "sodium.jar", "0.5")
        state.record_dependencies("sodium.jar", [self.dep])
        state.record_dependencies("lithium.jar", [self.dep])

    def test_list_installed_reads_bookkeeping(self):
        mods = {m.filename: m for m in mod_installer.list_installed(self.root)}
        self.assertEqual(mods["sodium.jar"].title, "Sodium")
        self.assertEqual(mods["fabric-api.jar"].dependents, ["lithium.jar", "sodium.jar"])

    def test_orphaned_dependency_goes_with_last_dependent(self):
        self.assertEqual(mod_installer.remove_mod(self.root, "sodium.jar")[0], True)
        self.assertTrue((self.root / "mods" / "fabric-api.jar").exists())
        self.assertEqual(mod_installer.remove_mod(self.root, "lithium.jar")[0], True)
        self.assertFalse((self.root / "mods" / "fabric-api.jar").exists())
        self.assertEqual(mod_installer.installed_jar_names(self.root), [])

    def test_find_jar_rejects_paths(self):
        self.assertIsNone(mod_installer.find_jar(self.root, "../server.properties"))
        self.assertIsNone(mod_installer.find_jar(self.root, "mods/sodium.jar"))
        self.assertIsNotNone(mod_installer.find_jar(self.root, "SODIUM.JAR"))


if __name__ == "__main__":
    unittest.main()

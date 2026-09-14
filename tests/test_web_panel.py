"""The web panel's login, grants and mod routes, driven over real HTTP against a throwaway data folder.

Run with:  python -m unittest
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

# Every module below reads DATA_DIR at import, so the folder has to be chosen first.
_TMP = tempfile.TemporaryDirectory()
os.environ["NIKSNAKS_HOSTING_DATA_DIR"] = _TMP.name

import niksnaks_hosting.i18n  # noqa: E402,F401  installs the _() builtin
from niksnaks_hosting.shared.backend import mod_installer  # noqa: E402
from niksnaks_hosting.shared.backend.server_manager import ServerManager  # noqa: E402
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

    def login(self, user="alice", password="hunter22"):
        status, _ = self.call("POST", "/api/login", {"username": user, "password": password})
        self.assertEqual(status, 200)

    def test_static_and_traversal(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as r:
            self.assertIn(b"<title>Niksnaks-Hosting</title>", r.read())
        self.assertEqual(self.call("GET", "/../pyproject.toml")[0], 404)

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

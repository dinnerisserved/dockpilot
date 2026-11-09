"""The background service (dockpilot_core.py run directly) answers its socket API without hardware,
writes status.json, and refuses a second copy."""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "dockpilot")
sys.path.insert(0, ROOT)


def test_daemon_socket_and_status(tmp_path):
    env = dict(os.environ, XDG_RUNTIME_DIR=str(tmp_path), XDG_CACHE_HOME=str(tmp_path / "cache"),
               XDG_CONFIG_HOME=str(tmp_path / "config"), HOME=str(tmp_path))
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "dockpilot_core.py")], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        import dockpilot_core as dockpilotd
        dockpilotd.SOCKET_PATH = str(tmp_path / "dockpilot.sock")
        for _ in range(50):
            if dockpilotd.already_running():
                break
            time.sleep(0.1)
        assert dockpilotd.already_running()
        r = dockpilotd.ask("status")
        assert r["ok"] and "opendock" in r and "rules" in r and "nic" in r
        r = dockpilotd.ask("od_set", key="name", value="x")
        assert r["ok"] is False and "no OpenDock" in r["message"]
        assert dockpilotd.ask("nonsense")["ok"] is False
        time.sleep(1.5)
        st = json.load(open(tmp_path / "cache" / "dockpilot" / "status.json"))
        assert set(st) >= {"time", "nic", "opendock", "rules"}
        # a second instance exits at once
        q = subprocess.run([sys.executable, os.path.join(ROOT, "dockpilot_core.py")], env=env, capture_output=True, text=True, timeout=10)
        assert q.returncode == 0 and "already running" in q.stderr
    finally:
        p.terminate()
        p.wait(5)


def test_ensure_extension_enabled_adds_the_uuid(monkeypatch, tmp_path):
    """With a fake Gio.Settings: the uuid is appended once, never duplicated, and gnome-extensions is asked to load it."""
    import types
    sys.path.insert(0, ROOT); import dockpilot_core as core  # noqa: E402
    calls = []
    store = {"enabled-extensions": ["other@x"]}

    class FakeSettings:
        def __init__(self, schema): calls.append(("new", schema))
        def get_strv(self, k): return list(store[k])
        def set_strv(self, k, v): store[k] = list(v)
        def sync(self): pass

    class FakeSource:
        def lookup(self, name, recursive): return object() if name == "org.gnome.shell" else None
    fake_gio = types.SimpleNamespace(Settings=types.SimpleNamespace(new=lambda schema: FakeSettings(schema)),
                                     SettingsSchemaSource=types.SimpleNamespace(get_default=lambda: FakeSource()))
    fake_repo = types.SimpleNamespace(Gio=fake_gio)
    monkeypatch.setitem(sys.modules, "gi", types.SimpleNamespace(repository=fake_repo))
    monkeypatch.setitem(sys.modules, "gi.repository", fake_repo)
    monkeypatch.setattr(core.os.path, "isdir", lambda p: p.endswith(core.EXTENSION_UUID))
    ran = []
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: ran.append(a[0]))
    assert core.ensure_extension_enabled() is True
    assert store["enabled-extensions"] == ["other@x", core.EXTENSION_UUID]
    assert ran == [["gnome-extensions", "enable", core.EXTENSION_UUID]]
    assert core.ensure_extension_enabled() is False                 # already there: nothing written
    assert store["enabled-extensions"] == ["other@x", core.EXTENSION_UUID]


def test_ensure_extension_enabled_without_the_extension_installed(monkeypatch):
    sys.path.insert(0, ROOT); import dockpilot_core as core  # noqa: E402
    monkeypatch.setattr(core.os.path, "isdir", lambda p: False)
    assert core.ensure_extension_enabled() is False

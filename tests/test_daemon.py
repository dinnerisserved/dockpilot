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

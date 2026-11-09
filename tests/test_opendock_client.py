"""The OpenDock client's firmware-file checks and the web-face upload (firmware 1.0, C line)."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dockpilot"))
import opendock  # noqa: E402
# ---------------------------------------------------------------- firmware files and the web face (firmware 1.0, C line)
def _uf2(n=4, family=0xE48BFF59):
    import struct
    out = b""
    for i in range(n):
        out += struct.pack("<8I", 0x0A324655, 0x9E5D5157, 0x2000, 0x10000000 + 256 * i, 256, i, n, family) + bytes(256) + bytes(220) + struct.pack("<I", 0x0AB16F30)
    return out


def test_check_uf2(tmp_path):
    good = tmp_path / "opendock-dev.uf2"; good.write_bytes(_uf2(6))
    ok, msg = opendock.check_uf2(str(good))
    assert ok and msg == "6 blocks, 3 KB"
    # the real thing starts with picotool's E10 marker block (family absolute at 0x10FFFF00, extension flag)
    import struct
    e10 = struct.pack("<8I", 0x0A324655, 0x9E5D5157, 0xA000, 0x10FFFF00, 256, 0, 2, 0xE48BFF57) + bytes(256) + struct.pack("<I", 0x9957E304) + bytes(216) + struct.pack("<I", 0x0AB16F30)
    real = tmp_path / "opendock-dev-real.uf2"; real.write_bytes(e10 + _uf2(6))
    ok, msg = opendock.check_uf2(str(real))
    assert ok and msg == "6 blocks, 3 KB", msg
    inst = tmp_path / "opendock-install-dev.uf2"; inst.write_bytes(_uf2(2, 0xE48BFF57))
    ok, msg = opendock.check_uf2(str(inst))
    assert not ok and "installer" in msg
    other = tmp_path / "x.uf2"; other.write_bytes(_uf2(2, 0xE48BFF5A))
    assert opendock.check_uf2(str(other)) == (False, "that file is not for this dock's chip")
    junk = tmp_path / "junk.uf2"; junk.write_bytes(b"A" * 1024)
    assert opendock.check_uf2(str(junk)) == (False, "not a firmware file (.uf2)")
    short = tmp_path / "short.uf2"; short.write_bytes(_uf2(2)[:-10])
    assert opendock.check_uf2(str(short))[0] is False
    assert opendock.check_uf2(str(tmp_path / "missing.uf2"))[0] is False


def test_update_firmware_posts_with_the_header(tmp_path, monkeypatch):
    import io
    import urllib.request
    f = tmp_path / "opendock-dev.uf2"; f.write_bytes(_uf2(3))
    seen = {}

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_open(req, timeout=0):
        seen["url"] = req.full_url; seen["headers"] = dict(req.header_items()); seen["body"] = req.data; seen["method"] = req.get_method()
        return R(b"OK update 3 blocks, 768 bytes; restarting\n")
    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    line = opendock.update_firmware({"host": "opendock.local", "ip": "169.254.77.1"}, str(f))
    assert line.startswith("OK update 3 blocks")
    assert seen["url"] == "http://169.254.77.1/update" and seen["method"] == "POST"
    assert seen["headers"].get("X-opendock") == "1" and seen["body"] == _uf2(3)


def test_update_firmware_refusal_and_silence(tmp_path, monkeypatch):
    import urllib.error
    import urllib.request
    f = tmp_path / "opendock-dev.uf2"; f.write_bytes(_uf2(3))

    def refused(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, __import__("io").BytesIO(b"ERR badfile blocks out of order\n"))
    monkeypatch.setattr(urllib.request, "urlopen", refused)
    with pytest.raises(opendock.DockRefused) as e:
        opendock.update_firmware({"ip": "169.254.77.1"}, str(f))
    assert e.value.code == "badfile" and e.value.message == "blocks out of order"

    def silent(req, timeout=0):
        raise OSError("no route")
    monkeypatch.setattr(urllib.request, "urlopen", silent)
    with pytest.raises(opendock.DockGone):
        opendock.update_firmware({"ip": "169.254.77.1"}, str(f))


def test_describe_fw_update():
    assert opendock.describe({"i": 9, "t": None, "up": 5, "ev": "fw_update", "v": 1}).endswith("firmware update (new firmware written, restarting into it)")
    assert opendock.describe({"i": 9, "t": None, "up": 5, "ev": "fw_update", "v": 2}).endswith("firmware update (new firmware running)")

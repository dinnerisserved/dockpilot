"""ASIX chips are named from (vid, pid, bcdDevice) the way the vendor drivers match them."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dockpilot"))
import dockpilot_core as core  # noqa: E402
asix_ids = core


def test_gigabit_generations():
    assert asix_ids.asix_identity(0x0b95, 0x1790, 0x0100)["name"] == "AX88179"
    assert asix_ids.asix_identity(0x0b95, 0x1790, 0x0200)["name"] == "AX88179A"
    assert asix_ids.asix_identity(0x0b95, 0x1790, 0x0300)["name"] == "AX88772D"
    assert asix_ids.asix_identity(0x0b95, 0x1790, 0x0400)["name"] == "AX88279"
    assert asix_ids.asix_identity(0x0b95, 0x1790, 0x0500)["name"] == "AX88279A"
    assert asix_ids.asix_identity(0x0b95, 0x178a, 0x0100)["name"] == "AX88178A"


def test_rebrands_keep_the_chip_and_name_the_brand():
    r = asix_ids.asix_identity(0x17ef, 0x304b, 0x0200)
    assert r["name"] == "AX88179A" and r["sold_as"] == "Lenovo"
    r = asix_ids.asix_identity(0x05ac, 0x1402, 0x0001)
    assert r["name"] == "AX88772A" and "Apple" in r["sold_as"]


def test_772b_vs_772c_by_bcd():
    assert asix_ids.asix_identity(0x0b95, 0x772b, 1)["name"] == "AX88772B"
    assert asix_ids.asix_identity(0x0b95, 0x772b, 2)["name"] == "AX88772C"
    assert asix_ids.asix_identity(0x0b95, 0x772b, None)["name"] == "AX88772B/C"


def test_unknown_is_none():
    assert asix_ids.asix_identity(0x0bda, 0x8153, 0x3100) is None     # that's a Realtek


def test_chip_identity_routes_asix_and_realtek():
    a = core.chip_identity({"vid": "0b95", "pid": "1790", "bcd": "0200", "driver": "ax88179_178a"})
    assert a["vendor"] == "ASIX" and a["name"] == "AX88179A"
    r = core.chip_identity({"vid": "0bda", "pid": "8153", "bcd": "3100", "driver": "cdc_ncm"})
    assert r["vendor"] == "Realtek" and "RTL8153" in r["name"]

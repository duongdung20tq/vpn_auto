"""A secret DPAPI-encrypted by another Windows user/machine must read as "not saved", never crash
the app at startup (pywintypes.error 'Key not valid for use in specified state.')."""
import base64
import json

import pywintypes
import win32crypt

from vpn_auto import config as config_mod
from vpn_auto import dpapi, rdp_store


def foreign_token(plaintext="fake-password"):
    """A real DPAPI blob this app cannot open (different entropy), like one from another user."""
    blob = win32crypt.CryptProtectData(plaintext.encode("utf-8"), "other", b"other-entropy", None, None, 0)
    return base64.b64encode(blob).decode("ascii")


def test_roundtrip():
    assert dpapi.decrypt(dpapi.encrypt("fake-password")) == "fake-password"
    assert dpapi.decrypt("") == ""


def test_foreign_or_corrupt_token_reads_as_empty():
    assert dpapi.decrypt(foreign_token()) == ""
    assert dpapi.decrypt("not base64!") == ""


def test_key_not_valid_error_reads_as_empty(monkeypatch):
    def raise_bad_key(*_):
        raise pywintypes.error(-2146893813, "CryptUnprotectData", "Key not valid for use in specified state.")
    monkeypatch.setattr(win32crypt, "CryptUnprotectData", raise_bad_key)
    assert dpapi.decrypt(dpapi.encrypt("fake-password")) == ""


def test_rdp_store_with_foreign_password_loads_and_can_be_resaved(tmp_path, monkeypatch):
    monkeypatch.setenv("VPN_AUTO_HOME", str(tmp_path))
    rdp_store.store_path().write_text(json.dumps({"last_ip": "10.0.0.1", "targets": [
        {"ip": "10.0.0.1", "username": "user-a", "password_dpapi": foreign_token()}]}), encoding="utf-8")

    targets, last = rdp_store.load()
    assert last == "10.0.0.1"
    assert [(t.ip, t.username, t.password) for t in targets] == [("10.0.0.1", "user-a", "")]

    rdp_store.save(rdp_store.Target("10.0.0.1", "user-a", "new-password"))
    assert rdp_store.get("10.0.0.1").password == "new-password"


def test_config_with_foreign_secrets_is_incomplete(tmp_path, monkeypatch):
    monkeypatch.setenv("VPN_AUTO_HOME", str(tmp_path))
    data = config_mod.Config(profile="profile-a", username="vpn-user").to_file_dict()
    data.update(password_dpapi=foreign_token(), pin_dpapi=foreign_token("0000"))
    config_mod.config_path().write_text(json.dumps(data), encoding="utf-8")

    loaded = config_mod.load()
    assert loaded.password == "" and loaded.pin == ""
    assert not loaded.is_complete()

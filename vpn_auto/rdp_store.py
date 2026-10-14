"""Saved RDP targets in %APPDATA%\\vpn_auto\\rdp_targets.json; passwords are DPAPI-encrypted."""
import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import dpapi
from .config import app_dir


def store_path():
    return app_dir() / "rdp_targets.json"


@dataclass
class Target:
    ip: str = ""
    username: str = ""
    password: str = ""


def load():
    """Return (list[Target] newest-first, last_ip)."""
    path = store_path()
    if not path.exists():
        return [], ""
    data = json.loads(path.read_text(encoding="utf-8"))
    targets = []
    for item in data.get("targets", []):
        targets.append(Target(item.get("ip", ""), item.get("username", ""),
                              dpapi.decrypt(item.get("password_dpapi", ""))))
    return targets, data.get("last_ip", "")


def save(target, make_last=True):
    """Insert/update a target by IP (keeps an existing password if the new one is blank)."""
    targets, last = load()
    by_ip = {t.ip: t for t in targets}
    existing = by_ip.get(target.ip)
    if existing and not target.password:
        target.password = existing.password
    others = [t for t in targets if t.ip != target.ip]
    ordered = [target] + others
    _write(ordered, target.ip if make_last else last)


def remove(ip):
    targets, last = load()
    ordered = [t for t in targets if t.ip != ip]
    _write(ordered, "" if last == ip else last)


def get(ip):
    for t in load()[0]:
        if t.ip == ip:
            return t
    return None


def _write(targets, last_ip):
    data = {"last_ip": last_ip, "targets": [
        {"ip": t.ip, "username": t.username,
         "password_dpapi": dpapi.encrypt(t.password) if t.password else ""}
        for t in targets]}
    path = store_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)

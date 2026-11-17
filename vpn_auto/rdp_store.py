"""Saved RDP targets in %APPDATA%\\vpn_auto\\rdp_targets.json; passwords are DPAPI-encrypted.

Each machine (IP) keeps its own settings, including the multi-remote session count - one
machine's count is never applied to another.
"""
import json
import os
from dataclasses import dataclass

from . import dpapi
from .config import app_dir

MAX_SESSIONS = 10


def store_path():
    return app_dir() / "rdp_targets.json"


def clamp_count(value):
    """Session count as an int in 1..MAX_SESSIONS; anything unreadable counts as 1."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(MAX_SESSIONS, n))


@dataclass
class Target:
    ip: str = ""
    username: str = ""
    password: str = ""
    multi: bool = False  # open several RDP sessions to this machine
    count: int = 1       # how many sessions when multi is on (kept even when multi is off)

    @property
    def sessions(self):
        return clamp_count(self.count) if self.multi else 1


def load():
    """Return (list[Target] newest-first, last_ip)."""
    path = store_path()
    if not path.exists():
        return [], ""
    data = json.loads(path.read_text(encoding="utf-8"))
    targets = []
    for item in data.get("targets", []):
        targets.append(Target(item.get("ip", ""), item.get("username", ""),
                              dpapi.decrypt(item.get("password_dpapi", "")),
                              bool(item.get("multi", False)), clamp_count(item.get("count", 1))))
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
         "password_dpapi": dpapi.encrypt(t.password) if t.password else "",
         "multi": bool(t.multi), "count": clamp_count(t.count)}
        for t in targets]}
    path = store_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)

"""Deployment file `vpn_auto.ini` placed next to the exe.

Imported into the machine's DPAPI config, after which its password and PIN
lines are wiped so the secrets do not stay on disk in clear text. The content
hash is remembered so later starts do not override changes made in the UI.
"""
import configparser
import hashlib
import logging
import os
import re
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from . import config as config_mod

log = logging.getLogger(__name__)

FILE_NAME = "vpn_auto.ini"
_TEXT_KEYS = ("backend", "profile", "server", "group", "username", "password", "pin")
_BOOL_KEYS = ("save_settings", "auto_connect", "logoff_others", "block_rdp")
_SECRET_LINE = re.compile(r"^(\s*(?:password|pin)\s*[=:]).*$", re.IGNORECASE | re.MULTILINE)

TEMPLATE = """\
; VPN Auto - file cấu hình triển khai
; Đặt file này cạnh VpnAuto.exe rồi chạy VpnAuto.exe.
; Lần chạy đầu tool nhập cấu hình vào máy (mã hóa DPAPI theo tài khoản Windows)
; và tự xóa giá trị password/pin khỏi file này.

[vpn]
; backend: cisco (Cisco Secure Client) hoặc pulse (Pulse Secure)
backend = {backend}
; Pulse: tên kết nối trong Pulse. Để trống: tự tìm theo "server".
profile = {profile}
; Cisco: địa chỉ gateway, ví dụ btcvpn.mof.gov.vn. Pulse: Server URL.
server = {server}
; Cisco: nhóm kết nối (tunnel group), ví dụ Cert-kyso.
group = {group}
; Tài khoản VPN
username = {username}
password =
; PIN token FPT-CA
pin =
; 1 = tích "Save settings" trong hộp thoại đăng nhập Pulse (chỉ Pulse)
save_settings = {save_settings}
; 1 = tự động kết nối khi có mạng, 0 = chỉ tự điền khi bấm Connect bằng tay
auto_connect = {auto_connect}
; 1 = trước mỗi lần kết nối, đăng xuất mọi phiên Windows trừ Administrator và tài khoản đang chạy tool
logoff_others = {logoff_others}
; 1 = tắt Remote Desktop khi bắt đầu kết nối, bật lại khi VPN kết nối xong
; (cả hai mục trên cần chạy VpnAuto.exe bằng quyền Administrator)
block_rdp = {block_rdp}
"""


def default_path():
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    return base / FILE_NAME


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_values(path):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8-sig")
    if not parser.has_section("vpn"):
        raise ValueError(f"{path}: thiếu mục [vpn]")
    section = parser["vpn"]
    values = {k: section.get(k, "").strip() for k in _TEXT_KEYS if section.get(k, "").strip()}
    for key in _BOOL_KEYS:
        if section.get(key, "").strip():
            values[key] = section.getboolean(key)
    return values


def scrub_secrets(path):
    """Blank the password/pin values in place, keeping comments. Returns the new text."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    cleaned = _SECRET_LINE.sub(r"\1", text)
    if cleaned != text:
        stamp = f"; Đã nhập vào máy {os.environ.get('COMPUTERNAME', '')} lúc {datetime.now():%Y-%m-%d %H:%M}, đã xóa password/pin.\n"
        cleaned = stamp + cleaned
        path.write_text(cleaned, encoding="utf-8")
    return cleaned


def import_file(path, cfg):
    """Merge the ini into `cfg`, save it, wipe the ini's secrets. Returns (new config, imported keys)."""
    values = read_values(path)
    new_cfg = replace(cfg, **values)
    try:
        text = scrub_secrets(path)
    except OSError as exc:
        log.warning("Không xóa được mật khẩu/PIN trong %s: %s", path, exc)
        text = Path(path).read_text(encoding="utf-8-sig")
    new_cfg.deploy_hash = _digest(text)
    config_mod.save(new_cfg)
    log.info("Đã nhập %s từ %s", ", ".join(sorted(values)) or "không có giá trị", path)
    return new_cfg, sorted(values)


def import_if_changed(cfg, path=None):
    """Import the ini next to the exe when present and not imported before in this exact content."""
    path = Path(path or default_path())
    if not path.exists():
        return cfg
    if _digest(path.read_text(encoding="utf-8-sig")) == cfg.deploy_hash:
        return cfg
    return import_file(path, cfg)[0]


def write_template(path, cfg):
    Path(path).write_text(TEMPLATE.format(
        backend=cfg.backend, profile=cfg.profile, server=cfg.server, group=cfg.group,
        username=cfg.username, save_settings=int(cfg.save_settings), auto_connect=int(cfg.auto_connect),
        logoff_others=int(cfg.logoff_others), block_rdp=int(cfg.block_rdp),
    ), encoding="utf-8")

"""Settings stored in %APPDATA%\\vpn_auto\\config.json; PIN and password are DPAPI-encrypted."""
import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import dpapi
from .cisco import CISCO_CLI
from .pulse import PULSE_EXE

SECRET_FIELDS = ("pin", "password")
BACKEND_PULSE = "pulse"
BACKEND_CISCO = "cisco"
CISCO_DEFAULT_SERVER = "btcvpn.mof.gov.vn"
CISCO_DEFAULT_GROUP = "Cert-kyso"


def app_dir():
    base = os.environ.get("VPN_AUTO_HOME") or os.path.join(os.environ["APPDATA"], "vpn_auto")
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path():
    return app_dir() / "config.json"


def log_path():
    return app_dir() / "vpn_auto.log"


@dataclass
class Config:
    backend: str = BACKEND_PULSE  # "pulse" or "cisco"
    profile: str = ""
    server: str = ""
    group: str = ""  # Cisco tunnel group, e.g. Cert-kyso
    username: str = ""
    password: str = ""
    pin: str = ""
    save_settings: bool = True
    auto_connect: bool = True
    check_interval: float = 3.0
    backoff_base: float = 10.0
    backoff_max: float = 300.0
    busy_timeout: float = 300.0
    prompt_close_timeout: float = 20.0
    confirm_after: float = 30.0
    connect_timeout: float = 150.0  # Cisco: posture + hostscan can take ~50 s
    state_poll: float = 6.0         # Cisco: how often to run `vpncli state`
    max_pin_attempts: int = 1
    max_account_attempts: int = 2
    network_check: bool = True
    logoff_others: bool = True     # before each connect: log off sessions other than Administrator/me
    block_rdp: bool = True         # Remote Desktop off from connect start until connected
    pulse_exe: str = PULSE_EXE
    cisco_cli: str = CISCO_CLI
    deploy_hash: str = ""  # vpn_auto.ini content already imported on this machine

    def normalize(self):
        """Fill the fixed Cisco defaults so gateway/group are always set for Cisco."""
        if self.backend == BACKEND_CISCO:
            self.server = self.server or CISCO_DEFAULT_SERVER
            self.group = self.group or CISCO_DEFAULT_GROUP
        return self

    def is_complete(self):
        if self.backend == BACKEND_CISCO:
            server = self.server or CISCO_DEFAULT_SERVER
            return bool(server and self.username and self.password and self.pin)
        return bool((self.profile or self.server) and self.username and self.password and self.pin)

    def to_file_dict(self):
        data = asdict(self)
        for name in SECRET_FIELDS:
            data[f"{name}_dpapi"] = dpapi.encrypt(data.pop(name)) if getattr(self, name) else ""
        return data

    @classmethod
    def from_file_dict(cls, data):
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known and k not in SECRET_FIELDS}
        for name in SECRET_FIELDS:
            values[name] = dpapi.decrypt(data.get(f"{name}_dpapi", ""))
        return cls(**values)


def load(path=None):
    path = Path(path or config_path())
    if not path.exists():
        return Config()
    return Config.from_file_dict(json.loads(path.read_text(encoding="utf-8"))).normalize()


def save(config, path=None):
    path = Path(path or config_path())
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config.to_file_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)

from vpn_auto import config as config_mod
from vpn_auto import deploy
from vpn_auto.network import server_address

INI = """\
; comment kept
[vpn]
profile =
server = vpn.example.test
username = vpn-user
password = fake#pass$1
pin = 0000
save_settings = 1
auto_connect = 0
"""


def test_import_wipes_secrets_and_keeps_them_encrypted(tmp_path, monkeypatch):
    monkeypatch.setenv("VPN_AUTO_HOME", str(tmp_path / "home"))
    ini = tmp_path / "vpn_auto.ini"
    ini.write_text(INI, encoding="utf-8")

    cfg, keys = deploy.import_file(ini, config_mod.Config())

    assert keys == ["auto_connect", "password", "pin", "save_settings", "server", "username"]
    assert (cfg.server, cfg.username, cfg.password, cfg.pin) == ("vpn.example.test", "vpn-user", "fake#pass$1", "0000")
    assert cfg.auto_connect is False and cfg.is_complete()
    text = ini.read_text(encoding="utf-8")
    assert "fake#pass$1" not in text and "0000" not in text
    assert "; comment kept" in text and "username = vpn-user" in text
    assert "password =\n" in text and "pin =\n" in text
    assert config_mod.load() == cfg


def test_import_if_changed_runs_once_per_content(tmp_path, monkeypatch):
    monkeypatch.setenv("VPN_AUTO_HOME", str(tmp_path / "home"))
    ini = tmp_path / "vpn_auto.ini"
    ini.write_text(INI, encoding="utf-8")
    cfg = deploy.import_if_changed(config_mod.Config(), ini)
    cfg.auto_connect = True  # changed later in the window
    assert deploy.import_if_changed(cfg, ini).auto_connect is True
    ini.write_text(INI.replace("vpn-user", "other-user"), encoding="utf-8")
    assert deploy.import_if_changed(cfg, ini).username == "other-user"


def test_import_keeps_existing_secrets_when_ini_has_none(tmp_path, monkeypatch):
    monkeypatch.setenv("VPN_AUTO_HOME", str(tmp_path / "home"))
    ini = tmp_path / "vpn_auto.ini"
    ini.write_text("[vpn]\nusername = new-user\npassword =\n", encoding="utf-8")
    cfg, _ = deploy.import_file(ini, config_mod.Config(password="kept", pin="1111"))
    assert (cfg.username, cfg.password, cfg.pin) == ("new-user", "kept", "1111")


def test_template_has_no_secrets(tmp_path):
    path = tmp_path / "out.ini"
    deploy.write_template(path, config_mod.Config(server="vpn.example.test", username="vpn-user",
                                                  password="fake", pin="0000"))
    values = deploy.read_values(path)
    assert values["server"] == "vpn.example.test" and "password" not in values and "pin" not in values


def test_server_address():
    assert server_address("vpn.example.test") == ("vpn.example.test", 443)
    assert server_address("https://vpn.example.test:8443/x") == ("vpn.example.test", 8443)

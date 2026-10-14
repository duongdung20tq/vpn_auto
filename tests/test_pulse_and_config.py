import json

from vpn_auto import config as config_mod
from vpn_auto.pulse import ACCOUNT_ERROR, TOKEN_ERROR, State, parse_item_texts, state_from_button

# Texts observed on a Pulse 9.1.4 connection list item.
CONNECTED_TEXTS = ['Connect', 'Server URL:', 'vpn.example.test', 'Status:', 'Connected', 'Details:',
                   'Compliance:', 'Meets security policies', 'Click to show details', 'profile-a',
                   'Connected', 'Disconnect']
FAILED_TEXTS = ['Connect', 'Server URL:', 'vpn.example.test', 'Status:', 'Connecting; Failed', 'Details:',
                'Compliance:', 'Click to show details', 'profile-a', 'Connecting; Failed', 'Cancel',
                '<a>User input timeout.</a>']


def test_state_from_button_labels():
    assert state_from_button("Disconnect") is State.CONNECTED
    assert state_from_button("Cancel") is State.CONNECTING
    assert state_from_button("&Connect") is State.DISCONNECTED
    assert state_from_button("???") is State.UNKNOWN
    assert state_from_button("Connect", "Disconnecting") is State.DISCONNECTING
    assert state_from_button("Connect", "Disconnected") is State.DISCONNECTED


def test_parse_item_texts():
    assert parse_item_texts(CONNECTED_TEXTS) == "Connected"
    assert parse_item_texts(FAILED_TEXTS) == "Connecting; Failed; User input timeout."
    assert parse_item_texts([]) == ""


def test_token_error_patterns_match_fptca_messages():
    for message in ("User Pin is Wrong! Retry times : 3", "User Pin has been blocked !",
                    "Mã PIN sai! Số lần còn lại: 3", "Mã PIN đã bị khóa!", "FPT-CA Token đã được rút ra!"):
        assert TOKEN_ERROR.search(message), message
    assert not TOKEN_ERROR.search("Now need to verify your User PIN:")


def test_account_error_pattern_ignores_timeouts():
    assert ACCOUNT_ERROR.search("Failed; Invalid username or password")
    assert not ACCOUNT_ERROR.search("Connecting; Failed; User input timeout.")


def test_config_roundtrip_keeps_secrets_encrypted(tmp_path, monkeypatch):
    monkeypatch.setenv("VPN_AUTO_HOME", str(tmp_path))
    cfg = config_mod.Config(profile="profile-a", username="vpn-user", password="fake-password", pin="0000")
    config_mod.save(cfg)
    raw = config_mod.config_path().read_text(encoding="utf-8")
    assert "fake-password" not in raw and '"0000"' not in raw
    stored = json.loads(raw)
    assert stored["pin_dpapi"] and stored["password_dpapi"]
    loaded = config_mod.load()
    assert loaded == cfg
    assert loaded.is_complete()


def test_missing_config_is_incomplete(tmp_path, monkeypatch):
    monkeypatch.setenv("VPN_AUTO_HOME", str(tmp_path))
    assert not config_mod.load().is_complete()

import pytest

from vpn_auto import prehook


@pytest.fixture(autouse=True)
def _no_real_session_changes(monkeypatch):
    """Tests must never log off sessions or toggle Remote Desktop on the dev machine."""
    state = {"deny": 0}
    monkeypatch.setattr(prehook, "rdp_guard", prehook.RdpGuard(
        read=lambda: state["deny"], write=lambda deny: state.update(deny=int(deny))))
    monkeypatch.setattr(prehook, "logoff_others", lambda: 0)

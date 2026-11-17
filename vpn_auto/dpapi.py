"""Windows DPAPI: secrets can only be decrypted by the same Windows user on this machine."""
import base64
import logging

import pywintypes
import win32crypt

_ENTROPY = b"vpn_auto/pulse-fptca"

log = logging.getLogger(__name__)


def encrypt(plaintext):
    blob = win32crypt.CryptProtectData(plaintext.encode("utf-8"), "vpn_auto", _ENTROPY, None, None, 0)
    return base64.b64encode(blob).decode("ascii")


def decrypt(token):
    """Return the secret, or "" when it was encrypted by another Windows user/machine (a copied
    %APPDATA%\\vpn_auto folder, a profile migrated or a password reset by an admin) or is corrupt.
    Such a secret is unusable here, so callers treat it as "not saved" and the user re-enters it."""
    if not token:
        return ""
    try:
        _, data = win32crypt.CryptUnprotectData(base64.b64decode(token), _ENTROPY, None, None, 0)
    except (pywintypes.error, ValueError) as exc:
        log.warning("Saved secret cannot be decrypted by this Windows user/machine, ignoring it: %s", exc)
        return ""
    return data.decode("utf-8")

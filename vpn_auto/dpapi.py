"""Windows DPAPI: secrets can only be decrypted by the same Windows user on this machine."""
import base64

import win32crypt

_ENTROPY = b"vpn_auto/pulse-fptca"


def encrypt(plaintext):
    blob = win32crypt.CryptProtectData(plaintext.encode("utf-8"), "vpn_auto", _ENTROPY, None, None, 0)
    return base64.b64encode(blob).decode("ascii")


def decrypt(token):
    if not token:
        return ""
    _, data = win32crypt.CryptUnprotectData(base64.b64decode(token), _ENTROPY, None, None, 0)
    return data.decode("utf-8")

"""Short-lived, single-use pairing without distributing a browser secret."""
import hashlib
import hmac
import secrets
import time


class Pairing:
    def __init__(self, token):
        self.token = hmac.new(token.encode(), b"fast-tabber-extension-v1", hashlib.sha256).hexdigest()
        self.code = None
        self.expires = 0

    def issue(self):
        self.code = secrets.token_hex(16)
        self.expires = time.monotonic() + 300
        return {"pairingCode": self.code, "expiresInSeconds": 300,
                "steps": ["Open the Fast-Tabber Chrome toolbar popup", "Paste this code into Pair with agent and click Pair",
                          "Reply here; the agent must check browser_status to confirm the connection"]}

    def consume(self, code):
        if not isinstance(code, str) or not self.code or time.monotonic() >= self.expires:
            return False
        if not hmac.compare_digest(code, self.code):
            return False
        self.code = None
        return True

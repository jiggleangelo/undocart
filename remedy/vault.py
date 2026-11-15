import secrets
from threading import Lock


class Vault:
    def __init__(self):
        self._values = {}
        self._lock = Lock()

    def put(self, value: str) -> str:
        token = "vault_" + secrets.token_urlsafe(18)
        with self._lock:
            self._values[token] = str(value)
        return token

    def has(self, token: str) -> bool:
        with self._lock:
            return token in self._values

    def count(self) -> int:
        with self._lock:
            return len(self._values)

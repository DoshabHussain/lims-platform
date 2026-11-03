"""Users, roles, and Part 11 style electronic signatures for one lab (tenant)."""
from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass

from .clock import SystemClock
from .errors import EntitlementError, PermissionDenied, SignatureError

_ITERATIONS = 100_000


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)


@dataclass(frozen=True)
class User:
    user_id: str
    full_name: str
    roles: frozenset


class UserDirectory:
    def __init__(self, tenant_id: str, max_users: int | None = None):
        self.tenant_id = tenant_id
        self.max_users = max_users
        self._users: dict[str, tuple[User, bytes, bytes]] = {}

    def list_users(self) -> list[User]:
        return [entry[0] for entry in self._users.values()]

    def count(self) -> int:
        return len(self._users)

    def add_user(self, user_id, full_name, password, roles) -> User:
        if self.max_users is not None and self.count() >= self.max_users:
            raise EntitlementError(f"User limit ({self.max_users}) reached")
        salt = os.urandom(16)
        user = User(user_id, full_name, frozenset(roles))
        self._users[user_id] = (user, salt, _hash(password, salt))
        return user

    def get(self, user_id: str) -> User:
        entry = self._users.get(user_id)
        if entry is None:
            raise PermissionDenied("Unknown user")
        return entry[0]

    def verify_password(self, user_id: str, password: str) -> bool:
        entry = self._users.get(user_id)
        if entry is None:
            return False
        return hmac.compare_digest(entry[2], _hash(password, entry[1]))


@dataclass(frozen=True)
class Signature:
    record_id: str
    signer_id: str
    signer_name: str
    meaning: str
    timestamp: str


class ESigner:
    def __init__(self, users: UserDirectory, clock=None):
        self._users = users
        self._clock = clock or SystemClock()

    def sign(self, user_id, password, meaning, record_id) -> Signature:
        if not self._users.verify_password(user_id, password or ""):
            raise SignatureError("Signature authentication failed")
        user = self._users.get(user_id)
        return Signature(record_id, user_id, user.full_name, meaning, self._clock.now().isoformat())

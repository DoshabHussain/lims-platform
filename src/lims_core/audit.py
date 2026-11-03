"""Append-only, hash-chained audit trail."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass

from .clock import SystemClock
from .errors import IntegrityError

GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    tenant_id: str
    actor: str
    action: str
    entity_type: str
    entity_id: str
    details: dict
    reason: str | None
    timestamp: str
    prev_hash: str
    hash: str


def _digest(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _payload(entry: AuditEntry) -> dict:
    data = asdict(entry)
    data.pop("hash")
    return data


class AuditTrail:
    def __init__(self, tenant_id: str, clock=None):
        self.tenant_id = tenant_id
        self._clock = clock or SystemClock()
        self._chain: list[AuditEntry] = []

    def append(self, actor, action, entity_type, entity_id, details=None, reason=None) -> AuditEntry:
        payload = {
            "seq": len(self._chain) + 1,
            "tenant_id": self.tenant_id,
            "actor": actor,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "details": copy.deepcopy(details or {}),
            "reason": reason,
            "timestamp": self._clock.now().isoformat(),
            "prev_hash": self._chain[-1].hash if self._chain else GENESIS_HASH,
        }
        entry = AuditEntry(**payload, hash=_digest(payload))
        self._chain.append(entry)
        return entry

    def entries(self, entity_id: str | None = None) -> tuple[AuditEntry, ...]:
        if entity_id is None:
            return tuple(self._chain)
        return tuple(e for e in self._chain if e.entity_id == entity_id)

    def verify(self) -> bool:
        prev = GENESIS_HASH
        for expected_seq, entry in enumerate(self._chain, start=1):
            if entry.seq != expected_seq:
                raise IntegrityError(f"Sequence gap at entry {expected_seq}")
            if entry.prev_hash != prev:
                raise IntegrityError(f"Broken chain at entry {entry.seq}")
            if _digest(_payload(entry)) != entry.hash:
                raise IntegrityError(f"Entry {entry.seq} was modified")
            prev = entry.hash
        return True

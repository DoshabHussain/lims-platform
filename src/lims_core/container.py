"""Builds an isolated workspace (one per lab / tenant)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .amv import AmvService
from .audit import AuditTrail
from .clock import SystemClock
from .config import ProductCatalog
from .entitlements import EntitlementService
from .errors import NotFoundError, ValidationError
from .identity import ESigner, UserDirectory
from .instruments import InstrumentRegistry
from .lab import LabService
from .sentinel import SentinelConfig, SentinelService
from .stability import StabilityService


@dataclass
class Workspace:
    tenant_id: str
    audit: AuditTrail
    users: UserDirectory
    catalog: ProductCatalog
    instruments: InstrumentRegistry
    lab: LabService
    stability: StabilityService
    amv: AmvService
    sentinel: SentinelService


class LimsPlatform:
    def __init__(self, clock=None):
        self.clock = clock or SystemClock()
        self.entitlements = EntitlementService(self.clock)
        self._workspaces: dict[str, Workspace] = {}

    def onboard(self, tenant_id: str, plan_name: str, expires_at: datetime,
                sentinel_config: SentinelConfig | None = None) -> Workspace:
        if tenant_id in self._workspaces:
            raise ValidationError(f"Tenant '{tenant_id}' already exists")
        self.entitlements.subscribe(tenant_id, plan_name, expires_at)
        plan = self.entitlements.plan_for(tenant_id)
        audit = AuditTrail(tenant_id, self.clock)
        users = UserDirectory(tenant_id, plan.max_users)
        signer = ESigner(users, self.clock)
        catalog = ProductCatalog(tenant_id, audit, users, self.entitlements)
        instruments = InstrumentRegistry(tenant_id, audit, users, self.clock)
        lab = LabService(tenant_id, catalog, instruments, audit, users, signer,
                         self.entitlements, self.clock)
        ws = Workspace(
            tenant_id, audit, users, catalog, instruments, lab,
            StabilityService(tenant_id, catalog, audit, users, self.entitlements, self.clock),
            AmvService(tenant_id, audit, users, self.entitlements),
            SentinelService(tenant_id, lab, audit, users, self.entitlements, self.clock,
                            sentinel_config))
        self._workspaces[tenant_id] = ws
        return ws

    def workspace(self, tenant_id: str) -> Workspace:
        if tenant_id not in self._workspaces:
            raise NotFoundError(f"Unknown tenant '{tenant_id}'")
        return self._workspaces[tenant_id]

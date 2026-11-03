"""Subscription plans and module gating."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .clock import SystemClock
from .errors import EntitlementError


@dataclass(frozen=True)
class Plan:
    name: str
    modules: frozenset
    max_users: int


PLANS = {
    "basic": Plan("basic", frozenset({"lims"}), 10),
    "professional": Plan("professional", frozenset({"lims", "stability", "amv"}), 50),
    "enterprise": Plan("enterprise", frozenset({"lims", "stability", "amv", "sentinel"}), 500),
}


@dataclass
class Subscription:
    tenant_id: str
    plan: Plan
    expires_at: datetime
    active: bool = True


class EntitlementService:
    def __init__(self, clock=None):
        self._clock = clock or SystemClock()
        self._subs: dict[str, Subscription] = {}

    def subscribe(self, tenant_id: str, plan_name: str, expires_at: datetime) -> Subscription:
        if plan_name not in PLANS:
            raise EntitlementError(f"Unknown plan '{plan_name}'")
        sub = Subscription(tenant_id, PLANS[plan_name], expires_at)
        self._subs[tenant_id] = sub
        return sub

    def plan_for(self, tenant_id: str) -> Plan:
        sub = self._subs.get(tenant_id)
        if sub is None:
            raise EntitlementError("No subscription for tenant")
        return sub.plan

    def cancel(self, tenant_id: str) -> None:
        sub = self._subs.get(tenant_id)
        if sub is None:
            raise EntitlementError("No subscription for tenant")
        sub.active = False

    def assert_module(self, tenant_id: str, module: str) -> None:
        sub = self._subs.get(tenant_id)
        if sub is None:
            raise EntitlementError("No subscription for tenant")
        if not sub.active:
            raise EntitlementError("Subscription is cancelled")
        if self._clock.now() >= sub.expires_at:
            raise EntitlementError("Subscription has expired")
        if module not in sub.plan.modules:
            raise EntitlementError(f"Module '{module}' is not in the {sub.plan.name} plan")

"""Analytical Method Validation: statistics and acceptance checks."""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from .errors import PermissionDenied, ValidationError

AMV_ROLES = frozenset({"analyst", "qa_manager"})


@dataclass(frozen=True)
class AmvCheck:
    name: str
    value: float
    criterion: str
    passed: bool
    details: dict = field(default_factory=dict)


def precision(values, max_rsd: float, name: str = "Precision") -> AmvCheck:
    values = list(values)
    if len(values) < 2:
        raise ValidationError("Precision needs at least two replicates")
    mean = statistics.fmean(values)
    if mean == 0:
        raise ValidationError("Mean is zero, RSD is undefined")
    rsd = statistics.stdev(values) / abs(mean) * 100
    return AmvCheck(name, rsd, f"RSD <= {max_rsd}%", rsd <= max_rsd, {"mean": mean, "n": len(values)})


def accuracy(pairs, low: float = 98.0, high: float = 102.0) -> AmvCheck:
    """pairs: (amount added, amount found). Every recovery must lie within the limits."""
    pairs = list(pairs)
    if not pairs:
        raise ValidationError("Accuracy needs data")
    recoveries = []
    for added, found in pairs:
        if added <= 0:
            raise ValidationError("Amount added must be positive")
        recoveries.append(found / added * 100)
    mean = statistics.fmean(recoveries)
    ok = all(low <= r <= high for r in recoveries)
    return AmvCheck("Accuracy", mean, f"recovery {low}-{high}%", ok, {"recoveries": recoveries})


def linearity(x, y, min_r2: float = 0.999) -> AmvCheck:
    x, y = list(x), list(y)
    if len(x) != len(y) or len(x) < 5:
        raise ValidationError("Linearity needs at least five matched points")
    if len(set(x)) < 2:
        raise ValidationError("Concentrations must vary")
    if len(set(y)) < 2:
        raise ValidationError("Responses must vary")
    slope, intercept = statistics.linear_regression(x, y)
    r2 = statistics.correlation(x, y) ** 2
    return AmvCheck("Linearity", r2, f"r2 >= {min_r2}", r2 >= min_r2,
                    {"slope": slope, "intercept": intercept})


def lod_loq(residual_sd: float, slope: float) -> dict:
    if slope == 0:
        raise ValidationError("Slope cannot be zero")
    if residual_sd < 0:
        raise ValidationError("Residual standard deviation cannot be negative")
    return {"LOD": 3.3 * residual_sd / abs(slope), "LOQ": 10 * residual_sd / abs(slope)}


class AmvService:
    def __init__(self, tenant_id, audit, users, entitlements):
        self.tenant_id, self.audit, self.users, self.entitlements = tenant_id, audit, users, entitlements

    def assess(self, actor_id: str, method: str, checks) -> dict:
        self.entitlements.assert_module(self.tenant_id, "amv")
        if not self.users.get(actor_id).roles & AMV_ROLES:
            raise PermissionDenied("Role may not assess method validation")
        checks = list(checks)
        if not checks:
            raise ValidationError("No validation checks supplied")
        overall = "PASS" if all(c.passed for c in checks) else "FAIL"
        report = {"method": method, "overall": overall, "assessed_by": actor_id,
                  "checks": [{"name": c.name, "value": c.value, "criterion": c.criterion,
                              "passed": c.passed} for c in checks]}
        self.audit.append(actor_id, "AMV_ASSESSED", "method", method,
                          {"overall": overall, "checks": [c.name for c in checks]})
        return report

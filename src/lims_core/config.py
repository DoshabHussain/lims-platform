"""Lab configuration: products and versioned, immutable specifications."""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

from .errors import NotFoundError, PermissionDenied, ValidationError
from .formula import Formula, Specification

CONFIG_ROLES = frozenset({"admin", "qa_manager"})


@dataclass(frozen=True)
class ParameterSpec:
    """One test parameter. With a formula, results are calculated from raw inputs."""
    name: str
    unit: str
    spec: Specification
    formula: str | None = None
    method: str | None = None
    instrument_required: bool = False

    def __post_init__(self):
        if not self.name.strip():
            raise ValidationError("Parameter needs a name")
        if self.formula is not None:
            Formula(self.formula)  # fail early on bad formulas

    @property
    def inputs(self) -> tuple[str, ...]:
        if self.formula is None:
            return ("result",)
        return tuple(sorted(Formula(self.formula).variables))

    def compute(self, values: dict) -> float:
        if self.formula is not None:
            return Formula(self.formula).evaluate(values)
        v = values.get("result")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(v) or math.isinf(v):
            raise ValidationError("'result' must be a finite number")
        return float(v)


@dataclass(frozen=True)
class ProductSpec:
    product_code: str
    version: int
    parameters: tuple[ParameterSpec, ...]

    def parameter(self, name: str) -> ParameterSpec:
        for p in self.parameters:
            if p.name == name:
                return p
        raise NotFoundError(f"Parameter '{name}' is not in specification v{self.version}")


class ProductCatalog:
    def __init__(self, tenant_id, audit, users, entitlements):
        self.tenant_id, self.audit, self.users, self.entitlements = tenant_id, audit, users, entitlements
        self._products: dict[str, str] = {}
        self._specs: dict[str, list[ProductSpec]] = {}

    def _gate(self, actor_id):
        self.entitlements.assert_module(self.tenant_id, "lims")
        if not self.users.get(actor_id).roles & CONFIG_ROLES:
            raise PermissionDenied("Role may not change laboratory configuration")

    def define_product(self, actor_id, code: str, name: str) -> None:
        self._gate(actor_id)
        if not code.strip() or not name.strip():
            raise ValidationError("Product needs a code and a name")
        if code in self._products:
            raise ValidationError(f"Product '{code}' already exists")
        self._products[code] = name
        self.audit.append(actor_id, "PRODUCT_DEFINED", "product", code, {"name": name})

    def product_name(self, code: str) -> str:
        if code not in self._products:
            raise NotFoundError(f"Unknown product '{code}'")
        return self._products[code]

    def publish_spec(self, actor_id, code: str, parameters) -> ProductSpec:
        self._gate(actor_id)
        self.product_name(code)
        params = tuple(parameters)
        if not params:
            raise ValidationError("A specification needs at least one parameter")
        names = [p.name for p in params]
        if len(set(names)) != len(names):
            raise ValidationError("Duplicate parameter names")
        versions = self._specs.setdefault(code, [])
        spec = ProductSpec(code, len(versions) + 1, params)
        versions.append(spec)
        self.audit.append(actor_id, "SPEC_PUBLISHED", "product", code,
                          {"version": spec.version, "parameters": names})
        return spec

    def list_products(self) -> dict[str, str]:
        return dict(self._products)

    def list_specs(self, code: str) -> list[ProductSpec]:
        if code not in self._products:
            raise NotFoundError(f"Unknown product '{code}'")
        return list(self._specs.get(code, []))

    def get_spec(self, code: str, version: int | None = None) -> ProductSpec:
        versions = self._specs.get(code)
        if not versions:
            raise NotFoundError(f"No specification published for '{code}'")
        if version is None:
            return versions[-1]
        if not 1 <= version <= len(versions):
            raise NotFoundError(f"'{code}' has no specification version {version}")
        return versions[version - 1]

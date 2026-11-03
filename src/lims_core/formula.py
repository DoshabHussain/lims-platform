"""Safe formula evaluation, pharmacopoeial rounding, and specification limits."""
from __future__ import annotations

import ast
import math
import operator
from decimal import ROUND_HALF_UP, Decimal

from .errors import ValidationError

_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub,
           ast.Mult: operator.mul, ast.Div: operator.truediv}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {"round": round, "abs": abs, "min": min, "max": max}


def round_half_up(value: float, decimals: int) -> float:
    """Round the way pharmacopoeias expect (0.5 always rounds up), not banker's rounding."""
    if math.isnan(value) or math.isinf(value):
        raise ValidationError("Cannot round non-finite numbers")
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP))


class Formula:
    """Whitelisted arithmetic only. Never uses eval()."""

    def __init__(self, expression: str):
        try:
            self._tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise ValidationError(f"Invalid formula: {exc.msg}") from exc
        self.expression = expression
        self.variables = self._check(self._tree.body)

    def _check(self, node) -> set[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return set()
        if isinstance(node, ast.Name):
            return {node.id}
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            return self._check(node.left) | self._check(node.right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return self._check(node.operand)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in _FUNCS and not node.keywords:
            names: set[str] = set()
            for arg in node.args:
                names |= self._check(arg)
            return names
        raise ValidationError(f"Unsupported element in formula: {type(node).__name__}")

    def evaluate(self, values: dict) -> float:
        missing = self.variables - set(values)
        if missing:
            raise ValidationError(f"Missing values: {sorted(missing)}")
        for name in self.variables:
            v = values[name]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(v) or math.isinf(v):
                raise ValidationError(f"Value for '{name}' must be a finite number")
        try:
            res = self._eval(self._tree.body, values)
            if not isinstance(res, (int, float)) or math.isnan(res) or math.isinf(res):
                raise ValidationError("Formula resulted in a non-finite value")
            return float(res)
        except ZeroDivisionError as exc:
            raise ValidationError("Division by zero in formula") from exc

    def _eval(self, node, values):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.BinOp):
            return _BINOPS[type(node.op)](self._eval(node.left, values), self._eval(node.right, values))
        if isinstance(node, ast.UnaryOp):
            return _UNARY[type(node.op)](self._eval(node.operand, values))
        return _FUNCS[node.func.id](*[self._eval(a, values) for a in node.args])


class Specification:
    """Acceptance limits. The result is rounded to `decimals` before comparison, if given."""

    def __init__(self, low: float | None = None, high: float | None = None,
                 decimals: int | None = None):
        if low is None and high is None:
            raise ValidationError("A specification needs at least one limit")
        if low is not None and high is not None and low > high:
            raise ValidationError("Lower limit exceeds upper limit")
        if decimals is not None and decimals < 0:
            raise ValidationError("Decimals cannot be negative")
        self.low, self.high, self.decimals = low, high, decimals

    def evaluate(self, result: float) -> str:
        if isinstance(result, bool) or not isinstance(result, (int, float)) or math.isnan(result) or math.isinf(result):
            raise ValidationError("Result must be a finite real number")
        r = round_half_up(result, self.decimals) if self.decimals is not None else result
        if self.low is not None and r < self.low:
            return "OOS"
        if self.high is not None and r > self.high:
            return "OOS"
        return "PASS"

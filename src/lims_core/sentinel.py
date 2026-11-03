"""Data Integrity Sentinel.

Continuously screens laboratory results and the audit trail for the patterns that
inspectors associate with data-integrity failures, then scores the risk per analyst,
instrument, and for the system as a whole. Pure functions, so every detector is unit-testable.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from .errors import IntegrityError, PermissionDenied

SEVERITY_WEIGHT = {"HIGH": 10, "MEDIUM": 4, "LOW": 1}
SENTINEL_ROLES = frozenset({"qa_manager"})
CRITICAL_ACTIONS_NEEDING_REASON = frozenset({"OOS_PHASE1", "OOS_PHASE2", "REJECT"})


@dataclass(frozen=True)
class ResultRecord:
    sample_id: str
    parameter: str
    attempt: int
    value: float
    status: str
    analyst: str
    instrument_id: str | None
    entered_at: datetime
    spec_low: float | None = None
    spec_high: float | None = None
    decimals: int | None = None
    authorized_retest: bool = False


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    subject: str
    message: str
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SentinelConfig:
    utc_offset_hours: float = 5.0          # local time used for working-hours checks
    work_start_hour: int = 7
    work_end_hour: int = 19
    work_days: frozenset = frozenset({0, 1, 2, 3, 4})
    off_hours_ratio: float = 0.30
    off_hours_min_n: int = 5
    edge_fraction: float = 0.05            # "near a limit" = within 5% of spec width
    edge_ratio: float = 0.40
    edge_min_n: int = 10
    duplicate_min_repeats: int = 3
    duplicate_min_decimals: int = 3
    digit_min_n: int = 50
    digit_chi2_critical: float = 21.67     # chi-square, 9 d.o.f., p = 0.01
    rapid_seconds: int = 20
    rapid_min_pairs: int = 3
    sigfail_count: int = 3
    sigfail_window_minutes: int = 15


def _decimals_of(value: float) -> int:
    exp = Decimal(str(value)).as_tuple().exponent
    return max(0, -exp) if isinstance(exp, int) else 0


# ---- result detectors -------------------------------------------------------
def detect_testing_into_compliance(records, cfg: SentinelConfig | None = None) -> list[Finding]:
    groups = defaultdict(list)
    for r in records:
        groups[(r.sample_id, r.parameter)].append(r)
    findings = []
    for (sample, param), items in groups.items():
        items.sort(key=lambda r: r.attempt)
        flagged = False
        for i, r in enumerate(items):
            if r.status == "PASS" and not r.authorized_retest \
                    and any(p.status == "OOS" for p in items[:i]):
                findings.append(Finding(
                    "TESTING_INTO_COMPLIANCE", "HIGH", r.analyst,
                    f"{sample}/{param}: a passing retest followed an OOS result without an authorised investigation",
                    {"sample": sample, "parameter": param, "attempts": len(items)}))
                flagged = True
                break
        if not flagged and len(items) >= 3:
            findings.append(Finding(
                "EXCESSIVE_RETESTING", "MEDIUM", items[-1].analyst,
                f"{sample}/{param} was tested {len(items)} times",
                {"sample": sample, "parameter": param, "attempts": len(items)}))
    return findings


def detect_limit_clustering(records, cfg: SentinelConfig) -> list[Finding]:
    groups = defaultdict(list)
    for r in records:
        if r.status == "PASS" and r.spec_low is not None and r.spec_high is not None \
                and r.spec_high > r.spec_low:
            groups[(r.analyst, r.parameter)].append(r)
    findings = []
    for (analyst, param), items in groups.items():
        if len(items) < cfg.edge_min_n:
            continue
        near = 0
        for r in items:
            band = (r.spec_high - r.spec_low) * cfg.edge_fraction
            if abs(r.value - r.spec_low) <= band or abs(r.spec_high - r.value) <= band:
                near += 1
        ratio = near / len(items)
        if ratio >= cfg.edge_ratio:
            findings.append(Finding(
                "RESULTS_CLUSTER_AT_LIMIT", "MEDIUM", analyst,
                f"{param}: {ratio:.0%} of {len(items)} passing results sit within "
                f"{cfg.edge_fraction:.0%} of a specification limit",
                {"parameter": param, "ratio": round(ratio, 3), "n": len(items)}))
    return findings


def detect_duplicate_values(records, cfg: SentinelConfig) -> list[Finding]:
    seen = defaultdict(lambda: defaultdict(set))
    for r in records:
        if _decimals_of(r.value) >= cfg.duplicate_min_decimals:
            seen[(r.analyst, r.parameter)][r.value].add(r.sample_id)
    findings = []
    for (analyst, param), values in seen.items():
        for value, samples in values.items():
            if len(samples) >= cfg.duplicate_min_repeats:
                findings.append(Finding(
                    "DUPLICATE_RESULTS", "HIGH", analyst,
                    f"{param}: identical value {value} reported for {len(samples)} different samples",
                    {"parameter": param, "value": value, "samples": sorted(samples)}))
    return findings


def detect_terminal_digit_bias(records, cfg: SentinelConfig) -> list[Finding]:
    digits = defaultdict(list)
    for r in records:
        if r.decimals is not None:
            digits[r.analyst].append(f"{r.value:.{r.decimals}f}"[-1])
    findings = []
    for analyst, ds in digits.items():
        n = len(ds)
        if n < cfg.digit_min_n:
            continue
        counts = Counter(ds)
        expected = n / 10
        chi2 = sum((counts.get(str(d), 0) - expected) ** 2 / expected for d in range(10))
        if chi2 > cfg.digit_chi2_critical:
            findings.append(Finding(
                "TERMINAL_DIGIT_BIAS", "MEDIUM", analyst,
                f"Last-digit distribution is not uniform (chi-square {chi2:.1f}, n={n})",
                {"chi2": round(chi2, 2), "n": n, "counts": dict(sorted(counts.items()))}))
    return findings


def _local(dt: datetime, cfg: SentinelConfig) -> datetime:
    return dt + timedelta(hours=cfg.utc_offset_hours)


def detect_off_hours_entry(records, cfg: SentinelConfig) -> list[Finding]:
    groups = defaultdict(list)
    for r in records:
        groups[r.analyst].append(r)
    findings = []
    for analyst, items in groups.items():
        if len(items) < cfg.off_hours_min_n:
            continue
        off = 0
        for r in items:
            t = _local(r.entered_at, cfg)
            if t.weekday() not in cfg.work_days or not (cfg.work_start_hour <= t.hour < cfg.work_end_hour):
                off += 1
        ratio = off / len(items)
        if ratio >= cfg.off_hours_ratio:
            findings.append(Finding(
                "OFF_HOURS_ENTRY", "MEDIUM", analyst,
                f"{ratio:.0%} of {len(items)} results were entered outside working hours",
                {"ratio": round(ratio, 3), "n": len(items)}))
    return findings


def detect_rapid_entry(records, cfg: SentinelConfig) -> list[Finding]:
    groups = defaultdict(list)
    for r in records:
        groups[r.analyst].append(r)
    findings = []
    for analyst, items in groups.items():
        items.sort(key=lambda r: r.entered_at)
        pairs = sum(
            1 for a, b in zip(items, items[1:])
            if a.sample_id != b.sample_id
            and (b.entered_at - a.entered_at).total_seconds() < cfg.rapid_seconds)
        if pairs >= cfg.rapid_min_pairs:
            findings.append(Finding(
                "IMPLAUSIBLY_FAST_ENTRY", "MEDIUM", analyst,
                f"{pairs} result pairs on different samples were entered less than "
                f"{cfg.rapid_seconds}s apart (verify instrument integration or batch upload)",
                {"pairs": pairs}))
    return findings


# ---- audit-trail review -----------------------------------------------------
def review_audit_trail(entries, cfg: SentinelConfig) -> list[Finding]:
    findings = []
    failures = defaultdict(list)
    for e in entries:
        if e.action == "SIGNATURE_FAILED":
            failures[e.actor].append(datetime.fromisoformat(e.timestamp))
        if e.action in CRITICAL_ACTIONS_NEEDING_REASON and not (e.reason and e.reason.strip()):
            findings.append(Finding(
                "MISSING_REASON", "HIGH", e.actor,
                f"{e.action} on {e.entity_id} has no recorded reason", {"seq": e.seq}))
    window = timedelta(minutes=cfg.sigfail_window_minutes)
    for actor, times in failures.items():
        times.sort()
        for i in range(len(times)):
            in_window = [t for t in times[i:] if t - times[i] <= window]
            if len(in_window) >= cfg.sigfail_count:
                findings.append(Finding(
                    "REPEATED_SIGNATURE_FAILURES", "HIGH", actor,
                    f"{len(in_window)} failed e-signature attempts within "
                    f"{cfg.sigfail_window_minutes} minutes", {"count": len(in_window)}))
                break
    return findings


# ---- scoring ----------------------------------------------------------------
def risk_level(score: int) -> str:
    if score >= 10:
        return "HIGH"
    if score >= 4:
        return "MEDIUM"
    return "LOW" if score > 0 else "NONE"


@dataclass(frozen=True)
class SentinelReport:
    generated_at: str
    findings: tuple
    scores: dict
    risk_by_subject: dict
    overall_risk: str
    chain_intact: bool
    records_analyzed: int
    entries_analyzed: int

    def summary(self) -> str:
        lines = [f"Data integrity review: overall risk {self.overall_risk}",
                 f"{self.records_analyzed} results and {self.entries_analyzed} audit entries analysed; "
                 f"audit chain {'intact' if self.chain_intact else 'BROKEN'}"]
        for subject, level in sorted(self.risk_by_subject.items(), key=lambda kv: -self.scores[kv[0]]):
            lines.append(f"  {subject}: {level} (score {self.scores[subject]})")
        for f in self.findings:
            lines.append(f"  [{f.severity}] {f.code} - {f.subject}: {f.message}")
        return "\n".join(lines)


def analyze(records, entries, cfg: SentinelConfig | None = None, chain_intact: bool = True,
            generated_at: str = "") -> SentinelReport:
    cfg = cfg or SentinelConfig()
    records, entries = list(records), list(entries)
    findings: list[Finding] = []
    findings += detect_testing_into_compliance(records, cfg)
    findings += detect_limit_clustering(records, cfg)
    findings += detect_duplicate_values(records, cfg)
    findings += detect_terminal_digit_bias(records, cfg)
    findings += detect_off_hours_entry(records, cfg)
    findings += detect_rapid_entry(records, cfg)
    findings += review_audit_trail(entries, cfg)
    if not chain_intact:
        findings.append(Finding("AUDIT_CHAIN_BROKEN", "HIGH", "system",
                                "The audit trail failed its integrity check"))
    scores: dict[str, int] = defaultdict(int)
    for f in findings:
        scores[f.subject] += SEVERITY_WEIGHT[f.severity]
    risk = {s: risk_level(v) for s, v in scores.items()}
    order = ["NONE", "LOW", "MEDIUM", "HIGH"]
    overall = max(risk.values(), key=order.index) if risk else "NONE"
    return SentinelReport(generated_at, tuple(findings), dict(scores), risk, overall,
                          chain_intact, len(records), len(entries))


class SentinelService:
    def __init__(self, tenant_id, lab, audit, users, entitlements, clock, config=None):
        self.tenant_id, self.lab, self.audit = tenant_id, lab, audit
        self.users, self.entitlements, self.clock = users, entitlements, clock
        self.config = config or SentinelConfig()

    def run(self, actor_id: str) -> SentinelReport:
        self.entitlements.assert_module(self.tenant_id, "sentinel")
        if not self.users.get(actor_id).roles & SENTINEL_ROLES:
            raise PermissionDenied("Only QA managers may run the integrity review")
        try:
            intact = self.audit.verify()
        except IntegrityError:
            intact = False
        entries = self.audit.entries()
        report = analyze(self.lab.result_records(), entries, self.config, intact,
                         self.clock.now().isoformat())
        self.audit.append(actor_id, "SENTINEL_RUN", "sentinel", "review",
                          {"overall_risk": report.overall_risk, "findings": len(report.findings)})
        return report

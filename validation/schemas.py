"""Domain validation for DATEV LODAS tool inputs.

Two layers, per the architecture doc:
  1. Structural — enforced by FastMCP from each tool's type hints.
  2. Plausibility — cross-field rules that catch legal-but-unusual or
     inconsistent input before a computer-use session is started. That's
     what lives here.

Also provides the read-back comparator used for write-then-read-back
verification (architecture doc Section 5): a mutating tool sends the model
back to read the form after filling it, and this module checks the
read-back values against what was intended.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

USAGE_FREQUENCIES = ("regularly", "occasionally")
TAX_TREATMENTS = ("benefit_in_kind", "employer_discount")

_USAGE_END_RE = re.compile(r"^(0[1-9]|1[0-2])/\d{4}$")
_IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {"errors": self.errors, "warnings": self.warnings}


def _iban_checksum_valid(iban: str) -> bool:
    """ISO 7064 mod-97-10 check used by IBAN: move the first 4 characters to
    the end, map letters to numbers (A=10 ... Z=35), and the result mod 97
    must be 1."""
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


def _check_iban(iban: str, errors: list[str]) -> None:
    normalized = iban.replace(" ", "").upper()
    if not _IBAN_RE.match(normalized):
        errors.append(
            f"iban must be a valid IBAN (e.g. 'DE89 3704 0044 0532 0130 00'), got {iban!r}"
        )
        return
    if not _iban_checksum_valid(normalized):
        errors.append(f"iban {iban!r} fails the IBAN checksum — check for a typo")


def _check_usage_end(usage_end: str, errors: list[str]) -> None:
    if not usage_end:
        return
    if not _USAGE_END_RE.match(usage_end):
        errors.append(f"usage_end must match MM/YYYY, got {usage_end!r}")
        return
    month, year = usage_end.split("/")
    today = date.today()
    if (int(year), int(month)) < (today.year, today.month):
        errors.append(f"usage_end {usage_end} is in the past")


def validate_commute(
    *,
    distance_km: int,
    work_days_per_month: int,
    usage_frequency: str,
    usage_end: str = "",
    flat_rate_tax: bool = False,
    current_distance_km: int | None = None,
) -> ValidationResult:
    r = ValidationResult()

    if not (1 <= distance_km <= 999):
        r.errors.append(f"distance_km must be 1-999, got {distance_km}")
    if not (1 <= work_days_per_month <= 31):
        r.errors.append(f"work_days_per_month must be 1-31, got {work_days_per_month}")
    elif work_days_per_month > 23:
        r.warnings.append(
            f"work_days_per_month={work_days_per_month} is unusually high (>23); "
            "confirm this is intentional"
        )
    if usage_frequency not in USAGE_FREQUENCIES:
        r.errors.append(f"usage_frequency must be one of {USAGE_FREQUENCIES}, got {usage_frequency!r}")

    _check_usage_end(usage_end, r.errors)

    if current_distance_km and current_distance_km > 0 and distance_km != current_distance_km:
        change = abs(distance_km - current_distance_km) / current_distance_km
        if change > 0.5:
            r.warnings.append(
                f"distance_km changes by {change:.0%} from current value "
                f"({current_distance_km} -> {distance_km}); confirm this isn't a typo"
            )

    return r


def validate_travel_subsidy(
    *,
    cash_allowance_eur: float | None,
    public_transport_tax_free: bool = False,
    public_transport_flat_rate_cash_allowance: bool = False,
    partial_month_rule: str = "",
    job_ticket_value_eur: float | None = None,
    tax_treatment: str = "benefit_in_kind",
    benefit_in_kind_exemption_exhausted: bool = False,
    public_transport_flat_rate_job_ticket: bool = False,
    flat_rate_taxation_method: str = "",
) -> ValidationResult:
    """Validate the DATEV LODAS commute-allowance screen (Personnel Data >
    Compensation > Commute Allowance), which has three sections: Commute
    Allowance (Cash Benefit), Job Ticket (Benefit in Kind), and Flat-Rate
    Taxation Details.

    Note: 'prea("Reduction for partial month/non-working
    month") and 'flat_rate_taxation_method' ("Flat-rate taxation method")
    are DATEV dropdowns whose full option lists aren't confirmed here — only
    one value each has been observed on a live screen ("Reduce partial
    month/Reduce non-working month" and "Distance allowance"). They're
    passed through as free text rather than validated against an invented
    enum; confirm the complete option sets against a live DATEV instance
    before enforcing them strictly.
    """
    r = ValidationResult()

    if tax_treatment not in TAX_TREATMENTS:
        r.errors.append(f"tax_treatment must be one of {TAX_TREATMENTS}, got {tax_treatment!r}")

    for label, value in (
        ("cash_allowance_eur", cash_allowance_eur),
        ("job_ticket_value_eur", job_ticket_value_eur),
    ):
        if value is not None and value < 0:
            r.errors.append(f"{label} cannot be negative, got {value}")

    if (cash_allowance_eur or 0) > 0 and (job_ticket_value_eur or 0) > 0:
        r.warnings.append(
            "both cash_allowance_eur and job_ticket_value_eur are set; "
            "having both is unusual and may be a mistake"
        )

    if public_transport_tax_free and public_transport_flat_rate_cash_allowance:
        r.warnings.append(
            "public_transport_tax_free and public_transport_flat_rate_cash_allowance "
            "are both set; these are alternative tax treatments for the same "
            "cash benefit — confirm only one should apply"
        )

    if benefit_in_kind_exemption_exhausted and (job_ticket_value_eur or 0) <= 0:
        r.warnings.append(
            "benefit_in_kind_exemption_exhausted is set but job_ticket_value_eur is 0; "
            "this flag only applies to the job-ticket benefit-in-kind amount"
        )

    return r


def validate_company_car(
    *,
    usage_frequency: str,
    usage_end: str,
    private_use: bool,
    commute_billing: bool,
    distance_km: int | None,
    work_days_per_month: int | None,
    trips_per_month: int | None,
) -> ValidationResult:
    r = ValidationResult()

    if usage_frequency not in USAGE_FREQUENCIES:
        r.errors.append(f"usage_frequency must be one of {USAGE_FREQUENCIES}, got {usage_frequency!r}")
    _check_usage_end(usage_end, r.errors)

    if usage_frequency == "occasionally" and not trips_per_month:
        r.errors.append("trips_per_month is required when usage_frequency is 'occasionally' (individual valuation)")

    if commute_billing:
        if not distance_km:
            r.errors.append("distance_km is required when commute_billing is true")
        if not work_days_per_month:
            r.errors.append("work_days_per_month is required when commute_billing is true")

    if distance_km is not None and not (1 <= distance_km <= 999):
        r.errors.append(f"distance_km must be 1-999, got {distance_km}")
    if work_days_per_month is not None and not (1 <= work_days_per_month <= 31):
        r.errors.append(f"work_days_per_month must be 1-31, got {work_days_per_month}")

    return r


PAYMENT_CYCLES = ("monthly", "one_time", "quarterly", "yearly")


def validate_salary_details(
    *,
    iban: str,
    amount_eur: float,
    cycle: str,
    valid_from: str,
    valid_until: str = "",
    reduce_for_partial_month: bool = False,
) -> ValidationResult:
    """Validate the DATEV LODAS Salary screen (Personnel Data > Compensation >
    Salary / Festbezüge), where a recurring payment to an IBAN is entered."""
    r = ValidationResult()

    _check_iban(iban, r.errors)

    if amount_eur < 0:
        r.errors.append(f"amount_eur cannot be negative, got {amount_eur}")
    elif amount_eur == 0 and cycle == "monthly":
        r.warnings.append("amount_eur is 0 for a monthly fixed-pay entry; confirm this is intentional")
    elif amount_eur > 50000:
        r.warnings.append(f"amount_eur={amount_eur} is unusually high; confirm this isn't a typo")

    if cycle not in PAYMENT_CYCLES:
        r.errors.append(f"cycle must be one of {PAYMENT_CYCLES}, got {cycle!r}")

    if not valid_from:
        r.errors.append("valid_from is required")
    elif not _USAGE_END_RE.match(valid_from):
        r.errors.append(f"valid_from must match MM/YYYY, got {valid_from!r}")

    if valid_until:
        if not _USAGE_END_RE.match(valid_until):
            r.errors.append(f"valid_until must match MM/YYYY, got {valid_until!r}")
        elif valid_from and _USAGE_END_RE.match(valid_from):
            vf_month, vf_year = valid_from.split("/")
            vu_month, vu_year = valid_until.split("/")
            if (int(vu_year), int(vu_month)) < (int(vf_year), int(vf_month)):
                r.errors.append(f"valid_until {valid_until} is before valid_from {valid_from}")

    return r


# ---------------------------------------------------------------------------
# Read-back verification (architecture doc Section 5)
# ---------------------------------------------------------------------------

_TRUE_STRINGS = {"true", "yes", "ja", "1"}
_FALSE_STRINGS = {"false", "no", "nein", "0"}


def _normalize(value) -> str:
    """Normalize a value for comparison so formatting differences (e.g.
    '29,00' vs '29', 'Ja' vs true) don't register as mismatches."""
    if value is None:
        return ""
    s = str(value).strip().lower()
    if s in _TRUE_STRINGS:
        return "true"
    if s in _FALSE_STRINGS:
        return "false"
    s = s.replace(",", ".")
    try:
        return format(float(s), "g")
    except ValueError:
        return re.sub(r"\s+", " ", s)


@dataclass
class VerificationResult:
    matched: dict[str, bool] = field(default_factory=dict)
    mismatches: dict[str, tuple] = field(default_factory=dict)

    @property
    def all_match(self) -> bool:
        return all(self.matched.values()) if self.matched else False

    def to_dict(self) -> dict:
        return {
            "all_match": self.all_match,
            "matched": self.matched,
            "mismatches": {k: {"expected": v[0], "actual": v[1]} for k, v in self.mismatches.items()},
        }


def verify_readback(expected: dict, actual: dict | None) -> VerificationResult:
    """Compare intended field values against what the computer-use model
    read back from the DATEV form after filling it in."""
    result = VerificationResult()
    if actual is None:
        for key, exp_val in expected.items():
            if exp_val in (None, ""):
                continue
            result.matched[key] = False
            result.mismatches[key] = (exp_val, None)
        return result

    for key, exp_val in expected.items():
        if exp_val in (None, ""):
            continue
        act_val = actual.get(key)
        ok = _normalize(exp_val) == _normalize(act_val)
        result.matched[key] = ok
        if not ok:
            result.mismatches[key] = (exp_val, act_val)

    return result

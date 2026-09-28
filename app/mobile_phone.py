"""KV02 — Mobile number normalisation and E.164 validation.

Uses Google's libphonenumber (Python: phonenumbers). Storage must always be
E.164 (e.g. +919876543210). Display formatting belongs in the UI only.
"""

from __future__ import annotations

import re
from typing import Optional

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat, PhoneNumberType

DEFAULT_REGION = "IN"
DEFAULT_DIAL_CODE = "91"

# AC3 copy
ERR_SELECT_COUNTRY = "Please select a country code"
ERR_INDIA_10_DIGIT = "Enter a valid 10-digit mobile number"
ERR_INDIA_START = "Mobile number cannot start with 0-5"
ERR_LANDLINE = "Please enter a mobile number. Landline numbers cannot receive OTP."
ERR_INVALID = "Enter a valid mobile number"


class MobileValidationError(ValueError):
    """Raised with user-facing AC copy."""


_NON_DIGIT_RE = re.compile(r"[^\d+]")
_MULTI_PLUS_RE = re.compile(r"\++")


def strip_phone_noise(raw: str) -> str:
    """AC4 / AC7 — strip spaces, hyphens, brackets, leading zeros noise."""
    text = str(raw or "").strip()
    if not text:
        return ""
    text = text.replace("(", "").replace(")", "").replace("-", "").replace(" ", "")
    text = text.replace("\u00a0", "").replace(".", "")
    text = _NON_DIGIT_RE.sub("", text)
    text = _MULTI_PLUS_RE.sub("+", text)
    if text.count("+") > 1:
        first = text.find("+")
        text = text[first] + text[first + 1 :].replace("+", "")
    return text


def _collapse_duplicated_dial_in_e164(digits: str) -> list[str]:
    """Return candidate national-digit strings after removing a repeated dial code."""
    candidates = [digits]
    # e.g. 91919876543210 → try 919876543210
    for code_len in (1, 2, 3):
        if len(digits) >= code_len * 2 + 8 and digits[:code_len] == digits[code_len : code_len * 2]:
            candidates.append(digits[code_len:])
    return candidates


def _strip_duplicated_dial_code(
    national_digits: str, dial_code: str, *, capacity: int = 10
) -> str:
    """Remove a duplicated country code pasted into the national number.

    Only strips when length exceeds the national capacity (e.g. 12 digits for
    India = 91 + 10). Valid 10-digit Indian mobiles that start with 91–99 must
    not be treated as country-code entry.
    """
    digits = re.sub(r"\D", "", national_digits or "")
    code = re.sub(r"\D", "", dial_code or "")
    if code and digits.startswith(code) and len(digits) > capacity:
        rest = digits[len(code) :]
        if len(rest) <= capacity and len(rest) >= min(8, capacity):
            digits = rest
    if digits.startswith("0") and len(digits) > capacity:
        digits = digits.lstrip("0")
    return digits


def split_e164(e164: str) -> tuple[str, str, str]:
    """Return (region, dial_code, national_digits) from an E.164 string."""
    try:
        parsed = phonenumbers.parse(e164, None)
        region = phonenumbers.region_code_for_number(parsed) or DEFAULT_REGION
        dial = str(parsed.country_code)
        national = str(parsed.national_number)
        return region, dial, national
    except NumberParseException:
        return DEFAULT_REGION, DEFAULT_DIAL_CODE, re.sub(r"\D", "", e164 or "")


def format_mobile_display(e164: str) -> str:
    """UI-only international display with country code. Never use for storage."""
    try:
        parsed = phonenumbers.parse(e164, None)
        return phonenumbers.format_number(parsed, PhoneNumberFormat.INTERNATIONAL)
    except NumberParseException:
        return e164 or ""


def format_otp_masked_display(e164: str) -> str:
    """OTP entry mask, e.g. +91 98••••3210 (dial + first 2 + bullets + last 4)."""
    _, dial, national = split_e164(e164)
    digits = re.sub(r"\D", "", national or "")
    dial_digits = re.sub(r"\D", "", dial or DEFAULT_DIAL_CODE) or DEFAULT_DIAL_CODE
    if len(digits) < 4:
        return f"+{dial_digits} ••••"
    if len(digits) >= 6:
        prefix = digits[:2]
        suffix = digits[-4:]
        middle = "•" * max(2, len(digits) - 6)
        return f"+{dial_digits} {prefix}{middle}{suffix}"
    return f"+{dial_digits} {'•' * (len(digits) - 4)}{digits[-4:]}"


def _india_national_rules(national_digits: str) -> None:
    digits = re.sub(r"\D", "", national_digits or "")
    if len(digits) != 10:
        raise MobileValidationError(ERR_INDIA_10_DIGIT)
    if digits[0] in "012345":
        raise MobileValidationError(ERR_INDIA_START)


def _is_mobile_type(parsed: phonenumbers.PhoneNumber, region: str) -> bool:
    ntype = phonenumbers.number_type(parsed)
    if ntype in (
        PhoneNumberType.MOBILE,
        PhoneNumberType.FIXED_LINE_OR_MOBILE,
        PhoneNumberType.PERSONAL_NUMBER,
    ):
        return True
    if ntype == PhoneNumberType.FIXED_LINE:
        return False
    if ntype == PhoneNumberType.UNKNOWN and region == "IN":
        return True
    return ntype != PhoneNumberType.FIXED_LINE


def national_digit_capacity(region: str) -> int:
    """Max national digits for the selected country (example-number length)."""
    region_code = (region or DEFAULT_REGION).strip().upper() or DEFAULT_REGION
    try:
        example = phonenumbers.example_number_for_type(
            region_code, PhoneNumberType.MOBILE
        )
        if example is None:
            example = phonenumbers.example_number(region_code)
        if example is not None:
            return len(str(example.national_number))
    except Exception:
        pass
    return 10 if region_code == "IN" else 15


def assert_national_only_input(
    raw_number: str,
    *,
    region: Optional[str] = None,
    dial_code: Optional[str] = None,
) -> str:
    """Reject country-code entry in the national field; return digit-only national.

    Users must select country separately and type only local digits
    (e.g. India → exactly 10 digits, never +91…).
    """
    region_code = (region or "").strip().upper() or DEFAULT_REGION
    dial = re.sub(r"\D", "", dial_code or "")
    cleaned = strip_phone_noise(raw_number)
    capacity = national_digit_capacity(region_code)

    if cleaned.startswith("+"):
        raise MobileValidationError(
            "Enter the mobile number without the country code. "
            "Select the country from the dropdown."
        )

    digits = re.sub(r"\D", "", cleaned)
    # Trunk prefix (0…) before dial+national paste (e.g. 0919876543210)
    if digits.startswith("0") and len(digits) > capacity:
        digits = digits.lstrip("0")

    # Strip accidental dial+national paste (12 digits for India); keep 10-digit
    # numbers that merely start with the dial digits (e.g. 9187654321).
    if dial and digits.startswith(dial) and len(digits) > capacity:
        rest = digits[len(dial) :]
        if len(rest) <= capacity and len(rest) >= min(8, capacity):
            digits = rest
        else:
            raise MobileValidationError(
                "Enter the mobile number without the country code. "
                "Select the country from the dropdown."
            )

    national = digits.lstrip("0") if digits.startswith("0") and len(digits) > 1 else digits
    if len(national) > capacity:
        raise MobileValidationError(
            f"Enter a valid {capacity}-digit mobile number"
            if region_code == "IN"
            else ERR_INVALID
        )
    return national


def normalize_and_validate_mobile(
    raw_number: str,
    *,
    region: Optional[str] = None,
    dial_code: Optional[str] = None,
    require_country: bool = True,
    national_only: bool = True,
) -> str:
    """Normalise input and return E.164. Raises MobileValidationError with AC copy.

    When national_only=True (default for profile flows), the number must be
    local digits for the selected country — not +CC… or CC-prefixed.
    """
    region_code = (region or "").strip().upper() or None
    dial = re.sub(r"\D", "", dial_code or "")
    cleaned = strip_phone_noise(raw_number)

    if require_country and not region_code and not dial and not cleaned.startswith("+"):
        raise MobileValidationError(ERR_SELECT_COUNTRY)

    if not cleaned:
        if region_code == "IN" or dial == "91":
            raise MobileValidationError(ERR_INDIA_10_DIGIT)
        raise MobileValidationError(ERR_INVALID)

    parsed = None

    # Profile / OTP flows: country is selected separately — no +CC in the box
    if national_only and (region_code or dial):
        national = assert_national_only_input(
            cleaned, region=region_code or DEFAULT_REGION, dial_code=dial
        )
        if not region_code:
            if dial == "91":
                region_code = "IN"
            else:
                raise MobileValidationError(ERR_SELECT_COUNTRY)
        if region_code == "IN":
            _india_national_rules(national)
        try:
            parsed = phonenumbers.parse(national, region_code)
        except NumberParseException as exc:
            if region_code == "IN":
                raise MobileValidationError(ERR_INDIA_10_DIGIT) from exc
            raise MobileValidationError(ERR_INVALID) from exc
    elif cleaned.startswith("+"):
        digit_body = re.sub(r"\D", "", cleaned)
        last_error: Exception | None = None
        for candidate_digits in _collapse_duplicated_dial_in_e164(digit_body):
            try:
                candidate = phonenumbers.parse(f"+{candidate_digits}", None)
            except NumberParseException as exc:
                last_error = exc
                continue
            if phonenumbers.is_valid_number(candidate):
                parsed = candidate
                break
            last_error = None
            parsed = candidate
        if parsed is None and last_error is not None:
            raise MobileValidationError(ERR_INVALID) from last_error
        if parsed is None:
            raise MobileValidationError(ERR_INVALID)
        region_code = phonenumbers.region_code_for_number(parsed) or region_code or DEFAULT_REGION
        dial = str(parsed.country_code)
    else:
        if not region_code:
            if dial == "91":
                region_code = "IN"
            else:
                raise MobileValidationError(ERR_SELECT_COUNTRY)

        national = _strip_duplicated_dial_code(
            cleaned, dial or DEFAULT_DIAL_CODE, capacity=national_digit_capacity(region_code)
        )
        if dial and national.startswith(dial) and len(national) > national_digit_capacity(region_code):
            national = national[len(dial) :]

        if region_code == "IN":
            if national.startswith("0"):
                national = national.lstrip("0")
            _india_national_rules(national)

        try:
            parsed = phonenumbers.parse(national, region_code)
        except NumberParseException as exc:
            if region_code == "IN":
                raise MobileValidationError(ERR_INDIA_10_DIGIT) from exc
            raise MobileValidationError(ERR_INVALID) from exc

    if not _is_mobile_type(parsed, region_code or DEFAULT_REGION):
        raise MobileValidationError(ERR_LANDLINE)

    if region_code == "IN":
        national = str(parsed.national_number)
        _india_national_rules(national)

    if not phonenumbers.is_valid_number(parsed):
        if region_code == "IN":
            raise MobileValidationError(ERR_INDIA_10_DIGIT)
        raise MobileValidationError(ERR_INVALID)

    return phonenumbers.format_number(parsed, PhoneNumberFormat.E164)


def require_mobile_e164(
    value: Optional[str],
    *,
    region: Optional[str] = None,
    dial_code: Optional[str] = None,
    national_only: bool = True,
) -> str:
    """Validate and return E.164 for API payloads (national digits + selected country)."""
    return normalize_and_validate_mobile(
        value or "",
        region=region,
        dial_code=dial_code,
        require_country=True,
        national_only=national_only,
    )

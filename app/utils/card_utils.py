"""
Offline card validation + brand detection (no network dependency).

Used by the saved-cards flow to validate a card before it is handed to the
gateway tokenization middleware. Only ever operates on the raw PAN in memory —
nothing here persists or logs the card number.

Brand detection is BIN-range based and ordered so India's RuPay is resolved
correctly against the overlapping Discover 6xxx space.
"""

from __future__ import annotations

from datetime import date


def _digits(number: str) -> str:
    if not isinstance(number, str):
        return ""
    return "".join(ch for ch in number if ch.isdigit())


def luhn_valid(number: str) -> bool:
    """Validate a card number with the Luhn checksum."""
    n = _digits(number)
    if len(n) < 12 or len(n) > 19:
        return False
    total = 0
    parity = len(n) % 2
    for i, ch in enumerate(n):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def detect_brand(number: str) -> str:
    """Return the card brand from its BIN, or ``unknown``.

    Order matters: the unambiguous Discover ranges are matched before RuPay so
    the shared 6xxx space resolves correctly for an India-first card mix.
    """
    n = _digits(number)
    if len(n) < 12:
        return "unknown"

    two, three, four, six = n[:2], n[:3], n[:4], n[:6]
    i2, i3, i4, i6 = int(two), int(three), int(four), int(six)

    if two in ("34", "37"):
        return "amex"
    if four == "3095" or two in ("36", "38", "39") or 300 <= i3 <= 305:
        return "diners"
    # Discover's unambiguous ranges first (6011, 644-649, 622126-622925).
    if four == "6011" or 644 <= i3 <= 649 or 622126 <= i6 <= 622925:
        return "discover"
    # RuPay (India): 60, 6521, 6522, 81, 82, 508.
    if two in ("60", "81", "82") or four in ("6521", "6522") or three == "508":
        return "rupay"
    # Remaining Discover 65 space (after RuPay 6521/6522 already captured).
    if two == "65":
        return "discover"
    if n[0] == "4":
        return "visa"
    if 51 <= i2 <= 55 or 2221 <= i4 <= 2720:
        return "mastercard"
    return "unknown"


def normalize_year(year: int) -> int:
    """Turn a 2-digit expiry year into a 4-digit one (e.g. 30 -> 2030)."""
    if year < 100:
        return 2000 + year
    return year


def validate_expiry(month: int, year: int) -> bool:
    """True when the (month, year) expiry is well-formed and not in the past.

    The card is valid through the LAST day of the expiry month.
    """
    if not isinstance(month, int) or not isinstance(year, int):
        return False
    if month < 1 or month > 12:
        return False
    year = normalize_year(year)
    today = date.today()
    if year < today.year:
        return False
    if year == today.year and month < today.month:
        return False
    return True


def last4(number: str) -> str:
    n = _digits(number)
    return n[-4:] if len(n) >= 4 else n

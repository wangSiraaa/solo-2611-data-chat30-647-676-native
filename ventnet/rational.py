"""Exact non-negative rational parsing and canonical formatting.

All ventilation quantities (volumes, flows, source strengths) are kept as
:class:`fractions.Fraction` so that network balance checks are exact.  Floats
are accepted only through their decimal *string* representation, which keeps
the conversion deterministic and avoids binary floating-point surprises.
"""

from __future__ import annotations

from fractions import Fraction

from .errors import ValidationError


def parse_rational(value, *, field: str = "value") -> Fraction:
    """Parse ``value`` into an exact :class:`Fraction`.

    Accepts ``int``, ``Fraction``, ``"a/b"`` strings, and decimal strings such
    as ``"0.5"``.  Plain ``float`` is accepted by going through ``repr`` but is
    discouraged; use strings for reproducible inputs.
    """
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        raise ValidationError(f"{field}: boolean is not a rational number")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        # repr() is the shortest round-tripping decimal; still deterministic.
        try:
            return Fraction(repr(value))
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"{field}: invalid float {value!r}") from exc
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValidationError(f"{field}: empty rational string")
        try:
            if "/" in text:
                num, den = text.split("/", 1)
                return Fraction(int(num.strip()), int(den.strip()))
            return Fraction(text)
        except (ValueError, ZeroDivisionError) as exc:
            raise ValidationError(f"{field}: invalid rational {value!r}") from exc
    raise ValidationError(f"{field}: unsupported rational type {type(value).__name__}")


def parse_nonnegative(value, *, field: str = "value") -> Fraction:
    frac = parse_rational(value, field=field)
    if frac < 0:
        raise ValidationError(f"{field}: must be non-negative, got {rational_str(frac)}")
    return frac


def parse_positive(value, *, field: str = "value") -> Fraction:
    frac = parse_rational(value, field=field)
    if frac <= 0:
        raise ValidationError(f"{field}: must be positive, got {rational_str(frac)}")
    return frac


def rational_str(value: Fraction | int) -> str:
    """Canonical text form: ``"n"`` or ``"n/d"`` (Fraction is already reduced)."""
    frac = Fraction(value)
    if frac.denominator == 1:
        return str(frac.numerator)
    return f"{frac.numerator}/{frac.denominator}"


def rational_pair(value: Fraction | int) -> dict:
    """Lossless structured form for JSON/SQLite."""
    frac = Fraction(value)
    return {"num": frac.numerator, "den": frac.denominator}


def from_pair(value: dict, *, field: str = "value") -> Fraction:
    """Inverse of :func:`rational_pair`."""
    try:
        num = value["num"]
        den = value["den"]
    except (TypeError, KeyError) as exc:
        raise ValidationError(f"{field}: expected rational pair {{num,den}}") from exc
    if isinstance(num, bool) or isinstance(den, bool):
        raise ValidationError(f"{field}: boolean in rational pair")
    if not isinstance(num, int) or not isinstance(den, int):
        raise ValidationError(f"{field}: rational pair parts must be integers")
    if den == 0:
        raise ValidationError(f"{field}: zero denominator")
    return Fraction(num, den)


def fraction_to_float(value: Fraction) -> float:
    return value.numerator / value.denominator


# The outdoor reservoir is an infinite, clean (concentration 0) node.
OUT = "OUT"

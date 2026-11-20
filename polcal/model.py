"""Numerical core for the *limited synthetic* polarization model.

MODEL (only this model; nothing about real instruments is claimed):

    theta : fixed channel angle in degrees, equivalent modulo 180
    s     : ideal signal = (I + Q*cos(2*theta) + U*sin(2*theta)) / 2
    y     : channel reading = g*s + b,  with g > 0

The module provides:

* strict one-variable least-squares line fits ``y = g*s + b`` per channel;
* a configurable, recorded numerical rank tolerance;
* weighted least-squares inversion of calibrated readings for (I, Q, U);
* the model-consistency test ``I >= 0`` and ``Q**2 + U**2 <= I**2``;
* reporting of the linear-polarization fraction and the polarization angle.

Every result is explicitly labelled as a result of the synthetic model above.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

MODEL_VERSION = "polcal.limited_synthetic.v1"
MODEL_FORMULA_S = "s = (I + Q*cos(2*theta) + U*sin(2*theta)) / 2"
MODEL_FORMULA_Y = "y = g*s + b, g > 0"
PRODUCT_FORMAT = "polcal.product.v1"

MODEL_STATEMENT = (
    "LIMITED SYNTHETIC MODEL ONLY: " + MODEL_FORMULA_S + " ; " + MODEL_FORMULA_Y +
    ". No instrument is connected. This is not a real physical, medical, or "
    "astronomical measurement and no such accuracy is claimed."
)
DISCLAIMER = (
    "All numbers below are outputs of the task-defined synthetic model "
    "(s=(I+Q*cos(2theta)+U*sin(2theta))/2, y=g*s+b, g>0). They are not medical, "
    "astronomical, or real-instrument measurements."
)

DEFAULT_CONFIG: dict[str, Any] = {
    # Relative singular-value threshold used for every matrix-rank decision.
    # Rank = count(sv > rank_tol * sv[0]).
    "rank_tol": 1e-10,
    # Tolerance for the model-consistency test I>=0, Q^2+U^2<=I^2.
    # allowed: I >= -(atol + rtol*scale), radial <= I^2 + (atol + rtol*scale2).
    "constraint_atol": 1e-9,
    "constraint_rtol": 1e-9,
    # Numerical "is exactly zero" tolerance for undefined polarization fraction
    # (I == 0) and undefined angle (Q == U == 0).
    "zero_tol": 1e-12,
    # Tolerance used when counting distinct s values (diagnostics only).
    "distinct_atol": 1e-12,
    "distinct_rtol": 1e-12,
    "min_channels_for_inversion": 3,
    "angle_period_deg": 180.0,
}


class ValidationError(ValueError):
    """Raised for malformed or contradictory input."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def merge_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if config:
        unknown = set(config) - set(DEFAULT_CONFIG)
        if unknown:
            raise ValidationError(f"unknown config keys: {sorted(unknown)}")
        cfg.update(config)
    validate_config(cfg)
    return cfg


def validate_config(cfg: Mapping[str, Any]) -> None:
    rt = cfg["rank_tol"]
    if not (isinstance(rt, (int, float)) and math.isfinite(rt) and 0.0 < rt < 1.0):
        raise ValidationError("rank_tol must be finite and in (0, 1)")
    for key in ("constraint_atol", "constraint_rtol", "zero_tol",
                "distinct_atol", "distinct_rtol"):
        v = cfg[key]
        if not (isinstance(v, (int, float)) and math.isfinite(v) and v >= 0.0):
            raise ValidationError(f"{key} must be finite and >= 0")
    if int(cfg["min_channels_for_inversion"]) < 1:
        raise ValidationError("min_channels_for_inversion must be >= 1")
    if float(cfg["angle_period_deg"]) != 180.0:
        raise ValidationError("this model fixes the angle period at 180 degrees")


def is_finite_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def require_finite(x: Any, name: str) -> float:
    if not is_finite_number(x):
        raise ValidationError(f"{name} must be a finite number, got {x!r}")
    return float(x)


def canonical_theta(theta_deg: float) -> float:
    """Normalize an angle in degrees to [0, 180)."""
    t = math.fmod(require_finite(theta_deg, "theta_deg"), 180.0)
    if t < 0.0:
        t += 180.0
    if t >= 180.0 or t == 0.0:
        t = 0.0
    return t


def cos2_sin2(theta_deg: float) -> tuple[float, float]:
    """Return (cos(2*theta), sin(2*theta)) for theta in degrees."""
    r = math.radians(2.0 * float(theta_deg))
    return math.cos(r), math.sin(r)


def ideal_s(I: float, Q: float, U: float, theta_deg: float) -> float:
    c2, s2 = cos2_sin2(theta_deg)
    return (float(I) + float(Q) * c2 + float(U) * s2) / 2.0


def matrix_rank_sv(sv: np.ndarray, rank_tol: float) -> int:
    """Configured numerical rank from singular values."""
    if sv.size == 0 or not (sv[0] > 0.0):
        return 0
    return int(np.count_nonzero(sv > rank_tol * sv[0]))


def count_distinct(values: Sequence[float], atol: float, rtol: float) -> int:
    vals = sorted(float(v) for v in values)
    if not vals:
        return 0
    groups = [vals[0]]
    for v in vals[1:]:
        if abs(v - groups[-1]) > atol + rtol * max(abs(v), abs(groups[-1])):
            groups.append(v)
    return len(groups)


def json_dumps(obj: Any) -> str:
    """Strict JSON (rejects NaN/Infinity rather than emitting invalid JSON)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_of(obj: Any) -> str:
    return hashlib.sha256(json_dumps(obj).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# calibration: strict line fit per channel
# ---------------------------------------------------------------------------

def fit_channel(
    points: Sequence[Mapping[str, Any]],
    theta_deg: float,
    cfg: Mapping[str, Any],
) -> dict[str, Any]:
    """Fit y = g*s + b for one channel.

    ``points`` entries need ``s`` and ``y`` (and are passed through for
    traceability).  Calibration requires at least two distinct s values and a
    fitted gain strictly greater than zero; otherwise the channel is
    uncalibratable, with the reason retained (and any attempted fit retained).
    """
    theta = canonical_theta(theta_deg)
    cleaned: list[dict[str, Any]] = []
    for p in points:
        s = require_finite(p.get("s"), "point.s")
        y = require_finite(p.get("y"), "point.y")
        item = dict(p)
        item["s"] = s
        item["y"] = y
        cleaned.append(item)

    base: dict[str, Any] = {
        "theta_deg": theta,
        "n_points": len(cleaned),
        "method": "one-variable ordinary least squares, y = g*s + b "
                  "(numpy.linalg.lstsq, SVD, rcond=rank_tol)",
        "rank_tol": cfg["rank_tol"],
        "status": "uncalibratable",
        "reason": None,
        "gain": None,
        "offset": None,
        "attempted_gain": None,
        "attempted_offset": None,
        "residual_ss": None,
        "r_squared": None,
        "n_distinct_s": 0,
        "rank": 0,
        "singular_values": [],
        "points": [],
    }

    n = len(cleaned)
    s_arr = np.asarray([p["s"] for p in cleaned], dtype=float)
    y_arr = np.asarray([p["y"] for p in cleaned], dtype=float)
    base["n_distinct_s"] = count_distinct(
        s_arr.tolist(), cfg["distinct_atol"], cfg["distinct_rtol"]
    )

    if n == 0:
        base["reason"] = "no_measurements"
        return base
    if n < 2:
        base["reason"] = "insufficient_measurements"
        return base

    A = np.column_stack([s_arr, np.ones(n)])
    sv = np.linalg.svd(A, compute_uv=False)
    rank = matrix_rank_sv(sv, cfg["rank_tol"])
    base["rank"] = rank
    base["singular_values"] = [float(v) for v in sv]

    if rank < 2:
        if base["n_distinct_s"] < 2:
            base["reason"] = "insufficient_distinct_s"
        else:
            base["reason"] = "design_rank_deficient_at_configured_tolerance"
        return base

    (g, b), *_ = np.linalg.lstsq(A, y_arr, rcond=cfg["rank_tol"])
    g = float(g)
    b = float(b)
    residuals = y_arr - (g * s_arr + b)
    rss = float(np.dot(residuals, residuals))
    tss = float(np.dot(y_arr - y_arr.mean(), y_arr - y_arr.mean()))
    base["attempted_gain"] = g
    base["attempted_offset"] = b
    base["residual_ss"] = rss

    # Keep every point and its residual, whether accepted or rejected.
    for p, res in zip(cleaned, residuals):
        base["points"].append({
            "measurement_id": p.get("measurement_id"),
            "reference_id": p.get("reference_id"),
            "reference_version": p.get("reference_version"),
            "s": p["s"],
            "y": p["y"],
            "residual": float(res),
        })

    if not (g > 0.0):
        base["reason"] = "gain_nonpositive"
        return base

    base["status"] = "calibrated"
    base["reason"] = None
    base["gain"] = g
    base["offset"] = b
    base["r_squared"] = None if tss <= 0.0 else 1.0 - rss / tss
    return base


def fit_product_channels(
    measurements: Sequence[Mapping[str, Any]],
    channels: Mapping[str, float],
    cfg: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Build per-channel fit records from a reference-measurement snapshot.

    Each measurement is ``{measurement_id, reference_id, I, Q, U, y_by_channel}``.
    """
    out: dict[str, dict[str, Any]] = {}
    for channel in sorted(channels):
        theta = channels[channel]
        pts: list[dict[str, Any]] = []
        for m in sorted(measurements, key=lambda x: str(x["measurement_id"])):
            y_map = m.get("y_by_channel", {})
            if channel not in y_map:
                continue
            s = ideal_s(m["I"], m["Q"], m["U"], theta)
            pts.append({
                "measurement_id": m["measurement_id"],
                "reference_id": m["reference_id"],
                "reference_version": m.get("reference_version"),
                "s": s,
                "y": float(y_map[channel]),
            })
        out[channel] = fit_channel(pts, theta, cfg)
    return out


# ---------------------------------------------------------------------------
# weighted inversion
# ---------------------------------------------------------------------------

def weighted_solve(
    A: np.ndarray,
    target: np.ndarray,
    weights: np.ndarray,
    rank_tol: float,
) -> tuple[int, np.ndarray, np.ndarray | None]:
    """Weighted least squares with an explicitly configured rank test."""
    n, p = A.shape
    if n == 0:
        return 0, np.asarray([], dtype=float), None
    sw = np.sqrt(weights)
    Aw = A * sw[:, None]
    tw = target * sw
    sv = np.linalg.svd(Aw, compute_uv=False)
    rank = matrix_rank_sv(sv, rank_tol)
    if rank < p:
        return rank, sv, None
    sol, *_ = np.linalg.lstsq(Aw, tw, rcond=rank_tol)
    return rank, sv, np.asarray(sol, dtype=float)


def check_model_constraints(
    I: float, Q: float, U: float, cfg: Mapping[str, Any]
) -> dict[str, Any]:
    """Check I>=0 and Q^2+U^2<=I^2 within recorded numerical tolerances."""
    I = float(I)
    Q = float(Q)
    U = float(U)
    radial = Q * Q + U * U
    scale_i = max(1.0, abs(I), math.sqrt(max(radial, 0.0)))
    i_tol = cfg["constraint_atol"] + cfg["constraint_rtol"] * scale_i
    i_ok = I >= -i_tol

    scale_r = max(1.0, I * I, radial)
    radial_tol = cfg["constraint_atol"] + cfg["constraint_rtol"] * scale_r
    radial_ok = radial <= I * I + radial_tol

    return {
        "I": I,
        "Q": Q,
        "U": U,
        "I_nonnegative": bool(i_ok),
        "radial_constraint": bool(radial_ok),
        "consistent": bool(i_ok and radial_ok),
        "radial": radial,
        "radial_minus_I_squared": radial - I * I,
        "I_tolerance": i_tol,
        "radial_tolerance": radial_tol,
        "constraint_atol": cfg["constraint_atol"],
        "constraint_rtol": cfg["constraint_rtol"],
    }


def polarization_report(
    I: float, Q: float, U: float, cfg: Mapping[str, Any], consistent: bool
) -> dict[str, Any]:
    """Linear-polarization fraction and angle for a legal (consistent) solution.

    fraction = sqrt(Q^2+U^2)/I, undefined when I == 0 (within zero_tol).
    angle    = atan2(U,Q)/2 normalized to [0,180) degrees, undefined when
               Q == U == 0 (within zero_tol).
    """
    out: dict[str, Any] = {
        "dolp": None,
        "dolp_defined": False,
        "angle_deg": None,
        "angle_defined": False,
        "undefined_reasons": [],
        "zero_tol": cfg["zero_tol"],
    }
    if not consistent:
        out["undefined_reasons"].append("solution_flagged_model_inconsistent")
        return out

    radial = Q * Q + U * U
    if abs(I) <= cfg["zero_tol"]:
        out["undefined_reasons"].append("dolp_undefined_I_zero")
    else:
        out["dolp"] = math.sqrt(radial) / I
        out["dolp_defined"] = True

    if abs(Q) <= cfg["zero_tol"] and abs(U) <= cfg["zero_tol"]:
        out["undefined_reasons"].append("angle_undefined_Q_U_zero")
    else:
        ang = math.degrees(math.atan2(U, Q)) / 2.0
        ang = ang % 180.0
        if ang >= 180.0 or ang == 0.0:
            ang = 0.0
        out["angle_deg"] = ang
        out["angle_defined"] = True
    return out


# ---------------------------------------------------------------------------
# product payload (the only thing a warehouse is allowed to consume)
# ---------------------------------------------------------------------------

def build_payload(
    *,
    product_id: str,
    created_at: str,
    product_version: int,
    kind: str,
    source_product_id: str | None,
    design_id: str | None,
    design_fingerprint: str,
    config: Mapping[str, Any],
    channels: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Self-contained parameter product; deliberately contains no reference readings."""
    ch_out: dict[str, Any] = {}
    for name in sorted(channels):
        c = channels[name]
        item = {
            "theta_deg": canonical_theta(c["theta_deg"]),
            "status": c["status"],
            "gain": c.get("gain"),
            "offset": c.get("offset"),
            "n_points": c.get("n_points", 0),
            "n_distinct_s": c.get("n_distinct_s", 0),
            "residual_ss": c.get("residual_ss"),
            "r_squared": c.get("r_squared"),
            "reason": c.get("reason"),
            "attempted_gain": c.get("attempted_gain"),
            "attempted_offset": c.get("attempted_offset"),
        }
        ch_out[name] = item
    summary = {
        "calibrated": sum(1 for c in ch_out.values() if c["status"] == "calibrated"),
        "uncalibratable": sum(1 for c in ch_out.values() if c["status"] != "calibrated"),
    }
    return {
        "format": PRODUCT_FORMAT,
        "model_version": MODEL_VERSION,
        "model_statement": MODEL_STATEMENT,
        "disclaimer": DISCLAIMER,
        "product_id": product_id,
        "created_at": created_at,
        "product_version": int(product_version),
        "kind": kind,
        "source_product_id": source_product_id,
        "design_id": design_id,
        "design_fingerprint": design_fingerprint,
        "config": dict(config),
        "channels": ch_out,
        "status_summary": summary,
    }


def validate_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an external same-format product; return a normalized copy."""
    if not isinstance(payload, Mapping):
        raise ValidationError("product payload must be a mapping")
    if payload.get("format") != PRODUCT_FORMAT:
        raise ValidationError(f"unsupported product format: {payload.get('format')!r}")
    if payload.get("model_version") != MODEL_VERSION:
        raise ValidationError(
            f"model_version mismatch: {payload.get('model_version')!r} != {MODEL_VERSION!r}"
        )
    cfg = merge_config(payload.get("config"))
    channels = payload.get("channels")
    if not isinstance(channels, Mapping) or not channels:
        raise ValidationError("product must contain a non-empty channels mapping")

    out_channels: dict[str, Any] = {}
    for name, c in channels.items():
        if not isinstance(name, str) or not name:
            raise ValidationError("channel names must be non-empty strings")
        if not isinstance(c, Mapping):
            raise ValidationError(f"channel {name!r} must be a mapping")
        theta = canonical_theta(c.get("theta_deg"))
        status = c.get("status")
        if status not in ("calibrated", "uncalibratable"):
            raise ValidationError(f"channel {name!r}: bad status {status!r}")
        gain = c.get("gain")
        offset = c.get("offset")
        reason = c.get("reason")
        if status == "calibrated":
            gain = require_finite(gain, f"{name}.gain")
            offset = require_finite(offset, f"{name}.offset")
            if not gain > 0.0:
                raise ValidationError(f"channel {name!r}: calibrated gain must be > 0")
            reason = None
        else:
            if not isinstance(reason, str) or not reason:
                raise ValidationError(
                    f"channel {name!r}: uncalibratable channel needs a reason"
                )
            gain = None if gain is None else require_finite(gain, f"{name}.gain")
            offset = None if offset is None else require_finite(offset, f"{name}.offset")
        out_channels[name] = {
            "theta_deg": theta,
            "status": status,
            "gain": gain,
            "offset": offset,
            "n_points": int(c.get("n_points", 0) or 0),
            "n_distinct_s": int(c.get("n_distinct_s", 0) or 0),
            "residual_ss": c.get("residual_ss"),
            "r_squared": c.get("r_squared"),
            "reason": reason,
            "attempted_gain": c.get("attempted_gain"),
            "attempted_offset": c.get("attempted_offset"),
        }

    normalized = dict(payload)
    normalized["config"] = cfg
    normalized["channels"] = out_channels
    json_dumps(normalized)  # rejects NaN/Infinity anywhere
    return normalized


def apply_channel_correction(
    payload: Mapping[str, Any], channel: str, y_raw: float
) -> dict[str, Any]:
    """Stand-alone linear correction using only a product payload.

    This is the "ordinary customer" path: no observation warehouse and no
    calibration references are needed.
    """
    product = validate_payload(payload)
    if channel not in product["channels"]:
        raise ValidationError(f"channel {channel!r} not present in product")
    c = product["channels"][channel]
    if c["status"] != "calibrated" or c["gain"] is None:
        raise ValidationError(
            f"channel {channel!r} is uncalibratable: {c['reason']}"
        )
    y = require_finite(y_raw, "y_raw")
    s = (y - c["offset"]) / c["gain"]
    return {
        "product_id": product.get("product_id"),
        "channel": channel,
        "theta_deg": c["theta_deg"],
        "y_raw": y,
        "corrected_s": s,
        "gain": c["gain"],
        "offset": c["offset"],
        "model_statement": MODEL_STATEMENT,
    }


def summarize_channels(channels: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "n_channels": len(channels),
        "calibrated": sorted(k for k, v in channels.items() if v["status"] == "calibrated"),
        "uncalibratable": {
            k: v.get("reason") for k, v in channels.items() if v["status"] != "calibrated"
        },
    }

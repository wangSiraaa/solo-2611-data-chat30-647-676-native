"""Counting repository: spectra, window quantification and result cache.

The repository consumes *published* calibration products only (its own or
external ones in the same portable JSON format).  It never reads the private
calibration-point table.  Each stored result keeps the exact per-bin coverage,
the raw counts, the live-time correction and the date conversion, so every
step can be replayed and checked.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from fractions import Fraction
from typing import Dict, List, Sequence, Tuple

import numpy as np

from .calibration import CalibrationError, CalibrationProduct
from .decay import decay_integral, invert_activity, to_reference_time
from .geometry import counts_from_coverages
from .limits import (COMPLETE, INCOMPLETE, MAX_CHANNELS_PER_SPECTRUM,
                     MAX_SPECTRA, MAX_WINDOWS, TOL_ABS, TOL_REL)
from .rationals import as_rational, rational_to_str, require_positive

REPO_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    product_id  TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL,
    version     INTEGER NOT NULL,
    payload     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spectra (
    spectrum_id  TEXT PRIMARY KEY,
    device_id    TEXT NOT NULL,
    product_id   TEXT NOT NULL,
    start_sec    INTEGER NOT NULL,
    T            TEXT NOT NULL,
    L            TEXT NOT NULL,
    n_channels   INTEGER NOT NULL,
    counts       BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS window_results (
    result_id   TEXT PRIMARY KEY,
    spectrum_id TEXT NOT NULL,
    window      TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS net_results (
    net_id      TEXT PRIMARY KEY,
    sample_id   TEXT NOT NULL,
    background_id TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_window_spectrum
    ON window_results(spectrum_id);
"""


class RepositoryError(ValueError):
    """Raised on invalid repository input or unknown references."""


def _float_close(a: float, b: float) -> bool:
    return abs(a - b) <= max(TOL_ABS, TOL_REL * max(abs(a), abs(b)))


class CountRepository:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(REPO_SCHEMA)
        self.conn.commit()
        self._spectrum_cache: Dict[str, Dict] = {}

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "CountRepository":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- products -----------------------------------------------------
    def import_product(self, json_text: str) -> str:
        """Accept an external same-format product; content is re-validated."""
        try:
            product = CalibrationProduct.from_json(json_text)
        except (CalibrationError, KeyError, ValueError) as exc:
            raise RepositoryError(f"rejected product: {exc}") from exc
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO products"
                "(product_id, device_id, version, payload) "
                "VALUES (?, ?, ?, ?)",
                (product.product_id, product.device_id, product.version,
                 json.dumps(
                     {"product_id": product.product_id,
                      **product.to_dict()}, sort_keys=True)))
        return product.product_id

    def _load_product(self, product_id: str) -> CalibrationProduct:
        row = self.conn.execute(
            "SELECT payload FROM products WHERE product_id = ?",
            (product_id,)).fetchone()
        if row is None:
            raise RepositoryError(f"unknown product {product_id}")
        return CalibrationProduct.from_json(row[0])

    def list_products(self) -> List[Dict]:
        return [{"product_id": p, "device_id": d, "version": v}
                for p, d, v in self.conn.execute(
                    "SELECT product_id, device_id, version FROM products "
                    "ORDER BY device_id, version")]

    # ---- spectra ------------------------------------------------------
    def import_spectrum(self, spectrum_id: str, device_id: str,
                        product_id: str, start_sec: int,
                        T, L, counts: Sequence[int]) -> None:
        product = self._load_product(product_id)
        if product.device_id != device_id:
            raise RepositoryError(
                f"product {product_id} belongs to {product.device_id}, "
                f"not {device_id}")
        n = len(counts)
        if n == 0 or n > MAX_CHANNELS_PER_SPECTRUM:
            raise RepositoryError(
                f"channel count must be in 1..{MAX_CHANNELS_PER_SPECTRUM}")
        if self.conn.execute("SELECT COUNT(*) FROM spectra").fetchone()[0] \
                >= MAX_SPECTRA and not self.conn.execute(
                "SELECT 1 FROM spectra WHERE spectrum_id = ?",
                (spectrum_id,)).fetchone():
            raise RepositoryError(f"spectrum limit {MAX_SPECTRA} reached")
        arr = np.asarray(counts, dtype=np.int64)
        if arr.ndim != 1 or np.any(arr < 0):
            raise RepositoryError("counts must be non-negative integers")
        Tf, Lf = as_rational(T), as_rational(L)
        require_positive(Tf, "T")
        require_positive(Lf, "L")
        if Lf > Tf:
            raise RepositoryError("effective live time L must satisfy L <= T")
        if not isinstance(start_sec, int):
            raise RepositoryError("start_sec must be an integer second")
        blob = arr.tobytes()
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO spectra"
                "(spectrum_id, device_id, product_id, start_sec, T, L, "
                "n_channels, counts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (spectrum_id, device_id, product_id, start_sec,
                 rational_to_str(Tf), rational_to_str(Lf), n,
                 sqlite3.Binary(blob)))
        self._spectrum_cache.pop(spectrum_id, None)

    def _load_spectrum(self, spectrum_id: str) -> Dict:
        if spectrum_id in self._spectrum_cache:
            return self._spectrum_cache[spectrum_id]
        row = self.conn.execute(
            "SELECT spectrum_id, device_id, product_id, start_sec, T, L, "
            "n_channels, counts FROM spectra WHERE spectrum_id = ?",
            (spectrum_id,)).fetchone()
        if row is None:
            raise RepositoryError(f"unknown spectrum {spectrum_id}")
        counts = np.frombuffer(row[7], dtype=np.int64).copy()
        spec = {
            "spectrum_id": row[0], "device_id": row[1], "product_id": row[2],
            "start_sec": row[3], "T": as_rational(row[4]),
            "L": as_rational(row[5]), "n_channels": row[6], "counts": counts,
        }
        self._spectrum_cache[spectrum_id] = spec
        return spec

    def list_spectra(self) -> List[Dict]:
        return [{"spectrum_id": s, "device_id": d, "product_id": p,
                 "start_sec": t, "n_channels": n}
                for s, d, p, t, n in self.conn.execute(
                    "SELECT spectrum_id, device_id, product_id, start_sec, "
                    "n_channels FROM spectra ORDER BY spectrum_id")]

    # ---- single-spectrum window quantification -----------------------
    @staticmethod
    def _result_key(spectrum_id: str, window: Tuple[Fraction, Fraction],
                    eff: Fraction, lam: float, t_ref: int) -> str:
        basis = "|".join([spectrum_id, rational_to_str(window[0]),
                          rational_to_str(window[1]), rational_to_str(eff),
                          repr(float(lam)), str(t_ref)])
        return hashlib.sha256(basis.encode()).hexdigest()[:16]

    def quantify_window(self, spectrum_id: str, e_lo, e_hi, eff,
                        lam: float, t_ref: int) -> Dict:
        """Quantify one half-open energy window for one spectrum."""
        if lam < 0.0:
            raise RepositoryError("decay constant lambda must be >= 0")
        eff = require_positive(as_rational(eff), "eff")
        e_lo, e_hi = as_rational(e_lo), as_rational(e_hi)
        if e_hi <= e_lo:
            raise RepositoryError("energy window must be non-empty")
        t_ref = int(t_ref)
        spec = self._load_spectrum(spectrum_id)
        result_id = self._result_key(spectrum_id, (e_lo, e_hi), eff, lam,
                                     t_ref)
        cached = self.conn.execute(
            "SELECT payload FROM window_results WHERE result_id = ?",
            (result_id,)).fetchone()
        if cached is not None:
            return json.loads(cached[0])

        product = self._load_product(spec["product_id"])
        geom = product.backproject_window(e_lo, e_hi, spec["n_channels"])
        live_frac = spec["L"] / spec["T"]
        coverage_payload = []
        for j, length in geom["coverage"]:
            raw = int(spec["counts"][j]) if 0 <= j < spec["n_channels"] \
                else None
            coverage_payload.append(
                {"bin": j, "coverage": rational_to_str(length),
                 "raw_count": raw})
        result: Dict = {
            "result_id": result_id,
            "kind": "window",
            "status": geom["status"],
            "spectrum_id": spectrum_id,
            "device_id": spec["device_id"],
            "product_id": spec["product_id"],
            "window": {"energy_lo": rational_to_str(e_lo),
                       "energy_hi": rational_to_str(e_hi)},
            "back_projection": {
                "channel_lo": (rational_to_str(geom["channel_lo"])
                               if geom["channel_lo"] is not None else None),
                "channel_hi": (rational_to_str(geom["channel_hi"])
                               if geom["channel_hi"] is not None else None),
                "coverage_total": rational_to_str(geom["coverage_total"]),
                "per_bin": coverage_payload,
                "reason": geom["reason"],
            },
            "timing": {
                "start_sec": spec["start_sec"], "t_ref": t_ref,
                "T": rational_to_str(spec["T"]),
                "L": rational_to_str(spec["L"]),
                "live_fraction_L_over_T": rational_to_str(live_frac),
            },
            "decay": {"lambda": lam,
                      "integral_I": None, "A_start": None, "A_ref": None},
            "eff": rational_to_str(eff),
            "expected_counts_N": None,
        }

        if geom["status"] == COMPLETE:
            N = counts_from_coverages(spec["counts"], geom["coverage"])
            I = decay_integral(spec["T"], lam)
            A_start = invert_activity(N, eff, live_frac, spec["T"], lam)
            A_ref = to_reference_time(A_start, lam, spec["start_sec"], t_ref)
            result["expected_counts_N"] = rational_to_str(N)
            result["decay"] = {
                "lambda": lam, "integral_I": I,
                "A_start": A_start, "A_ref": A_ref}
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO window_results"
                "(result_id, spectrum_id, window, payload) "
                "VALUES (?, ?, ?, ?)",
                (result_id, spectrum_id,
                 rational_to_str(e_lo) + ":" + rational_to_str(e_hi),
                 json.dumps(result, sort_keys=True)))
        return result

    def quantify_windows(self, spectrum_id: str,
                         windows: Sequence[Tuple], eff, lam: float,
                         t_ref: int) -> List[Dict]:
        if len(windows) > MAX_WINDOWS:
            raise RepositoryError(f"at most {MAX_WINDOWS} windows per call")
        ordered = sorted(((as_rational(lo), as_rational(hi))
                          for lo, hi in windows), key=lambda w: w[0])
        for (_, prev_hi), (lo, _) in zip(ordered, ordered[1:]):
            if lo < prev_hi:
                raise RepositoryError(
                    "energy windows must be non-overlapping half-open "
                    f"intervals; conflict around {lo}")
        return [self.quantify_window(spectrum_id, lo, hi, eff, lam, t_ref)
                for lo, hi in ordered]

    # ---- sample minus background -------------------------------------
    def quantify_net(self, sample_id: str, background_id: str,
                     e_lo, e_hi, eff, lam: float, t_ref: int) -> Dict:
        """Background is converted with its own date and L/T, then subtracted.

        Both sides are first referenced to the same ``t_ref``; negative net
        values are kept, never clipped to zero.  Any incomplete side makes the
        whole net result incomplete.
        """
        sample = self.quantify_window(sample_id, e_lo, e_hi, eff, lam, t_ref)
        background = self.quantify_window(background_id, e_lo, e_hi, eff,
                                          lam, t_ref)
        net_id = hashlib.sha256(
            ("net|" + sample["result_id"] + "|" + background["result_id"])
            .encode()).hexdigest()[:16]
        cached = self.conn.execute(
            "SELECT payload FROM net_results WHERE net_id = ?",
            (net_id,)).fetchone()
        if cached is not None:
            return json.loads(cached[0])

        status = COMPLETE if (sample["status"] == COMPLETE and
                              background["status"] == COMPLETE) else INCOMPLETE
        payload = {
            "net_id": net_id,
            "kind": "net",
            "status": status,
            "t_ref": t_ref,
            "window": sample["window"],
            "sample_result_id": sample["result_id"],
            "background_result_id": background["result_id"],
            "sample": _ref_summary(sample),
            "background": _ref_summary(background),
            "net": None,
        }
        if status == COMPLETE:
            payload["net"] = {
                "A_ref_sample": sample["decay"]["A_ref"],
                "A_ref_background": background["decay"]["A_ref"],
                "A_ref_net": (sample["decay"]["A_ref"]
                              - background["decay"]["A_ref"]),
                "N_sample": sample["expected_counts_N"],
                "N_background": background["expected_counts_N"],
            }
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO net_results"
                "(net_id, sample_id, background_id, payload) "
                "VALUES (?, ?, ?, ?)",
                (net_id, sample_id, background_id,
                 json.dumps(payload, sort_keys=True)))
        return payload

    # ---- verification -------------------------------------------------
    def verify_result(self, result_id: str) -> Dict:
        """Recompute a stored result from raw inputs and compare every step."""
        row = self.conn.execute(
            "SELECT payload FROM window_results WHERE result_id = ?",
            (result_id,)).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT payload FROM net_results WHERE net_id = ?",
                (result_id,)).fetchone()
            if row is None:
                raise RepositoryError(f"unknown result {result_id}")
            return self._verify_net(json.loads(row[0]))
        stored = json.loads(row[0])
        rebuilt = self.quantify_window(
            stored["spectrum_id"], stored["window"]["energy_lo"],
            stored["window"]["energy_hi"], stored["eff"],
            stored["decay"]["lambda"], stored["timing"]["t_ref"])
        problems = []
        if rebuilt["result_id"] != stored["result_id"]:
            problems.append("result id mismatch")
        if rebuilt["status"] != stored["status"]:
            problems.append("status mismatch")
        if rebuilt["back_projection"] != stored["back_projection"]:
            problems.append("per-bin coverage mismatch")
        if rebuilt["timing"] != stored["timing"]:
            problems.append("timing/live-time correction mismatch")
        if stored["status"] == COMPLETE:
            if rebuilt["expected_counts_N"] != stored["expected_counts_N"]:
                problems.append("exact expected counts mismatch")
            for key in ("integral_I", "A_start", "A_ref"):
                if not _float_close(rebuilt["decay"][key],
                                    stored["decay"][key]):
                    problems.append(f"decay field {key} mismatch")
        return {"result_id": result_id, "ok": not problems, "problems": problems}

    def _verify_net(self, stored: Dict) -> Dict:
        problems = []
        child_rows = self.conn.execute(
            "SELECT result_id, payload FROM window_results "
            "WHERE result_id IN (?, ?)",
            (stored["sample_result_id"],
             stored["background_result_id"])).fetchall()
        if len(child_rows) != 2:
            return {"result_id": stored["net_id"], "ok": False,
                    "problems": ["child window result missing"]}
        children = {rid: json.loads(payload) for rid, payload in child_rows}
        sample = children[stored["sample_result_id"]]
        background = children[stored["background_result_id"]]
        rebuilt = self.quantify_net(
            sample["spectrum_id"], background["spectrum_id"],
            sample["window"]["energy_lo"], sample["window"]["energy_hi"],
            sample["eff"], sample["decay"]["lambda"], stored["t_ref"])
        if rebuilt["net_id"] != stored["net_id"]:
            problems.append("net id mismatch")
        if rebuilt["status"] != stored["status"]:
            problems.append("status mismatch")
        if stored["status"] == COMPLETE and not _float_close(
                rebuilt["net"]["A_ref_net"], stored["net"]["A_ref_net"]):
            problems.append("net activity mismatch")
        return {"result_id": stored["net_id"], "ok": not problems,
                "problems": problems}

    def list_results(self) -> List[Dict]:
        out = [{"result_id": r, "kind": "window", "spectrum_id": s}
               for r, s in self.conn.execute(
                   "SELECT result_id, spectrum_id FROM window_results")]
        out.extend({"result_id": n, "kind": "net",
                    "spectrum_id": sid + "-" + bid}
                   for n, sid, bid in self.conn.execute(
                       "SELECT net_id, sample_id, background_id "
                       "FROM net_results"))
        return out


def _ref_summary(result: Dict) -> Dict:
    if result["status"] != COMPLETE:
        return {"status": INCOMPLETE,
                "reason": result["back_projection"]["reason"],
                "lambda": result["decay"]["lambda"], "A_ref": None}
    return {"status": COMPLETE, "lambda": result["decay"]["lambda"],
            "A_ref": result["decay"]["A_ref"],
            "start_sec": result["timing"]["start_sec"]}

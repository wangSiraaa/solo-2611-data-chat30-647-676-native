"""Calibration catalog: private calibration points and published products.

The catalog owns the private (channel, energy) points in its own SQLite file.
Publishing turns the current points of one device into an immutable, portable
:class:`CalibrationProduct`.  Every mutating operation is transactional: a
failed update or publication leaves the previous state untouched.
"""

from __future__ import annotations

import json
import sqlite3
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

from .calibration import CalibrationError, CalibrationProduct
from .limits import MAX_CALIBRATION_POINTS
from .rationals import Number, as_rational, rational_to_str

CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS calibration_points (
    device_id  TEXT NOT NULL,
    version    INTEGER NOT NULL,
    seq        INTEGER NOT NULL,
    channel    TEXT NOT NULL,
    energy     TEXT NOT NULL,
    PRIMARY KEY (device_id, version, seq)
);
CREATE TABLE IF NOT EXISTS device_heads (
    device_id     TEXT PRIMARY KEY,
    head_version  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    product_id  TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL,
    version     INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_products_device ON products(device_id, version);
"""


class CalibrationCatalog:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(CATALOG_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "CalibrationCatalog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- point editing -----------------------------------------------
    def _head_version(self, device_id: str) -> int:
        row = self.conn.execute(
            "SELECT head_version FROM device_heads WHERE device_id = ?",
            (device_id,)).fetchone()
        return row[0] if row else 0

    def replace_points(self, device_id: str,
                       points: List[Tuple[Number, Number]]) -> int:
        """Validate then install a new point set; old state kept on failure."""
        # Full validation happens inside the product constructor.
        draft = CalibrationProduct(device_id, self._head_version(device_id) + 1,
                                   points)
        if len(draft.points) > MAX_CALIBRATION_POINTS:
            raise CalibrationError("too many calibration points")
        try:
            with self.conn:  # atomic
                new_version = self._head_version(device_id) + 1
                self.conn.executemany(
                    "INSERT INTO calibration_points"
                    "(device_id, version, seq, channel, energy) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [(device_id, new_version, i,
                      rational_to_str(c), rational_to_str(e))
                     for i, (c, e) in enumerate(draft.points)])
                self.conn.execute(
                    "INSERT INTO device_heads(device_id, head_version) "
                    "VALUES (?, ?) "
                    "ON CONFLICT(device_id) DO UPDATE SET head_version = ?",
                    (device_id, new_version, new_version))
        except sqlite3.Error:
            raise CalibrationError("catalog update failed; state unchanged")
        return new_version

    def get_points(self, device_id: str,
                   version: Optional[int] = None
                   ) -> List[Tuple[Fraction, Fraction]]:
        version = version or self._head_version(device_id)
        rows = self.conn.execute(
            "SELECT channel, energy FROM calibration_points "
            "WHERE device_id = ? AND version = ? ORDER BY seq",
            (device_id, version)).fetchall()
        if not rows:
            raise CalibrationError(
                f"no calibration points for {device_id} version {version}")
        return [(as_rational(c), as_rational(e)) for c, e in rows]

    def list_devices(self) -> List[Dict]:
        return [{"device_id": d, "head_version": v} for d, v in
                self.conn.execute(
                    "SELECT device_id, head_version FROM device_heads "
                    "ORDER BY device_id")]

    # ---- publishing ---------------------------------------------------
    def publish(self, device_id: str,
                version: Optional[int] = None) -> CalibrationProduct:
        """Build, validate and store an immutable product (atomic)."""
        version = version or self._head_version(device_id)
        points = self.get_points(device_id, version)
        product = CalibrationProduct(device_id, version, points)
        payload = json.dumps(
            {"product_id": product.product_id, **product.to_dict()},
            sort_keys=True, ensure_ascii=False)
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT OR IGNORE INTO products"
                    "(product_id, device_id, version, payload) "
                    "VALUES (?, ?, ?, ?)",
                    (product.product_id, device_id, version, payload))
        except sqlite3.Error:
            raise CalibrationError("publish failed; state unchanged")
        return product

    def get_product(self, product_id: str) -> CalibrationProduct:
        row = self.conn.execute(
            "SELECT payload FROM products WHERE product_id = ?",
            (product_id,)).fetchone()
        if row is None:
            raise CalibrationError(f"unknown product {product_id}")
        return CalibrationProduct.from_json(row[0])

    def list_products(self, device_id: Optional[str] = None) -> List[Dict]:
        if device_id is None:
            rows = self.conn.execute(
                "SELECT product_id, device_id, version FROM products "
                "ORDER BY device_id, version").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT product_id, device_id, version FROM products "
                "WHERE device_id = ? ORDER BY version", (device_id,)).fetchall()
        return [{"product_id": p, "device_id": d, "version": v}
                for p, d, v in rows]

    def export_product(self, product_id: str) -> str:
        """Portable JSON document accepted by an external repository."""
        row = self.conn.execute(
            "SELECT payload FROM products WHERE product_id = ?",
            (product_id,)).fetchone()
        if row is None:
            raise CalibrationError(f"unknown product {product_id}")
        return json.dumps(json.loads(row[0]), indent=2, ensure_ascii=False)

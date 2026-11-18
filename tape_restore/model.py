"""Catalog / volume data model plus structural validation.

The catalog is the *only* source of truth about which physical blocks belong to
which logical file.  Validation therefore never trusts the whole-file digest as a
substitute for structure: total effective length, per-fragment bounds, fragment
identity duplication and physical overlap are all checked explicitly.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

from .errors import CatalogError

FORMAT_NAME = "tape-restore"
FORMAT_VERSION = 1
SHA256_HEX_LEN = 64


def _is_hex_digest(value: object) -> bool:
    if not isinstance(value, str) or len(value) != SHA256_HEX_LEN:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _as_nonneg_int(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CatalogError(f"{what} must be a non-negative integer", got=value)
    return value


def _as_pos_int(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CatalogError(f"{what} must be a positive integer", got=value)
    return value


def _as_name(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise CatalogError(f"{what} must be a non-empty string", got=value)
    return value


@dataclass(frozen=True)
class Block:
    """One fixed-size physical block.

    ``kind`` is ``"data"`` or ``"filemark"``.  A data block's ``payload`` is
    exactly ``block_size`` bytes: the recorded physical payload, including any
    end-of-block padding.  Which of those bytes are *file content* is decided by
    the catalog's ``valid_bytes`` for the fragment covering the block -- padding
    is never treated as file body.  Filemark blocks carry no payload.
    """

    kind: str
    payload: bytes = b""

    def __post_init__(self) -> None:
        if self.kind not in ("data", "filemark"):
            raise CatalogError("block kind must be data or filemark", got=self.kind)
        if self.kind == "filemark" and self.payload:
            raise CatalogError("filemark blocks carry no payload")
        if not isinstance(self.payload, (bytes, bytearray)):
            raise CatalogError("block payload must be bytes")


@dataclass(frozen=True)
class Volume:
    volume_id: str
    block_size: int
    blocks: tuple[Block, ...]

    def __post_init__(self) -> None:
        _as_name(self.volume_id, "volume_id")
        _as_pos_int(self.block_size, "volume block_size")
        for i, block in enumerate(self.blocks):
            if len(block.payload) > self.block_size:
                raise CatalogError(
                    "block payload exceeds declared block_size",
                    volume=self.volume_id,
                    block=i,
                    payload_len=len(block.payload),
                    block_size=self.block_size,
                )

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    def data_blocks(self) -> int:
        return sum(1 for b in self.blocks if b.kind == "data")


@dataclass(frozen=True)
class VolumeRef:
    volume_id: str
    path: Path

    def __post_init__(self) -> None:
        _as_name(self.volume_id, "volume_id")


@dataclass(frozen=True)
class Fragment:
    index: int
    fragment_id: str
    volume_id: str
    start_block: int
    block_count: int
    valid_bytes: int
    sha256: str

    @property
    def end_block(self) -> int:
        """Exclusive block bound."""
        return self.start_block + self.block_count

    @property
    def physical_identity(self) -> tuple[str, int, int]:
        return (self.volume_id, self.start_block, self.block_count)

    def covers(self, block: int) -> bool:
        return self.start_block <= block < self.end_block

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "fragment_id": self.fragment_id,
            "volume_id": self.volume_id,
            "start_block": self.start_block,
            "block_count": self.block_count,
            "valid_bytes": self.valid_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class FileEntry:
    file_id: str
    path: str
    total_length: int
    sha256: str
    fragments: tuple[Fragment, ...]

    def as_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "path": self.path,
            "total_length": self.total_length,
            "sha256": self.sha256,
            "fragments": [f.as_dict() for f in self.fragments],
        }

    def fragment_bytes(self) -> int:
        return sum(f.valid_bytes for f in self.fragments)


@dataclass(frozen=True)
class Catalog:
    block_size: int
    volumes: dict[str, Volume]
    files: dict[str, FileEntry]
    format_version: int = FORMAT_VERSION
    volume_block_counts: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if not self.volume_block_counts:
            object.__setattr__(
                self, "volume_block_counts",
                {vid: vol.block_count for vid, vol in self.volumes.items()},
            )

    # -- construction ----------------------------------------------------
    def as_dict(self) -> dict:
        return {
            "format": FORMAT_NAME,
            "format_version": self.format_version,
            "block_size": self.block_size,
            "volumes": {
                vid: {
                    "volume_id": vid,
                    "block_size": vol.block_size,
                    "block_count": self.volume_block_counts.get(vid, vol.block_count),
                    "data_blocks": vol.data_blocks(),
                }
                for vid, vol in sorted(self.volumes.items())
            },
            "files": [self.files[fid].as_dict() for fid in sorted(self.files)],
            "warnings": list(self.warnings),
        }

    def write_json(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n")
        tmp.replace(path)

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)

    # -- lookup ----------------------------------------------------------
    def file_ids(self) -> list[str]:
        return sorted(self.files)

    def volume_ids(self) -> list[str]:
        return sorted(self.volumes)

    def fragments_for_volume(self, volume_id: str) -> list[tuple[str, Fragment]]:
        out: list[tuple[str, Fragment]] = []
        for fid, fe in self.files.items():
            for frag in fe.fragments:
                if frag.volume_id == volume_id:
                    out.append((fid, frag))
        out.sort(key=lambda pair: (pair[1].start_block, pair[0], pair[1].index))
        return out

    def files_on_volume(self, volume_id: str) -> list[str]:
        seen = []
        for fid, _ in self.fragments_for_volume(volume_id):
            if fid not in seen:
                seen.append(fid)
        return seen


# ---------------------------------------------------------------------------
# loading + validation
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    try:
        raw = Path(path).read_text()
    except OSError as exc:
        raise CatalogError(f"cannot read catalog: {exc}", path=str(path)) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CatalogError(f"catalog is not valid JSON: {exc}", path=str(path)) from exc
    if not isinstance(data, dict):
        raise CatalogError("catalog root must be an object")
    return data


def parse_catalog(data: dict, volumes: Optional[dict[str, Volume]] = None) -> Catalog:
    """Parse + fully validate a catalog dict.

    ``volumes`` may be supplied (already synthesised) so the validator can check
    every fragment against real block counts and block kinds.  When omitted the
    catalog is still structurally validated (lengths, identities, ranges against
    *declared* volume sizes if present in the dict).
    """

    if data.get("format") != FORMAT_NAME:
        raise CatalogError("unexpected catalog format", got=data.get("format"))
    version = data.get("format_version")
    if version != FORMAT_VERSION:
        raise CatalogError("unsupported catalog format version", got=version)
    block_size = _as_pos_int(data.get("block_size"), "catalog block_size")

    warnings: list[str] = []

    parsed_volumes: dict[str, Volume] = {}
    declared_sizes: dict[str, int] = {}
    raw_volumes = data.get("volumes")
    if not isinstance(raw_volumes, dict):
        raise CatalogError("catalog.volumes must be an object")
    for vid, vdata in raw_volumes.items():
        _as_name(vid, "volume id")
        if not isinstance(vdata, dict):
            raise CatalogError("volume entry must be an object", volume=vid)
        vbs = _as_pos_int(vdata.get("block_size", block_size), f"{vid}.block_size")
        if vbs != block_size:
            raise CatalogError(
                "volume block_size differs from catalog block_size",
                volume=vid, volume_block_size=vbs, catalog_block_size=block_size,
            )
        if volumes is not None:
            if vid not in volumes:
                raise CatalogError("declared volume has no synthetic media", volume=vid)
            vol = volumes[vid]
            if vol.block_size != block_size:
                raise CatalogError(
                    "synthetic volume block_size mismatch",
                    volume=vid, volume_block_size=vol.block_size,
                )
            if vdata.get("block_count") is not None and vdata.get("block_count") != vol.block_count:
                raise CatalogError(
                    "catalog block_count disagrees with synthetic volume",
                    volume=vid, declared=vdata.get("block_count"), actual=vol.block_count,
                )
            parsed_volumes[vid] = vol
            declared_sizes[vid] = vol.block_count
        else:
            bc = _as_nonneg_int(vdata.get("block_count"), f"{vid}.block_count")
            declared_sizes[vid] = bc

    if volumes is not None:
        extra = sorted(set(volumes) - set(raw_volumes))
        if extra:
            warnings.append("synthetic volumes not referenced by catalog: " + ", ".join(extra))

    raw_files = data.get("files")
    if not isinstance(raw_files, list):
        raise CatalogError("catalog.files must be a list")
    if not raw_files:
        warnings.append("catalog contains no files")

    files: dict[str, FileEntry] = {}
    file_paths: dict[str, str] = {}
    physical_owner: dict[tuple[str, int], str] = {}
    seen_fragment_ids: dict[str, str] = {}

    for fi, fdata in enumerate(raw_files):
        if not isinstance(fdata, dict):
            raise CatalogError("file entry must be an object", position=fi)
        fid = _as_name(fdata.get("file_id"), f"files[{fi}].file_id")
        if fid in files:
            raise CatalogError("duplicate file_id", file_id=fid)
        rel_path = _as_name(fdata.get("path"), f"{fid}.path")
        _validate_rel_path(rel_path, fid)
        if rel_path in file_paths:
            raise CatalogError(
                "two files share the same output path",
                path=rel_path, files=sorted([fid, file_paths[rel_path]]),
            )
        file_paths[rel_path] = fid
        total_length = _as_nonneg_int(fdata.get("total_length"), f"{fid}.total_length")
        digest = fdata.get("sha256")
        if not _is_hex_digest(digest):
            raise CatalogError("file sha256 must be 64 hex chars", file_id=fid)

        raw_frags = fdata.get("fragments")
        if not isinstance(raw_frags, list):
            raise CatalogError("file fragments must be a list", file_id=fid)

        fragments: list[Fragment] = []
        frag_ids_here: set[str] = set()
        expected_offset = 0
        for idx, fr in enumerate(raw_frags):
            if not isinstance(fr, dict):
                raise CatalogError("fragment must be an object", file_id=fid, index=idx)
            frag_id = _as_name(fr.get("fragment_id"), f"{fid}.fragments[{idx}].fragment_id")
            if frag_id in frag_ids_here:
                raise CatalogError("duplicate fragment_id within file", file_id=fid, fragment_id=frag_id)
            if frag_id in seen_fragment_ids:
                raise CatalogError(
                    "fragment_id reused across files",
                    fragment_id=frag_id, files=sorted([fid, seen_fragment_ids[frag_id]]),
                )
            frag_ids_here.add(frag_id)
            seen_fragment_ids[frag_id] = fid

            vid = _as_name(fr.get("volume_id"), f"{fid}.{frag_id}.volume_id")
            if vid not in declared_sizes:
                raise CatalogError(
                    "fragment references unknown volume", file_id=fid, fragment_id=frag_id, volume_id=vid,
                )
            start = _as_nonneg_int(fr.get("start_block"), f"{fid}.{frag_id}.start_block")
            count = _as_pos_int(fr.get("block_count"), f"{fid}.{frag_id}.block_count")
            valid = _as_pos_int(fr.get("valid_bytes"), f"{fid}.{frag_id}.valid_bytes")
            fsha = fr.get("sha256")
            if not _is_hex_digest(fsha):
                raise CatalogError("fragment sha256 must be 64 hex chars", file_id=fid, fragment_id=frag_id)

            end = start + count
            if end > declared_sizes[vid]:
                raise CatalogError(
                    "fragment extends past end of volume",
                    file_id=fid, fragment_id=frag_id, volume_id=vid,
                    start_block=start, block_count=count,
                    volume_block_count=declared_sizes[vid],
                )
            if valid > count * block_size:
                raise CatalogError(
                    "fragment valid_bytes exceeds block capacity",
                    file_id=fid, fragment_id=frag_id,
                    valid_bytes=valid, capacity=count * block_size,
                )
            if valid <= (count - 1) * block_size:
                raise CatalogError(
                    "fragment block_count is larger than its valid_bytes needs",
                    file_id=fid, fragment_id=frag_id,
                    valid_bytes=valid, block_count=count, block_size=block_size,
                )

            frag = Fragment(
                index=idx, fragment_id=frag_id, volume_id=vid, start_block=start,
                block_count=count, valid_bytes=valid, sha256=fsha,
            )
            fragments.append(frag)

            for b in range(start, end):
                key = (vid, b)
                if key in physical_owner:
                    raise CatalogError(
                        "physical block claimed by two fragments",
                        volume_id=vid, block=b,
                        owners=sorted([physical_owner[key], f"{fid}:{frag_id}"]),
                    )
                physical_owner[key] = f"{fid}:{frag_id}"

            expected_offset += valid

        if total_length == 0:
            if fragments:
                raise CatalogError("zero-length file must not declare fragments", file_id=fid)
        else:
            if not fragments:
                raise CatalogError("non-empty file must declare fragments", file_id=fid)
            if expected_offset != total_length:
                raise CatalogError(
                    "fragment valid_bytes do not sum to file total_length",
                    file_id=fid, fragment_bytes=expected_offset, total_length=total_length,
                )

        files[fid] = FileEntry(
            file_id=fid, path=rel_path, total_length=total_length,
            sha256=digest, fragments=tuple(fragments),
        )

    # Cross-check with real synthetic media when supplied: fragment blocks must
    # be data blocks, and no fragment may run into a filemark.
    if volumes is not None:
        for fid, fe in files.items():
            for frag in fe.fragments:
                vol = volumes[frag.volume_id]
                for b in range(frag.start_block, frag.end_block):
                    if vol.blocks[b].kind != "data":
                        raise CatalogError(
                            "fragment covers a filemark block",
                            file_id=fid, fragment_id=frag.fragment_id,
                            volume_id=frag.volume_id, block=b,
                        )
        for vid, vol in volumes.items():
            claimed = {b for (v, b) in physical_owner if v == vid}
            for b in range(vol.block_count):
                if vol.blocks[b].kind == "data" and b not in claimed:
                    warnings.append(f"unreferenced data block {vid}:{b}")

    if warnings:
        warnings = list(dict.fromkeys(warnings))  # de-dup, keep order

    return Catalog(
        block_size=block_size, volumes=parsed_volumes, files=files,
        format_version=version, volume_block_counts=dict(declared_sizes),
        warnings=tuple(warnings),
    )


def _validate_rel_path(rel_path: str, file_id: str) -> None:
    pp = PurePosixPath(rel_path)
    if pp.is_absolute() or ".." in pp.parts or rel_path.startswith("/"):
        raise CatalogError("file path must be a safe relative path", file_id=file_id, path=rel_path)
    if not pp.parts:
        raise CatalogError("file path must not be empty", file_id=file_id)


def load_catalog(path: Path, volumes: Optional[dict[str, Volume]] = None) -> Catalog:
    return parse_catalog(_load_json(Path(path)), volumes=volumes)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

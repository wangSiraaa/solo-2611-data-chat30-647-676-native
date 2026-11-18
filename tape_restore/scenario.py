"""Synthetic media + catalog generator.

Builds a self-consistent scenario *plus true source bytes* so tests can compare
restored output byte for byte, independently of the catalog digests.  The
generator never writes its truth into the media/catalog itself.

Scenario ingredients required by the brief:

* multi-volume files (fragments spanning volumes),
* shared interleaved volumes (several files' fragments alternating),
* a zero-length file,
* end-of-block padding,
* a quota-insufficiency case (file larger than the 32 KiB workspace),
* a bad fragment (catalog says X, medium holds Y),
* a resume case (a long file interrupted mid-way).

Sizes stay inside the limits: <= 6 volumes, <= 100 blocks/volume, 1024-byte
blocks, <= 20 files, 32 KiB work quota.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import media
from .model import Block, Catalog, FileEntry, Fragment, Volume, parse_catalog

BLOCK_SIZE = 1024
QUOTA_BYTES = 32 * 1024


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rng_bytes(rng: random.Random, n: int) -> bytes:
    return bytes(rng.getrandbits(8) for _ in range(n))


def _chunks(data: bytes, block_size: int) -> list[bytes]:
    return [data[i:i + block_size] for i in range(0, len(data), block_size)] or []


@dataclass
class PlannedFragment:
    file_id: str
    fragment_id: str
    volume_id: str
    start_block: int
    block_count: int
    valid_bytes: int
    sha256: str
    true_bytes: bytes


@dataclass
class PlannedFile:
    file_id: str
    path: str
    data: bytes
    save_every: Optional[int] = None  # blocks processed between checkpoints


@dataclass
class Scenario:
    name: str
    volumes: dict[str, Volume]
    catalog: Catalog
    truth: dict[str, bytes]
    truth_segments: dict[str, list[tuple[int, int, bytes]]]
    media_paths: dict[str, Path]
    quota_bytes: int
    scenarios: dict[str, dict]
    bad_fragments: list[dict] = field(default_factory=list)
    root: Optional[Path] = None
    meta: dict = field(default_factory=dict)

    def catalog_path(self) -> Path:
        assert self.root is not None
        return self.root / "catalog.json"

    def truth_bytes(self, file_id: str) -> bytes:
        return self.truth[file_id]

    def as_summary(self) -> dict:
        return {
            "name": self.name,
            "quota_bytes": self.quota_bytes,
            "volumes": {vid: v.block_count for vid, v in self.volumes.items()},
            "files": {
                fid: {
                    "total_length": fe.total_length,
                    "fragments": len(fe.fragments),
                    "sha256": fe.sha256,
                }
                for fid, fe in self.catalog.files.items()
            },
            "scenarios": self.scenarios,
            "bad_fragments": self.bad_fragments,
            "truth_paths": {fid: fe.path for fid, fe in self.catalog.files.items()},
            "meta": self.meta,
        }


class _Builder:
    def __init__(self, block_size: int, block_counts: dict[str, int]) -> None:
        self.block_size = block_size
        self.blocks: dict[str, list[Optional[Block]]] = {
            vid: [None] * n for vid, n in block_counts.items()
        }
        self.cursors: dict[str, int] = {vid: 0 for vid in block_counts}
        self.files: list[PlannedFile] = []
        self.frags_by_file: dict[str, list[PlannedFragment]] = {}

    def add_file(self, file_id: str, path: str, data: bytes,
                 *, save_every: Optional[int] = None) -> None:
        self.files.append(PlannedFile(file_id, path, data, save_every))
        self.frags_by_file[file_id] = []

    def place(self, file_id: str, volume_id: str, nblocks: int) -> int:
        """Reserve the next ``nblocks`` data blocks on ``volume_id``."""
        cur = self.cursors[volume_id]
        total = len(self.blocks[volume_id])
        if cur + nblocks > total:
            raise RuntimeError(
                f"volume {volume_id} cannot fit {nblocks} more blocks at {cur}/{total}"
            )
        self.cursors[volume_id] = cur + nblocks
        return cur

    def fill_fragment(self, file_id: str, volume_id: str, data: bytes) -> PlannedFragment:
        """Place ``data`` as one contiguous fragment on ``volume_id``."""
        chunk_list = _chunks(data, self.block_size)
        nblocks = len(chunk_list)
        if nblocks == 0:
            raise RuntimeError("empty fragment")
        start = self.place(file_id, volume_id, nblocks)
        for k, chunk in enumerate(chunk_list):
            pad = b"\x00" * (self.block_size - len(chunk))
            self.blocks[volume_id][start + k] = Block("data", chunk + pad)
        valid = len(data)
        frag = PlannedFragment(
            file_id=file_id, fragment_id=f"{file_id}-{len(self.frags_by_file[file_id]):02d}",
            volume_id=volume_id, start_block=start, block_count=nblocks,
            valid_bytes=valid, sha256=_sha(data), true_bytes=data,
        )
        self.frags_by_file[file_id].append(frag)
        return frag

    def scatter(self, file_id: str, data: bytes, interleave: list[tuple[str, int]],
                 ) -> None:
        """Split a file into pieces placed across ``interleave`` volumes.

        ``interleave`` is a repeating list of (volume_id, blocks_per_piece).
        Each piece is 1..blocks_per_piece blocks of content.
        """
        chunks = _chunks(data, self.block_size)
        offset = 0
        idx = 0
        while offset < len(chunks):
            vid, piece_blocks = interleave[idx % len(interleave)]
            idx += 1
            piece = chunks[offset:offset + piece_blocks]
            body = b"".join(piece)
            # valid bytes: last piece may be smaller than a full block span
            valid = len(body) if offset + len(piece) < len(chunks) else len(body)
            start = self.place(file_id, vid, len(piece))
            for k, chunk in enumerate(piece):
                pad = b"\x00" * (self.block_size - len(chunk))
                self.blocks[vid][start + k] = Block("data", chunk + pad)
            frag = PlannedFragment(
                file_id=file_id, fragment_id=f"{file_id}-{len(self.frags_by_file[file_id]):02d}",
                volume_id=vid, start_block=start, block_count=len(piece),
                valid_bytes=valid, sha256=_sha(body), true_bytes=body,
            )
            self.frags_by_file[file_id].append(frag)
            offset += len(piece)

    def append_filemark(self, volume_id: str, count: int = 1) -> None:
        cur = self.cursors[volume_id]
        total = len(self.blocks[volume_id])
        if cur + count > total:
            raise RuntimeError("not enough room for filemarks")
        for k in range(count):
            self.blocks[volume_id][cur + k] = Block("filemark")
        self.cursors[volume_id] = cur + count

    def finish(self, name: str, *, block_size: int) -> Scenario:
        # Fill remaining slots with filemarks + a little unreferenced data so
        # not every tail is already a filemark.
        for vid, slots in self.blocks.items():
            for i, b in enumerate(slots):
                if b is None:
                    slots[i] = Block("filemark") if (i % 3) else Block("data", b"\xee" * self.block_size)
            if any(b is None for b in slots):  # pragma: no cover
                raise AssertionError("unfilled block")
        volumes = {
            vid: Volume(vid, block_size, tuple(slots))
            for vid, slots in self.blocks.items()
        }
        files: list[FileEntry] = []
        truth: dict[str, bytes] = {}
        truth_segments: dict[str, list[tuple[int, int, bytes]]] = {}
        scenarios: dict[str, dict] = {}
        for pf in self.files:
            frags = self.frags_by_file[pf.file_id]
            entry = FileEntry(
                file_id=pf.file_id, path=pf.path, total_length=len(pf.data),
                sha256=_sha(pf.data),
                fragments=tuple(
                    Fragment(
                        index=i, fragment_id=f.fragment_id, volume_id=f.volume_id,
                        start_block=f.start_block, block_count=f.block_count,
                        valid_bytes=f.valid_bytes, sha256=f.sha256,
                    ) for i, f in enumerate(frags)
                ),
            )
            files.append(entry)
            truth[pf.file_id] = pf.data
            segments = []
            off = 0
            for f in frags:
                segments.append((off, f.valid_bytes, f.true_bytes))
                off += f.valid_bytes
            truth_segments[pf.file_id] = segments
            if pf.save_every is not None:
                scenarios[pf.file_id] = {"save_every_blocks": pf.save_every}
        catalog = Catalog(
            block_size=block_size, volumes=volumes,
            files={e.file_id: e for e in files},
        )
        return Scenario(
            name=name, volumes=volumes, catalog=catalog,
            truth=truth, truth_segments=truth_segments,
            media_paths={}, quota_bytes=QUOTA_BYTES, scenarios=scenarios,
        )


def _build_volumes_ordered(files: list[PlannedFile], builder: _Builder) -> None:
    """Volume-major placement: finish one volume before touching the next."""
    # files are in deterministic order; we interleave a few on shared volumes
    # by grouping: first two files share V1&V2 alternately, etc.
    pass


def generate_scenario(out_root: Path, name: str = "main", *, force: bool = False) -> Scenario:
    """Generate the canonical synthetic scenario under ``out_root``."""
    out_root = Path(out_root)
    media_dir = out_root / "media"
    if out_root.exists() and not force:
        # idempotent: reuse if a scenario marker exists
        marker = out_root / "scenario.json"
        if marker.exists():
            return load_scenario(out_root)
    if out_root.exists() and force:
        import shutil
        shutil.rmtree(out_root)
    media_dir.mkdir(parents=True, exist_ok=True)

    rng = random.Random(20261118)

    # ----------------------------------------------------------------
    # File truth bytes are generated first; placement follows.
    # ----------------------------------------------------------------
    f01 = _rng_bytes(rng, 3 * BLOCK_SIZE + 400)       # 3.39 blocks, multi-volume
    f02 = _rng_bytes(rng, 4 * BLOCK_SIZE + 100)       # interleaved with f01
    f03 = _rng_bytes(rng, 1 * BLOCK_SIZE - 1)         # padding within 1 block
    f04 = _rng_bytes(rng, 0)                          # zero-length
    f05 = _rng_bytes(rng, 2 * BLOCK_SIZE + 700)       # second volume spans
    f06 = _rng_bytes(rng, 5 * BLOCK_SIZE + 300)       # interleaved shared
    f07 = _rng_bytes(rng, 6 * BLOCK_SIZE + 5)         # save_every for resume
    f08 = _rng_bytes(rng, 2 * BLOCK_SIZE - 3)         # padding trailing
    f09 = _rng_bytes(rng, 9 * BLOCK_SIZE + 950)       # "big" but < quota
    f10 = _rng_bytes(rng, 41 * BLOCK_SIZE + 500)      # > 32KiB quota case
    f11 = _rng_bytes(rng, 3 * BLOCK_SIZE + 1)         # bad fragment case
    f12 = _rng_bytes(rng, 10 * BLOCK_SIZE + 7)        # long resume file
    f13 = _rng_bytes(rng, 1 * BLOCK_SIZE)             # full block padding-free
    f14 = _rng_bytes(rng, 2 * BLOCK_SIZE + 9)         # split three volumes
    f15 = _rng_bytes(rng, 3 * BLOCK_SIZE + 2)         # two fragments same volume
    f16 = _rng_bytes(rng, 4 * BLOCK_SIZE + 11)        # spans three volumes
    f17 = _rng_bytes(rng, 1)                          # 1-byte file
    f18 = _rng_bytes(rng, 2 * BLOCK_SIZE + 4)         # volume truncation case
    f19 = _rng_bytes(rng, 3 * BLOCK_SIZE + 6)         # interleaved again
    f20 = _rng_bytes(rng, 2 * BLOCK_SIZE + 8)         # zero-padding-sensitive
    # ensure some puts digest recompute paths in play
    builder = _Builder(
        BLOCK_SIZE,
        {"V1": 100, "V2": 100, "V3": 100, "V4": 100, "V5": 100, "V6": 100},
    )
    builder.add_file("F01", "docs/f01.bin", f01)
    builder.add_file("F02", "docs/f02.bin", f02)
    builder.add_file("F03", "docs/f03.bin", f03)
    builder.add_file("F04", "docs/empty.dat", f04)
    builder.add_file("F05", "docs/f05.bin", f05)
    builder.add_file("F06", "docs/f06.bin", f06)
    builder.add_file("F07", "docs/f07.bin", f07, save_every=2)
    builder.add_file("F08", "docs/f08.bin", f08)
    builder.add_file("F09", "docs/f09.bin", f09)
    builder.add_file("F10", "docs/f10.bin", f10)
    builder.add_file("F11", "docs/f11.bin", f11)
    builder.add_file("F12", "docs/f12.bin", f12, save_every=3)
    builder.add_file("F13", "docs/f13.bin", f13)
    builder.add_file("F14", "docs/f14.bin", f14)
    builder.add_file("F15", "docs/f15.bin", f15)
    builder.add_file("F16", "docs/f16.bin", f16)
    builder.add_file("F17", "docs/f17.bin", f17)
    builder.add_file("F18", "docs/f18.bin", f18)
    builder.add_file("F19", "docs/f19.bin", f19)
    builder.add_file("F20", "docs/f20.bin", f20)

    # V1: f01 (2 blocks), f02 (2), interleave, plus f03 & f17 & filemark runs.
    builder.fill_fragment("F01", "V1", f01[: 2 * BLOCK_SIZE])
    builder.append_filemark("V1")
    builder.fill_fragment("F02", "V1", f02[: 2 * BLOCK_SIZE])
    builder.append_filemark("V1")
    builder.fill_fragment("F03", "V1", f03)
    builder.append_filemark("V1", 2)
    builder.fill_fragment("F01", "V2", f01[2 * BLOCK_SIZE:])
    builder.append_filemark("V1")
    builder.fill_fragment("F02", "V2", f02[2 * BLOCK_SIZE:])
    builder.append_filemark("V1")
    builder.fill_fragment("F17", "V1", f17)
    builder.append_filemark("V1", 4)
    # V2: f05 spans V2/V3, f08, and a shared run with f06 pieces
    builder.fill_fragment("F05", "V2", f05[: 1 * BLOCK_SIZE])
    builder.append_filemark("V2")
    builder.fill_fragment("F08", "V2", f08[: 1 * BLOCK_SIZE])
    builder.append_filemark("V2")
    builder.fill_fragment("F05", "V3", f05[1 * BLOCK_SIZE:])
    builder.append_filemark("V2")
    builder.fill_fragment("F08", "V2", f08[1 * BLOCK_SIZE:])
    builder.append_filemark("V2")
    builder.fill_fragment("F13", "V2", f13)
    builder.append_filemark("V2", 2)
    # V3: interleave f06 & f19 & f14 on shared volume
    builder.fill_fragment("F06", "V3", f06[: 2 * BLOCK_SIZE])
    builder.fill_fragment("F19", "V3", f19[: 2 * BLOCK_SIZE])
    builder.fill_fragment("F06", "V3", f06[2 * BLOCK_SIZE: 4 * BLOCK_SIZE])
    builder.fill_fragment("F14", "V3", f14[: 1 * BLOCK_SIZE])
    builder.append_filemark("V3")
    builder.fill_fragment("F19", "V3", f19[2 * BLOCK_SIZE:])
    builder.fill_fragment("F06", "V3", f06[4 * BLOCK_SIZE:])
    builder.fill_fragment("F14", "V3", f14[1 * BLOCK_SIZE: 2 * BLOCK_SIZE])
    builder.append_filemark("V3", 3)
    # V4: f09, f15 (two fragments same volume), f07 (resume)
    builder.fill_fragment("F09", "V4", f09[: 3 * BLOCK_SIZE])
    builder.append_filemark("V4")
    builder.fill_fragment("F15", "V4", f15[: 1 * BLOCK_SIZE])
    builder.append_filemark("V4")
    builder.fill_fragment("F07", "V4", f07[: 2 * BLOCK_SIZE])
    builder.append_filemark("V4")
    builder.fill_fragment("F15", "V4", f15[1 * BLOCK_SIZE:])
    builder.append_filemark("V4")
    builder.fill_fragment("F14", "V4", f14[2 * BLOCK_SIZE:])
    builder.fill_fragment("F09", "V4", f09[3 * BLOCK_SIZE: 5 * BLOCK_SIZE])
    builder.append_filemark("V4")
    builder.fill_fragment("F07", "V4", f07[2 * BLOCK_SIZE: 4 * BLOCK_SIZE])
    builder.append_filemark("V4")
    builder.fill_fragment("F09", "V4", f09[5 * BLOCK_SIZE:])
    builder.fill_fragment("F16", "V4", f16[: 1 * BLOCK_SIZE])
    builder.fill_fragment("F18", "V4", f18[: 1 * BLOCK_SIZE])
    builder.append_filemark("V4", 2)
    # V5: f07 remainder, f12 (long resume), f20, f16 pieces
    builder.fill_fragment("F07", "V5", f07[4 * BLOCK_SIZE:])
    builder.append_filemark("V5")
    builder.fill_fragment("F12", "V5", f12[: 3 * BLOCK_SIZE])
    builder.append_filemark("V5")
    builder.fill_fragment("F20", "V5", f20[: 1 * BLOCK_SIZE])
    builder.append_filemark("V5", 2)
    builder.fill_fragment("F12", "V5", f12[3 * BLOCK_SIZE: 6 * BLOCK_SIZE])
    builder.append_filemark("V5")
    builder.fill_fragment("F16", "V5", f16[1 * BLOCK_SIZE: 2 * BLOCK_SIZE])
    builder.append_filemark("V5")
    builder.fill_fragment("F12", "V5", f12[6 * BLOCK_SIZE:])
    builder.fill_fragment("F16", "V5", f16[2 * BLOCK_SIZE: 3 * BLOCK_SIZE])
    builder.fill_fragment("F20", "V5", f20[1 * BLOCK_SIZE:])
    builder.fill_fragment("F18", "V5", f18[1 * BLOCK_SIZE:])
    builder.append_filemark("V5", 2)
    # V6: f16 remainder, f11 (bad fragment), f10 (larger than the 32 KiB quota)
    builder.fill_fragment("F16", "V6", f16[3 * BLOCK_SIZE:])
    builder.append_filemark("V6")
    builder.fill_fragment("F10", "V6", f10[: 20 * BLOCK_SIZE])
    builder.append_filemark("V6")
    builder.fill_fragment("F11", "V6", f11)
    builder.append_filemark("V6")
    builder.fill_fragment("F10", "V6", f10[20 * BLOCK_SIZE:])
    builder.append_filemark("V6", 3)

    scenario = builder.finish(name, block_size=BLOCK_SIZE)
    scenario.root = out_root
    scenario.name = name

    # --- bad fragment: catalog claims one digest, medium holds another -----
    v3 = list(scenario.volumes["V3"].blocks)
    # F14's first fragment lives at block 6 of V3 (0-indexed from the sequence
    # above). Find it via the catalog rather than hard-code.
    f14_entry = scenario.catalog.files["F14"]
    bad_frag = f14_entry.fragments[0]
    flip_at = bad_frag.start_block
    orig = v3[flip_at].payload
    mutated = bytearray(orig)
    mutated[0] ^= 0x01
    v3[flip_at] = Block("data", bytes(mutated))
    scenario.volumes["V3"] = Volume("V3", BLOCK_SIZE, tuple(v3))
    scenario.bad_fragments.append({
        "file_id": "F14", "fragment_id": bad_frag.fragment_id,
        "volume_id": "V3", "block": flip_at,
        "reason": "media payload mutated; catalog sha256 unchanged",
    })

    # Also corrupt F18's first block so the truncation case (F18) can assert the
    # catalog is rich enough to detect missing data.  (F18 itself is reported via
    # truncated media in a separate truncation scenario, see tests.)
    v4 = list(scenario.volumes["V4"].blocks)
    f18_entry = scenario.catalog.files["F18"]
    f18_frag = f18_entry.fragments[0]
    mutated = bytearray(v4[f18_frag.start_block].payload)
    mutated[0] ^= 0x02
    v4[f18_frag.start_block] = Block("data", bytes(mutated))
    scenario.volumes["V4"] = Volume("V4", BLOCK_SIZE, tuple(v4))
    scenario.bad_fragments.append({
        "file_id": "F18", "fragment_id": f18_frag.fragment_id,
        "volume_id": "V4", "block": f18_frag.start_block,
        "reason": "media payload mutated (second bad-fragment probe)",
    })

    # --- write media images ------------------------------------------------
    media_dir.mkdir(parents=True, exist_ok=True)
    media_paths: dict[str, Path] = {}
    for vid, vol in scenario.volumes.items():
        p = media_dir / f"{vid}.tape"
        media.write_volume(p, vol)
        media_paths[vid] = p
    scenario.media_paths = media_paths

    # --- validate catalog against the *synthetic* volumes ------------------
    scenario.catalog = parse_catalog(
        scenario.catalog.as_dict(), volumes=scenario.volumes
    )
    scenario.catalog.write_json(scenario.catalog_path())

    scenario.meta = {
        "block_size": BLOCK_SIZE,
        "quota_bytes": QUOTA_BYTES,
        "drive_count": 2,
        "generated_random_seed": 20261118,
        "note": "truth bytes are kept in scenario.json only for independent verification",
    }
    (out_root / "scenario.json").write_text(
        json.dumps(scenario.as_summary(), indent=2, sort_keys=True) + "\n"
    )
    _write_truth(out_root / "truth", scenario.truth)
    scenario.scenarios.setdefault("F07", {"save_every_blocks": 2})
    scenario.scenarios.setdefault("F12", {"save_every_blocks": 3})
    return scenario


def _write_truth(dest: Path, truth: dict[str, bytes]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for fid, data in truth.items():
        (dest / f"{fid}.bin").write_bytes(data)
    (dest / "manifest.json").write_text(
        json.dumps({fid: _sha(data) for fid, data in truth.items()}, indent=2, sort_keys=True) + "\n"
    )


def load_scenario(root: Path) -> Scenario:
    """Reload a scenario from disk (media + catalog + truth), re-validating."""
    root = Path(root)
    summary = json.loads((root / "scenario.json").read_text())
    media_dir = root / "media"
    volumes = {
        vid: media.read_volume(media_dir / f"{vid}.tape")
        for vid in summary["volumes"]
    }
    catalog = parse_catalog(json.loads((root / "catalog.json").read_text()), volumes=volumes)
    truth = {fid: (root / "truth" / f"{fid}.bin").read_bytes() for fid in catalog.files}
    truth_segments: dict[str, list[tuple[int, int, bytes]]] = {}
    for fid, fe in catalog.files.items():
        off = 0
        segs = []
        for frag in fe.fragments:
            segs.append((off, frag.valid_bytes, truth[fid][off:off + frag.valid_bytes]))
            off += frag.valid_bytes
        truth_segments[fid] = segs
    return Scenario(
        name=summary["name"],
        volumes=volumes,
        catalog=catalog,
        truth=truth,
        truth_segments=truth_segments,
        media_paths={vid: media_dir / f"{vid}.tape" for vid in volumes},
        quota_bytes=summary["quota_bytes"],
        scenarios=summary.get("scenarios", {}),
        bad_fragments=summary.get("bad_fragments", []),
        root=root,
        meta=summary.get("meta", {}),
    )


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results/scenario")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    sc = generate_scenario(Path(a.out), force=a.force)
    print(json.dumps(sc.as_summary(), indent=2))

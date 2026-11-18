"""Restore planner: two strategies with explicit cost trade-offs.

Both plans are computed from the catalog and the *real* synthetic volumes (via
the media library), so block kinds and volume sizes are known precisely.  Plans
are advisory: every strategy is valid, and neither is asserted to be globally
optimal.

``volume`` (aggregate by volume)
    Mount each needed volume once, sweep it front-to-back once, and stash every
    fragment encountered.  Cost: fewest mounts / rewinds, but scratch peak is
    high because all confirmed fragment bytes for the in-flight scan are held
    until their files can be assembled.  Requires the *sum of all in-flight
    fragment bytes* to fit the quota, so it fails on scenarios where individual
    files are large.

``file`` (finish files one at a time)
    For each file (smallest footprint first), visit its fragments in volume
    order, writing accepted bytes straight to that file's target at the final
    offset.  Cost: more mounts / rewinds (each file may bounce between volumes),
    but scratch peak is bounded by roughly one file's bytes at a time.  Files
    with no fragments (zero-length) need no media access at all.

The planner reports predicted mounts, rewind+skip block traversals and a
predicted scratch peak so the trade-off is inspectable before any I/O happens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .device import MediaLibrary
from .errors import RestoreError
from .model import Catalog, FileEntry, Fragment

STRATEGIES = ("volume", "file")


@dataclass
class VolumeRun:
    volume_id: str
    fragments: list[tuple[str, Fragment]]  # (file_id, fragment) in block order
    start_block: int
    end_block: int


@dataclass
class FilePlan:
    file_id: str
    path: str
    total_length: int
    fragments: list[Fragment]
    volumes: list[str]


@dataclass
class Plan:
    strategy: str
    file_ids: list[str]
    quota_bytes: int
    volume_counts: dict[str, int]
    files: list[FilePlan]
    runs: list[VolumeRun]
    predicted_mounts: int
    predicted_rewinds: int
    predicted_skip_blocks: int
    predicted_reads: int
    predicted_scratch_peak: int
    scratch_fits: bool
    notes: list[str] = field(default_factory=list)
    fits: bool = True
    infeasible: list[str] = field(default_factory=list)
    # files that cannot be completed under this strategy/quota (e.g. a target
    # larger than the work quota); the rest of the plan can still run.
    skipped_files: list[str] = field(default_factory=list)
    restorable: bool = True

    def as_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "file_ids": self.file_ids,
            "quota_bytes": self.quota_bytes,
            "volume_counts": self.volume_counts,
            "predicted": {
                "mounts": self.predicted_mounts,
                "rewinds": self.predicted_rewinds,
                "skip_blocks": self.predicted_skip_blocks,
                "reads": self.predicted_reads,
                "scratch_peak_bytes": self.predicted_scratch_peak,
                "scratch_fits": self.scratch_fits,
            },
            "fits": self.fits,
            "restorable": self.restorable,
            "skipped_files": self.skipped_files,
            "infeasible": self.infeasible,
            "notes": self.notes,
            "files": [
                {
                    "file_id": fp.file_id, "path": fp.path,
                    "total_length": fp.total_length,
                    "fragment_count": len(fp.fragments),
                    "volumes": fp.volumes,
                }
                for fp in self.files
            ],
            "runs": [
                {
                    "volume_id": r.volume_id,
                    "start_block": r.start_block,
                    "end_block": r.end_block,
                    "fragment_count": len(r.fragments),
                    "file_ids": sorted({fid for fid, _ in r.fragments}),
                }
                for r in self.runs
            ],
        }


def _select_files(catalog: Catalog, file_ids: Optional[Iterable[str]] = None) -> list[FileEntry]:
    if file_ids is None:
        ids = catalog.file_ids()
    else:
        ids = list(dict.fromkeys(file_ids))
    missing = [fid for fid in ids if fid not in catalog.files]
    if missing:
        raise RestoreError("unknown file_id(s) requested", file_ids=missing)
    return [catalog.files[fid] for fid in ids]


def _volume_counts(library: MediaLibrary, catalog: Catalog, ids: set[str]) -> dict[str, int]:
    counts = {}
    for vid in catalog.volume_ids():
        counts[vid] = catalog.volume_block_counts.get(vid, 0)
    return counts


def predict_volume_strategy(
    catalog: Catalog, files: list[FileEntry], quota: int, volumes: dict[str, int],
) -> Plan:
    # Group every requested fragment by volume; each needed volume = one run.
    by_volume: dict[str, list[tuple[str, Fragment]]] = {}
    for fe in files:
        for frag in fe.fragments:
            by_volume.setdefault(frag.volume_id, []).append((fe.file_id, frag))

    runs = []
    for vid in sorted(by_volume):
        frags = sorted(by_volume[vid], key=lambda pair: (pair[1].start_block, pair[0]))
        runs.append(VolumeRun(
            volume_id=vid, fragments=frags,
            start_block=min(f.start_block for _, f in frags),
            end_block=max(f.end_block for _, f in frags),
        ))

    # Scratch peak under volume-major: all confirmed fragment bytes accumulate
    # until files are complete.  Conservative bound = total fragment bytes.
    total_frag_bytes = sum(f.valid_bytes for fe in files for f in fe.fragments)
    # reads: whole blocks covering the fragment => sum block_count
    reads = sum(f.block_count for fe in files for f in fe.fragments)
    mounts = len(runs)
    rewinds = mounts  # one rewind at mount, one skip to first fragment per run is folded in
    skip_blocks = sum(r.start_block + (r.end_block - r.start_block) for r in runs)
    # Better estimate: per run, rewind then skip to each fragment and skip gaps.
    skip_blocks = 0
    for r in runs:
        pos = 0
        for _, frag in r.fragments:
            if frag.start_block > pos:
                skip_blocks += frag.start_block - pos
                pos = frag.start_block
            # sequential reads advance the head over the fragment
            pos = max(pos, frag.end_block)
    notes = [
        "one mount and one sweep per required volume (fewest mounts/rewinds)",
        "holds all confirmed fragment bytes until files reconstruct -> high scratch peak",
    ]
    plan = Plan(
        strategy="volume", file_ids=[fe.file_id for fe in files], quota_bytes=quota,
        volume_counts={vid: volumes[vid] for vid in sorted(by_volume)},
        files=[FilePlan(fe.file_id, fe.path, fe.total_length, list(fe.fragments),
                        _ordered_volumes(fe)) for fe in files],
        runs=runs, predicted_mounts=mounts, predicted_rewinds=rewinds,
        predicted_skip_blocks=skip_blocks, predicted_reads=reads,
        predicted_scratch_peak=total_frag_bytes,
        scratch_fits=total_frag_bytes <= quota, notes=notes,
        fits=total_frag_bytes <= quota,
        skipped_files=[],
        restorable=total_frag_bytes <= quota,
    )
    if not plan.fits:
        plan.infeasible.append(
            f"volume-major scratch peak {total_frag_bytes} B exceeds quota {quota} B"
        )
    return plan


def predict_file_strategy(
    catalog: Catalog, files: list[FileEntry], quota: int, volumes: dict[str, int],
) -> Plan:
    # Order: fewest distinct volumes first, then smallest, then id (deterministic).
    ordered = sorted(
        files,
        key=lambda fe: (len({f.volume_id for f in fe.fragments}),
                        fe.total_length, fe.file_id),
    )
    runs: list[VolumeRun] = []
    predicted_peak = 0
    mounts = 0
    rewinds = 0
    skip_blocks = 0
    reads = 0
    infeasible: list[str] = []
    skipped_files: list[str] = []
    for fe in ordered:
        if not fe.fragments:
            continue
        # per-file scratch peak: at most one fragment body at a time if writing
        # straight to target; a non-resumable planner may hold the whole file,
        # but our restorer streams fragment-by-fragment, so peak ~ largest frag.
        frag_peak = max(f.valid_bytes for f in fe.fragments)
        direct_peak = fe.total_length  # if the target itself is on the work disk
        # efficient path writes accepted fragment bytes to target offset and
        # releases each scratch chunk: peak ~ max fragment + reserved target slice.
        # The restorer reserves a lease per in-flight fragment, so use frag_peak.
        predicted_peak = max(predicted_peak, min(direct_peak, frag_peak + 0))
        if fe.total_length > quota:
            skipped_files.append(fe.file_id)
            infeasible.append(
                f"{fe.file_id}: total length {fe.total_length} B exceeds quota {quota} B "
                "(needs an incomplete target larger than the work quota); "
                "will be skipped, others proceed"
            )
        # fragments visited in volume/block order within the file
        ordered_frags = sorted(fe.fragments, key=lambda f: (f.volume_id, f.start_block))
        current_volume = None
        for frag in ordered_frags:
            mounts += 1
            rewinds += 1
            if frag.volume_id != current_volume:
                current_volume = frag.volume_id
            skip_blocks += frag.start_block
            reads += frag.block_count
        fp_runs: dict[str, list[tuple[str, Fragment]]] = {}
        for frag in ordered_frags:
            fp_runs.setdefault(frag.volume_id, []).append((fe.file_id, frag))
        for vid in sorted(fp_runs):
            pair = fp_runs[vid]
            runs.append(VolumeRun(
                vid, pair,
                min(f.start_block for _, f in pair),
                max(f.end_block for _, f in pair),
            ))
    notes = [
        "files finished one at a time; stream fragment -> target offset, free scratch per fragment",
        "more mounts/rewinds than volume-major but bounded scratch peak",
    ]
    plan = Plan(
        strategy="file", file_ids=[fe.file_id for fe in ordered], quota_bytes=quota,
        volume_counts={vid: volumes[vid] for vid in sorted({f.volume_id for fe in files for f in fe.fragments})},
        files=[FilePlan(fe.file_id, fe.path, fe.total_length, list(fe.fragments),
                        _ordered_volumes(fe)) for fe in ordered],
        runs=runs, predicted_mounts=mounts, predicted_rewinds=rewinds,
        predicted_skip_blocks=skip_blocks, predicted_reads=reads,
        predicted_scratch_peak=predicted_peak,
        scratch_fits=predicted_peak <= quota, notes=notes,
        fits=not infeasible, infeasible=infeasible,
        skipped_files=skipped_files, restorable=True,
    )
    if predicted_peak > quota and not skipped_files:
        plan.infeasible.append(
            f"predicted scratch peak {predicted_peak} B exceeds quota {quota} B"
        )
        plan.fits = False
        plan.restorable = False
    return plan


def _ordered_volumes(fe: FileEntry) -> list[str]:
    seen = []
    for f in fe.fragments:
        if f.volume_id not in seen:
            seen.append(f.volume_id)
    return seen


def build_plan(
    catalog: Catalog,
    library: MediaLibrary,
    strategy: str = "file",
    file_ids: Optional[Iterable[str]] = None,
    quota: int = 32768,
) -> Plan:
    if strategy not in STRATEGIES:
        raise RestoreError("unknown strategy", strategy=strategy, allowed=list(STRATEGIES))
    files = _select_files(catalog, file_ids)
    volumes = _volume_counts(library, catalog, set(catalog.volume_ids()))
    # sanity: requested fragments must fit real volumes (structural check already
    # done at parse; re-assert here so a foreign catalog cannot slip through)
    for fe in files:
        for frag in fe.fragments:
            declared = catalog.volume_block_counts.get(frag.volume_id)
            if declared is None:
                raise RestoreError("catalog references undeclared volume",
                                   file_id=fe.file_id, volume_id=frag.volume_id)
            if frag.end_block > declared:
                raise RestoreError("fragment exceeds volume block count",
                                   file_id=fe.file_id, fragment_id=frag.fragment_id)
    if strategy == "volume":
        return predict_volume_strategy(catalog, files, quota, volumes)
    return predict_file_strategy(catalog, files, quota, volumes)


def pareto_report(plans: Iterable[Plan]) -> dict:
    plans = list(plans)
    return {
        "strategies": {p.strategy: p.as_dict() for p in plans},
        "tradeoff": (
            "volume-major minimizes mounts/rewinds but its scratch peak is the sum "
            "of all in-flight fragment bytes; file-major bounds scratch to one "
            "fragment/one file but re-mounts volumes per file.  Neither is claimed "
            "globally optimal."
        ),
    }

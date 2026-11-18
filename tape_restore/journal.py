"""Append-only run journal + atomic checkpoints.

Only *confirmed* facts are journalled: a fragment record is written after its
bytes are on the work disk and its digest matched the catalog.  Resume replays
these records and re-hashes the staged bytes before trusting them; it never
skips a fragment merely because a log line exists.

Device positions are captured at every checkpoint so a resume can re-mount,
rewind and skip back to the recorded block and assert the real head position.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Iterable, Optional


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_json(path: Path, obj: object) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)
    _fsync_dir(path.parent)


def new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]


class Journal:
    def __init__(self, state_dir: Path, run_id: str) -> None:
        self.state_dir = Path(state_dir)
        self.run_id = run_id
        self.path = self.state_dir / "journal.jsonl"
        self.checkpoint_path = self.state_dir / "checkpoint.json"
        self.meta_path = self.state_dir / "run.json"
        self.seq = 0
        self._fh = None

    # -- lifecycle -------------------------------------------------------
    def open_for_append(self) -> None:
        if self._fh is not None:
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")

    def close(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
            self._fh = None

    def __del__(self) -> None:  # pragma: no cover - safety net
        try:
            self.close()
        except Exception:
            pass

    def append(self, event_type: str, **fields: object) -> dict:
        self.seq += 1
        entry = {"seq": self.seq, "run_id": self.run_id, "type": event_type,
                 "ts": round(time.time(), 6)}
        entry.update(fields)
        if self._fh is None:
            self.open_for_append()
        self._fh.write(json.dumps(entry, sort_keys=True) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        return entry

    def write_meta(self, obj: dict) -> None:
        atomic_write_json(self.meta_path, obj)

    def read_meta(self) -> Optional[dict]:
        if not self.meta_path.exists():
            return None
        return json.loads(self.meta_path.read_text())

    def write_checkpoint(self, obj: dict) -> None:
        atomic_write_json(self.checkpoint_path, obj)

    def read_checkpoint(self) -> Optional[dict]:
        if not self.checkpoint_path.exists():
            return None
        return json.loads(self.checkpoint_path.read_text())

    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    # a torn final line is ignored; only complete records count
                    break
        return out

    # -- replay ----------------------------------------------------------
    def replay(self) -> dict:
        """Fold journal events into the resume-relevant state."""
        state = {
            "confirmed": {},        # fid -> {frag_id: record}
            "published": {},        # fid -> record
            "failed": {},           # fid -> record
            "scan": {},             # vid -> next_block
            "interrupted": None,
            "last_event_seq": 0,
            "events": 0,
        }
        for ev in self.events():
            state["last_event_seq"] = ev.get("seq", state["last_event_seq"])
            state["events"] += 1
            t = ev.get("type")
            if t == "fragment_confirmed":
                fid = ev["file_id"]
                state["confirmed"].setdefault(fid, {})[ev["fragment_id"]] = ev
                state["published"].pop(fid, None)
                state["failed"].pop(fid, None)
            elif t == "file_published":
                state["published"][ev["file_id"]] = ev
                state["confirmed"].pop(ev["file_id"], None)
                state["failed"].pop(ev["file_id"], None)
            elif t == "file_failed":
                state["failed"][ev["file_id"]] = ev
                state["confirmed"].pop(ev["file_id"], None)
            elif t == "file_invalidated":
                fid = ev["file_id"]
                state["confirmed"].pop(fid, None)
                state["published"].pop(fid, None)
                state["failed"].pop(fid, None)
            elif t == "scan_progress":
                state["scan"][ev["volume_id"]] = ev["next_block"]
            elif t == "interrupted":
                state["interrupted"] = ev
            elif t == "run_resumed":
                state["interrupted"] = None
            elif t == "resume_restart":
                state["confirmed"].clear()
                state["scan"].clear()
                state["failed"].clear()
        state["self"] = self
        return state

    def run_summary(self) -> dict:
        events = self.events()
        counts: dict[str, int] = {}
        for ev in events:
            counts[ev["type"]] = counts.get(ev["type"], 0) + 1
        return {
            "run_id": self.run_id,
            "events": len(events),
            "event_counts": counts,
            "checkpoint_present": self.checkpoint_path.exists(),
        }

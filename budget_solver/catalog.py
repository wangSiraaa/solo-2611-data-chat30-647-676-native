"""模型目录：创建、修订、固定快照与可重开的文件持久化。

存储布局::

    root/
      catalog.json                 # 全局序列、模型元数据（永久 id、HEAD 指针）
      models/<model_id>/
        revs/<rev_id>.json         # 每次修订的完整规范化公式（不可变）
        snaps/<snap_id>.json       # 固定快照（完整公式 + 外部输入版本钉选）

关键约束：

* ``model_id`` 是永久身份，``name`` 只是可改的显示名，改名不动 id、不换指针。
* 任何引用非法（目标模型/格不存在、修订后被别的模型依赖的格消失）都会让整次
  修订在落盘前被拒绝，目录里不会出现悬挂引用。
* 快照不可变；快照内每个跨模型引用都钉住目标模型当时的快照 id。
"""

import os

from .errors import NotFoundError, ReferenceRejectedError, ValidationError
from .rationals import parse_rational, qstr
from .util import (
    content_hash,
    ensure_dir,
    read_json,
    utc_now_iso,
    write_json_atomic,
)

_ALLOWED_TOP_KEYS = {
    "model_id",
    "name",
    "description",
    "cells",
    "outputs",
}
_ALLOWED_CELL_KEYS = {"cell_id", "kind", "value", "constant", "terms"}
_ALLOWED_TERM_KEYS = {"coef", "model_id", "cell_id"}


class Catalog:
    """文件支持的模型目录；构造即重开，元数据一次性读入内存。"""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self._meta_path = os.path.join(self.root, "catalog.json")
        ensure_dir(self.root)
        if os.path.exists(self._meta_path):
            self._meta = read_json(self._meta_path)
        else:
            self._meta = {"seq": 0, "models": {}}
            self._flush_meta()

    # ------------------------------------------------------------------ 基础
    def _next_id(self, prefix):
        self._meta["seq"] += 1
        return f"{prefix}{self._meta['seq']}"

    def _flush_meta(self):
        write_json_atomic(self._meta_path, self._meta)

    def _model_dir(self, model_id):
        return os.path.join(self.root, "models", model_id)

    def _rev_path(self, model_id, rev_id):
        return os.path.join(self._model_dir(model_id), "revs", f"{rev_id}.json")

    def _snap_path(self, model_id, snap_id):
        return os.path.join(self._model_dir(model_id), "snaps", f"{snap_id}.json")

    @staticmethod
    def _ref_key(model_id, cell_id):
        return f"{model_id}::{cell_id}"

    # ------------------------------------------------------------- 校验/归一
    def _normalize_cell(self, cell_id, raw):
        if not isinstance(raw, dict):
            raise ValidationError(f"格 {cell_id} 必须是对象", [f"cell={cell_id}"])
        unknown = set(raw) - _ALLOWED_CELL_KEYS
        if unknown:
            raise ValidationError(
                f"格 {cell_id} 含未知字段 {sorted(unknown)}", [f"cell={cell_id}"]
            )
        kind = raw.get("kind")
        if kind not in ("input", "formula"):
            raise ValidationError(
                f"格 {cell_id} 的 kind 必须是 input 或 formula", [f"cell={cell_id}"]
            )
        norm = {"cell_id": cell_id, "kind": kind}
        if kind == "input":
            if "value" not in raw:
                raise ValidationError(
                    f"输入格 {cell_id} 必须提供 value", [f"cell={cell_id}"]
                )
            if raw.get("terms"):
                raise ValidationError(
                    f"输入格 {cell_id} 不允许带 terms", [f"cell={cell_id}"]
                )
            norm["value"] = qstr(parse_rational(raw["value"], field=f"cell {cell_id}.value"))
            return norm
        # formula
        constant = parse_rational(
            raw.get("constant", 0), field=f"cell {cell_id}.constant"
        )
        norm["constant"] = qstr(constant)
        merged = {}
        for index, term in enumerate(raw.get("terms", [])):
            if not isinstance(term, dict):
                raise ValidationError(
                    f"格 {cell_id} 的第 {index} 项不是对象", [f"cell={cell_id}"]
                )
            unknown_t = set(term) - _ALLOWED_TERM_KEYS
            if unknown_t:
                raise ValidationError(
                    f"格 {cell_id} 第 {index} 项含未知字段 {sorted(unknown_t)}",
                    [f"cell={cell_id}"],
                )
            if "cell_id" not in term:
                raise ValidationError(
                    f"格 {cell_id} 第 {index} 项缺少 cell_id", [f"cell={cell_id}"]
                )
            # 形状本身就禁止“两个未知格相乘”：一项只允许 有理系数 × 单个格引用
            coef = parse_rational(
                term.get("coef", 1), field=f"cell {cell_id}.terms[{index}].coef"
            )
            ref_model = term.get("model_id")  # None 在归一时补上本模型 id
            ref_cell = term["cell_id"]
            if not isinstance(ref_cell, str) or not ref_cell:
                raise ValidationError(
                    f"格 {cell_id} 第 {index} 项 cell_id 非法", [f"cell={cell_id}"]
                )
            key = (ref_model, ref_cell)
            merged[key] = merged.get(key, 0) + coef
        terms = []
        for (ref_model, ref_cell), coef in sorted(
            merged.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])
        ):
            if coef == 0:
                continue
            terms.append(
                {
                    "coef": qstr(coef),
                    "model_id": ref_model,  # None 先占位，随后统一补全
                    "cell_id": ref_cell,
                }
            )
        norm["terms"] = terms
        return norm

    def _validate_payload(self, payload):
        if not isinstance(payload, dict):
            raise ValidationError("模型负载必须是 JSON 对象", ["payload"])
        unknown = set(payload) - _ALLOWED_TOP_KEYS
        if unknown:
            raise ValidationError(f"模型含未知顶层字段 {sorted(unknown)}", [])
        model_id = payload.get("model_id")
        name = payload.get("name")
        if not isinstance(model_id, str) or not model_id:
            raise ValidationError("model_id 必须是非空字符串", ["model_id"])
        if not isinstance(name, str) or not name:
            raise ValidationError("name 必须是非空字符串", ["name"])
        cells_raw = payload.get("cells")
        if not isinstance(cells_raw, dict) or not cells_raw:
            raise ValidationError("cells 必须是非空对象", ["cells"])
        normalized = {}
        for cell_id, raw in cells_raw.items():
            if not isinstance(cell_id, str) or not cell_id:
                raise ValidationError("cell_id 必须是非空字符串", [f"cell={cell_id}"])
            normalized[cell_id] = self._normalize_cell(cell_id, raw)
        # 统一补全本模型引用，并核对全部引用目标存在
        ref_errors = []
        external = {}
        for cell_id, cell in normalized.items():
            if cell["kind"] != "formula":
                continue
            for term in cell["terms"]:
                target_model = term["model_id"] or model_id
                term["model_id"] = target_model
                if target_model == model_id:
                    if term["cell_id"] not in normalized:
                        ref_errors.append(
                            f"{cell_id} 引用了本模型不存在的格 {term['cell_id']}"
                        )
                    continue
                target_meta = self._meta["models"].get(target_model)
                if target_meta is None:
                    ref_errors.append(
                        f"{cell_id} 引用了不存在的模型 {target_model}"
                    )
                    continue
                head = self.get_head_revision(target_model)
                if term["cell_id"] not in head["cells"]:
                    ref_errors.append(
                        f"{cell_id} 引用模型 {target_model} 中不存在的格 {term['cell_id']}"
                    )
                    continue
                external.setdefault(
                    target_model,
                    {"head_rev": target_meta["head_rev"]},
                )
        outputs = payload.get("outputs", [])
        if not isinstance(outputs, list):
            raise ValidationError("outputs 必须是数组", ["outputs"])
        outputs = list(dict.fromkeys(outputs))  # 去重保序
        for out in outputs:
            if not isinstance(out, str) or out not in normalized:
                raise ValidationError(
                    f"输出格 {out!r} 不存在于 cells", [f"output={out}"]
                )
        if ref_errors:
            raise ReferenceRejectedError(
                "非法引用，整次修订拒绝", sorted(ref_errors)
            )
        cell_hashes = {cid: content_hash(cell) for cid, cell in normalized.items()}
        return {
            "model_id": model_id,
            "name": name,
            "description": payload.get("description", ""),
            "cells": normalized,
            "outputs": outputs,
            "external_refs": external,
            "cell_hashes": cell_hashes,
        }

    def _inbound_check(self, model_id, new_cell_ids):
        """已有模型被修订：其他模型 HEAD 中指向它的格不能消失。"""
        errors = []
        for other_id, other_meta in self._meta["models"].items():
            if other_id == model_id:
                continue
            head = self.get_head_revision(other_id)
            for cell in head["cells"].values():
                if cell["kind"] != "formula":
                    continue
                for term in cell["terms"]:
                    if term["model_id"] == model_id and term["cell_id"] not in new_cell_ids:
                        errors.append(
                            f"模型 {other_id} 的格 {cell['cell_id']} 仍依赖 "
                            f"{model_id}::{term['cell_id']}，修订将使其悬挂"
                        )
        if errors:
            raise ReferenceRejectedError("修订破坏入向引用，整次拒绝", sorted(errors))

    # --------------------------------------------------------------- put-model
    def put_model(self, payload):
        """创建或修订一个模型；非法时抛异常且不写任何文件。"""
        norm = self._validate_payload(payload)
        model_id = norm["model_id"]
        existing = self._meta["models"].get(model_id)
        if existing is not None:
            self._inbound_check(model_id, norm["cells"].keys())
            prev_rev = existing["head_rev"]
            prev_record = self.get_head_revision(model_id)
            revision_number = prev_record["revision_number"] + 1
        else:
            prev_rev = None
            revision_number = 1
        rev_id = self._next_id("r")
        record = {
            "rev_id": rev_id,
            "model_id": model_id,
            "revision_number": revision_number,
            "prev_rev_id": prev_rev,
            "name": norm["name"],
            "description": norm["description"],
            "cells": norm["cells"],
            "outputs": norm["outputs"],
            "external_refs": norm["external_refs"],
            "cell_hashes": norm["cell_hashes"],
            "created_at": utc_now_iso(),
        }
        record["content_hash"] = content_hash(
            {k: v for k, v in record.items() if k != "content_hash"}
        )
        # 先写修订文件，再原子切换 HEAD 指针
        write_json_atomic(self._rev_path(model_id, rev_id), record)
        if existing is None:
            self._meta["models"][model_id] = {
                "model_id": model_id,
                "name": norm["name"],
                "description": norm["description"],
                "created_at": record["created_at"],
                "head_rev": rev_id,
                "head_snapshot": None,
                "updated_at": record["created_at"],
            }
        else:
            existing.update(
                head_rev=rev_id,
                head_snapshot=None,  # 新修订使旧 HEAD 快照不再是 HEAD（旧快照文件保留）
                name=norm["name"],
                description=norm["description"],
                updated_at=record["created_at"],
            )
        self._flush_meta()
        return {
            "model_id": model_id,
            "rev_id": rev_id,
            "revision_number": revision_number,
            "name": norm["name"],
            "outputs": norm["outputs"],
            "external_refs": norm["external_refs"],
            "content_hash": record["content_hash"],
            "created": existing is None,
        }

    def _external_pins_are_current(self, snap):
        """已有快照钉住的外部快照是否仍是各外部模型的 HEAD 快照。"""
        for ext_model, pin in snap["external_refs"].items():
            ext_meta = self._meta["models"].get(ext_model)
            if ext_meta is None or ext_meta.get("head_snapshot") != pin["snap_id"]:
                return False
        return True

    # ----------------------------------------------------------------- 快照
    def take_snapshot(self, model_id, label=None):
        """钉住 HEAD 修订；递归钉住所有跨模型依赖的 HEAD 快照。"""
        meta = self._require_model(model_id)
        head_rev = meta["head_rev"]
        if meta.get("head_snapshot"):
            snap = self.get_snapshot(meta["head_snapshot"])
            if snap["rev_id"] == head_rev and self._external_pins_are_current(snap):
                return snap  # 幂等：自身未修订且外部输入版本均未前移才复用
        head = self.get_head_revision(model_id)
        pinned = {}
        for ext_model in sorted(head["external_refs"]):
            # 外部输入版本以固定时刻各外部模型的 HEAD 为准：即便本模型自身未
            # 修订，外部模型改名/更正后重新固定也会钉住新 HEAD（递归固定）。
            ext_snap = self.take_snapshot(ext_model)
            ext_head_rev = self._meta["models"][ext_model]["head_rev"]
            pinned[ext_model] = {
                "rev_id": ext_head_rev,
                "snap_id": ext_snap["snap_id"],
            }
        snap_id = self._next_id("s")
        snapshot = {
            "snap_id": snap_id,
            "model_id": model_id,
            "name": head["name"],
            "label": label,
            "rev_id": head_rev,
            "revision_number": head["revision_number"],
            "cells": head["cells"],
            "outputs": head["outputs"],
            "cell_hashes": head["cell_hashes"],
            "external_refs": pinned,
            "created_at": utc_now_iso(),
        }
        snapshot["content_hash"] = content_hash(
            {k: v for k, v in snapshot.items() if k != "content_hash"}
        )
        write_json_atomic(self._snap_path(model_id, snap_id), snapshot)
        meta["head_snapshot"] = snap_id
        self._flush_meta()
        return snapshot

    # ----------------------------------------------------------------- 查询
    def _require_model(self, model_id):
        meta = self._meta["models"].get(model_id)
        if meta is None:
            raise NotFoundError(f"模型 {model_id} 不存在")
        return meta

    def list_models(self):
        return [
            {
                "model_id": mid,
                "name": m["name"],
                "head_rev": m["head_rev"],
                "head_snapshot": m.get("head_snapshot"),
                "revision_count": self._revision_count(mid),
            }
            for mid, m in sorted(self._meta["models"].items())
        ]

    def _revision_count(self, model_id):
        rev_dir = os.path.join(self._model_dir(model_id), "revs")
        if not os.path.isdir(rev_dir):
            return 0
        return len([f for f in os.listdir(rev_dir) if f.endswith(".json")])

    def get_model_meta(self, model_id):
        return dict(self._require_model(model_id))

    def get_head_revision(self, model_id):
        meta = self._require_model(model_id)
        return self.get_revision(model_id, meta["head_rev"])

    def get_revision(self, model_id, rev_id):
        path = self._rev_path(model_id, rev_id)
        if not os.path.exists(path):
            raise NotFoundError(f"修订 {rev_id} 不存在于模型 {model_id}")
        return read_json(path)

    def list_revisions(self, model_id):
        self._require_model(model_id)
        rev_dir = os.path.join(self._model_dir(model_id), "revs")
        if not os.path.isdir(rev_dir):
            return []
        ids = sorted(
            (f[:-5] for f in os.listdir(rev_dir) if f.endswith(".json")),
            key=lambda rid: int(rid[1:]),
        )
        return [self.get_revision(model_id, rid) for rid in ids]

    def get_snapshot(self, snap_id):
        for model_id in self._meta["models"]:
            path = self._snap_path(model_id, snap_id)
            if os.path.exists(path):
                return read_json(path)
        raise NotFoundError(f"快照 {snap_id} 不存在")

    def list_snapshots(self, model_id):
        self._require_model(model_id)
        snap_dir = os.path.join(self._model_dir(model_id), "snaps")
        if not os.path.isdir(snap_dir):
            return []
        ids = sorted(
            (f[:-5] for f in os.listdir(snap_dir) if f.endswith(".json")),
            key=lambda sid: int(sid[1:]),
        )
        return [self.get_snapshot(sid) for sid in ids]

    def catalog_snapshot_descriptor(self):
        """报告中固定的“目录快照”描述：HEAD 指针与全部快照清单。"""
        models = {}
        for mid, meta in sorted(self._meta["models"].items()):
            models[mid] = {
                "name": meta["name"],
                "head_rev": meta["head_rev"],
                "head_snapshot": meta.get("head_snapshot"),
                "snapshots": [s["snap_id"] for s in self.list_snapshots(mid)],
            }
        return {"seq": self._meta["seq"], "models": models}

    # --------------------------------------------------------- 供编译器使用
    def load_snapshot_cells(self, snap_id):
        snap = self.get_snapshot(snap_id)
        return snap["model_id"], snap

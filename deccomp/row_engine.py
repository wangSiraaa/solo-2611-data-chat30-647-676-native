"""行式执行器：逐行递归求值，天然短路、无分支物化。

作为列式执行器的语义基准（reference）。CASE 未命中分支根本不会被
递归访问，因此未选分支中的除零/溢出不可能产生数据错误。
"""

from .decimals import parse_decimal_text
from .runtime import eval_scalar_node


class RowEngine:
    name = "row"

    def __init__(self, plan, trace=False):
        self.plan = plan
        self.nodes = plan.nodes
        # trace 开启时：{row: {nid: (Decimal|bool|None, error_code|None)}}，
        # 只记录实际求值到的节点（未选 CASE 分支不会出现）。
        self.trace_enabled = trace
        self.traces = {}

    def execute(self, batch):
        results, errors = [], {}
        for i in range(batch.nrows):
            if self.trace_enabled:
                self.traces[i] = {}
            if i in batch.input_errors:
                results.append(None)
                errors[i] = batch.input_errors[i]
                continue
            sink = []
            value = self._eval(self.plan.root_id, batch, i, sink)
            results.append(None if sink else value)
            if sink:
                errors[i] = sink[0]
        return results, errors

    def _record(self, nid, row_idx, value, own_error=None):
        if self.trace_enabled:
            code = own_error.code if own_error else None
            self.traces[row_idx][nid] = (value, code)

    def _eval(self, nid, batch, row_idx, sink):
        node = self.nodes[nid]
        op = node.op

        if op == "column":
            v = batch.columns[node.name][row_idx]
            self._record(nid, row_idx, v)
            return v

        if op == "const":
            v = (None if node.value is None
                 else parse_decimal_text(node.value))
            self._record(nid, row_idx, v)
            return v

        if op == "case":
            for cond_id, then_id in node.when_children:
                cond = self._eval(cond_id, batch, row_idx, sink)
                if sink:
                    return None
                if cond is True:          # False 与 NULL 均跳过，短路返回
                    v = self._eval(then_id, batch, row_idx, sink)
                    self._record(nid, row_idx, v)
                    return v
            if node.else_child is not None:
                v = self._eval(node.else_child, batch, row_idx, sink)
                self._record(nid, row_idx, v)
                return v
            self._record(nid, row_idx, None)
            return None

        child_values = []
        for cid in node.children:
            child_values.append(self._eval(cid, batch, row_idx, sink))
            if sink:
                # 左子树出错：右兄弟与本节点均不可达（与列式 walk 同构）。
                return None
        before = len(sink)
        v = eval_scalar_node(node, child_values, sink)
        own_error = sink[before] if len(sink) > before else None
        self._record(nid, row_idx, v, own_error)
        return v

"""命令行入口：compile / run / explain / verify。

示例
----
python -m deccomp.cli compile --ast expr.json --schema schema.json --out plan.json
python -m deccomp.cli run --plan plan.json --batch batch.jsonl --out out/ --engine vector
python -m deccomp.cli explain --plan plan.json
python -m deccomp.cli verify
"""

import argparse
import json
import sys

from . import api
from .errors import CompileError


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="deccomp")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_compile = sub.add_parser("compile", help="编译 AST 为可保存计划")
    p_compile.add_argument("--ast", required=True)
    p_compile.add_argument("--schema", required=True)
    p_compile.add_argument("--out", required=True)

    p_run = sub.add_parser("run", help="读取保存计划执行批次")
    p_run.add_argument("--plan", required=True)
    p_run.add_argument("--batch", required=True, nargs="+",
                       help="一个或多个 JSONL 批次文件")
    p_run.add_argument("--out", required=True)
    p_run.add_argument("--engine", choices=["row", "vector"],
                       default="vector")

    p_explain = sub.add_parser("explain", help="解释已保存计划")
    p_explain.add_argument("--plan", required=True)

    sub.add_parser("verify", help="运行内置测试套件")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "compile":
            ast = _load_json(args.ast)
            schema = _load_json(args.schema)
            plan = api.compile(ast, schema, plan_path=args.out)
            print(f"编译成功：{len(plan.nodes)} 个节点，"
                  f"结果类型 {plan.result_type}，计划已保存 {args.out}")
        elif args.cmd == "run":
            reports = api.run(args.plan, args.batch, args.out,
                              engine=args.engine)
            for rep in reports:
                print(f"批次 {rep['batch_index']} ({args.engine}): "
                      f"{rep['n_ok']} 行成功 / {rep['n_errors']} 行数据错误"
                      f"；回读核对={'通过' if rep['readback_ok'] else '失败'}")
                print(f"  Parquet: {rep['parquet']}")
                print(f"  错误清单: {rep['errors_file']}")
        elif args.cmd == "explain":
            print(api.explain(args.plan))
        elif args.cmd == "verify":
            ok = api.verify()
            sys.exit(0 if ok else 1)
    except CompileError as exc:
        print(f"编译/计划错误: {exc} (node={exc.node_id})", file=sys.stderr)
        sys.exit(2)
    return 0


if __name__ == "__main__":
    main()

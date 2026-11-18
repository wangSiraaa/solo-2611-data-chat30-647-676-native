# ignarc — 独立 .ignore 规则解释服务与可追溯目录归档器

纯 Python 3.11 标准库实现，零第三方依赖。只处理调用方显式指定的采集根
（测试与 demo 均使用自行生成的合成目录），不扫描任何其他用户文件。

## 目录结构

```
ignarc/
  paths.py        路径校验（禁止绝对路径/NUL/.. /空分量）与类型常量
  patterns.py     ? * ** glob 编译（其他字符一律普通字符，无转义扩展）
  rules.py        .ignore 解析与 Rule（行号、作用域、锚定、仅目录）
  engine.py       RuleEngine.check(path, kind)：规则链、祖先剪枝、缓存与热更
  fs.py           AuditedFS：根约束 + 全量审计 + 只取链接文本不跟随
  traversal.py    带剪枝的 DFS 选择（plan/collect 共用同一条代码路径）
  planner.py      plan：只读元数据与规则，审计自检无 body-read
  archiver.py     collect / inspect_package / verify_package
  synthgen.py     确定性合成采集树（5000+ 项、深度 12、100 份规则、20MiB）
  cli.py          plan / collect / inspect / verify 四个子命令
tests/
  naive_interpreter.py  不 import 生产代码的第二套朴素解释器
  test_patterns.py      glob 与解析单元测试
  test_semantics.py     规则语义单元测试（不接触文件系统）
  test_edge.py          空树/无规则树/剪枝子树内规则
  test_crosscheck.py    生产引擎 vs 朴素解释器逐路径交叉核对
  test_archive.py       归档端到端：审计、逐字节、链接安全、热更、防篡改
  test_scale.py         规模指标与性能预算
demo.py                 一键端到端自证
```

## 规则语义（实现约定）

每个目录可含一份 `.ignore`，规则按文件行序生效；空行（含纯空白行）与
`#` 起始行不产生规则；行首 `!` 表示重新包含；行尾 `/` 表示仅匹配目录。

* **无斜杠模式**：匹配规则所在目录及其后代中的任意单个路径分量
  （规则不约束所在目录本身）。
* **含斜杠模式**：锚定到规则所在目录，匹配相对该目录的完整相对路径；
  前导 `/` 仅用于显式表达锚定。
* `?` = 一个非斜杠字符；`*` = 零个或多个非斜杠字符；
  `**` 仅作为完整路径分量时有效，匹配零个或多个目录分量。
  裸 `**` 匹配任意相对路径；`a/**` 匹配 `a` 之下内容但不匹配 `a` 自身。
* 其余字符（含 `( ) [ ] + . \` 等正则元字符）全部按 Unicode 码点普通比较，
  不存在反斜杠转义；不做大小写折叠与规范化。
* 进入目录时继承全部祖先规则，再按从浅到深追加本地规则；同一目标由
  **最后命中**的规则决定，初始默认包含。
* 父目录最终被排除时不进入其子树（连 `scandir` 都不发生），子树内规则
  无法隔空复活任何文件；重新包含文件要求其全部父目录可进入。
* 符号链接是独立 kind：“仅目录”规则不匹配链接；链接只保存链接文本，
  绝不跟随、不 `open`。

`RuleEngine.check(path, kind)` 返回 `Decision`，包含包含结论、原因以及
**真正起作用的规则来源行**（`文件:行号` + 原始规则行）；它只依赖编译后的
规则树，不读取任何业务文件正文。

## 可追溯归档

`collect` 每次都重新执行选择（不接受外部旧选择缓存），生成
`.pack.zip` 自包含包与旁车 manifest：

* `payload/`：入选业务文件与链接文本（链接以 Unix S_IFLNK 条目保存）；
* `rules/`：全部 `.ignore` 原文（含位于剪枝子树内的，标注
  `inside_pruned_subtree`）；控制输入单列，不会当业务文件导出；
* `MANIFEST.json`：逐文件相对路径、长度、SHA256、规则依据，以及全部
  排除决定、剪枝目录、规则摘要 `rule_digest`；
* `AUDIT.json`：全部真实 I/O 审计，可证明被排除文件没有任何 `body-read`。

规则修订后 `rule_digest` 改变，产出全新包；旧包保留且 `inspect` 仍可离线
查来源。用新规则 `verify` 旧包会报告选择集合漂移而不是静默通过。

## 使用

```bash
python3 demo.py                       # 生成合成树并跑完整自证
python3 -m ignarc.cli plan    --root <合成根> --output plan.json
python3 -m ignarc.cli collect --root <合成根> --output-dir out/
python3 -m ignarc.cli inspect <包.pack.zip>
python3 -m ignarc.cli verify  <包.pack.zip> --root <合成根> --extract-root unpack/
python3 -m unittest discover -s tests # 59 个测试
```

## 验证证据（demo 实测）

* 合成树：约 1.4 万个条目、100 份 `.ignore`（每份 20–100 行）、正文
  ≥20 MiB、深度 12 的目录链、7 个符号链接（含根外/成环/悬空/中文）。
* plan 审计仅含 `metadata`，无任何 `body-read`；剪枝目录不出现在扫描日志。
* collect 每个入选文件恰好 `open` 一次；排除文件与正文读取集合交集为空。
* 解包后逐字节对照全部入选文件（不是只比文件数），SHA256 全部一致。
* 生产引擎与独立朴素解释器对合成树每个路径的结论与来源行一致。
* 篡改包内 payload 后 verify 必失败；规则热更后旧包 verify 报告漂移。

# heximg — 有限 Intel HEX 映像目录与分区差量核对仓

纯 Python 3.11 标准库实现，**只分析合成的 Intel HEX 文本文件，不连接、不写入任何硬件**。

针对现有工具的三类问题给出明确语义：

1. **先切换扩展地址再写数据**：02/04 记录按各自定义形成后续 data 的绝对地址，
   切换时**替换**前一基址而不是累加；基址作用域严格限定在单个文件内。
2. **16 位偏移不是绝对地址**：绝对地址 = 当前基址 + 记录内 16 位偏移。
3. **“未提供的字节”不是 `0x00`**：未定义地址是显式空洞，与值恰好为 `0x00`
   的有效定义严格区分。

## 目录结构

```
heximg/
├── heximg/
│   ├── records.py    # HEX 文本解析（行号 + 原字节范围，严格校验，不跳坏行）
│   ├── image.py      # 稀疏映像：段区间 + 来源游程、overlay 层次、范围读取
│   ├── catalog.py    # 接入/overlay 清单（manifest）与产品构建
│   ├── partition.py  # 不可变分区目录（修订号、禁止重叠、填充值策略）
│   ├── delta.py      # 分区差量分析（只消费映像公开 API）
│   ├── repo.py       # 产品/报告本地持久化仓（稀疏二进制 + 整体校验）
│   ├── synth.py      # 合成 HEX 生成（测试与样例共用）
│   ├── cli.py        # 命令行
│   └── __main__.py
├── samples/          # 真实可解析的 HEX 样例（含故意制造的坏文件）
├── tests/            # 66 个 unittest 用例（含逐字节参考模型与规模用例）
└── pyproject.toml
```

接入/overlay（`catalog.py` + `image.py`）与差量分析（`delta.py`）是两个实质模块；
`delta.py` 不导入 `records.py`，并由静态 + 运行时双重测试保证分析器只能消费
`read_spans / byte_at / sources_at / defined_spans` 等公开 API，无法重新解析
文件绕过 overlay 冲突结论。

## 解析规则（records.py）

* 支持记录：`00` data、`01` EOF、`02` extended-segment-address、
  `04` extended-linear-address；其余类型（如 `03`/`05`）明确报
  `UnsupportedRecordType`。
* 每行逐字节校验：起始冒号、十六进制字符集、字符数为偶数、字节数字段与实际
  数据一致、8 位二补校验和（全记录字节和模 256 必须为 0）。
* 任何坏行都抛出带 **1 起始行号**与**物理文件原字节范围（半开区间，兼容
  `\n` 与 `\r\n`）**的异常，绝不静默跳过。
* EOF 只允许出现一次；EOF 之后除空行（含纯空白行）外不得再有记录。
* 地址语义：
  * 02：`base = segment << 4`；04：`base = upper << 16`；切换即替换，不累加。
  * data：`absolute = base + addr16`；结果超出 `0..0xFFFFFFFF` 直接拒绝。
  * 每次 `parse_hex` 从零基址开始，文件间作用域不泄漏。

## 映像与 overlay（image.py / catalog.py）

* 稀疏存储：有序、互不相交、相邻即合并的段；**绝不为 4 GiB 空间分配全量数组**。
  段内用“来源游程”记录每个字节的出处，空洞不占数据空间。
* 同一地址多次提供：
  * **同值** → 归并，保留全部来源（去重、稳定顺序）；
  * **异值且同层** → 收集全部冲突点并拒绝整份产品（`OverlayConflict`）；
  * **异值且跨层** → 由显式 overlay 最高层胜出，只看层次号，与到达顺序无关；
    同值跨层同样合并两层来源。
* 产品冻结后不可变；公开读取 API：
  * `read_spans(start, length)`：逐段返回定义/空洞，单次请求上限 **1 MiB**，
    越过 32 位上界的窗口按空洞返回且总长度等于请求长度；
  * `byte_at`（空洞返回 `None`，绝不返回 0）、`is_defined`、
    `sources_at`（逐字节来源回查）、`defined_spans`、`digest`。

## 分区与差量（partition.py / delta.py）

* 分区目录不可变，每区有 `start / length / fill`；分区重叠、长度非法、
  越界、填充值越界都在构造时拒绝。
* 目录修订通过 `revised(..., revision=...)` 产生新修订号与新摘要；
  报告内嵌目录快照与摘要，**旧报告永久绑定旧目录修订**。
* 两种互斥比较视图，报告显式记录视图，跨视图混用时 `assert_same_view` 报错：
  * `unknown`：空洞保持未知；双空洞只进空洞统计，不产生差量段；
  * `materialized`：空洞按所属分区 `fill` 物化后比较；分区无填充策略时拒绝
    该视图。双空洞物化为同值 → 不变段，并统计物化读取字节数。
* 每分区输出新增定义（added）、撤去定义（removed）、值变化（changed）、
  不变（unchanged）四类**最大连续段**；定义状态变化（hole↔defined）始终归
  added/removed，即使某侧有效值恰好等于填充值也不会被并入不变。
* 空洞变化单独统计：`both_hole / hole_to_defined / defined_to_hole /
  net_hole_change`，与四分类字节计数互不重复。
* 扫描按 ≤1 MiB 分页（内部 256 KiB/页），通过公开 `read_spans` 取数，
  分区扫描成本只与分区窗口和已定义段数相关。

## 持久化（repo.py）

* 产品为紧凑二进制：仅编码已定义段 + 来源表 + 来源游程，存储量随**定义字节数**
  增长，与地址跨度无关；文件末尾附整体 SHA-256，任何字节损坏加载时即报错。
* 重开产品后逐字节值与全部来源仍可回查；差量报告为 JSON，内嵌映像摘要、
  视图、目录修订号、目录摘要与目录快照。

## 命令行

```bash
python -m heximg validate <hex 文件>...           # 严格校验（坏文件给出行号/范围）
python -m heximg build --manifest m.json --repo ./store
python -m heximg build --file a.hex --layer 0 --file b.hex --layer 1 \
                 --name app --repo ./store
python -m heximg info --repo ./store --name app
python -m heximg read  --repo ./store --name app --start 0x100 --length 0x40
python -m heximg sources --repo ./store --name app --address 0x101
python -m heximg partitions --dir partitions.json
python -m heximg diff --repo ./store --a base --b app \
                 --dir partitions.json --view unknown|materialized --id case1
python -m heximg report --repo ./store --id case1
```

## 快速上手

```bash
cd hexrepo
python3 -m unittest discover -s tests -v   # 全部测试（无需第三方依赖）
python3 -m heximg validate samples/*.hex
```

## 测试与规模

`tests/` 共 66 个用例，覆盖题目要求的全部情形：

* 两类扩展地址切换（02/04）、跨 64 KiB 单记录、同值重复、同层冲突、
  显式覆盖、`0x00` 与空洞、分区边界；
* `helpers.py` 提供独立逐字节 dict 参考模型，对 30+ 随机小地址域场景
  全量核对字节值、来源与差量分类（参考模型不使用库内任何稀疏结构）；
* 规模用例：**100 个文件、20 MiB 定义、100 个分区**，验证构建、落盘体积
  （< 26 MiB，含全部来源开销）、重开回查与差量扫描；
* 高地址稀疏用例：仅 1 KiB 定义贴近 `0xFFFFFFFF`，落盘 < 8 KiB，
  低地址巨大区域保持空洞；
* API 隔离：AST 静态检查 + 公开白名单代理运行时检查；
* 持久化：重开摘要一致、来源可回查、损坏检测、报告绑定目录修订。

# 稳定条目身份的 ZIP 档案服务与表数据入仓器

Python 3.11 实现。档案服务与关系入仓是两个独立源码模块：

| 模块 | 职责 |
| --- | --- |
| `archive_service/zip_service.py` | 严格解析/校验真实 ZIP；稳定条目身份；`list`、按 `entry_id` 读正文、`source-range` |
| `archive_service/delivery.py` | 由显式 `entry_id → 相对路径` 清单做安全交付（拒绝绝对路径、`..`、空段、冲突） |
| `archive_service/csv_reader.py` | RFC 4180 CSV（引号、字段内换行）、`\N` NULL、空串保留、严格类型转换 |
| `archive_service/ingest.py` | 清单绑定档案摘要与 `entry_id`；每表一个 SQLite 事务；行级来源账本 |
| `archive_service/fixtures.py` | 测试/样例用的底层 ZIP 构造器（可造越界、伪造长度等） |
| `archive_service/generate.py` | 固定种子生成交付样例、对抗样例与独立真值 |
| `archive_service/verify.py` | 独立核对：逐行/逐类型/逐来源复核，并驱动全部对抗样例 |
| `archive_service/__main__.py` | 命令行：`list` / `read` / `source-range` / `deliver` / `ingest` / `verify` |

## 真实 ZIP 与身份

* 只接受 **stored(0)** 与 **deflate(8)**、无加密、无分卷、**非 ZIP64**。
* 成员身份 `entry_id = "<整个归档 SHA256>:<中央目录条目序号>"`。
  **名字从不作为唯一键**；同名成员原样保留、各自有独立身份。
* 名字 UTF-8 由 EFS 标志声明；若标志未置位但字节是合法 UTF-8 也接受（常见遗留写入器），否则按 CP437。
* 核对中央目录与本地头的方法、通用标志、名字原始字节、压缩/未压缩长度；带 data descriptor 的条目按 16 字节（含签名）或 12 字节（无签名）明确长度核验，且必须与中央目录的 CRC/长度一致。
* 每个条目按**明确长度**解压，CRC32 核真实解压正文；解压输出超过声明长度立即失败。不扫描下一个 `PK` 字节猜边界（stored 正文中出现的 `PK\x03\x04` 不会误判）。
* 条目范围（本地头 + 正文 + 可选 descriptor）不得重叠、不得覆盖中央目录；EOCD 必须精确结束于文件末尾；多余尾随字节、分卷、ZIP64、加密、未知方法/标志一律拒绝。
* **元数据矛盾或解压长度超声明 → 拒绝整个档案**；CRC 不符属于**条目级**错误（见下）。

上限：**每条目正文 8 MiB**、**整个解压总量 100 MiB**，均以**实际输出**计量（声明压缩得很小、实际膨胀超限同样拒绝），超限不产生成功包。

## 清单与入仓语义

入仓清单（`artifacts/demo/load_manifest.json`）绑定 `archive_sha256` 与显式 `entry_ids`，为每表声明表名、`encoding`（只允许 UTF-8）、列类型与主键：

```json
{"archive_sha256": "<sha>", "tables": [
  {"table": "orders", "encoding": "utf-8",
   "columns": [["order_id","INTEGER",true], ["customer_id","INTEGER",false],
               ["note","TEXT",false], ["amount","REAL",false]],
   "entry_ids": ["<sha>:2", "<sha>:3"]}
]}
```

* 每个表的所有指定成员按清单顺序组成**一个事务批**：整表提交或整表不留痕；其他已完整提交的表保留。表已存在时拒绝覆盖。
* 坏 CRC、坏行、重复主键（含跨成员）、类型错误只回滚**该表**；错误回执含 `entry_id`、物理行号与记录序号。
* CSV：引号、逗号、`""` 转义、字段内换行；`\N` 是唯一的 NULL 记号，**空串保留为空串**；头部必须与声明列一致；字段数/类型严格。
* 正文只通过 **`ArchiveReader.read_entry(entry_id)` / `iter_entry_chunks(entry_id)`** 读取，绝不按名字重新打开档案，因此同名成员的“第一份表被最后一份替换”不可能发生。

### 设计取舍：先验证后载入 vs 边验边载

采用**先完整校验（一次解压）+ 再事务化载入（第二次解压到临时内存）**：

* 失败回滚：CRC/结构问题在任何 SQLite 写入前确定，避免半张表可见；每表事务仍保证行级错误可整体回滚。
* 解压次数：正文最多解压两次（一次预检、一次入仓）；实现不接受整档常驻内存，单成员上限 8 MiB 由读取路径强制。
* 临时空间：每表悬停一个成员正文（≤8 MiB）于内存，不使用磁盘暂存。

交付路径（`delivery.py`）采用**先写临时文件、全部成员验证通过后原子 rename 提交**；任一成员失败则本次交付不留下任何目标文件。

## 目录

```
archive_service/          源码（服务与入仓两模块 + CSV + 交付 + 生成器 + CLI）
tests/                    unittest 测试（80 个）
artifacts/demo/
  warehouse.zip           交付样例 ZIP（同名不同正文、data descriptor、UTF-8 名字）
  load_manifest.json      入仓清单（绑定摘要与 entry_id）
  warehouse.sqlite        实际入仓得到的 SQLite 库（含 _zs_ingest / _zs_row_source 账本）
  ground_truth.json       独立生成的真值（逐行值与物理行号）
  verification_report.json 独立核对报告（行/类型/来源/对抗样例/路径）
  delivered/              安全交付结果（每成员一个按 id 命名的路径）
  bad/                    对抗样例：同名、descriptor、伪造长度、坏 CRC 末尾、范围重叠、坏 UTF-8
```

## 复跑

```bash
python3 -m archive_service.generate     # 重新生成 ZIP/真值/对抗样例
python3 -m archive_service.demo         # 列成员、双读同名、交付、入仓、重开库计数
python3 -m archive_service.verify       # 独立逐行/逐来源复核并写报告
python3 -m unittest discover -s tests -v
```

CLI 示例（无需数据库即可读取原件）：

```bash
python3 -m archive_service list artifacts/demo/warehouse.zip
python3 -m archive_service read artifacts/demo/warehouse.zip "<sha>:1" /tmp/out.csv
python3 -m archive_service source-range artifacts/demo/warehouse.zip "<sha>:1" 0 16 | xxd
python3 -m archive_service ingest artifacts/demo/warehouse.zip \
        artifacts/demo/load_manifest.json /tmp/warehouse.sqlite
```

## 测试覆盖要点

* **同名不同正文**：两个同名成员独立 `entry_id`，正文不同；清单只绑定其中一个，另一个从不入表。
* **data descriptor**：标准库以 non-seekable 输出写出的 descriptor 档案可读；descriptor 与中央目录不一致时拒绝。
* **伪造长度/伪造中央长度/截断/尾随字节/ZIP64/分卷/加密/未知方法**：拒绝。
* **CRC 坏末尾**：坏成员被标记，只有它所在的表回滚；好消息表仍在。
* **路径规范冲突**：绝对路径、`..`、空段、反斜杠、`C:`、NFC 冲突、大小写冲突、文件/目录前缀冲突全部拒绝且不“清洗后覆盖”。
* **跨成员主键冲突**：整表回滚，回执给出第二个成员的 `entry_id` 与行号。
* **独立核对**：`tests/helpers.py` 用标准库 `csv` 独立解析每个成员，逐行比对真值的值与物理行号；随后从 SQLite 每行经 `_zs_row_source` 回查 `entry_id` 与行号并重读原件；关闭重开后再回查一遍。

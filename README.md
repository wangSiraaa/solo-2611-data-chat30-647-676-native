# 合成磁带上的受限设备服务与真实文件恢复器

Python 3.11、零第三方依赖。全部"介质"都是普通文件模拟的卷镜像，不接触任何真实硬件
（所有输出都带 `provenance: "simulated-media"`）。恢复端只能通过设备 API 取正文，
规划/审计可以查看卷结构，但 **正文字节一律从 `drive.read` 流出**。

```
tape_restore/
  errors.py     带稳定 code 的类型化异常
  model.py      卷/块/片段/文件目录模型 + 结构校验
  media.py      卷镜像的编码/解码/审计（普通文件）
  device.py     双 drive 设备：mount/rewind/skip/read，全量调用日志
  staging.py    以字节计量的工作盘租赁台账（ScratchLedger）
  journal.py    只追加日志 + 原子检查点
  planner.py    volume-major / file-major 两种编排与成本预估
  restorer.py   恢复引擎（流式校验、发布、中断、恢复）
  scenario.py   合成场景生成器 + 独立真值
  cli.py        命令行
scripts/demo.py 一键跑全部案例并写 results/report.json
tests/          33 个单元/端到端测试
```

## 快速开始

```bash
python3 -m tape_restore.cli gen --dest results/scenario --force
python3 -m tape_restore.cli plan   --scenario results/scenario
python3 -m tape_restore.cli restore --scenario results/scenario --strategy file \
    --fallback --root results/run_file --clean --out results/restore_file.json
python3 -m tape_restore.cli verify  --scenario results/scenario --root results/run_file
python3 -m tape_restore.cli inspect --scenario results/scenario --audit-media
python3 -m tape_restore.cli device  --scenario results/scenario --volume V3
python3 scripts/demo.py            # 全部案例 + results/report.json
python3 -m unittest discover -s tests -v
```

中断与恢复：

```bash
# 跑 5 个已确认片段后强制中断（返回码 3，提示 resume 命令）
python3 -m tape_restore.cli restore --scenario results/scenario \
    --strategy file --files F07,F12,F01 --run-id R1 --interrupt-after 5 \
    --root results/run_r1 --clean
# 恢复：重新挂载、rewind+skip 回记录块位并断言，重新哈希已暂存片段后再继续
python3 -m tape_restore.cli resume --scenario results/scenario \
    --run-id R1 --root results/run_r1
```

## 目录格式与结构校验

`catalog.json` 独立列出每个文件 `file_id / path / total_length / sha256` 与有序片段
`fragment_id / volume_id / start_block / block_count / valid_bytes / sha256`。

`model.parse_catalog` **不拿整文件摘要替代结构检查**，逐项校验：

* 片段 `valid_bytes` 之和 == `total_length`（零长度文件不得带片段）；
* 片段区间不得越出卷尾；`valid_bytes` 不得超过 `block_count*block_size`，也不得小到
  让块数虚高；
* `fragment_id` 在文件内、跨文件均不得重复；`(volume, start, count)` 物理块不得被
  两个片段同时占用；
* 若提供真实卷，片段覆盖的块必须是 data 块（不得压到 filemark），卷块数与目录声明
  一致；未引用的 data 块记为 warning。

末块填充：设备交回完整物理块（含 padding），恢复端严格按 `valid_bytes` 切片，
padding 永不进入文件正文（`test_padding_is_not_file_body`）。

## 设备层语义

* 两台 drive，各至多一卷；同一卷不能同时在两台（`VolumeInUse`）；drive 已占用再
  mount 抛 `DriveOccupied`。
* mount 后位于块 0；`skip` 只前移（负数/越界报错，越界的实际步数仍记日志）；
  `read` 从当前块推进并返回该块，文件标记返回 0 字节；**没有随机 seek**，回退只能
  `rewind` 后重新 `skip`（`seek_to` 就是 rewind+skip 的组合）。
* 每次 mount/unmount/rewind/skip/read（含 filemark）都记录 before/after 与
  `bytes_returned`；`summary()` 汇总裁量。设备客户可脱离恢复器独立整卷顺序读
  （`DeviceManager.scan` / `cli device`）。

## 工作盘计量

`ScratchLedger` 是**真实**台账：每次占用先 `reserve`（超额度在写之前抛
`QuotaExceeded`），字节落在 `work/scratch/*.bin`，`snapshot()` 报告 current/peak，
`release` 时回收。恢复器每一步都写入 `quota_steps`（current/peak/quota），测试逐条
断言 `current <= quota` 且 `peak <= quota`。已完成并发布的目标在 `out/`，不计临时
额度；未完成目标（含直接按最终偏移写的半成品）计入额度。没有任何字节放在未计量的
Python 缓存里。

## 两种编排与取舍（不要求全局最优）

| | volume-major | file-major |
|---|---|---|
| 装载/倒带 | 每卷一次 sweep（最少 mount/rewind） | 每个文件可能在多卷间往返（mount 多） |
| 暂存峰值 | 所有在途片段之和，常爆额度 | 约一个文件/一个片段，受额度约束 |
| 适用 | 小文件集合、额度充足 | 大文件、额度紧张 |

`cli plan` 同时给出两种策略的预测 mount/rewind/skip/read/峰值与 `fits`。实现选择
**按文件偏移直接写未完成目标**，因此 file-major 峰值 ≈ 当前文件总长；超过额度的
文件（F10=42484B > 32KiB）被标记 `skipped_quota`，其余文件照常完成。`--strategy
volume` 若峰值超限会报告不可行（或 `--fallback` 自动改用 file-major）。

## 失败、截断、恢复、目录变化

* **坏片段**：片段 SHA256 与目录不符 → 该文件 `failed_fragment`，绝不发布；
  同批其他文件不受影响，照常完成并通过字节比对（F14/F18 为注入坏片段）。
* **介质截断**：卷镜像无法完整解码 → 受影响文件 `failed_media`，不发布前缀；
  不涉及该卷的文件仍成功。
* **中断/恢复**：只在**已确认块边界**写检查点（片段落盘且摘要匹配后）。恢复时
  (1) 比较目录指纹，变化则作废旧计划、丢弃旧暂存、从头重跑；(2) 重新挂载并按
  `rewind+skip` 复原真实块位并断言（不可达则显式报错）；(3) 对每个"已确认"片段
  **重新哈希暂存区**，不符即丢弃重读——绝不因日志里有一行就跳过未确认片段；
  (4) 比较卷镜像摘要，变化卷上的片段一律作废，避免把另一卷同位置字节接进旧文件。
  恢复同样覆盖 volume-major（多个目标同时打开）的多文件检查点。

## 生成的场景（6 卷 × 100 块 × 1024B，20 文件，32KiB 额度）

包含跨卷文件（F16 跨 V4/V5/V6）、共享卷交错（V3 上 F05/F06/F14/F19 交错）、零长度
文件（F04）、末块填充（F03=1023B 等）、额度不足（F10>额度）、注入坏片段（F14、F18）、
长恢复文件（F07/F12 带 `save_every`）。独立真值字节单独存放在 `scenario/truth/`，
校验时与目录 sha 无关地逐字节比较（`scripts/demo.py` 的 `audit_outputs`）。

`results/report.json` 记录每个案例的：发布文件数、逐文件字节是否等于真值、是否出现
"complete 但字节错误"、失败文件是否被误发布、暂存峰值是否越额、skip 是否只前移、
以及完整设备调用。

## 测试

`python3 -m unittest discover -s tests -v`（33 项）覆盖：场景额度/卷数/零长度/填充、
目录结构校验（长度和、越界、重复片段身份、物理块冲突、filemark 重叠、整文件摘要不能
掩盖坏片段）、设备语义（单卷单drive、双挂载拒绝、只前移 skip、日志字节数、独立顺序
读、无直接 seek）、台账超限、file/volume 两种策略的正确字节与成本差异、失败隔离、
介质截断、中断恢复（含篡改暂存、卷变化、目录变化三类）。每项恢复测试都以"已发布文件
必须逐字节等于真值"为准，绝不以文件数量代替正确性。

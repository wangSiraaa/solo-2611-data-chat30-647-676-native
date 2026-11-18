# pcapng_kit —— 有限 PCAPNG 结构目录与包级流量事实仓

纯 Python 3.11 标准库实现（无第三方依赖），解决“第二节把接口 0 重定义为
另一条链路、但导入仍沿用第一节的时间精度与网络名，导致流量日报出现
千倍时间偏移”这一类问题：**接口的时间分辨率、整秒偏移和身份严格限定
在所属节内，跨节同号接口绝不继承任何状态**；所有时间换算用
`fractions.Fraction` 完成，64 位时间戳先由高低 32 位拼整数再除分辨率，
全程不经过浮点。

## 模块分工（至少两个模块，单向依赖）

```
pcapng_kit/
├── blocks.py     # 块/选项常量、4 字节对齐、选项 TLV 解析等底层合同
├── errors.py     # 框架损坏 / 合同违规 / 缓冲上限三类异常
├── catalog.py    # 模块一：PCAPNG 结构 + 时间目录（不做任何业务协议解析）
├── traffic.py    # 模块二：协议事实分析 + 流量仓（只经 catalog API 取字节）
├── writer.py     # PCAPNG 写出器（含损坏构造逃生口 write_raw）
└── generator.py  # 合成样例与独立包真值生成
```

依赖方向只有 `traffic -> catalog`。`catalog.py` 不导入 `traffic`；
`traffic.py` 不接收文件路径、不 `open`、不导入 `os/pathlib`，所有字节
都来自 `Catalog.read_body()`，测试中有源码级断言保证这一点。

## 有限合同

* 只识别 Section Header（SHB）、Interface Description（IDB）、
  Enhanced Packet（EPB）和自定义保留块（`0x00000BAD` / `0x00004000`）；
  其他块按块总长度整体跳过并登记，不据正文猜包。
* 每节由 SHB 的 byte-order magic 判大小端，同一文件可多节切换。
* 块总长度必须 `>=12` 且 4 字节对齐，且块首/块尾两个总长度必须相等；
  违反属于**框架损坏（fatal）**，扫描从下一个 SHB magic 重新同步并隔离。
* 接口号只在所属节内有效；接口事实含链路类型、snaplen、时间分辨率
  （`10^-n` 或 `2^-n` 秒）与有符号整秒偏移。
* 选项合同：`if_tsresol`、`if_tsoffset` 为单值选项，重复出现即合同违规，
  该 IDB 整块跳过；`if_name`、`if_description`、注释允许重复，全部保留；
  未知选项结构合法即跳过。
* EPB 校验 `captured_len <= orig_len`（截断不能伪装完整包）、
  `captured_len <= snaplen`、引用接口存在、填充后的正文与选项区不越界。
* 正文回读有 1 MiB 单次缓冲上限，超限抛 `BodyBufferLimitError`，且绝不
  读取捕获正文范围之外的字节（填充字节不会进入正文）。

## 目录给出的身份与范围

`PacketRecord` 对每个包提供：节号 + 节内接口号（真实身份）、块字节范围
`[block_offset, block_end)`、正文精确范围 `[body_offset, body_end)`、
选项区范围、`Fraction` 真实时间、截断标记、链路类型、大小端。
目录可独立使用（`catalog_list.py`），不生成任何业务统计。

## 流量事实仓

`TrafficStore.build(catalog)` 逐包经目录取字节，仅对**以太网 IPv4 无
分片 UDP** 下完整结论：

* 其他链路（如 Linux SLL）、ARP/TCP、分片 IPv4、非 IPv4 以太类型 →
  `UNSUPPORTED`，属于正常数据而非坏文件；
* 被 snaplen 截断、字段无法全部确定 → `INCOMPLETE`，同时给出还能确定的
  IP/UDP 字段并显式声明 `udp_payload_complete=False`；
* 方向流键 = `(节号, 接口号, 源IP, 目的IP, 源端口, 目的端口)`，反向流
  与不同节同号接口都不会合并；
* `window(start, end)` 按有理数半开区间 `[start, end)` 产出包数、线上
  字节（orig_len 之和）、实际捕获字节（captured_len 之和）、完整 UDP
  正文长度与完整 UDP 包数，并附源包序号清单。不做 TCP 重组。

## 设计取舍

采用“**一次扫描建轻量偏移索引，之后按需回读正文**”：

* 随机回查：任意包可按登记范围 `seek+read` 精确回读，重开文件也可复现；
* 损坏隔离：框架损坏按 SHB magic 重新同步，合同违规只跳单块，二者都带
  `recovered/fatal` 标记，不影响其余节；
* 内存：常驻的只有小记录（每包约百字节级），正文不常驻，单次读取受
  1 MiB 上限约束。5 节 × 20 接口 × 100000 包可直接处理。

## 运行

```bash
python3 demo.py                       # 生成样例、列目录、建仓、时间窗统计、重开回读
python3 catalog_list.py samples/sample.pcapng --pretty
python3 -m unittest discover -s tests -v
```

`demo.py` 产物位于 `samples/`：

| 文件 | 内容 |
| --- | --- |
| `sample.pcapng` | 真实样例：5 业务节（大小端交替、10/2 基分辨率、负偏移、同刻多包、snaplen 截断、自定义/未知块）+ 损坏尾长度 + 恢复节 |
| `sample.truth.json` | 与实现独立的逐包真值（Fraction 时间、捕获正文 SHA-256、范围、截断） |
| `catalog.json` | 目录 JSON 快照 |
| `summary.json` | 事实分布、半开窗统计、按源包重算结果、损坏清单 |
| `readback.sha256` | 重开后按目录范围回读全部正文的摘要 |

规模样例（5 节 × 20 接口 × 100000 包）由测试中的 `generate_scale_sample`
按需生成到临时目录，并按 JSONL 真值逐包核对时间与来源哈希。

## 测试覆盖（27 项，全部通过）

跨节大小端切换；十进制/二进制分辨率与负偏移；64 位高低字时间戳；
同刻多包；snaplen 截断的标记、范围与字段级完整性；单值选项重复违规；
可重复 `if_name` 保留；尾长度损坏隔离与新节恢复；未对齐长度；
未知/自定义块跳过且不猜包；EPB 引用缺失接口；分辨率跨节不泄漏；
unsupported 分类；反向流/跨节流不合并；半开窗边界与正文长度；IPv4
选项头；1 MiB 缓冲上限；逐包真值对账；按源包重算统计；重开回读摘要；
损坏恢复标记；traffic 不直接碰文件；10 万包规模。

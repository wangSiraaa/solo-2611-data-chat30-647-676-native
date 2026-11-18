# 有序双端地址规则产品与真实流量事实仓

本仓库实现一个**有序双端地址规则产品**（ordered dual-address rule product）、
一个**精确规则目录**（回答“某规则是否被前序规则完全遮蔽”，并返回覆盖证据或
合法五元组反例）、一个**无重叠决策单元编译器**，以及消费规则产品的**流量事实仓**。
全部输入数据均为本地合成；不与任何真实防火墙交互，也不下发任何真实规则。

## 快速开始

```bash
# 生成 80 条规则（base / 仅换序 / 范围修订 / 删除 P1 四个产品）+ 100000 个合成包
python3 -m netobs.cli generate --root . --packets 100000

# 全流程：遮蔽目录 → 编译 + 精确等价检查 → 小域完整枚举 → 装载 + 逐包核对
python3 -m netobs.cli demo --root .

# 单独使用
python3 -m netobs.cli check    --rules data/rules_base.json
python3 -m netobs.cli compile  --rules data/rules_base.json --out artifacts/cells_manual.json
python3 -m netobs.cli classify --rules data/rules_base.json \
    --src 203.0.113.7 --dst 198.51.100.7 --proto TCP --sport 9000 --dport 443
python3 -m netobs.cli load     --rules data/rules_base.json \
    --packets data/packets.jsonl --out artifacts/facts_manual.json

# 测试（标准库 unittest，共 50 个用例）
python3 -m unittest discover -s tests
```

主要产物在 `data/`（规则产品与包）与 `artifacts/`（遮蔽目录、决策单元、事实仓、
`demo_report.json`）。所有产品按规范 JSON 做 SHA-256 内容寻址。

## 语义（严格顺序首匹配）

* IPv4 按无符号 32 位整数处理；CIDR 主机位必须为 0，否则报错（不做静默截断）。
* 规则字段：`rule_id`（稳定、唯一）、源/目前缀、协议 `ANY/TCP/UDP`、
  闭区间源/目端口、`action`、`category`。
* **ANY 规则端口必须为 `0..65535`**；本期所有包都带 TCP/UDP 端口。
* 显式列表顺序执行，**第一条全条件匹配**决定结果；无命中则用末尾
  `default.action/category`。绝不使用最长前缀或最后匹配。
* 地址域、端口端点、前缀长度、action、category、重复 `rule_id` 均严格校验。
* 规则产品不可变（`RuleSet` 内部为 tuple），`digest` 对顺序敏感：
  仅换序也会产生不同产品摘要。

## 模块

| 模块 | 职责 |
| --- | --- |
| `netobs/rules.py` | 规则产品、严格解析/校验、内容寻址摘要、重排 |
| `netobs/spatial.py` | 精确五维集合代数：前缀规范化、区间规范化、乘积区域差集 |
| `netobs/shadow.py` | 规则目录：逐规则遮蔽判定、覆盖证据、反例、独立复核 |
| `netobs/compile.py` | 优先级展开编译为两两不交决策单元、精确等价报告、资源拒绝 |
| `netobs/index.py` | 源/目二进制前缀 trie；只做候选裁剪，语义仍是首匹配 |
| `netobs/traffic.py` | 流量事实仓：逐包归因、窗口/category 聚合、来源留存、守恒校验 |
| `netobs/reference.py` | 独立参考实现：小域完整枚举、逐包核对、高位地址用例 |
| `netobs/generate.py` | 合成规则/流量生成器（含题面要求的各类案例） |
| `netobs/demo.py` | 全流程编排与产物落盘 |
| `netobs/cli.py` | 命令行入口 |

## 遮蔽分析（规则目录）

判定是集合关系 `R_i ⊆ R_0 ∪ … ∪ R_{i-1}`，其中 `R_j` 是规则在
（源、目的、协议、源端口、目端口）上的**完整乘积区域**。

* 前序规则可以**联合**遮蔽后规则；实现逐条做精确差集
  `remaining ← remaining \ R_prev`，不只看单条包含。
* **完全遮蔽**：返回覆盖证据——目标区域被按最早可覆盖顺序切成若干互不相交的
  乘积区域，每片标注来源 `rule_id`；`verify_verdict` 会独立复核
  “证据并集 ⊇ 目标且 ⊆ 目标”，并用边界点做首匹配抽查。
* **未完全遮蔽**：返回残余区域与一个**合法五元组反例**，并独立校验反例确实
  在目标规则内且不被任何前序规则命中。
* 目录只依赖规则产品，不依赖任何流量样本。

生成器刻意包含：两条前序规则联合覆盖（端口切分 `U_C`、源/目 2×2 交错
`I5A/I5B`）、协议分离遮蔽 ANY（`P3`）、端口边界（`B1/B2` 联合覆盖 `B3`，
`B4` 在 99 / 301 留下反例）、单条包含（`N3`）、未遮蔽反例（`O_DENY`、`M1`）。

## 编译为无重叠决策单元

第 `i` 条规则的可达单元为 `U_i = R_i \ (R_0 ∪ … ∪ R_{i-1})`，用精确差集分解
成互不相交的乘积区域；单元连同默认单元构成全域的**无重叠划分**。单元携带
来源 `rule_id` 与 action/category，对任意输入先落单元再读标签，与逐条首匹配
**集合恒等式**等价：

* 单元两两不交（`equivalence_report` 在预算内做全量配对，否则报告未全量检查）；
* 每个 `U_i` 的单元并集与可达区域双向差集皆空；
* 默认单元不与任何规则区域相交；全部单元并集覆盖全域（`full \ all = ∅`）；
* 可选额外五元组列表逐点对照首匹配。

**不物化**也可行：`equivalence_report` 是独立入口，可用额外五元组只做集合级
检查而不落盘单元。若展开体积过大，抛出 `CompilationResourceError`（明确资源
拒绝），**绝不把拒绝说成规则等价**；`demo_report.json` 中有一次 `max_cells=5`
的拒绝演示。

## 流量事实仓

* 输入 `packet_id`、非负整数 `ts`、五元组、非负字节数；严格校验协议/端口/地址。
* 每条事实记录命中 `rule_id`（或 `DEFAULT`）、action、category、规则下标，并绑定
  消费的 `ruleset_digest` 与 `identity_digest`。
* 聚合按（窗口起点, category, action）给出**包数、字节数**、命中 rule 计数、
  原包 `packet_id` 列表及来源摘要。**DENY/丢弃类别同样统计**，因此总字节守恒
  可逐包核验，不会靠丢弃拒绝类别伪造守恒。
* 规则换序 / 范围修订 / 删除后重新装载会真实重新分类（`variant_diffs` 显示
  改变的包数）；同一规则产品重复装载得到同一摘要；已落盘的旧产品文件不被改写
  （`save_analysis` 内容相同不触碰文件，`base_product_file_unchanged` 复核）。

## 验证

* `reference.py` 用与产品解耦的显式循环做“第二实现”。
* 小域（地址 0..7 × 端口 0..5）**完整枚举 4608 点**，逐点比较
  `RuleSet` / `RuleIndex` / `CompiledRules` 与参考实现。
* 32 位高位用例（`128.0.0.0/1`、`240.0.0.0/4`、`0.0.0.0/0` 目的前缀、
  `255.255.255.255` 等）逐点核对，避免只覆盖小样本。
* 100000 包逐包核对归因、聚合包数/字节/来源与总字节守恒，四个规则产品全部
  0 差异。

## 取舍说明

见 `TRADEOFFS.md`。

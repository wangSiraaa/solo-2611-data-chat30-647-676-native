# 本地广告播出核对工具

从合成单声道 PCM WAV 素材与 30 分钟节目流出发，完成两件相互独立的事：

1. **音频检索 / 监测**（`adverify.monitor` + `adverify.matcher`）：登记素材、
   从真实音频计算检索特征、扫描节目寻找每次素材出现，输出逐条匹配与可复查证据；
2. **履约核对**（`adverify.fulfill` + `adverify.solver`）：只消费监测结果
   （不读音频），把确定匹配分配给排期要求，整体最优而非逐条贪心。

两端各自持久化到独立 SQLite，监测批次与履约运行均**不可变**，可固定旧批次
重开查询；更正产生新批次并触发履约整体重算。

## 目录与模块

| 模块 | 职责 |
| --- | --- |
| `audioio.py` | 单声道 16-bit PCM WAV 读写、增益/噪声工具 |
| `features.py` | 分帧 log-mel + 稀疏频谱峰指纹特征，特征配置带哈希 |
| `matcher.py` | FFT 互相关召回 → 局部子段 → 能量感知对角验证 → 合并/分级/跨素材 NMS |
| `monitor.py` | 素材登记/软删除/查询、不可变扫描批次、更正批次、外部监测表校验 |
| `solver.py` | 最小费用最大流 + 字典序决胜的履约分配 |
| `fulfill.py` | 排期导入、不可变履约运行、消费监测库或外部表 |
| `generator.py` | 合成素材/节目/真值（真值仅供验证路径） |
| `evidence.py` | 导出对齐 JSON、逐帧证据摘要、节目侧与素材侧 WAV 片段、CSV |
| `validator.py` | 真值比对（定位误差/漏检/误检）+ 独立穷举器核对分配最优性 |
| `cli.py` | 建库/扫描/核对/证据/验证命令 |

## 命令一览

```bash
PY=.venv/bin/python
$PY -m adverify.cli gen --out data                     # 合成 30 素材 + 30 分钟节目 + 真值
$PY -m adverify.cli build-db --db data/monitor.db --materials data/materials
$PY -m adverify.cli list-materials --db data/monitor.db
$PY -m adverify.cli scan --db data/monitor.db --program data/program.wav
$PY -m adverify.cli list-scans --db data/monitor.db
$PY -m adverify.cli show-scan --db data/monitor.db --out data/monitor_table.json
$PY -m adverify.cli make-schedules --gt data/ground_truth.json --out data/schedules.json
$PY -m adverify.cli check --monitor-db data/monitor.db \
    --fulfill-db data/fulfill.db --schedules data/schedules.json --replace-schedules
# 也可只给合法外部监测表，完全不接触音频/监测库：
$PY -m adverify.cli check --monitor-table data/monitor_table.json \
    --fulfill-db data/fulfill_external.db --schedules data/schedules.json
$PY -m adverify.cli evidence --db data/monitor.db --out data/evidence
$PY -m adverify.cli correct-match --db data/monitor.db --play-id m017#p01 \
    --overrides-json '{"certainty":"review"}' --reason "reclassify truncated"
$PY -m adverify.cli validate --gt data/ground_truth.json --monitor-db data/monitor.db
$PY -m tests.test_pipeline                              # 20 项测试
```

阈值均可通过 `scan --config scan_cfg.json` 覆盖（字段见 `matcher.ScanConfig`）。

## 检索表示及其对误检与读取成本的影响

### 表示：稀疏频谱峰指纹

25 ms 汉宁窗、10 ms 帧移，32 个梅尔带取 log 能量；每帧只保留能量最高的
`n_peaks=6` 个频带（静态峰）与一阶差分绝对值的 6 个峰（动态峰），拼成二值
向量并做 L2 归一化。于是两帧的余弦相似度就是**峰集合的重合比例**。

为什么不用连续 log-mel 余弦：连续高维单位向量的余弦天然集中在 0.7~0.9，
背景床与素材的谐波托底长期高相似，截断点之后仍过阈，无法分辨边界，也压不住
跨素材误报。改成稀疏峰后，同一声音峰高度重合（实测 0.7~0.95），背景或其它
素材的窄带峰几乎不重合（0.15~0.3），基线被整体压低：

* **误检**：背景相似不再过验证阈；跨素材啁啾/谐波的偶发重合只形成短而低覆盖
  的片段，被待核门槛与跨素材 NMS 滤除。共享短 jingle 单播时，宿主素材只能得到
  低覆盖片段，不可能被判为完整播放。
* **音量变化**：整体增益不改变每帧 top-K 峰的选择，也不改变归一化方向，
  因此 0.35x~2.1x 增益的播放照常召回。
* **读取成本**：特征按素材以 numpy blob 存 SQLite，扫描时节目特征只算一次
  （30 分钟约 18 万帧），每个素材一次 FFT 互相关（O(N log N)）+ 候选邻域内
  的少量对角验证；全量 30 素材扫描在普通笔记本上约 7 秒。不加载素材原始
  波形进入扫描热路径，能量掩码只在建库时按素材计算一次。

### 三段式匹配

1. **召回**：仅在完全重叠滞后上算整段平均余弦（无边界归一化伪峰），阈值以上
   按分数降序做非极大抑制取峰；平顶峰/相关平台也能被取到，截断不漏。
2. **局部子段 + 对角验证**：候选滞后先看素材侧 0.6 s 最佳子段均分，再逐帧
   余弦过阈，结合素材帧能量掩码桥接少量坏帧，取最长连续段，得到真实素材
   覆盖区间——截断只覆盖素材的一部分，覆盖率自然低。
3. **合并与分级**：同素材节目区间严格重叠才合并（重复播放互不相交，保持
   独立）；跨素材按 (确定性, 均分, 覆盖率) 排序做 IoU NMS，同一段节目声音
   只归属最强素材。`score≥0.55 且 coverage≥0.85` 为确定，满足待核门槛
   （`score≥0.55 且 coverage≥0.55`）为待核，其余不输出。

## 分配求解及其对更正兼容性的影响

### 三级目标（严格字典序，全程整数）

1. 最大化已满足要求的总次数；
2. 最小化 `sum |2·prog_start − win_start − win_end|`（距离乘 2，半整数中点
   不引入浮点）；
3. 仍并列时，把被分配的 `(schedule_id, play_id)` 对排序，取字典序最小列表。

前两目标由单源单汇**最小费用最大流**（SPFA 增广，容量均为 1）一次给出：
源→排期容量=要求次数、配对边费用=加倍距离、播放→汇容量 1。第三目标无法靠
边权微扰可靠表达，于是对候选配对按字典序逐一做“强制接受/可行性”判定：
若强制该配对（禁用与其争用同一播放或排期剩余容量的边）后仍能取得相同的
最优流量与费用则接受，否则永久拒绝。这不是逐条贪心：每次判定都以全局最优
`(flow, cost)` 为约束，60 组随机竞争表与人工构造表均与独立穷举器逐案一致。

### 资格与去向

只有 `confirmed` 播放、素材一致、开始点落在半开窗口、连续匹配采样点数
（`contiguous_frames × hop`）达标才可分配。每条播放最多被一条排期消费一次；
未达要求次数的排期报 `partial` 并保留已满足部分；无合格排期或竞争落败的
播放进入 `leftover_plays`，每个分配都带窗口、素材、时长三项依据。

### 更正兼容性

监测端把一次 confirmed 更正为截断（review）时，`correct_match` **不修改旧
批次**，而是复制出指向 `supersedes` 的新批次。履约端对新批次调用 `run`
得到全新运行：求解器从新的 confirmed 集合整体重算流量、费用与字典序决胜，
而不是删掉一个成功标记——受影响排期可能改由别的播放满足，分配与距离全部
重排。排期调整只影响履约库，无需重新识别音频。

## 缓存失效方案

* 特征配置（`FeatureConfig`）与扫描配置（`ScanConfig`）都序列化成稳定 JSON
  并取哈希；特征按 `(material_id, feature_config_hash)` 存盘，扫描批次记录
  创建时的两个哈希。
* 用不同 `FeatureConfig` 打开库扫描时，发现素材缺少对应哈希的索引即抛
  `IncompatibleIndexError`，**绝不静默复用**旧索引；需 `reindex` 重建。
* 素材源文件 sha256 变化后重新登记，特征哈希更新；软删除只置 `deleted_at`，
  旧批次的匹配与证据原样可读。
* 履约运行记录监测表哈希与排期哈希，输入变化产生新运行，旧运行不可变。

## 验证结果（固定种子可复跑）

* 30 素材（5~30 s，多谐波+扫频+FM+独立啁啾+签名标记音，非单频、两两不同），
  30 分钟、8000 Hz 节目，含 53 个事件（完整/头截断/尾截断/重复/音量/共享
  jingle/弱增益）。
* 扫描：52 个带素材真值事件**零漏检、零误报**；30 完整 + 9 重复 + 5 音量全部
  confirmed，8 截断全部 review；开始定位平均误差约 0.55 s（10 ms 帧移量级）。
* 履约：100 排期、166 次总要求，44 个确定播放满足 40 次，4 个播放无合格排期
  进入剩余清单；穷举器在 5 个人工竞争表 + 60 个随机表上确认流量、费用、
  字典序与确定性全部最优。

# SOLO 数据工程本地实现：广告播出核对

本地（无网络、无界面）的广告素材监测与排期履约核对工具。两个独立子系统：

- **监测端**：从真实 PCM WAV 计算稀疏频谱峰指纹，扫描节目流定位每次素材
  播放（重复分开、截断可辨、共享短片段不误判），输出稳定播放 ID 与可复查
  对齐证据；批次不可变，更正产生新批次。
- **履约端**：只消费监测结果（支持外部监测表，无需音频），用最小费用最大流
  + 字典序决胜做整体最优分配，三级目标为「最大化满足次数 → 最小化加倍距离
  → 字典序最小配对」，绝不逐条贪心。

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install numpy
.venv/bin/python -m adverify.cli gen --out data
.venv/bin/python -m adverify.cli build-db --db data/monitor.db --materials data/materials
.venv/bin/python -m adverify.cli scan --db data/monitor.db --program data/program.wav
.venv/bin/python -m adverify.cli make-schedules --gt data/ground_truth.json --out data/schedules.json
.venv/bin/python -m adverify.cli check --monitor-db data/monitor.db \
    --fulfill-db data/fulfill.db --schedules data/schedules.json --replace-schedules
.venv/bin/python -m adverify.cli evidence --db data/monitor.db --out data/evidence
.venv/bin/python -m adverify.cli validate --gt data/ground_truth.json --monitor-db data/monitor.db
.venv/bin/python -m tests.test_pipeline
```

设计取舍（检索表示、分配求解、缓存失效如何影响误检、读取成本与更正兼容性）
见 [DESIGN.md](DESIGN.md)。命令完整清单见 `python -m adverify.cli --help`。

## 规模与输入约定

- 30 个素材，每个 5~30 秒；30 分钟、8000 Hz、单声道、16-bit PCM 节目；100 排期。
- 采样率统一，不做变速/变调、语音理解或现实品牌识别。
- 生成器真值 `ground_truth.json` 仅验证路径读取，监测模块不导入生成器、
  不读插入位置，也不用文件名或整段哈希代替片段匹配。

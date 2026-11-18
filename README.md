# BMP 像素目录与分块区域统计数据仓

Python 3.11 实现。核心库只依赖标准库；测试用 Pillow 作独立解码器交叉核验，
`numpy` 不需要。

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install pytest "pillow==11.3.0"   # 仅测试需要
.venv/bin/python -m bmpcatalog.demo demo_assets/out   # 生成真实 BMP + report.json
.venv/bin/python -m pytest tests -q
```

## 模块

| 模块 | 职责 |
| --- | --- |
| `bmpcatalog/bmp.py` | BMP 校验、像素目录、逻辑矩形读取、逐行原字节范围 |
| `bmpcatalog/geometry.py` | 半开整数矩形、越界拒绝 |
| `bmpcatalog/revision.py` | 目录修订：只改覆盖的真实像素，其他字节保持 |
| `bmpcatalog/analysis.py` | 分块统计、精确并集、不可变报告 |
| `bmpcatalog/writer.py` | 用于生成样例/导出的受限 BMP 写入器 |
| `bmpcatalog/demo.py` | 生成多格式样例、统计与修订验证 |

## 支持的输入

* 仅 `BITMAPINFOHEADER`（40 字节 DIB）。
* 8 位调色板、24 位 BGR、32 位 BGRX（`BI_RGB`）。
* 32 位 `BI_BITFIELDS`：R/G/B 掩码必须各自连续、两两不重叠。
* 不接受 RLE8/RLE4、嵌入 JPEG/PNG、OS/2 头、其他位深。

## 坐标与真值约定

* 所有公开坐标是**左上原点、半开**逻辑矩形 `(x0, y0, x1, y1)`。
* 正高度 BMP 自底向上存储，负高度自顶向下；目录内部换算，对外始终逐行自上而下。
* 每行按 4 字节对齐；补齐字节只用于定位，**不参与 RGBA、颜色和摘要**。
* 8 位通过调色板索引取色；24 位按 BGR；32 位 `BI_RGB` 按 BGRX，X 不为 alpha。
* `BI_BITFIELDS`：三掩码之外若第四个双字是合法且与 RGB 不重叠的连续掩码，则作为
  alpha；否则 alpha 恒为 255。每通道按 `round-half-up` 映射：
  `floor(value*255/(2**n-1)+0.5)`，不用高 8 位截断。
* 未知/非法/截断文件抛 `BmpFormatError`，不会产生可用的部分目录。
* 越界矩形抛 `RectangleOutOfBounds`，绝不静默裁剪。

## 目录核对项

`product.catalog_record()` 记录头声明尺寸、真实文件长度、像素起点、行跨度、
每行有效字节、补齐字节、声明图像大小、所需像素字节、调色板条目、掩码等。
`iter_rows(rect)` 返回 `RowView`，其中 `raw` 只含逻辑 x 范围内的真实像素字节，
`file_range` 给出其原文件偏移，`padding_offset/length` 单独描述补齐区。

## 修订

```python
revised = product.revise([((x0, y0, x1, y1), rgba_bytes_or_callable)])
revised.save("out.bmp")
```

只改覆盖像素；补齐字节、调色板、未覆盖像素与其他头字段原样保留。修订结果
重新走完整校验后才成为新目录。修订后的像素会真实写入导出文件。

## 统计

```python
analyzer = RegionAnalyzer(product, tile_size=64)
report = analyzer.report([(0, 0, 10, 10), (5, 5, 20, 20)])  # 允许重叠
report.regions[0].sums        # 精确通道和
report.regions[0].means       # 均值
report.regions[0].transparent # alpha == 0 的像素数
report.union                  # 并集，重叠像素只算一次
```

* 每个矩形：像素数、RGBA 精确和、均值、透明/不透明/半透明像素数。
* 并集用水平扫描线：相邻矩形边界之间合并 x 区间后再读取，因此重叠像素只计一次，
  不能由各矩形统计相加得到。
* 分块缓存带硬上限；矩形统计复用整块结果，边缘只读少量真实像素。
* `RegionReport` 绑定产品 sha256 与逻辑矩形；修订后产生新报告，旧报告仍指向旧产品。

## 规模与工作集

测试 `test_large_workload_2048x2048_1000_rects_under_4mib` 使用 2048×2048、
1000 个随机（可重叠）矩形，统计阶段 `tracemalloc` 峰值 ≤ 4 MiB；不按矩形复制整图。

## 测试

* `tests/test_catalog.py`：像素逐点真值、padding/方向/调色板/掩码/透明度、
  头校验、截断、越界、修订。
* `tests/test_analysis.py`：矩形真值、跨块/触边、并集去重、旧报告绑定、
  2048×2048×1000 工作集。
* `tests/test_pillow_crosscheck.py`：Pillow 独立解码/编码交叉核验。
* `tests/test_demo.py`：样例导出、重开真实像素验证。

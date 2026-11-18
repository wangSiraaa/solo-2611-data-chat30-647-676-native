# 纠删码条带服务与受损对象恢复仓

纯 Python 3.11 标准库实现，无第三方依赖。固定有限域 **GF(2^8)，不可约多项式
0x11d，域加法 = 字节异或**。包含两个相互独立、可单独使用的组件：

1. **独立条带服务** (`StripeService`)：配置登记（MDS 校验）、encode、verify、
   从任意合法片集恢复，不依赖对象仓。
2. **受损对象恢复仓** (`ObjectRepository`)：任意长度对象分条带归档、清单管理、
   逐条带检视、整对象/部分恢复、半开范围读，可消费外部同格式清单与片。

系统中不存在任何"隐蔽完整副本"：仓内只存编码片与清单，恢复结果只能从片集经
有限域运算重新解出。

## 目录结构

```
ecstore/
  gf.py         # GF(2^8): exp/log 表乘法、异或加法、逆元、慢速参考实现
  codec.py      # 配置、MDS 枚举校验、高斯-约当求逆、编码与条带恢复
  shard.py      # 自描述片文件格式 (JSON 头 + 正文, 正文 SHA-256)
  service.py    # 独立条带服务 (配置登记表 + encode/verify/recover)
  manifest.py   # 对象清单格式 (真实长度、条带顺序、各片与原始条带摘要)
  repository.py # 对象恢复仓 (归档/检视/恢复/范围读/外部清单消费)
  cli.py        # 命令行入口 (python3 -m ecstore ...)
tests/run_all.py  # 82 项断言的复跑测试
scripts/demo.py   # 8 类损伤场景的一键演示
```

## 核心设计

### 有限域 (`ecstore/gf.py`)

- 生成元 **g=0x02**。注意：0x03 在 0x11d 下乘法阶只有 51，不是本原元
  （0x03 是 0x11b/AES 多项式下的生成元，二者不可混用）。
- 乘法用 255 项 exp/log 表 + 标量乘查找表；另附俄罗斯农夫慢速实现 `mul_slow`，
  测试对全部 256×256 个乘法逐对交叉验证。
- 编码是**域内**线性组合 `Σ 系数 ⊗ 数据片`（⊗ 为 GF 乘法，求和为异或），
  不是普通整数加乘。

### 配置与 MDS 校验 (`ecstore/codec.py`)

- 参数范围：`2 ≤ k ≤ 6`，`k ≤ n ≤ 10`，`L` 为正整数（测试覆盖到 4096）。
- 编码矩阵前 k 行固定为单位阵（数据片直通），其余 n-k 行为登记时给定的
  长度 k 字节向量；缺省生成 Cauchy 行 `1/(x_i XOR y_j)`。
- 登记时枚举全部 `C(n, k)` 个行组合，对每个 k×k 矩阵做 GF(2^8) 高斯-约当消元，
  任一奇异即拒绝配置（最大规模 C(10,6)=210 次求逆）。验证结果按配置摘要缓存
  并持久化，重复登记不重复枚举。

### 身份绑定：参数相同 ≠ 同一次编码

- `stripe_id = SHA256(版本前缀 || 配置摘要 || 原始 k*L 字节摘要)` 的前 32 位十六进制。
- 每个片文件头都带 `(config_digest, stripe_id, index, body_sha256)`。
- 恢复只接受同配置、同 stripe_id 的片；别的 stripe（哪怕编码参数完全相同、
  片序号相同）一律判 `foreign`，绝不拼入。

### 恢复闸门（任一不过不发布成功）

1. 解析片文件：结构损坏或正文摘要不符 → 隔离（quarantine）。
2. 身份过滤：配置/stripe 不匹配 → foreign。
3. 同序号不同正文 → conflict；同序号同正文 → 重复去重。
4. 有效不同片少于 k → `insufficient`，**不靠补零**。
5. 取任意 k 片，求对应矩阵逆，解出 k 个数据片。
6. 用解出的数据**重新编码**，逐一核对所有提供过的有效片。
7. 核对原始条带摘要与推导出的 stripe_id。

### 对象仓与清单 (`ecstore/manifest.py`, `repository.py`)

- 对象按正文 SHA-256 内容寻址；清单原子写入后不可变，重名导入新正文不会
  改写旧清单（别名文件保留完整历史）。
- 末条带补零到 k×L，但清单 `object_length` 保留真实长度，恢复输出按其截断。
- **零字节对象**有合法空清单（0 个条带、0 个片），不生成假条带。
- 清单内嵌完整配置规格，外部新仓可凭清单自动登记配置并消费外来片
  （登记后重新计算摘要，与清单 `config_digest` 不一致则拒绝）。
- 恢复目录是自包含的：仅含 `manifest.json` + `shards/`，可整体复制到别处；
  恢复进程不接触原始正文，也不共享服务内存缓存。
- 整个片目录只解析一次并按 stripe_id 分桶（O(文件数)），逐条带解码。
- 部分恢复只导出从头开始的**连续已验证前缀**（`.partial.bin`），
  绝不标 `complete`；整对象发布要求所有条带验证通过且对象正文摘要与清单相同。
- 范围读 `[start, end)` 为半开区间，跨越的条带必须全部已验证，否则拒绝。

## 命令一览

```bash
# 独立条带服务 (与对象仓无关)
python3 -m ecstore svc-register <svc_root> --k 3 --n 5 --L 64 [--rows '[[...]]']
python3 -m ecstore svc-configs  <svc_root>
python3 -m ecstore svc-encode   <svc_root> --config-digest <d> --input <kL.bin> --out-dir <dir>
python3 -m ecstore svc-verify   <svc_root> --dir <dir> [--stripe-id <sid>]
python3 -m ecstore svc-recover  <svc_root> --dir <dir> --output <out.bin> [--stripe-id <sid>]

# 对象恢复仓
python3 -m ecstore archive    <repo> --input <obj> --name <name>
python3 -m ecstore repo-list  <repo>
python3 -m ecstore inspect    <repo> --recovery-dir <dir>
python3 -m ecstore recover    <repo> --recovery-dir <dir> --output <out.bin>
python3 -m ecstore read-range <repo> --recovery-dir <dir> --start S --end E --output <out.bin>
python3 -m ecstore rerun      <repo> --recovery-dir <dir> --output <out.bin>
```

核验/恢复类命令成功退出码 0，存在不足/冲突/失败时退出码 2，用法或配置错误 1。
所有命令支持 `--json`。

## 复跑

```bash
python3 tests/run_all.py     # 82 项断言, 约 3 秒, 产物在临时目录自动清理
python3 scripts/demo.py [dir] # 8 类损伤场景端到端演示 + 真值比对
```

测试覆盖：

- GF 运算手算（xtime、02/03/80 标量乘、归约）与 65536 对乘法的独立参考实现比对；
- 短向量逐字节手算编码，并验证结果不同于整数加乘；
- 枚举小配置全部 `C(n,k)` 个 k 片组合，逐一恢复并与原文逐字节比较；
- 非 MDS/全零行/越界参数配置拒绝；
- 缺片、坏片（隔离）、同序号冲突、重复片、跨条带混入、清单摘要不符、
  截断清单、末条带补零、零字节对象、部分恢复前缀、范围读越界、
  外部清单消费、关闭重开、旧清单不可变、L=4096 大条带。
- 规模：10 个对象（含 1 MiB），恢复正文与独立保存的生成器真值逐字节比较。

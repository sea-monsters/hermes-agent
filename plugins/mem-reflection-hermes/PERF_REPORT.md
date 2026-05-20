# mem-reflection-hermes 性能优化路线图

> 基于 2026-05-20 全链路时延基准测试（50 条记忆 + 10 Skill，Palace 模式，纯 TF-IDF）
> 基准脚本: `bench_latency.py`

---

## 一、性能基线（优化前）

```
 Step                              Mean      P95      Bottleneck
 ──────────────────────────────────────────────────────────────────
 Plugin Init                      22.3ms    22.9ms   一次性，可接受
 Context Block (palace)            1.7ms     1.8ms   79% 被 palace_index.md 写盘吃掉
 Context Block (legacy)            2.4ms     2.6ms   TF-IDF 实时检索
 Context Block (profile)           1.0ms     1.1ms   最快：读预编译文件
 Memory Search                    ~1.0ms     1.5ms   TF-IDF 50% + 排序 50%
 Put+Delete                       11.7ms    21.9ms   ⚠️ 双峰抖动 1-22ms
 Delete (pure)                    10.7ms    21.0ms   O(n) 目录遍历
 Palace Recall                     2.0ms     2.0ms   search×3 + zone scope
 Token Estimate                    2.96ms    3.08ms  纯 Python 逐字符 CJK 判断
 Batch Stat (×10)                206µs     228µs   可接受
 Skill Search                      7µs       8µs    可忽略
```

## 二、四大瓶颈根因分析

### 瓶颈 1: palace_index.md 每轮强制写盘（0.9ms, 79%）
- **病根**: 写放大 — 无变化也全量覆写
- **不是缓存问题**: 是「生成 context」和「持久化文件」职责耦合
- **机制**: Write-on-Change + Event-Driven Rebuild
- **效果**: 稳住态 0 写盘，省 100%

### 瓶颈 2: MemoryStore.delete() O(n) 目录扫描
- **病根**: 缺少 id → path 反向索引
- **不是缓存问题**: 是数据结构缺失
- **机制**: id→path 内存 dict，put/delete 时维护
- **效果**: O(n) → O(1)，省 10ms/次

### 瓶颈 3: Token 估算 2.96ms（纯计算，非 I/O）
- **病根**: 纯 Python 逐字符 Unicode range 判断
- **不是缓存问题**: 是计算路径低效
- **机制**: 字节级快速估算（`len(text.encode()) // 3`）
- **效果**: 2.96ms → ~5µs（~600x）

### 瓶颈 4: Write 双峰抖动（1ms ↔ 22ms）
- **病根**: 文件系统 buffer cache 命中/未命中随机性
- **不是缓存问题**: 同步等待磁盘 I/O
- **机制**: Async I/O（后台线程写）+ WAL 模式
- **效果**: agent 感知延迟 → 0

## 三、优化路线图

```
 优先级  优化                          预期节省      代码改动   风险
 ──────────────────────────────────────────────────────────────────
 P0      palace write-on-change        0.9ms/轮      +15 行     零
 P0      delete id→path 索引          10ms/次       +20 行     零
 P1      token 快速字节估算            2.9ms→5µs     +5 行      低
 P1      stat 异步刷盘                0.2ms/轮      +30 行     低
 P2      event-driven index rebuild   复合收益      +10 行     零
 P2      async memory write           消抖          +50 行     中
 ──────────────────────────────────────────────────────────────────
 P3      SQLite metadata（可选）      规模化        +200 行    中
```

## 四、架构原理

### 核心洞察：解耦两个职责

当前设计中 `_build_context_block()` 同时做了两件事：
1. **为 LLM 生成 context**（需要快速，纳秒级）
2. **为持久化写文件**（需要可靠，毫秒级）

正确设计应该解耦：
- Context 生成 → 内存中完成（纳秒级）
- 文件持久化 → 异步完成（不影响 hot path）

### 数据流重构

```
优化前（同步 Write-Through）:
  put/delete → 写内存 + 同步写盘 → 返回（agent 等待磁盘）

优化后（异步 Write-Back）:
  put/delete → 写内存 → 立即返回
               ↓
          后台线程 → 写盘（agent 不感知）

优化后（Event-Driven）:
  context build → 检查 dirty flag → 干净: 复用内存 → 返回
                                   脏: 重建 + hash + 写盘
```

## 五、验收标准

优化后目标：
- Context Block (palace, 稳住态): 1.7ms → <0.6ms（-65%）
- Delete: 11ms → <2ms（-82%）
- Token Estimate: 2.96ms → <10µs（-99.7%）
- Context Block stat flush: 非阻塞
- Memory write agent 感知延迟: <1ms

## 六、文件变更清单

| 文件 | 变更 |
|------|------|
| `__init__.py` | P0-P2 六项优化 |
| `bench_latency.py` | 更新以验证新指标 |
| `PERF_REPORT.md` | 本文档 |

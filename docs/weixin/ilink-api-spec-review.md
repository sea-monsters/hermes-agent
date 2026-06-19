# WeChat iLink Bot API 规格文档 & 自定义修改审查

> 基于腾讯官方 OpenClaw / iLink Bot 协议技术规格
> 参考资料：hao-ji-xing/openclaw-weixin 逆向分析 | 腾讯云开发者社区

---

## 第一部分：iLink Bot API 官方规格

### 1. 协议基础

| 项目 | 值 |
|------|-----|
| 基础 URL | `https://ilinkai.weixin.qq.com` |
| CDN URL | `https://novac2c.cdn.weixin.qq.com/c2c` |
| App ID | `bot` |
| 协议 | HTTP/JSON，无 SDK 要求 |
| 鉴权 | Bearer Token (`bot_token`) |
| 安全头 | `X-WECHAT-UIN`: base64(String(randomUint32())), 每次随机 |
| 传输 | 长轮询（Long-poll），非 WebSocket |

### 2. 完整 API 列表

| Endpoint | Method | 功能 | 本文实现 |
|----------|--------|------|----------|
| `/ilink/bot/get_bot_qrcode` | GET | 获取登录二维码 (?bot_type=3) | ✅ |
| `/ilink/bot/get_qrcode_status` | GET | 轮询扫码状态 (?qrcode=xxx) | ✅ |
| `/ilink/bot/getupdates` | POST | 长轮询收消息（核心，hold 35s） | ✅ |
| `/ilink/bot/sendmessage` | POST | 发送消息（文字/图片/文件/视频/语音） | ✅ |
| `/ilink/bot/getuploadurl` | POST | 获取 CDN 预签名上传地址 | ✅ |
| `/ilink/bot/getconfig` | POST | 获取配置（typing_ticket 等） | ✅ |
| `/ilink/bot/sendtyping` | POST | 发送"正在输入"状态 | ✅ |

### 3. 长轮询消息收取

```
POST /ilink/bot/getupdates
{
  "get_updates_buf": "<cursor, 首次空>",
  "base_info": { "channel_version": "<version>" }
}
```

- Server hold 最多 **35 秒**
- 返回 `msgs[]` + `get_updates_buf`（新游标，必须保存）
- 游标不对会导致重复消息

### 4. 消息结构

```json
{
  "from_user_id": "xxx@im.wechat",
  "to_user_id": "xxx@im.bot",
  "message_type": 1,
  "message_state": 2,
  "context_token": "BASE64...",
  "item_list": [
    { "type": 1, "text_item": { "text": "你好" } }
  ]
}
```

**ID 格式**：用户 `xxx@im.wechat`，Bot `xxx@im.bot`  
**消息类型 (item_list[].type)**：

| type | 含义 |
|------|------|
| 1 | 文本 |
| 2 | 图片（CDN 加密） |
| 3 | 语音（silk 编码，可选附转文字） |
| 4 | 文件附件 |
| 5 | 视频 |

### 5. context_token — 核心约束

> 这是整个协议里最关键的细节：**每条收到的消息都带有 `context_token`，回复时必须原样带上**，否则消息不会关联到正确对话窗口。

发送消息时：
```json
POST /ilink/bot/sendmessage
{
  "msg": {
    "to_user_id": "...",
    "message_type": 2,       // BOT 发出类型
    "message_state": 2,       // FINISH（完整消息）
    "context_token": "<从 inbound 消息里取>",  // ← 必填
    "item_list": [...]
  }
}
```

### 6. 媒体文件：AES-128-ECB 加密

- CDN 上的所有媒体文件都经过 AES-128-ECB 加密
- 发送流程：生成随机 AES-128 key → 加密文件 → 调用 `getuploadurl` 获取预签名 URL → PUT 到 CDN → sendmessage 带上 `aes_key`(base64)
- AES key 在每条消息中独立生成，不跨消息复用

### 7. 速率限制

- 腾讯在服务条款中保留控制"信息收发规模或频率"的权利
- 错误码：`ret=-2` / `errcode=-2` = 频率限制  
  ⚠️ 但 `ret=-2 + errmsg="unknown error"` = **会话过期信号**（stale session），不是真限速
- `SESSION_EXPIRED_ERRCODE = -14` = 明确会话过期
- iLink 对同一 API 的调用频率有隐式限制（没有文档说明具体阈值）

### 8. Context Token 持久化

- `context_token` 在手机重启或长时间回话后可能过期
- 建议做磁盘持久化，跨进程重启保持上下文（Hermes 实现中有 `token_store`）

### 9. 其他已知限制

- 群聊支持有限：iLink 返回的消息中可能有 `group_id`，但官方只保证 DM
- 消息长度：根据协议包分析，单条文本上限约 **2000 字符**
- typing ticket TTL：`getconfig` 返回的 `typing_ticket` 有固定 TTL（≈600 秒）
- `bot_type=3` 是标准个人场景

---

## 第二部分：我们的自定义修改清单

基于 `upstream/main` 之上的 commits（按 diff 顺序）：

| # | Commit | 功能 | 代码位置 |
|---|--------|------|----------|
| C1 | `a3da30c4d` | 出站文本分块批合并 (batch merge) | `_batch_chunks()`, `_batch_merge_enabled`, `_batch_max_chars` |
| C2 | `b8469a81e` | 限流断路器 (rate limit circuit breaker) | `_rate_limit_*` 属性/方法, `_send_text_gate` 锁 |
| C3 | `09a554862` | Typing ticket 自动刷新 | `_ensure_typing_ticket()`, `_typing_cache` |
| C4 | `(uncommitted)` | `supports_code_blocks = True` | 类常量 |
| C5 | `(part of C1)` | `_coerce_bool()` 工具函数 | 独立函数 |

### C1: 出站文本分块批合并 (`_batch_chunks`)

**实现**：
- 合并顺序文本 chunk 为 batch，用 `\n\n` 分隔
- `_batch_merge_enabled=False`（默认关闭）
- `_batch_max_chars=500`（可配置）
- `_batch_inter_chunk_delay_seconds=0.5`
- 在 send 流中：当 `batch_merge_enabled` 时用 `_batch_chunks` 预处理 chunks，然后使用 `_batch_inter_chunk_delay_seconds` 替代原有 `_send_chunk_delay_seconds`

### C2: 限流断路器

**实现**：
- `_send_text_gate`: `asyncio.Lock()` 序列化所有出站文本发送
- `_rate_limit_circuit_threshold=1`（1 次限流事件即触发）
- `_rate_limit_circuit_window_seconds=30`
- `_rate_limit_circuit_open_seconds=30`
- `_record_rate_limit_event()`: 滑动窗口内计数，超阈值则打开断路器
- `_reset_rate_limit_circuit()`: 成功后重置

### C3: Typing Ticket 自动刷新

`_ensure_typing_ticket(chat_id)`:
- 先查 `_typing_cache`，命中直接返回
- 过期则调用 `getconfig` 获取新 `typing_ticket`
- `send_typing()` / `stop_typing()` 改用此方法

### C4: `supports_code_blocks = True`

类级别常量声明，表示微信渲染代码块。影响上游 `format_message` 行为。

---

## 第三部分：逐项审查评估

### C1: `_batch_chunks` — 批合并 ⭐⭐⭐⭐

**符合官方规格**：✅ iLink 没有任何"多条消息合并发送"的原生 API。每个 `sendmessage` 调用对应一次 HTTP 请求。我们要减少请求数，只能在业务层合并文本内容。

**实现评价**：
- ✅ `_batch_chunks()` 算法正确：用 `\n\n` 拼接保持了 Markdown 块分割，对单条超长 chunk 正确处理（直接发出），边界条件处理完整
- ⚠️ **默认关闭** 是合理的保守选择，但 `_batch_max_chars=500` 相对于官方上限 2000 比较保守。500 字符的 batc h 意义不大（大多数单条消息已接近此值），建议调高到 1500-1800 以真正减少请求数
- ⚠️ 批合并后的消息丢失了逐条 `context_token` 控制。上游逐条发送每条都有独立 `client_id`。batch 后只有一条消息一个 token。这在逻辑上是正确的（合并后应是同一条消息），但若 agent 生成的工具调用和文本交错出现，batch 可能不慎混合同一逻辑组
- ❌ **配置接口不一致**：新增了 3 个 env var (`WEIXIN_BATCH_MERGE_ENABLED`, `WEIXIN_BATCH_MAX_CHARS`, `WEIXIN_BATCH_INTER_CHUNK_DELAY_SECONDS`)。项目已有 `text_batch_delay_seconds`（上游 debounce 机制），但新配置与上游系统平行而不整合，可能造成混淆
- ✅ 与上游 `text_batch_delay_seconds`（上游的文本去抖 batch，合并快速 burst）是不同的层次，互补不冲突

**建议**：
1. 调高默认 `_batch_max_chars` 至 1500
2. 考虑将 `WEIXIN_BATCH_*` env var 整合到已有的 config schema（`platforms.weixin.extra`）— 目前已在 `extra` 中注册 ✅
3. 文档说明与 `text_batch_delay_seconds` 的关系

### C2: 限流断路器 ⭐⭐⭐

**符合官方规格**：✅ 官方规格确认了 `ret=-2` 是频率限制信号，但 `ret=-2 + "unknown error"` 是会话过期（`_is_stale_session_ret` 正确处理了）。我们的断路器针对真限速做反应，正确。

**实现评价**：
- ✅ `_is_stale_session_ret()` 正确区分了真限速与会话过期（对比 errmsg）
- ✅ 断路器设计合理：滑动窗口计数 → 达到阈值断开 → 冷却期后自动恢复
- ⚠️ **阈值默认值 1** 太敏感。一次限速就断开 30 秒，可能将短暂抖动升级为完全不可用。建议默认 3-5
- ⚠️ 断路器只用于 `_send_text_chunk_locked()` 内的限速场景，但限速可能发生在 `send_typing`、`getuploadurl`、`sendmessage` 的媒体版本等路径。这些路径没有断路器保护，可能导致部分请求失败而另一部分阻塞
- ⚠️ `_send_text_gate` (`asyncio.Lock`) 序列化所有出站文本发送，与上游 `_send_chunk_retries` 和 `_send_chunk_delay_seconds` 的配合未被充分测试。锁在断路器打开时也会阻塞后续请求，但断路器不释放锁，导致队列堆积
- ✅ 每次成功发送后 `_reset_rate_limit_circuit()` 重置，恢复正确

**建议**：
1. 提高默认阈值到 3
2. 将 `_send_text_gate` 与断路器整合为"断路器打开时快速失败，不获取锁"
3. 考虑将断路器范围扩展到其他 API 调用

### C3: Typing Ticket 自动刷新 ⭐⭐⭐⭐⭐

**符合官方规格**：✅ `getconfig` 正是官方用于获取 `typing_ticket` 的 API。ticket 有 TTL（实现中通过 `TypingTicketCache` 管理），过期后需要刷新。我们的做法完全正确。

**实现评价**：
- ✅ `_ensure_typing_ticket()` 逻辑完美：缓存在先 → 命中直接返回 → 过期后调用 `getconfig` → 提取 `typing_ticket` → 回写缓存
- ✅ 使用最新 `context_token` 调用 getconfig，符合协议设计
- ✅ 优雅降级：任何异常返回 `None`，上游 `send_typing`/`stop_typing` 安全跳过
- ✅ `send_typing`/`stop_typing` 的双 path（直接缓存 vs 自动刷新）整合合

**建议**：无。这是三项中最干净的实现。

### C4: `supports_code_blocks = True` ⭐⭐

**符合官方规格**：❌ **推测性假设，未经官方确认**。iLink 没有任何文档说明 WeChat 客户端渲染 Markdown 代码块。WeChat 客户端本身不支持代码块渲染（只有 Telegram、Discord 等原生支持）。此标记可能导致 agent 输出代码块但在微信上渲染为裸文本。

**实现评价**：
- ❌ 没有实际测试依据。设置为 `True` 会让上游 `format_message` 保留 fenced code blocks，但 WeChat 客户端可能不渲染它们
- ⚠️ 即使部分支持，是否支持所有代码块类型（` ```python `, ` ``` `）也未确认

**建议**：
1. 改为 `False` 或删除此行，恢复上游默认行为（code blocks 在 WeChat 上不保证）
2. 或通过配置项可选启用

### C5: `_coerce_bool()` ⭐⭐⭐

虽然主要用于配置解析，但存在重复：上游已有类似逻辑在 `BasePlatformAdapter` 和 `tools_config.py` 中。不过作为纯工具函数，没问题。

---

## 第四部分：总体评分 & 差距分析

| 功能 | 评分 | 状态 |
|------|------|------|
| C3: Typing ticket 刷新 | ⭐⭐⭐⭐⭐ | ✅ 正确，建议保持 |
| C1: 批合并 | ⭐⭐⭐⭐ | ✅ 好功能，调参后可长期使用 |
| C2: 限流断路器 | ⭐⭐⭐ | ⚠️ 可用但阈值太敏感、范围太窄 |
| C4: 代码块支持 | ⭐⭐ | ❌ 假设性，缺乏验证，建议回退 |

**总结**：
- 三项自定义中**两个通过了规格审查**（C1 批合并策略正确但参数可优化，C2 断路器结构合理但默认值太保守）
- **C4 需要立即修正**：`supports_code_blocks = True` 没有官方依据，建议撤回
- 文档需要补充：Context Token 持久化的重要性、与上游 `text_batch_delay_seconds` 的配合、env var 命名规范

是否按上述建议进行调整？

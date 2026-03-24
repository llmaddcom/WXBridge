# 腾讯微信 iLink Bot 官方 SDK 深度参考

> 基于 `@tencent-weixin/openclaw-weixin` v2.0.1（2026-03-22）逆向分析
> 源码路径：`/tmp/openclaw_extract/package/src/`

---

## 目录

1. [入站消息类型（用户→Bot）](#1-入站消息类型用户bot)
2. [消息堆积与并发处理行为](#2-消息堆积与并发处理行为)
3. [出站消息类型（Bot→用户）](#3-出站消息类型bot用户)
4. [媒体处理详细规则](#4-媒体处理详细规则)
5. [CDN 上传完整流程（含关键纠正）](#5-cdn-上传完整流程含关键纠正)
6. [错误处理与重试机制](#6-错误处理与重试机制)
7. [Typing Indicator 规则](#7-typing-indicator-规则)
8. [contextToken 持久化](#8-contexttoken-持久化)
9. [其他业务规则](#9-其他业务规则)

---

## 1. 入站消息类型（用户→Bot）

### 消息类型枚举（`MessageItemType`，`api/types.ts`）

| 值 | 枚举名 | 说明 |
|---|---|---|
| 0 | NONE | 无类型 |
| 1 | TEXT | 文本（含引用回复） |
| 2 | IMAGE | 图片（含缩略图、高清图） |
| 3 | VOICE | 语音（SILK 格式，服务端 STT 转文字） |
| 4 | FILE | 文件附件（任意格式） |
| 5 | VIDEO | 视频 |

### 顶层消息结构（`WeixinMessage`）

```json
{
  "seq": 12345,
  "message_id": 67890,
  "from_user_id": "abc123@im.wechat",
  "to_user_id": "bot456@im.bot",
  "client_id": "uuid-string",
  "create_time_ms": 1740000000000,
  "update_time_ms": 1740000000000,
  "delete_time_ms": null,
  "session_id": "session-string",
  "group_id": null,
  "message_type": 1,
  "message_state": 0,
  "context_token": "opaque-token-string",
  "item_list": [ /* 见下面各子类型 */ ]
}
```

**关键字段说明：**
- `message_type`：`1`=用户消息，`2`=机器人自身消息（**必须跳过**）
- `from_user_id` 格式：`xxx@im.wechat`；bot ID 格式：`xxx@im.bot`
- SDK 以 `endsWith("@im.wechat")` 判断是否为微信用户 ID

### TEXT item（type=1）

```json
{
  "type": 1,
  "create_time_ms": 1740000000000,
  "update_time_ms": 1740000000000,
  "is_completed": true,
  "msg_id": "item-msg-id",
  "text_item": {
    "text": "用户发送的文字内容"
  },
  "ref_msg": {
    "title": "引用摘要",
    "message_item": {
      /* 被引用的完整 MessageItem，结构同本层 */
    }
  }
}
```

**引用消息（`ref_msg`）规则：**
- 仅在用户长按引用其他消息时存在
- 当引用的是媒体消息时，`ref_msg.message_item` 包含完整媒体 item 结构
- 文字内容只取当前 `text_item.text`，**不拼接**引用文字
- 媒体下载优先从主 `item_list` 取，主列表无媒体时**降级检查 `ref_msg.message_item`**

### IMAGE item（type=2）

```json
{
  "type": 2,
  "image_item": {
    "aeskey": "85750d16a3b2c4d5e6f7...",
    "url": "(legacy hex format，不使用)",
    "mid_size": 12345,
    "thumb_size": 2345,
    "thumb_height": 120,
    "thumb_width": 160,
    "hd_size": 56789,
    "media": {
      "encrypt_query_param": "base64-encoded-cdn-param",
      "aes_key": "ODU3NTBk...",
      "encrypt_type": 0
    },
    "thumb_media": {
      "encrypt_query_param": "...",
      "aes_key": "...",
      "encrypt_type": 0
    }
  }
}
```

**AES Key 两种位置的区别：**

| 字段 | 位置 | 编码格式 | 处理方式 |
|---|---|---|---|
| `image_item.aeskey` | 顶层 | 原始 hex 字符串（32 字符） | `Buffer.from(aeskey, "hex")` → 16 字节 |
| `image_item.media.aes_key` | media 子对象 | `base64(hex字符串)` | 先 base64 解码得 32 字节 hex，再 `fromHex()` |

**SDK 优先级**（`media-download.ts`）：
```typescript
const aesKeyBase64 = img.aeskey
  ? Buffer.from(img.aeskey, "hex").toString("base64")  // 优先用顶层 aeskey
  : img.media.aes_key;                                  // 降级用 media.aes_key
```

### VOICE item（type=3）

```json
{
  "type": 3,
  "voice_item": {
    "encode_type": 6,
    "bits_per_sample": 16,
    "sample_rate": 24000,
    "playtime": 3500,
    "text": "语音转文字结果（STT）",
    "media": {
      "encrypt_query_param": "...",
      "aes_key": "..."
    }
  }
}
```

**`encode_type` 枚举：**
- 1=PCM, 2=ADPCM, 3=Feature, 4=Speex, 5=AMR, **6=SILK（微信默认）**, 7=MP3, 8=OGG-Speex

**STT 特殊处理规则（重要）：**
- 有 `voice_item.text`（STT 完成）→ **不下载音频**，直接用 STT 文字作为消息正文
- 无 `voice_item.text` 且 `media.encrypt_query_param` 存在 → 下载 SILK，调用 `silk-wasm` 转 WAV，失败则保留原始 SILK

### FILE item（type=4）

```json
{
  "type": 4,
  "file_item": {
    "file_name": "document.pdf",
    "md5": "hex-md5-string",
    "len": "102400",
    "media": {
      "encrypt_query_param": "...",
      "aes_key": "..."
    }
  }
}
```

> **注意**：`len` 是**字符串类型**，不是数字。

### VIDEO item（type=5）

```json
{
  "type": 5,
  "video_item": {
    "video_size": 1048576,
    "play_length": 15000,
    "video_md5": "hex-md5",
    "thumb_height": 240,
    "thumb_width": 320,
    "thumb_size": 8192,
    "media": {
      "encrypt_query_param": "...",
      "aes_key": "..."
    },
    "thumb_media": {
      "encrypt_query_param": "...",
      "aes_key": "..."
    }
  }
}
```

### `parseAesKey` 三种格式（`pic-decrypt.ts`）

```typescript
function parseAesKey(aesKeyBase64: string): Buffer {
  const decoded = Buffer.from(aesKeyBase64, "base64");
  if (decoded.length === 16) return decoded;                   // 格式1：base64(16字节原始)
  if (decoded.length === 32 && isHex(decoded)) {
    return Buffer.from(decoded.toString("ascii"), "hex");      // 格式2：base64(32字符hex)
  }
  throw new Error("invalid aes key");
}
```

| 格式 | 判断条件 | 适用场景 |
|---|---|---|
| `base64(原始16字节)` | base64解码后长度=16 | 文件/语音/视频的 `media.aes_key` |
| `base64(32字符hex字符串)` | base64解码后长度=32且全hex | 图片的 `media.aes_key` |
| 顶层 hex（不经过parseAesKey） | `image_item.aeskey` 直接hex解码 | 仅图片顶层字段 |

---

## 2. 消息堆积与并发处理行为

### 官方 SDK：严格串行

```typescript
// monitor.ts 主循环（简化）
while (!aborted) {
  const resp = await getUpdates(...)         // 长轮询，服务端35s超时，客户端40s
  for (const msg of resp.msgs) {
    await processOneMessage(msg, ...)        // 完全阻塞，等待 AI + 发送全部完成
  }
  // 动态调整下次轮询超时
  if (resp.longpolling_timeout_ms) timeout = resp.longpolling_timeout_ms
}
```

**行为特征：**

| 特征 | 行为 |
|---|---|
| 并发处理 | ❌ 无，严格串行 |
| 防抖/合并 | ❌ 无，每条独立触发 AI |
| 同批次顺序 | ✅ 按 `msgs[]` 数组顺序 |
| 跨批次队列 | ❌ 无，AI 处理期间新消息等下次 `getUpdates` |
| 单用户隔离 | ❌ 无，不同用户消息也串行 |

**与 WXBridge 设计对比：**

| | 官方 SDK | WXBridge |
|---|---|---|
| 消息处理 | 串行 `await` | 每条独立 `asyncio.Task`（并发） |
| 优点 | 顺序保证，资源可控 | 不被慢速 AI 阻塞 |
| 缺点 | 慢 AI 阻塞所有后续消息 | 消息乱序风险 |

---

## 3. 出站消息类型（Bot→用户）

### 文本消息

```json
{
  "msg": {
    "from_user_id": "",
    "to_user_id": "abc123@im.wechat",
    "client_id": "openclaw-weixin-<uuid>",
    "message_type": 2,
    "message_state": 2,
    "context_token": "原样回传的 token",
    "item_list": [
      { "type": 1, "text_item": { "text": "回复内容" } }
    ]
  },
  "base_info": { "channel_version": "2.0.1" }
}
```

### 图片消息（出站，媒体嵌套在 `media` 子对象）

```json
{
  "msg": {
    "to_user_id": "...",
    "client_id": "...",
    "message_type": 2,
    "message_state": 2,
    "context_token": "...",
    "item_list": [{
      "type": 2,
      "image_item": {
        "media": {
          "encrypt_query_param": "来自CDN PUT响应头 x-encrypted-param",
          "aes_key": "base64(aeskey_hex_bytes)",
          "encrypt_type": 1
        },
        "mid_size": 12345
      }
    }]
  },
  "base_info": { "channel_version": "2.0.1" }
}
```

### 视频消息（出站）

```json
{
  "item_list": [{
    "type": 5,
    "video_item": {
      "media": {
        "encrypt_query_param": "...",
        "aes_key": "base64(aeskey_hex_bytes)",
        "encrypt_type": 1
      },
      "video_size": 1048576
    }
  }]
}
```

### 文件消息（出站）

```json
{
  "item_list": [{
    "type": 4,
    "file_item": {
      "media": {
        "encrypt_query_param": "...",
        "aes_key": "base64(aeskey_hex_bytes)",
        "encrypt_type": 1
      },
      "file_name": "report.pdf",
      "len": "102400"
    }
  }]
}
```

### MIME 路由规则（`send-media.ts`）

| MIME 前缀 | 上传函数 | 发送函数 | item type |
|---|---|---|---|
| `video/*` | `uploadVideoToWeixin` | `sendVideoMessageWeixin` | 5 |
| `image/*` | `uploadFileToWeixin` | `sendImageMessageWeixin` | 2 |
| 其他 | `uploadFileAttachmentToWeixin` | `sendFileMessageWeixin` | 4 |

### 媒体来源三种形式

- 本地绝对路径（直接读取）
- `file://` URI
- `http(s)://` URL（先下载到临时目录 `weixin/media/outbound-temp`，再上传）

### 图片 Caption 发送规则

发送带 caption 的图片时，**两条独立请求**：
1. 先发文字消息（caption）
2. 再发图片消息

### 文本处理规则

- **分块限制**：4000 字符/块（`textChunkLimit: 4000`）
- **Markdown 转纯文本**（`markdownToPlainText`）：
  - 代码块：保留代码内容，去围栏标记
  - 图片 `![...](...)`：完全删除
  - 链接 `[text](url)`：保留 `text`
  - 表格：转为空格分隔文本
  - 其余 Markdown 标记：`stripMarkdown` 处理
- **流式回复**：合并参数 `minChars: 200`，`idleMs: 3000ms`（每 200 字或空闲 3 秒发一块）

---

## 4. 媒体处理详细规则

### 入站媒体下载优先级

一条消息的 `item_list` 含多种媒体时，**只取第一个下载**：

```
IMAGE (2) > VIDEO (5) > FILE (4) > VOICE 无STT文字时 (3)
```

主 `item_list` 无媒体时，**检查 `ref_msg.message_item`** 中的媒体作为降级。

### 媒体大小限制

- 单个媒体文件最大 **100 MB**（`WEIXIN_MEDIA_MAX_BYTES = 100 * 1024 * 1024`）

### 媒体下载失败处理

下载/解密失败时：
- 记录日志
- `mediaOpts` 保持为空
- **继续走文本处理流程，不中断消息**

---

## 5. CDN 上传完整流程（含关键纠正）

### 完整上传流程

```
1. 生成随机 16 字节 AES 密钥（Buffer）

2. AES-128-ECB + PKCS7 加密原始文件字节

3. POST /ilink/bot/getuploadurl
   请求体：{
     filekey: "<random-uuid>",
     media_type: 1,          // 1=图片, 2=视频, 4=文件
     rawsize: <原始文件大小>,
     rawfilemd5: "<原始文件MD5 hex>",
     filesize: <加密后文件大小>,
     aeskey: "<aeskey.toString('hex')>"  // ⚠️ 传 hex 字符串，不是 base64
   }
   响应体：{
     upload_url: "<PUT 上传地址>",
     encrypt_query_param: "<上传用参数（不是下载用参数！）>"
   }

4. PUT <upload_url>
   Body: 加密后的文件字节
   ⚠️ 响应头 x-encrypted-param 即为出站消息用的 encrypt_query_param

5. 构造 sendmessage item：
   encrypt_query_param = response.headers["x-encrypted-param"]  // 来自 CDN PUT 响应头
   aes_key = Buffer.from(aeskey).toString("base64")             // 转 base64 传给微信
```

### ⚠️ 关键纠正：`encrypt_query_param` 来源

**错误理解**：`encrypt_query_param` 来自 `getuploadurl` 响应体

**正确行为**：`encrypt_query_param`（用于出站消息 / 接收方下载）来自 **CDN PUT 请求的响应头 `x-encrypted-param`**

```typescript
// cdn-upload.ts
const uploadResp = await fetch(uploadUrl, { method: "PUT", body: encryptedBuffer })
const downloadParam = uploadResp.headers.get("x-encrypted-param")  // ← 这里！
```

`getuploadurl` 响应体中的 `encrypt_query_param` 是**上传参数**（用于构造 PUT URL），不是出站消息的下载参数。

### CDN 上传 URL 构造

```typescript
// cdn-url.ts
`${cdnBaseUrl}/upload?encrypted_query_param=${encodeURIComponent(uploadParam)}&filekey=${encodeURIComponent(filekey)}`
```

### CDN 上传重试

- 最多 **3 次**（`UPLOAD_MAX_RETRIES = 3`）
- 5xx → 重试
- 4xx → 立即终止

---

## 6. 错误处理与重试机制

### errcode=-14（Token 过期）

**官方 SDK 行为**：暂停该账号 **整整 1 小时**（`sleep(3600_000)`），不是停止，1 小时后自动恢复轮询。

> 注意：这与"需要重新登录"是不同的策略——官方 SDK 选择等待 token 自动刷新。

### 普通错误重试策略

```
consecutiveFailures++
if consecutiveFailures < MAX_CONSECUTIVE_FAILURES(3):
    等待 2 秒（RETRY_DELAY_MS）重试
else:
    等待 30 秒（BACKOFF_DELAY_MS）退避
    consecutiveFailures = 0
```

### 发送失败通知

发送失败时向用户发送错误通知消息（fire-and-forget，不抛出异常）：

| 错误类型 | 通知内容 |
|---|---|
| 媒体下载失败 | "媒体文件下载失败，请检查链接是否可访问" |
| CDN 上传失败 | "媒体文件上传失败，请稍后重试" |
| 其他 | "消息发送失败：{错误内容}" |

---

## 7. Typing Indicator 规则

- 每条消息处理前从 `WeixinConfigManager` 获取 `typing_ticket`
  - 每个用户的 ticket 缓存最多 **24 小时**，TTL 随机化防止集中刷新
- AI 处理超过 **5 秒** → 每 5 秒续发 typing 状态（`keepaliveIntervalMs: 5000`）
- AI 回复完成 → 发送 `TypingStatus.CANCEL`

---

## 8. contextToken 持久化

- **双写**：内存 Map + 磁盘文件（`{accountId}.context-tokens.json`）
- **恢复**：服务重启后调用 `restoreContextTokens` 从磁盘自动恢复
- **缺失处理**：无 contextToken 时记录警告日志，仍然发送消息（不中断）

---

## 9. 其他业务规则

### 斜杠指令系统

文本消息以 `/` 开头时，**不走 AI 管道**，直接处理：

| 指令 | 行为 |
|---|---|
| `/echo <message>` | 原样回复 + 通道耗时统计 |
| `/toggle-debug` | 开关 debug 模式（持久化到磁盘） |
| 其他 `/xxx` | `handled=false`，继续走 AI 管道 |

### Debug 模式

启用后每次 AI 回复后追加全链路耗时报告（作为微信消息发给用户）：
- 平台→插件延迟
- 媒体下载耗时
- AI 生成耗时
- 总耗时
- 事件时间戳

### 多账号支持

- 多 Weixin bot 账号并行运行，按 `accountId` 隔离
- 出站消息 accountId 解析策略：
  1. 精确匹配
  2. 通过 contextToken store 查找哪个账号有该用户会话（唯一匹配时自动选择）
  3. 多个匹配时报错，要求显式指定

### getupdates 动态超时

服务端可在响应中返回 `longpolling_timeout_ms` 动态调整客户端下次轮询等待时间，SDK 自动应用。

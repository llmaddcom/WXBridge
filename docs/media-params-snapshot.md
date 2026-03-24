# 媒体上传/下载参数快照

**记录日期**：2026-03-24
**对应官方 SDK 版本**：`@tencent-weixin/openclaw-weixin` v2.0.1
**用途**：在协议修复前记录各接口的参数现状，供后续与官方 API 变化对比时参考。

---

## 1. `channel_version` 版本号

| 位置 | 修复前 | 修复后 | 官方 SDK |
|------|--------|--------|---------|
| `ilink_client.py:28` `CHANNEL_VERSION` 常量 | `"1.0.2"` | `"2.0.1"` | `"2.0.1"` |

此字段出现在所有请求体的 `base_info` 对象中：
```json
{ "base_info": { "channel_version": "2.0.1" } }
```

---

## 2. `getuploadurl` 请求体字段

**接口路径**：`POST /ilink/bot/getuploadurl`

| 字段 | 类型 | 说明 | 修复前 | 修复后 |
|------|------|------|--------|--------|
| `filekey` | string | UUID，本次上传唯一标识 | ✅ 正确 | ✅ 不变 |
| `media_type` | int | 2=图片, 3=语音, 4=文件, 5=视频 | ✅ 正确 | ✅ 不变 |
| `rawsize` | int | 未加密原始字节数 | ✅ 正确 | ✅ 不变 |
| `rawfilemd5` | string | 未加密数据的 hex MD5 | ✅ 正确 | ✅ 不变 |
| `filesize` | int | 加密后字节数 | ✅ 正确 | ✅ 不变 |
| `aeskey` | string | AES-128 密钥编码 | ❌ Base64 字符串 | ✅ hex 字符串（`key.hex()`） |
| `base_info` | object | 含 `channel_version` | ✅ 正确 | ✅ 不变 |

**官方 SDK 参考**（`cdn-upload.ts`）：
```typescript
aeskey: aeskey.toString("hex")   // Buffer → hex 字符串
```

---

## 3. CDN 上传（PUT）与 `encrypt_query_param` 来源

**官方 SDK 参考**（`cdn-upload.ts`）：
```typescript
const uploadResp = await fetch(uploadUrl, { method: "PUT", body: encryptedData })
const downloadParam = uploadResp.headers.get("x-encrypted-param")
```

| 参数 | 修复前来源 | 修复后来源 |
|------|-----------|-----------|
| `encrypt_query_param` | `getuploadurl` 响应体 `upload_param.encrypt_query_param` | CDN PUT 响应头 `x-encrypted-param`（响应体作降级） |

**修复前**（`ilink_client.py:176`）：
```python
encrypt_query_param = upload_param.get("encrypt_query_param", "")
```

**修复后**：
```python
encrypt_query_param = await upload_to_cdn(client, upload_url, encrypted)  # 返回响应头值
if not encrypt_query_param:
    encrypt_query_param = upload_param.get("encrypt_query_param", "")     # 降级
```

---

## 4. `sendmessage` 出站媒体 item 结构

出站媒体 item 的 CDN 参数嵌套在 `media` 子对象内（与入站结构一致）：

```json
{
  "type": 2,
  "image_item": {
    "media": {
      "encrypt_query_param": "<CDN PUT 响应头 x-encrypted-param>",
      "aes_key": "<base64(aes_key_bytes)>",
      "encrypt_type": 1
    },
    "mid_size": 12345
  }
}
```

**修复前**：item payload 直接存 `encrypt_query_param` 和 `aes_key`，未嵌套到 `media` 子对象。
**修复后**：嵌套到 `media` 子对象，并加入 `encrypt_type: 1`，与入站/官方 SDK 结构一致。

---

## 5. 入站图片 `aeskey` 优先级与编码格式

**官方 SDK 参考**（`media-download.ts`）：
```typescript
const aesKeyBase64 = img.aeskey
  ? Buffer.from(img.aeskey, "hex").toString("base64")  // 优先：顶层 hex → base64
  : img.media.aes_key;                                  // 降级：media.aes_key（已是 base64）
```

| 字段路径 | 编码格式 | 修复前优先级 | 修复后优先级 |
|---------|---------|------------|------------|
| `image_item.aeskey` | hex 字符串（如 `85750d16...`） | ❌ 降级 | ✅ 优先 |
| `image_item.media.aes_key` | base64(hex string) | ✅ 优先 | 降级 |
| `image_item.aes_key`（顶层，非官方字段）| 未知 | 末位降级 | 移除 |

处理逻辑（修复后）：
```python
top_aeskey_hex = img.get("aeskey")   # 顶层 hex 字符串
if top_aeskey_hex:
    aes_key = base64.b64encode(bytes.fromhex(top_aeskey_hex)).decode()
else:
    aes_key = media.get("aes_key")   # media.aes_key（base64(hex_string)）
```

---

## 6. CDN 下载 URL 构造

**接口**：`GET {CDN_BASE_URL}/download?encrypted_query_param={url_encoded}`

**CDN_BASE_URL**：`https://novac2c.cdn.weixin.qq.com/c2c`（`media.py:16`，正确）

**官方 SDK 参考**（`cdn-url.ts`）：
```typescript
`${cdnBaseUrl}/download?encrypted_query_param=${encodeURIComponent(encryptedQueryParam)}`
```

**当前实现**（`ilink_client.py:336`）：
```python
url = f"{CDN_BASE_URL}/download?encrypted_query_param={urllib.parse.quote(encrypt_query_param)}"
```
✅ 与官方 SDK 一致，无需修改。

---

## 7. AES key 解码（`aes_key_from_b64`）

支持两种格式（`media.py:42–62`），与官方 SDK `pic-decrypt.ts parseAesKey()` 一致：

| 格式 | 判断条件 | 处理方式 |
|------|---------|---------|
| `base64(原始 16 字节)` | decoded 长度 == 16 | 直接使用 |
| `base64(32 字符 hex 字符串)` | decoded 长度 == 32 且全 hex 字符 | `bytes.fromhex()` 再解码 |

✅ 实现正确，无需修改。

---

## 8. 语音消息（type=3）

| 场景 | 修复前 | 修复后 |
|------|--------|--------|
| `voice_item.text` 有值（STT 成功） | ✅ 直接取文字 | ✅ 不变 |
| `voice_item.text` 为空（STT 失败） | ❌ 丢弃消息 | ✅ 下载 SILK 音频到 `media_bytes` |

---

## 变更文件汇总

| 文件 | 变更内容 |
|------|---------|
| `wxbridge/ilink_client.py` | Fix 1: `CHANNEL_VERSION` → `"2.0.1"`；Fix 2: `aeskey` 传 hex；Fix 3: `upload_to_cdn` 返回响应头；Fix 4（出站）: item 嵌套 `media` 子对象 |
| `wxbridge/media.py` | 新增 `aes_key_to_hex()` 辅助函数 |
| `wxbridge/models.py` | Fix 4（入站）: 图片 `aeskey` 优先级修正 |
| `wxbridge/bridge.py` | Feature 5: Typing Indicator；Feature 6: 语音 SILK 下载；Feature 8: 超长文本分段 |

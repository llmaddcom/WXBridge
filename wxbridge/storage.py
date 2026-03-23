"""
WXBridge 存储后端

定义可插拔存储协议及两种实现：
- DictStorage：纯内存，用于测试和演示（无外部依赖）
- RedisStorage：生产环境，需安装 redis[asyncio]
"""
from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

# Redis key 常量
WEIXIN_BOT_TOKEN = "weixin:bot_token"
WEIXIN_BOT_ID = "weixin:bot_id"
WEIXIN_BASE_URL = "weixin:base_url"
WEIXIN_CURSOR = "weixin:cursor"
WEIXIN_LOGIN_QR_TOKEN = "weixin:login:qrcode_token"
WEIXIN_LOGIN_QR_IMG = "weixin:login:qrcode_img"
WEIXIN_LOGIN_STATUS = "weixin:login:status"


@runtime_checkable
class Storage(Protocol):
    """存储后端协议，支持简单 KV 存储和可选 TTL"""

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl: int | None = None) -> None: ...

    async def delete(self, *keys: str) -> None: ...

    async def expire(self, key: str, ttl: int) -> None: ...


class DictStorage:
    """
    纯内存存储，用于测试和无 Redis 场景。

    TTL 通过 (value, expire_at) 元组实现，get 时惰性检查是否过期。
    """

    def __init__(self) -> None:
        # key → (value, expire_at_monotonic | None)
        self._store: dict[str, tuple[str, float | None]] = {}

    async def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expire_at = entry
        if expire_at is not None and time.monotonic() > expire_at:
            del self._store[key]
            return None
        return value

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        expire_at = time.monotonic() + ttl if ttl is not None else None
        self._store[key] = (value, expire_at)

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self._store.pop(key, None)

    async def expire(self, key: str, ttl: int) -> None:
        entry = self._store.get(key)
        if entry is not None:
            value, _ = entry
            self._store[key] = (value, time.monotonic() + ttl)


class RedisStorage:
    """
    Redis 存储后端（生产环境）。

    需安装：pip install "wxbridge[redis]" 或 pip install redis[asyncio]
    """

    def __init__(self, redis_url: str = "redis://localhost") -> None:
        try:
            import redis.asyncio as aioredis
        except ImportError as exc:
            raise ImportError(
                "RedisStorage requires redis[asyncio]. "
                "Install with: pip install 'wxbridge[redis]'"
            ) from exc
        self._redis = aioredis.from_url(redis_url, decode_responses=True)

    async def get(self, key: str) -> str | None:
        return await self._redis.get(key)  # type: ignore[return-value]

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        if ttl is not None:
            await self._redis.setex(key, ttl, value)
        else:
            await self._redis.set(key, value)

    async def delete(self, *keys: str) -> None:
        if keys:
            await self._redis.delete(*keys)

    async def expire(self, key: str, ttl: int) -> None:
        await self._redis.expire(key, ttl)

    async def close(self) -> None:
        await self._redis.aclose()

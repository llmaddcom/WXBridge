"""
Claude (Anthropic) 适配器示例

演示如何接入 Anthropic Claude API，实现多轮对话（每个微信用户独立对话历史）。

依赖：pip install anthropic

用法：
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/claude_adapter.py
"""
import asyncio
import logging
import os

from wxbridge import AIAdapter, WeixinBridge, WeixinMessage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class ClaudeAdapter(AIAdapter):
    """
    Anthropic Claude 适配器

    以 from_user_id 为 key 维护每用户独立的对话历史。
    注意：历史存储在内存中，服务重启后清空。如需持久化，请将 _histories 接入数据库。
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-opus-4-6",
        system_prompt: str = "你是一个友好的 AI 助手。",
        max_history: int = 20,
    ) -> None:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:
            raise ImportError("ClaudeAdapter 需要安装 anthropic：pip install anthropic") from exc

        self._client = AsyncAnthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])
        self._model = model
        self._system_prompt = system_prompt
        self._max_history = max_history
        # from_user_id → [{role, content}, ...]
        self._histories: dict[str, list[dict[str, str]]] = {}

    async def reply(self, message: WeixinMessage) -> str:
        uid = message.from_user_id
        history = self._histories.setdefault(uid, [])

        # 超出最大历史长度时截断（保留最新的 max_history 条）
        if len(history) >= self._max_history:
            history[:] = history[-self._max_history + 1:]

        history.append({"role": "user", "content": message.text or ""})

        async with self._client.messages.stream(
            model=self._model,
            max_tokens=4096,
            system=self._system_prompt,
            messages=history,  # type: ignore[arg-type]
        ) as stream:
            response = await stream.get_final_message()

        answer = next(
            (block.text for block in response.content if block.type == "text"), ""
        )
        history.append({"role": "assistant", "content": answer})
        return answer

    async def on_new_session(self, from_user_id: str) -> None:
        """会话超时后清空对话历史，让 Claude 以全新状态开始。"""
        self._histories.pop(from_user_id, None)
        logging.getLogger(__name__).info("New session for user %s, history cleared", from_user_id)


async def main() -> None:
    bridge = WeixinBridge(
        adapter=ClaudeAdapter(model="claude-opus-4-6"),
        redis_url="redis://localhost",
    )

    token_info = await bridge.auth.load_token()
    if not token_info:
        print("尚未登录，请先扫码登录。运行以下命令完成登录：")
        print("  python examples/echo_adapter.py")
        return

    await bridge.start()
    print("WXBridge Claude 适配器已启动，按 Ctrl+C 停止")

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await bridge.stop()


if __name__ == "__main__":
    asyncio.run(main())

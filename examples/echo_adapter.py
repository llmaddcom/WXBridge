"""
Echo 适配器示例

最简单的调试适配器：将用户消息原样回显。
用于验证 WXBridge 接入是否正常工作。

用法：
    # 先确保 Redis 已运行，并完成微信扫码登录
    python examples/echo_adapter.py
"""
import asyncio
import logging

from wxbridge import AIAdapter, WeixinBridge, WeixinMessage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class EchoAdapter(AIAdapter):
    """将用户消息原样回显"""

    async def reply(self, message: WeixinMessage) -> str:
        return f"[Echo] {message.text}"


async def main() -> None:
    bridge = WeixinBridge(
        adapter=EchoAdapter(),
        redis_url="redis://localhost",
    )

    # 如果尚未登录，先发起登录流程
    token_info = await bridge.auth.load_token()
    if not token_info:
        print("尚未登录，正在申请二维码...")
        qrcode_token, qrcode_img = await bridge.auth.start_login()
        print(f"请扫描二维码（token={qrcode_token}）")
        print(f"二维码图片数据（前 100 字符）: {qrcode_img[:100]}")
        status = await bridge.auth.poll_login()
        if status != "confirmed":
            print(f"登录失败: {status}")
            return
        print("登录成功！")

    await bridge.start()
    print("WXBridge Echo 已启动，按 Ctrl+C 停止")

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await bridge.stop()
        print("已停止")


if __name__ == "__main__":
    asyncio.run(main())

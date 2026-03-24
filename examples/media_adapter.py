"""
WXBridge 媒体适配器示例

演示如何：
  1. 接收并处理图片/文件消息（media_bytes 自动填充）
  2. 发送图片/文件回复（Reply.image / Reply.file）
  3. 混合文本 + 媒体回复

运行前提：
  pip install 'wxbridge[media]'   # 安装 cryptography 依赖
  # 确保 Redis 可用，并已完成微信扫码登录
"""
from __future__ import annotations

import asyncio
import logging

from wxbridge import AIAdapter, Reply, WeixinBridge, WeixinMessage, configure_logging
from wxbridge.models import AdapterReply


class ImageEchoAdapter(AIAdapter):
    """
    图片/文件 Echo 适配器：
    - 收到图片 → 原图回传
    - 收到文件 → 文件回传（带原始文件名）
    - 收到文本 → 文本回复
    - 收到媒体但下载失败 → 提示失败
    """

    async def reply(self, message: WeixinMessage) -> AdapterReply:
        # 处理媒体消息（auto_download_media=True 时 media_bytes 已填充）
        for item in message.media_items:
            if item.media_bytes is None:
                # 下载失败（网络错误等）
                return Reply.text("媒体文件下载失败，无法处理")

            if item.type == 2:  # 图片
                return Reply.image(item.media_bytes)

            elif item.type == 4:  # 文件
                fname = item.filename or "attachment"
                return Reply.file(item.media_bytes, filename=fname)

            elif item.type == 5:  # 视频
                fname = item.filename or "video.mp4"
                return Reply.video(item.media_bytes, filename=fname)

        # 纯文本消息
        text = message.text or ""
        return Reply.text(f"你说：{text}")

    async def on_new_session(self, from_user_id: str) -> None:
        logging.getLogger(__name__).info("新会话开始 | user=%s", from_user_id)


async def main() -> None:
    configure_logging(logging.DEBUG)

    bridge = WeixinBridge(
        adapter=ImageEchoAdapter(),
        redis_url="redis://localhost",
        auto_download_media=True,   # 自动下载入站媒体
    )

    # 发起登录（首次运行需扫码）
    qrcode_token, qrcode_img = await bridge.auth.start_login()
    print(f"请扫描二维码登录（token: {qrcode_token[:8]}...）")
    # qrcode_img 是 base64 编码的 PNG，可保存或渲染到终端

    status = await bridge.auth.poll_login()
    if status != "confirmed":
        print(f"登录失败：{status}")
        return

    print("登录成功！启动消息桥接...")
    await bridge.start()

    try:
        # 持续运行直到手动中断
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\n停止中...")
        await bridge.stop()


if __name__ == "__main__":
    asyncio.run(main())

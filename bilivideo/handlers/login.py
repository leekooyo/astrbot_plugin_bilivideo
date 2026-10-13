"""`/B站登录` and `/B站登出` handlers."""

from __future__ import annotations

import contextlib
import logging
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from ..auth.qrlogin import LoginStatus
from ..services import BiliVideoServices
from ._utils import require_access

logger = logging.getLogger(__name__)

# Lazy import (test environments lack AstrBot)
try:
    from astrbot.api.message_components import Image, Plain  # type: ignore[import]
except Exception:  # pragma: no cover
    Image = Plain = None  # type: ignore[assignment]


@require_access
async def handle_login(services: BiliVideoServices, event: object) -> AsyncIterator[object]:
    if services.is_logged_in():
        yield event.plain_result("B站已登录。如需更换账号，请先发送 /B站登出。")  # type: ignore[attr-defined]
        return

    yield event.plain_result("正在生成 B站登录二维码，请稍候。")  # type: ignore[attr-defined]

    qr = await services.qrlogin.generate()
    if qr is None:
        yield event.plain_result("暂时无法生成登录二维码，请稍后重试。")  # type: ignore[attr-defined]
        return

    qr_path = Path(services.data_dir) / f"login_qr_{uuid.uuid4().hex[:8]}.png"
    try:
        import segno

        segno.make(qr.url).save(str(qr_path), scale=10, border=4)
    except ImportError:
        yield event.plain_result("二维码功能缺少所需组件，请联系机器人管理员安装 segno。")  # type: ignore[attr-defined]
        return
    except Exception:
        logger.exception("生成 B站登录二维码图片失败")
        yield event.plain_result("登录二维码生成失败，请稍后重试；若持续失败，请联系机器人管理员。")  # type: ignore[attr-defined]
        return

    chain = [
        Plain("请使用 B站 App 扫描下方二维码登录。二维码有效期为 3 分钟。\n"),
        Image.fromFileSystem(str(qr_path)),
    ]
    yield event.chain_result(chain)  # type: ignore[attr-defined]

    result = await services.qrlogin.run_until_complete(qr.key, total_timeout=180)

    if result.status == LoginStatus.SUCCESS and result.cookies:
        services.update_cookies(result.cookies)
        yield event.plain_result("B站登录成功。")  # type: ignore[attr-defined]
    elif result.status == LoginStatus.EXPIRED:
        yield event.plain_result("二维码已过期，请重新发送 /B站登录。")  # type: ignore[attr-defined]
    elif result.status == LoginStatus.TIMEOUT:
        yield event.plain_result("登录超时，请重新发送 /B站登录。")  # type: ignore[attr-defined]
    else:
        yield event.plain_result("登录失败，请重新发送 /B站登录。")  # type: ignore[attr-defined]

    with contextlib.suppress(OSError):
        os.remove(qr_path)


@require_access
async def handle_logout(services: BiliVideoServices, event: object) -> AsyncIterator[object]:
    if not services.is_logged_in():
        yield event.plain_result("当前尚未登录 B站。发送 /B站登录 开始登录。")  # type: ignore[attr-defined]
        return

    services.update_cookies(None)
    yield event.plain_result("已退出 B站登录。")  # type: ignore[attr-defined]

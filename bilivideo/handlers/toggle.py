"""`/识别开关` handler."""

from __future__ import annotations

from collections.abc import AsyncIterator

from ..services import BiliVideoServices


async def handle_toggle_detect(services: BiliVideoServices, event: object) -> AsyncIterator[object]:
    before = "开启" if services.enable_miniapp_detect else "关闭"
    services.enable_miniapp_detect = not services.enable_miniapp_detect
    services.runtime_state.set_bool("enable_miniapp_detect", services.enable_miniapp_detect)
    status = "开启" if services.enable_miniapp_detect else "关闭"
    yield event.plain_result(f"B站链接自动识别：{before} → {status}。\n作用范围：视频总结插件。/识别开关 每次切换状态。")  # type: ignore[attr-defined]

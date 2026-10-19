"""Health-check (`/总结状态`) and cache-clear (`/总结清缓存`) handlers."""

from __future__ import annotations

from collections.abc import AsyncIterator

from .. import __version__
from ..api.endpoints import clear_video_info_cache, video_info_cache_size
from ..api.wbi import clear_wbi_cache
from ..core.logging import get_logger
from ..llm.provider import DisabledLLMProvider
from ..services import BiliVideoServices

logger = get_logger("BiliVideo/Status")


async def handle_status(services: BiliVideoServices, event: object) -> AsyncIterator[object]:
    cfg = services.config
    cookie_state = "✅ 已登录" if services.is_logged_in() else "❌ 未登录"
    if not cfg.enable_auto_push:
        scheduler_state = "关闭(配置)"
    elif services.scheduler is not None and services.scheduler.is_running():
        scheduler_state = "✅ 运行中"
    else:
        scheduler_state = "⚠️ 已启用但未运行"
    targets = await services.subscription_manager.get_push_targets()
    if isinstance(services.llm, DisabledLLMProvider):
        llm_state = "未配置"
    elif cfg.is_openai_compatible:
        llm_state = cfg.llm_model
    else:
        pinned = getattr(services.llm, "provider_id", "") or ""
        llm_state = pinned or "当前聊天模型"

    if cfg.enable_multi_platform:
        multi_state = "已开启（仅 /总结）"
    else:
        multi_state = "已关闭"

    body = (
        "视频总结状态\n"
        f"版本：{__version__}\n"
        f"B 站账号：{cookie_state}\n"
        f"总结模型：{llm_state}\n"
        f"自动识别视频：{'已开启' if services.enable_miniapp_detect else '已关闭'}\n"
        f"其他视频平台：{multi_state}\n"
        f"订阅更新检查：{scheduler_state}，每 {cfg.check_interval_minutes} 分钟检查一次\n"
        f"推送目标：{len(targets)} 个\n"
        f"缓存的视频信息：{video_info_cache_size()} 条\n"
        f"图片输出：{'已开启' if cfg.output_image else '已关闭'}\n"
    )
    yield event.plain_result(body)  # type: ignore[attr-defined]


async def handle_clear_cache(services: BiliVideoServices, event: object) -> AsyncIterator[object]:
    before = video_info_cache_size()
    await clear_video_info_cache()
    await clear_wbi_cache()
    await services.orchestrator.clear_cache()
    yield event.plain_result(  # type: ignore[attr-defined]
        f"🧹 已清除缓存(视频信息 {before} 条 + WBI 密钥 + 总结结果)"
    )

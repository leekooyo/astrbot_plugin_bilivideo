"""`on_all_message` auto-detect handler.

Refactored from a ~300-line nested branch in the original `main.py` into
clear stages:

  1. Reject obvious quoted/replied messages (the user is likely answering
     someone, not requesting a summary).
  2. Build a `MessageContext` so subsequent code knows what the user
     actually typed vs. what was quoted.
  3. Search a strict ordering of sources for a Bilibili URL.
  4. If found, fetch info via the typed API and send back the configured
     fields (cover, uploader, etc.). Optionally trigger a summary.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

from ..access.control import is_allowed
from ..api.endpoints import get_video_info
from ..core.exceptions import BiliVideoError
from ..messaging.builders import format_video_summary_lines
from ..parsing.message_router import (
    looks_like_quoted_message,
    parse_event,
    url_from_card,
    url_from_raw_payload,
)
from ..parsing.triggers import TriggerSet
from ..parsing.url_extractor import (
    extract_bvid,
    extract_long_url,
    extract_short_url,
    is_bilibili_domain,
)
from ..services import BiliVideoServices
from ._render_helper import render_note_components
from ._send_helper import yield_note_response

try:
    from astrbot.api.message_components import Image, Plain  # type: ignore[import]
except Exception:  # pragma: no cover - test stub
    Image = Plain = None  # type: ignore[assignment]


# ─────────────────────── patcher:autodetect-fix (start) ────────────────────
#  本地补丁（非上游代码）。插件升级整目录覆盖会丢 → 用重跑脚本重打。
#  目的：(1) QQ 分享卡片可靠取链（容忍 Python repr 单引号 / JSON 转义斜杠）；
#        (2) 消掉"假 BV 抢跑→-400→静默"；
#        (3) @/引用 不再因"可见文本为空"被误弃；(4) 全链路诊断 + 卡片原文落盘。
#
#  NOTE: 上游 QQDOC_URL_REGEX 只认双引号 JSON（"qqdocurl":"..."），
#        但 AstrBot 的 Json 组件 str() 出来是 Python repr（单引号），
#        且 OneBot 事件里的卡片 json 可能带 \/ 与 \u0026 转义 → 上游必失败。

_TRAILING = "\"',}])> \t\r\n"

# 键名后的引号/冒号写法任意：\"qqdocurl\": / 'qqdocurl': / qqdocurl= / "url":
_QDOC_KEY_RE = re.compile(
    r"""(?:qqdocurl|jumpUrl|jump_url|targetUrl|target_url)["']?\s*[:=]\s*["']?(https?://[^"'\s,}\]]+)""",
    re.IGNORECASE,
)
_URL_KEY_RE = re.compile(
    r"""(?:qqdocurl|jumpUrl|jump_url|targetUrl|target_url|url)["']?\s*[:=]\s*["']?(https?://[^"'\s,}\]]+)""",
    re.IGNORECASE,
)
_SHORT_HINT_RE = re.compile(
    r"https?://(?:b23\.tv|bili2233\.cn|bili22\.cn|bili23\.cn|bili33\.cn)/\S{1,256}",
    re.IGNORECASE,
)
_ANY_URL_RE = re.compile(r"""https?://[^"'\s,}\]]+""", re.IGNORECASE)
# 无协议头的裸 URL（QQ 小程序卡片的 url 字段常是 "m.q.qq.com/a/s/xxx"）
_BARE_URL_KEY_RE = re.compile(
    r"""(?:qqdocurl|jumpUrl|jump_url|targetUrl|target_url|url)["']?\s*[:=]\s*["']?((?:[A-Za-z0-9\-]+\.)+[A-Za-z]{2,}/[^"'\s,}\]]*)""",
    re.IGNORECASE,
)
_CARD_MARK_RE = re.compile(r"qqdocurl|detail_1|appid|miniapp", re.IGNORECASE)


def _short(value: object, limit: int = 160) -> str:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else f"{text[:limit]}...<{len(text)}>"


def _normalize(text: str) -> str:
    """把各种序列化形态归一：转义斜杠/引号、\\uXXXX、HTML 实体。

    这是修复的核心 —— 卡片可能以 JSON 字符串（双引号 + 转义）或
    Python repr（单引号）两种形态出现，必须统一后再做正则。
    """

    if not text:
        return ""
    out = (
        text.replace("\\/", "/")
        .replace('\\"', '"')
        .replace("\\'", "'")
        .replace("&amp;", "&")
        .replace("&#44;", ",")
        .replace("&#91;", "[")
        .replace("&#93;", "]")
    )
    out = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), out)
    return out


def _trim_url(url: str) -> str:
    url = (url or "").strip().strip("<>").rstrip(_TRAILING)
    if url and not url.lower().startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")
    return url


def _blob(ctx) -> str:
    return "\n".join(
        x for x in (ctx.plain_text, ctx.json_card_text, ctx.raw_message) if x
    )


def _card_fulltext(ctx) -> str:
    """卡片相关的全部字符串：组件 str + raw_message + 事件原始 repr，已归一化。"""

    raw_repr = ""
    if ctx.raw_payload is not None:
        try:
            raw_repr = str(ctx.raw_payload)
        except Exception:
            raw_repr = ""
    joined = "\n".join(
        x for x in (ctx.json_card_text, ctx.raw_message, raw_repr) if x
    )
    return _normalize(joined)


def _interesting(ctx) -> bool:
    """只对"像链接/卡片/B站"的消息打日志，避免把群聊日志刷爆。"""

    if ctx.json_card_text:
        return True
    low = f"{ctx.plain_text} {ctx.raw_message}".lower()
    return (
        "http" in low
        or "b23.tv" in low
        or "bilibili" in low
        or "哔哩哔哩" in low
    )


def _bili_hint_anywhere(ctx) -> bool:
    blob = _blob(ctx)
    if TriggerSet.has_bilibili_hint(blob):
        return True
    flat = _normalize(blob)
    return bool(_SHORT_HINT_RE.search(flat) or _QDOC_KEY_RE.search(flat))


def _has_card(ctx) -> bool:
    """判定这条消息里是否夹带了 JSON 卡片。"""

    if ctx.json_card_text:
        return True
    return bool(_CARD_MARK_RE.search(_normalize(ctx.raw_message or "")))


def _qdoc_url_from_card(ctx) -> str | None:
    """从卡片全文里取 B站 直链：qqdocurl 优先，其次任意 B站 URL。"""

    flat = _card_fulltext(ctx)
    for pattern in (_QDOC_KEY_RE, _URL_KEY_RE, _BARE_URL_KEY_RE, _ANY_URL_RE):
        for match in pattern.finditer(flat):
            url = _trim_url(match.group(1) if match.groups() else match.group(0))
            if is_bilibili_domain(url):
                return url
    return None


def _card_candidate_urls(ctx) -> list[str]:
    """列出卡片里所有 URL（去重、跳过图床/静态资源）。"""

    flat = _card_fulltext(ctx)
    found: list[str] = []
    for pattern in (_URL_KEY_RE, _BARE_URL_KEY_RE, _ANY_URL_RE):
        for match in pattern.finditer(flat):
            url = _trim_url(match.group(1) if match.groups() else match.group(0))
            if url and url not in found:
                found.append(url)

    def _keep(u: str) -> bool:
        low = u.lower()
        if any(ext in low for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg")):
            return False
        if "hdslb.com" in low and "/bfs/" in low:
            return False
        return True

    return [u for u in found if _keep(u)][:6]


def _dump_card(ctx, event) -> None:
    """把卡片原文 + 组件结构落盘，便于事后精确诊断（本地补丁，量小）。"""

    try:
        import pathlib
        import time

        data_dir = (
            pathlib.Path(__file__).resolve().parents[4]
            / "plugin_data"
            / "astrbot_plugin_bilivideo"
        )
        data_dir.mkdir(parents=True, exist_ok=True)

        comp_lines: list[str] = []
        try:
            mo = getattr(event, "message_obj", None)
            for i, comp in enumerate(list(getattr(mo, "message", []) or [])):
                comp_lines.append(
                    f"  [{i}] type={getattr(comp, 'type', None)!r} "
                    f"data={getattr(comp, 'data', None)!r}"
                )
        except Exception:
            pass

        payload = "\n".join(
            [
                f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====",
                f"-- message_str --\n{_short(getattr(event, 'message_str', ''), 500)}",
                f"-- components --\n" + ("\n".join(comp_lines) or "  (none)"),
                f"-- json_card_text --\n{_normalize(ctx.json_card_text)[:6000]}",
                f"-- raw_message --\n{_normalize(ctx.raw_message)[:6000]}",
            ]
        )
        with (data_dir / "card_dump.log").open("a", encoding="utf-8") as fh:
            fh.write(payload + "\n")
    except Exception:
        pass


# ─────────────────────── patcher:autodetect-fix (end) ──────────────────────


async def handle_auto_detect(
    services: BiliVideoServices, event: object
) -> AsyncIterator[object]:
    ctx = parse_event(event)

    # ── patcher:autodetect-fix (start) ──
    verbose = _interesting(ctx) or _has_card(ctx)

    def diag(message: str) -> None:
        if verbose:
            try:
                services.logger.info(f"[AutoDetect/diag] {message}")
            except Exception:
                pass

    if _has_card(ctx):
        _dump_card(ctx, event)

    diag(
        f"enter msg_str={_short(getattr(event, 'message_str', ''))!r} "
        f"plain={_short(ctx.plain_text)!r} reply={ctx.is_reply} at={ctx.has_at} "
        f"card={_short(ctx.json_card_text, 140)!r} "
        f"raw_type={type(ctx.raw_payload).__name__} raw_len={len(ctx.raw_message)}"
    )
    # ── patcher:autodetect-fix (end) ──

    if not services.enable_miniapp_detect:
        diag("exit: enable_miniapp_detect=False")
        return

    # Hard intercept: any message with reply markers is *not* a paste.
    if looks_like_quoted_message(ctx.raw_message, ctx.plain_text) or ctx.is_reply:
        triggers = TriggerSet(services.config.trigger_keywords)
        # patcher:autodetect-fix: 卡片消息可见文本为空 → 用整条消息兜底判定
        if not (triggers.matches(ctx.plain_text) or _bili_hint_anywhere(ctx)):
            diag("exit: quoted/reply without trigger or bili hint")
            return
        diag("pass: quoted/reply allowed by trigger/bili hint")

    # Skip command messages
    if ctx.plain_text.strip().startswith("/"):
        diag("exit: command-shaped text")
        return

    if not is_allowed(getattr(event, "unified_msg_origin", ""), config=services.config):
        diag("exit: is_allowed=False")
        return

    # Skip pure @mention messages with no Bilibili hint
    # patcher:autodetect-fix: 同上，用整条消息兜底
    if ctx.has_at and not (
        TriggerSet.has_bilibili_hint(ctx.plain_text) or _bili_hint_anywhere(ctx)
    ):
        diag("exit: has_at without bili hint")
        return

    bvid = await _resolve_bvid(
        services, ctx, allow_full_text=not ctx.is_reply, diag=diag
    )
    diag(f"resolve -> bvid={bvid!r}")
    if not bvid:
        diag("exit: no bvid resolved")
        return

    try:
        info = await get_video_info(services.http_client, bvid)
    except BiliVideoError as exc:
        services.logger.warning(f"video info fetch failed: {exc}")
        diag(f"exit: fetch failed: {exc}")
        return

    chain: list[object] = []
    if services.config.detect_show_cover and info.normalized_pic and Image is not None:
        chain.append(Image.fromURL(info.normalized_pic))
    if Plain is not None:
        text = "\n".join(format_video_summary_lines(info, config=services.config))
        chain.append(Plain(text))

    if chain:
        diag(f"send: bvid={bvid} chain_len={len(chain)}")
        yield event.chain_result(chain)  # type: ignore[attr-defined]

    if services.config.detect_auto_summary:
        yield event.plain_result("⏳ 正在生成视频总结...")  # type: ignore[attr-defined]
        try:
            note = await services.inflight.run(
                bvid, lambda: services.orchestrator.generate(info.url)
            )
        except BiliVideoError as exc:
            yield event.plain_result(exc.user_message)  # type: ignore[attr-defined]
            return
        components = await render_note_components(services, note.markdown)
        async for resp in yield_note_response(services, event, components, video_info=info):
            yield resp


# ──────────────────────────── helpers ──────────────────────────────


async def _resolve_bvid(
    services: BiliVideoServices,
    ctx,
    *,
    allow_full_text: bool,
    diag=None,
) -> str | None:
    """Run the resolution waterfall: card → JSON in raw → text → short URL."""

    def _d(message: str) -> None:
        if diag is not None:
            diag(message)

    # 1. JSON card from raw payload
    bili_url = url_from_raw_payload(ctx.raw_payload)
    _d(f"step1 url_from_raw_payload={_short(bili_url or '')!r}")

    # 1b. patcher:autodetect-fix —— 卡片全文里直接找 qqdocurl / B站 URL
    #     （容忍 Python repr 单引号、JSON 转义斜杠、\u0026）
    if not bili_url:
        bili_url = _qdoc_url_from_card(ctx)
        _d(f"step1b qqdocurl_in_card={_short(bili_url or '')!r}")

    # 2. JSON card stringified into a component（先归一化再交给插件解析器）
    if not bili_url and ctx.json_card_text:
        bili_url = url_from_card(_normalize(ctx.json_card_text))
        _d(f"step2 url_from_card={_short(bili_url or '')!r}")

    # 3. message_str itself may be a JSON object (rare)
    if not bili_url and ctx.raw_message.strip().startswith("{"):
        bili_url = url_from_card(_normalize(ctx.raw_message))
        _d(f"step3 url_from_card(raw_message)={_short(bili_url or '')!r}")

    # 4. text-based extraction
    text_pool = ctx.plain_text
    if allow_full_text and ctx.raw_message:
        text_pool = f"{text_pool} {ctx.raw_message}"

    if bili_url:
        bvid = extract_bvid(bili_url)
        if bvid:
            _d(f"resolve via card bvid={bvid}")
            return bvid
        if is_bilibili_domain(bili_url):
            resolved = await services.http_client.follow_redirect(bili_url)
            _d(f"step4 card redirect -> {_short(resolved or '')!r}")
            if resolved:
                bvid = extract_bvid(resolved)
                if bvid:
                    _d(f"resolve via card redirect bvid={bvid}")
                    return bvid

    # patcher:autodetect-fix: 卡片存在却没直链时，遍历候选 URL 探测
    # （含 QQ 小程序中转页 m.q.qq.com —— 跟一次跳转看是否落到 B站）
    if not bili_url and _has_card(ctx):
        candidates = _card_candidate_urls(ctx)
        _d(f"step4 card candidates={[_short(c, 90) for c in candidates]}")
        for candidate in candidates:
            if is_bilibili_domain(candidate):
                bili_url = candidate
                break
        if not bili_url:
            for candidate in candidates:
                try:
                    resolved = await services.http_client.follow_redirect(candidate)
                except Exception as exc:  # noqa: BLE001
                    _d(f"step4b probe {_short(candidate, 80)!r} EXC {exc}")
                    continue
                _d(
                    f"step4b probe {_short(candidate, 80)!r} -> "
                    f"{_short(resolved or '', 90)!r}"
                )
                if resolved and is_bilibili_domain(resolved):
                    bili_url = resolved
                    break
        if bili_url:
            bvid = extract_bvid(bili_url)
            if bvid:
                _d(f"resolve via card-url bvid={bvid}")
                return bvid
            resolved = await services.http_client.follow_redirect(bili_url)
            if resolved:
                bvid = extract_bvid(resolved)
                if bvid:
                    _d(f"resolve via card-url redirect bvid={bvid}")
                    return bvid

    # patcher:autodetect-fix: BV 只从"用户可见文本"取，
    # 绝不再对整段事件 JSON 跑 extract_bvid（那正是假 BV→-400 的来源）
    bvid = extract_bvid(ctx.plain_text)
    _d(f"step5 extract_bvid(plain_text)={bvid!r}")
    if bvid:
        return bvid

    long_url = extract_long_url(text_pool)
    _d(f"step6 long_url={_short(long_url or '')!r}")
    if long_url:
        bvid = extract_bvid(long_url)
        if bvid:
            return bvid

    short_url = extract_short_url(text_pool)
    _d(f"step7 short_url={_short(short_url or '')!r}")
    if short_url:
        resolved = await services.http_client.follow_redirect(short_url)
        _d(f"step7b short redirect -> {_short(resolved or '')!r}")
        if resolved:
            return extract_bvid(resolved)

    _d("step8 nothing matched")
    return None

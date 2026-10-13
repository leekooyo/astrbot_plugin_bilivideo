#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""[patcher:autodetect-fix] bilivideo「自动识别」本地补丁 · 重跑脚本

本脚本**可放在插件目录内，也可放在插件目录之外**（运行时会自动定位插件）。
⚠️ 插件升级是**整目录覆盖**，会连插件内的副本一起删掉 → **务必另存一份到插件目录之外**：
       C:\Users\81510\.astrbot\data\_patches_archive\bilivideo_2026-10-11\reapply_autodetect_fix.py
   插件升级后，直接跑外部那份即可恢复本补丁（该副本亦已登记进 reapply_all_patches.py 统一编排）。

   定位插件根目录的优先级（取第一个「含目标文件」的候选）：
     ① 环境变量 ASTRBOT_BILIVIDEO_PLUGIN_DIR
     ② 脚本所在目录本身（脚本放在插件根内时）
     ③ 脚本所在目录下的 astrbot_plugin_bilivideo\（脚本放在 plugins\ 下时）
     ④ 脚本父目录下的 astrbot_plugin_bilivideo\
     ⑤ 本机默认路径 C:\Users\81510\.astrbot\data\plugins\astrbot_plugin_bilivideo

为什么要这个补丁
----------------
上游(astrbot_plugin_bilivideo)的 QQ 分享卡片取链对 AstrBot 的事件形态不兼容：
  1) 上游 QQDOC_URL_REGEX 只认**双引号 JSON**；而 AstrBot 的 Json 组件 str() 是
     Python repr（**单引号**），事件里卡片还可能带 \/ 与 \u0026 转义 → 上游取 URL 恒 None。
  2) _resolve_bvid 的 text_pool 含**整段事件 JSON**，且 extract_bvid（无归属校验）
     排在短链解析之前 → 偶发“假 BV”抢跑 → B站 API 返回 -400 → **静默 return、什么都不发**。
  3) @/引用 分支用 plain_text 判定，卡片消息可见文本为空 → 被误判“非 B站”而放弃。
本补丁的修法：
  - 卡片全文归一化（解转义/单双引号统一）后再扫 qqdocurl/jumpUrl/url，卡片 url 无协议头自动补 https://；
  - **BV 只从用户可见文本取**，不再对整段事件 JSON 跑 extract_bvid（拔掉 -400 根源）；
  - @/引用 用整条消息兜底判定；
  - 全链路 [AutoDetect/diag] 诊断日志 + 卡片原文落盘 card_dump.log。

机制：整份还原（payload）
------------------------
脚本内嵌「补丁后的 auto_detect.py」全文（sha256 见 PATCHED_SHA256），--apply 时整份写回目标文件。
相比“锚点式微创补丁”，整份还原**不受上游改锚点影响**；代价是**会覆盖上游对该文件的新改动**，
故当目标文件既不是已知上游原版、也不是本补丁版时，脚本会**拒绝覆盖**（需显式 --force）。

用法
----
    python reapply_autodetect_fix.py                     # 体检（= --status）
    python reapply_autodetect_fix.py --status            # 只读体检
    python reapply_autodetect_fix.py --apply             # 备份 + 写回补丁
    python reapply_autodetect_fix.py --apply --force     # 目标非已知状态时强制覆盖
    python reapply_autodetect_fix.py --rollback          # 还原到最早备份（= 上游原版）
    python reapply_autodetect_fix.py --dump [目标路径]    # 导出内嵌补丁文件，便于人工比对

⚠️ 改 .py 前后都建议先停后端：& "E:\AstrBot\kill-backend-processes.ps1" -InstallDir "E:\AstrBot"
   （改完让用户双击 E:\AstrBot\astrbot-desktop-tauri.exe 生效；只重启 AstrBot 后端不影响 NapCat）
"""
from __future__ import annotations

import argparse
import hashlib
import os
import py_compile
import shutil
import socket
import sys
from datetime import datetime
from pathlib import Path

# ------------------------------------------------------------------ 路径 / 常量
HERE = Path(__file__).resolve().parent                 # 脚本所在目录（可能在插件内，也可能在外）
BACKUP_ROOT = Path(r"C:\Users\81510\.astrbot\data\_config_backups")
BACKUP_PREFIX = "bilivideo_autodetect_"
MARKER = "patcher:autodetect-fix"
PLUGIN_DIR_ENV = "ASTRBOT_BILIVIDEO_PLUGIN_DIR"
DEFAULT_PLUGIN_DIR = Path(r"C:\Users\81510\.astrbot\data\plugins\astrbot_plugin_bilivideo")
_REL_TARGET = Path("bilivideo") / "handlers" / "auto_detect.py"


def _resolve_plugin_dir() -> Path:
    """定位 bilivideo 插件根目录 —— 使脚本在插件内/外都能工作。

    按优先级取第一个「含 bilivideo/handlers/auto_detect.py」的候选；都不含时
    返回本机默认路径（供报错时展示最可能的位置）。
    """

    cands = []
    env = os.environ.get(PLUGIN_DIR_ENV)
    if env:
        cands.append(Path(env))
    cands += [
        HERE,                                        # 脚本就在插件根内
        HERE / "astrbot_plugin_bilivideo",           # 脚本放在 plugins/ 下
        HERE.parent / "astrbot_plugin_bilivideo",    # 脚本放在 plugins/ 子目录下
        DEFAULT_PLUGIN_DIR,                          # 本机规范路径兜底
    ]
    for cand in cands:
        if (cand / _REL_TARGET).exists():
            return cand
    return DEFAULT_PLUGIN_DIR


PLUGIN_DIR = _resolve_plugin_dir()
TARGET = PLUGIN_DIR / _REL_TARGET

# 已知的「上游原版」快照（本次两轮补丁前留的第一份备份）→ rollback 兜底
PRISTINE_BACKUP = BACKUP_ROOT / "bilivideo_diag_20261011-121908" / "auto_detect.py.bak"

# 已知上游原版（打补丁前）→ 用于判定“目标是否还是原版”
PRISTINE_SHA256 = "fb58328e0862a3dc72fdf3f5e04a145b81320da992679bd114270ae9c1d3868a"
# 本脚本内嵌的补丁后版本 → 校验写入结果
PATCHED_SHA256 = "5f6a53f8b0b7c83a3c8ab73c8a6a1fd7e8b7cf11a088a5084e89313cf819d64c"

PORTS = (6185, 6199, 6099)  # 面板 / AstrBot WS / NapCat

_PAYLOAD = r'''"""`on_all_message` auto-detect handler.

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
'''


# ------------------------------------------------------------------ 工具
def _payload_bytes() -> bytes:
    return _PAYLOAD.encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(p: Path) -> bytes:
    return p.read_bytes() if p.exists() else b""


def _write(p: Path, data: bytes) -> None:
    """合约：无 BOM + 纯 LF + 尾换行（与上游 auto_detect.py 同款）。"""
    text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    p.write_bytes(text.encode("utf-8"))


def backend_running() -> list:
    live = []
    for port in PORTS:
        s = socket.socket()
        s.settimeout(0.6)
        try:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                live.append(port)
        finally:
            s.close()
    return live


def _backups() -> list:
    if not BACKUP_ROOT.exists():
        return []
    out = []
    for d in sorted(BACKUP_ROOT.glob(BACKUP_PREFIX + "*")):
        f = d / (TARGET.name + ".bak")
        if f.exists():
            out.append(f)
    return out


# ------------------------------------------------------------------ 体检
def show_status() -> int:
    print("=" * 74)
    print("bilivideo 自动识别补丁 · 体检   [patcher:autodetect-fix]")
    print("=" * 74)
    live = backend_running()
    print(f"后端状态 : {'运行中（端口 ' + ', '.join(map(str, live)) + '）' if live else '未运行'}")
    print(f"目标文件 : {TARGET}")
    if not TARGET.exists():
        print("  结果   : ✘ 目标文件不存在（插件是否已安装 / 目录是否正确？）")
        return 0
    cur = _read(TARGET)
    h = _sha(cur)
    text = cur.decode("utf-8", "replace")
    patched = MARKER in text
    print(f"  状态   : {'✔ 已打补丁' if patched else '✘ 未打补丁'}")
    print(f"  大小   : {len(cur)} B")
    print(f"  sha256 : {h}")
    if h == PATCHED_SHA256:
        print("         = 内嵌补丁版（完全一致）")
    elif h == PRISTINE_SHA256:
        print("         = 已知上游原版")
    else:
        print("         != 内嵌补丁版 且 != 已知上游原版 → 文件已被改动（上游可能已更新）")
    print()
    print("补丁点自检：")
    for needle, label in (
        (MARKER, "补丁标记 " + MARKER),
        ("def _normalize(", "卡片全文归一化"),
        ("def _qdoc_url_from_card(", "卡片直链提取"),
        ("_d(f\"step1b qqdocurl_in_card=", "1b 支线（卡片取链）"),
        ("bvid = extract_bvid(ctx.plain_text)", "BV 只从可见文本取"),
    ):
        print(f"  {'✔' if needle in text else '✘'} {label}")
    return 0


# ------------------------------------------------------------------ 应用
def cmd_apply(force: bool) -> int:
    if not TARGET.exists():
        print(f"[中止] 目标文件不存在：{TARGET}")
        return 2

    cur = _read(TARGET)
    h = _sha(cur)
    payload = _payload_bytes()

    if h == PATCHED_SHA256:
        print("内容无变化（已应用，幂等）")
        return 0

    live = backend_running()
    if live and not force:
        print(f"[中止] 后端正在运行（端口 {', '.join(map(str, live))}）。")
        print('  请先停后端: & "E:\\AstrBot\\kill-backend-processes.ps1" -InstallDir "E:\\AstrBot"')
        print("  或加 --force 强制（改插件源码时后端在跑可能读旧缓存）。")
        return 2

    if h != PRISTINE_SHA256 and not force:
        print("[中止] 目标文件既不是已知上游原版、也不是本补丁版。")
        print("       很可能上游已更新该文件 —— 继续将覆盖上游改动。")
        print("       如确认要用本补丁覆盖，请加 --force。")
        return 2

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = BACKUP_ROOT / f"{BACKUP_PREFIX}{ts}"
    bdir.mkdir(parents=True, exist_ok=True)
    bpath = bdir / (TARGET.name + ".bak")
    shutil.copy2(TARGET, bpath)
    print(f"备份 -> {bpath}")

    _write(TARGET, payload)

    new = _read(TARGET)
    nh = _sha(new)
    ok_hash = nh == PATCHED_SHA256
    try:
        py_compile.compile(str(TARGET), doraise=True)
        ok_compile = True
    except py_compile.PyCompileError as exc:
        ok_compile = False
        print(f"!! py_compile 失败: {exc}")
    print(f"写入完成: {len(new)} B / sha256 {nh}")
    print(f"  {'✔' if ok_hash else '✘'} 哈希校验（期望 {PATCHED_SHA256[:12]}…）")
    print(f"  {'✔' if ok_compile else '✘'} py_compile")
    if ok_hash and ok_compile:
        print("\n✔ 补丁已就位。请重启 AstrBot 后端（让用户双击 astrbot-desktop-tauri.exe）生效。")
        return 0
    print("\n[异常] 请人工确认。")
    return 1


# ------------------------------------------------------------------ 回滚
def cmd_rollback() -> int:
    bs = _backups()
    src = None
    if bs:
        src = bs[0]           # 最早 = 打补丁前
    elif PRISTINE_BACKUP.exists():
        src = PRISTINE_BACKUP  # 回退到已知「上游原版」快照
        print("未找到本补丁的备份目录，改用已知「上游原版」快照。")
    if src is None:
        print("未找到任何可还原的备份：")
        print(f"  本补丁备份  : {BACKUP_ROOT / (BACKUP_PREFIX + '*')}")
        print(f"  上游原版快照: {PRISTINE_BACKUP}")
        return 2
    _write(TARGET, _read(src))
    print(f"已还原 <- {src}")
    print(f"当前大小: {len(_read(TARGET))} B / sha256 {_sha(_read(TARGET))}")
    return 0


# ------------------------------------------------------------------ 导出补丁文件
def cmd_dump(out: str | None) -> int:
    dst = Path(out) if out else (HERE / "auto_detect.patched.py")
    dst.write_bytes(_payload_bytes())
    print(f"已导出内嵌补丁文件 -> {dst}")
    print(f"  {len(_payload_bytes())} B / sha256 {PATCHED_SHA256}")
    return 0


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(description="bilivideo 自动识别补丁重跑器 [patcher:autodetect-fix]")
    ap.add_argument("--status", action="store_true", help="只读体检（默认）")
    ap.add_argument("--apply", action="store_true", help="备份 + 写回补丁")
    ap.add_argument("--rollback", action="store_true", help="还原到最早备份")
    ap.add_argument("--force", action="store_true", help="后端在跑 / 目标非已知状态时也执行")
    ap.add_argument("--dump", nargs="?", const="", default=None, metavar="PATH",
                    help="导出内嵌补丁文件（默认写到插件目录 auto_detect.patched.py）")
    args = ap.parse_args()

    if args.dump is not None:
        return cmd_dump(args.dump or None)
    if args.apply:
        return cmd_apply(args.force)
    if args.rollback:
        return cmd_rollback()
    return show_status()


if __name__ == "__main__":
    raise SystemExit(main())

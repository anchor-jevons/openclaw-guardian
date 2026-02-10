#!/usr/bin/env python3
"""
OpenClaw Guardian - Health Fetcher

Goals:
- Deterministic parsing (no LLM) of OpenClaw logs + cron config.
- Output either JSON (machine) or Markdown (ready-to-post).
- Timezone aligned to the user's configured timezone (default: Asia/Shanghai).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


STATE_DIR = Path.home() / ".openclaw"
CONFIG_FILE = STATE_DIR / "openclaw.json"
LOG_DIR = STATE_DIR / "logs"
GATEWAY_LOG = LOG_DIR / "gateway.log"
ERROR_LOG = LOG_DIR / "gateway.err.log"
WATCHDOG_AUDIT = STATE_DIR / "guardian" / "watchdog-audit.jsonl"
CRON_JOBS = STATE_DIR / "cron" / "jobs.json"


TS_RE = re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)")
PROVIDER_MODEL_RE = re.compile(r"\bprovider=(?P<provider>[\w-]+)\b.*\bmodel=(?P<model>[\w.\-]+)\b")
UNKNOWN_MODEL_RE = re.compile(r"Unknown model:\s*(?P<model>[\w\-./]+)")
MODEL_QUOTED_RE = re.compile(r'Model\s+"(?P<model>[\w\-./]+)"')
NO_API_KEY_RE = re.compile(r'No API key found for provider\s+"(?P<provider>[\w-]+)"')
MODEL_NOT_ALLOWED_RE = re.compile(r'Model\s+"(?P<model>[\w\-./]+)"\s+is not allowed')
RESET_AFTER_RE = re.compile(r"reset after (?P<after>(?:\d+h)?(?:\d+m)?(?:\d+s)?)", re.IGNORECASE)
COOLDOWN_PROVIDER_RE = re.compile(r"\bProvider\s+(?P<provider>[\w-]+)\s+is\s+in\s+cooldown\b", re.IGNORECASE)
CAPACITY_EXHAUSTED_RE = re.compile(r"exhausted your capacity on this model", re.IGNORECASE)
CONTEXT_LIMIT_RE = re.compile(r"(context length|max(?:imum)? tokens|token limit|too many tokens)", re.IGNORECASE)
ALL_MODELS_FAILED_BODY_RE = re.compile(r"all models failed\s*\(\d+\)\s*:\s*(?P<body>.*)$", re.IGNORECASE)
QUOTA_RESET_RE = re.compile(r"quota will reset after (?P<after>(?:\d+h)?(?:\d+m)?(?:\d+s)?)", re.IGNORECASE)
RPM_HINT_RE = re.compile(r"(rpm|requests per minute|too many requests|rate limit exceeded)", re.IGNORECASE)
LANE_AGENT_RE = re.compile(r"\blane=session:agent:(?P<agent>[^:]+):", re.IGNORECASE)
AGENT_DIR_RE = re.compile(r"/\.openclaw/agents/(?P<agent>[^/]+)/", re.IGNORECASE)


def _parse_hms_duration(s: str) -> dt.timedelta | None:
    """
    Parse strings like "14h19m18s", "17m16s", "3h", "30s".
    Returns None if not parseable.
    """
    raw = (s or "").strip().lower()
    if not raw:
        return None
    m = re.fullmatch(r"(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?", raw)
    if not m:
        return None
    h = int(m.group("h") or 0)
    mm = int(m.group("m") or 0)
    ss = int(m.group("s") or 0)
    if h == 0 and mm == 0 and ss == 0:
        return None
    return dt.timedelta(hours=h, minutes=mm, seconds=ss)


def _reset_after_from_text(text: str) -> tuple[dt.timedelta, str] | None:
    m = RESET_AFTER_RE.search(text) or QUOTA_RESET_RE.search(text)
    if not m:
        return None
    raw = (m.group("after") or "").strip()
    td = _parse_hms_duration(raw)
    if not td:
        return None
    return td, raw


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _parse_ts_utc(line: str) -> dt.datetime | None:
    m = TS_RE.search(line)
    if not m:
        return None
    # Example: 2026-02-07T02:28:57.903Z
    raw = m.group("ts").replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(raw)
    except Exception:
        return None


def _resolve_tz(config: dict[str, Any] | None, tz_name: str | None) -> dt.tzinfo:
    if tz_name:
        name = tz_name
    else:
        name = (
            (config or {})
            .get("agents", {})
            .get("defaults", {})
            .get("userTimezone")
            or "Asia/Shanghai"
        )
    if ZoneInfo is None:
        # Fall back to fixed UTC+8 if zoneinfo missing.
        if name == "Asia/Shanghai":
            return dt.timezone(dt.timedelta(hours=8))
        return dt.timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def _fmt_local(ts_utc: dt.datetime, tz: dt.tzinfo, fmt: str = "%H:%M") -> str:
    if ts_utc.tzinfo is None:
        ts_utc = ts_utc.replace(tzinfo=dt.timezone.utc)
    return ts_utc.astimezone(tz).strftime(fmt)


def get_recent_lines(path: Path, since_utc: dt.datetime) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    try:
        with path.open("r") as f:
            for line in f:
                ts = _parse_ts_utc(line)
                if ts is None:
                    continue
                if ts >= since_utc:
                    out.append(line.rstrip("\n"))
    except Exception:
        return []
    return out


@dataclass(frozen=True)
class ModelRef:
    model_id: str  # "provider/model" when possible
    provider: str
    model: str


def _normalize_model_id(provider: str, model: str) -> str:
    return f"{provider}/{model}"


def _extract_provider_model(line: str) -> tuple[str | None, str | None, str | None]:
    """
    Returns (provider, model, model_id) best-effort.
    - If log has provider/model fields, use them.
    - If log has quoted model id, use that (provider/model).
    """
    m = PROVIDER_MODEL_RE.search(line)
    if m:
        provider = m.group("provider")
        model = m.group("model")
        return provider, model, _normalize_model_id(provider, model)

    mq = MODEL_QUOTED_RE.search(line)
    if mq:
        mid = mq.group("model")
        if "/" in mid:
            p, mm = mid.split("/", 1)
            return p, mm, mid
        return None, mid, mid

    return None, None, None


def get_configured_models(config: dict[str, Any]) -> list[ModelRef]:
    models: list[str] = []
    defaults = config.get("agents", {}).get("defaults", {})
    model_conf = defaults.get("model", {}) or {}

    primary = model_conf.get("primary")
    if isinstance(primary, str) and primary.strip():
        models.append(primary.strip())

    fallbacks = model_conf.get("fallbacks", [])
    if isinstance(fallbacks, list):
        for x in fallbacks:
            if isinstance(x, str) and x.strip():
                models.append(x.strip())

    # Explicit allowlist of models (commonly used by OpenClaw)
    models_map = defaults.get("models", {})
    if isinstance(models_map, dict):
        for k in models_map.keys():
            if isinstance(k, str) and k.strip():
                models.append(k.strip())

    # Agent overrides
    for a in config.get("agents", {}).get("list", []) or []:
        if not isinstance(a, dict):
            continue
        m = a.get("model")
        if isinstance(m, str) and m.strip():
            models.append(m.strip())

    # Dedup preserve order
    seen: set[str] = set()
    unique = [x for x in models if not (x in seen or seen.add(x))]

    out: list[ModelRef] = []
    for mid in unique:
        if "/" in mid:
            p, m = mid.split("/", 1)
            out.append(ModelRef(model_id=mid, provider=p, model=m))
        else:
            out.append(ModelRef(model_id=mid, provider="unknown", model=mid))
    return out


def _worse_status(a: str, b: str) -> str:
    # Order: red > yellow > green
    order = {"🔴": 3, "🟡": 2, "🟢": 1}
    ea = a.lstrip()[:1]
    eb = b.lstrip()[:1]
    return a if order.get(ea, 0) >= order.get(eb, 0) else b


def _find_model_refs_in_line(line: str, models: list[ModelRef]) -> list[ModelRef]:
    # Many OpenClaw errors embed full model ids in free text:
    # "All models failed ... google-gemini-cli/gemini-3-pro-preview: ..."
    out: list[ModelRef] = []
    for m in models:
        if m.model_id in line:
            out.append(m)
    return out


def parse_llm_status(
    lines: Iterable[str],
    models: list[ModelRef],
    tz: dt.tzinfo,
    *,
    now_utc: dt.datetime,
    cooldown_sticky_minutes: int = 240,
    rate_limit_sticky_minutes: int = 30,
) -> tuple[list[dict[str, str]], list[str]]:
    # Build a lookup so "provider=x model=y" can resolve to the exact configured model_id.
    by_provider_model: dict[tuple[str, str], ModelRef] = {(m.provider, m.model): m for m in models}

    matrix: dict[str, dict[str, str]] = {}
    for m in models:
        matrix[m.model_id] = {
            "Provider": m.provider,
            "Model": m.model,
            "Status": "🟢 健康",
            "Diagnosis": "状态稳定，就绪中。",
        }

    events: list[str] = []
    last_seen: dict[str, tuple[dt.datetime, str]] = {}  # model_id -> (ts_utc, kind)

    for line in lines:
        # If a line is explicitly about another agent (e.g., buddy), don't let it pollute main-agent health.
        ma = LANE_AGENT_RE.search(line)
        if ma and ma.group("agent").strip().lower() != "main":
            continue
        md = AGENT_DIR_RE.search(line)
        if md and md.group("agent").strip().lower() != "main":
            continue

        ts = _parse_ts_utc(line)
        ts_local = _fmt_local(ts, tz) if ts else "??:??"
        lowered = line.lower()

        def apply(mid: str, status: str, diagnosis: str, event: str | None) -> None:
            if mid not in matrix:
                if "/" in mid:
                    p, mm = mid.split("/", 1)
                else:
                    p, mm = "unknown", mid
                matrix[mid] = {"Provider": p, "Model": mm, "Status": "🟢 健康", "Diagnosis": "状态稳定，就绪中。"}
            matrix[mid]["Status"] = _worse_status(matrix[mid]["Status"], status)
            if diagnosis:
                # Prefer non-generic diagnosis, and update when status is non-green.
                if matrix[mid]["Diagnosis"].startswith("状态稳定") or matrix[mid]["Status"].lstrip()[:1] != "🟢":
                    matrix[mid]["Diagnosis"] = diagnosis
            if event:
                events.append(event)

        def record_last(mid: str, kind: str) -> None:
            if not ts:
                return
            prev = last_seen.get(mid)
            if not prev or ts > prev[0]:
                last_seen[mid] = (ts, kind)

        def apply_incident(
            mid: str,
            *,
            kind: str,
            base_status: str,
            diagnosis: str,
            sticky_until: dt.datetime | None,
            event: str | None,
        ) -> None:
            # If incident is still "active" (sticky), apply severity; otherwise keep green but enrich diagnosis.
            active = False
            if ts and sticky_until and now_utc < sticky_until:
                active = True
            if active:
                apply(mid, base_status, diagnosis, event)
            else:
                # Avoid falsely asserting "healthy" when we only saw an old incident.
                # Keep status as-is, but carry the breadcrumb in diagnosis (unless already non-green).
                if mid not in matrix:
                    apply(mid, "🟢 健康", "状态稳定，就绪中。", None)
                if matrix[mid]["Status"].lstrip()[:1] == "🟢":
                    matrix[mid]["Diagnosis"] = f"最近一次异常: [{ts_local}] {kind}（窗口外/可能已恢复，需验证）"
                if event:
                    events.append(event)
            record_last(mid, kind)

        # Special case: richest signal with per-model reasons in one line.
        # Example:
        #   Embedded agent failed before reply: All models failed (2): <modelId>: <msg> | <modelId>: <msg>
        m_failed = ALL_MODELS_FAILED_BODY_RE.search(line)
        if m_failed:
            body = m_failed.group("body")
            for raw_seg in body.split("|"):
                seg = raw_seg.strip()
                if ":" not in seg:
                    continue
                mid, msg = seg.split(":", 1)
                mid = mid.strip()
                msg = msg.strip()
                # Guard: only accept real model ids (or already-known rows).
                if "/" not in mid and mid not in matrix:
                    continue
                mlow = msg.lower()
                reset_info = _reset_after_from_text(msg)
                reset_td = reset_info[0] if reset_info else None
                reset_raw = reset_info[1] if reset_info else ""
                recovery = f"预计 {reset_raw} 后重置" if reset_td and reset_raw else ""

                if "cooldown" in mlow or COOLDOWN_PROVIDER_RE.search(msg):
                    sticky_until = ts + dt.timedelta(minutes=cooldown_sticky_minutes) if ts else None
                    apply_incident(
                        mid,
                        kind="cooldown",
                        base_status="🟡 瞬时限流",
                        diagnosis="Provider cooldown / 瞬时限流（窗口内曾出现）。",
                        sticky_until=sticky_until,
                        event=f"[{ts_local}] {mid}: cooldown",
                    )
                elif "429" in mlow or "rate_limit" in mlow or CAPACITY_EXHAUSTED_RE.search(msg):
                    # If reset-after is short, treat as RPM/短期限流; long reset implies capacity/quota exhaustion.
                    if reset_td and reset_td <= dt.timedelta(hours=1):
                        sticky_until = ts + reset_td if ts else None
                        status = "🟡 429 限流"
                        diag = f"429 限流（短期/RPM 可能）。{recovery}".strip()
                    elif reset_td:
                        sticky_until = ts + reset_td if ts else None
                        status = "🔴 429 配额/容量限制"
                        diag = f"429 配额/容量限制（需等待重置）。{recovery}".strip()
                    else:
                        sticky_until = ts + dt.timedelta(minutes=rate_limit_sticky_minutes) if ts else None
                        status = "🟡 429 限流"
                        diag = "429 限流（可能是 RPM/并发）。"
                    apply_incident(
                        mid,
                        kind="429/rate_limit",
                        base_status=status,
                        diagnosis=diag,
                        sticky_until=sticky_until,
                        event=f"[{ts_local}] {mid}: 429/rate_limit {recovery}".strip(),
                    )
                elif "timeout" in mlow or "etimedout" in mlow:
                    sticky_until = ts + dt.timedelta(minutes=30) if ts else None
                    apply_incident(
                        mid,
                        kind="timeout",
                        base_status="🟡 连接超时",
                        diagnosis="timeout / ETIMEDOUT（窗口内曾出现）。",
                        sticky_until=sticky_until,
                        event=f"[{ts_local}] {mid}: timeout",
                    )
                elif CONTEXT_LIMIT_RE.search(msg):
                    sticky_until = ts + dt.timedelta(hours=6) if ts else None
                    apply_incident(
                        mid,
                        kind="token/context limit",
                        base_status="🟡 Token/上下文上限",
                        diagnosis="Token/上下文上限触发（窗口内曾出现）。",
                        sticky_until=sticky_until,
                        event=f"[{ts_local}] {mid}: token/context limit",
                    )
            continue

        provider, model, model_id = _extract_provider_model(line)
        resolved = None
        if provider and model:
            resolved = by_provider_model.get((provider, model))
        if resolved:
            model_id = resolved.model_id
        matched_models = _find_model_refs_in_line(line, models) if not model_id else []

        # Provider-level failures (no model id)
        nk = NO_API_KEY_RE.search(line)
        if nk:
            p = nk.group("provider")
            events.append(f"[{ts_local}] provider={p}: No API key")
            # Mark all models under this provider as red; this prevents "looks green but cannot run" confusion.
            for m in models:
                if m.provider != p:
                    continue
                apply(m.model_id, "🔴 配置缺失", "未配置 API key（provider 认证失败）。", None)
            continue

        cp = COOLDOWN_PROVIDER_RE.search(line)
        if cp and not model_id and not matched_models:
            p = cp.group("provider")
            events.append(f"[{ts_local}] provider={p}: cooldown")
            for m in models:
                if m.provider != p:
                    continue
                sticky_until = ts + dt.timedelta(minutes=cooldown_sticky_minutes) if ts else None
                apply_incident(
                    m.model_id,
                    kind="provider cooldown",
                    base_status="🟡 瞬时限流",
                    diagnosis="Provider cooldown（该 provider 下所有 profile 不可用）。",
                    sticky_until=sticky_until,
                    event=None,
                )
            continue

        # Model-specific: unknown / not allowed
        um = UNKNOWN_MODEL_RE.search(line) or MODEL_NOT_ALLOWED_RE.search(line)
        if um:
            mid = um.group("model")
            events.append(f"[{ts_local}] model={mid}: Unknown/Not allowed")
            if mid not in matrix:
                # Add an "observed" row so the report remains complete.
                if "/" in mid:
                    p, mm = mid.split("/", 1)
                else:
                    p, mm = "unknown", mid
                matrix[mid] = {"Provider": p, "Model": mm, "Status": "🔴 模型无效", "Diagnosis": "Unknown model / not allowed"}
            else:
                apply(mid, "🔴 模型无效", "Unknown model / not allowed", None)
            continue

        # Some errors don't include provider/model fields but do embed model ids.
        model_ids: list[str] = []
        if model_id:
            model_ids = [model_id]
        elif matched_models:
            model_ids = [m.model_id for m in matched_models]

        if not model_ids:
            # Keep at least a timeline breadcrumb for rate limits / context limits.
            lowered = line.lower()
            if "429" in lowered or "rate_limit" in lowered or CAPACITY_EXHAUSTED_RE.search(line):
                reset_info = _reset_after_from_text(line)
                reset_td = reset_info[0] if reset_info else None
                reset_raw = reset_info[1] if reset_info else ""
                recovery = f"预计 {reset_raw} 后重置" if reset_td and reset_raw else ""
                events.append(f"[{ts_local}] rate_limit(unknown model) {recovery}".strip())
            elif CONTEXT_LIMIT_RE.search(line):
                events.append(f"[{ts_local}] token/context limit (unknown model)")
            continue

        for mid in model_ids:
            if mid not in matrix:
                # Observed but not configured; add it so matrix stays truthful.
                if "/" in mid:
                    p, mm = mid.split("/", 1)
                else:
                    p, mm = "unknown", mid
                matrix[mid] = {"Provider": p, "Model": mm, "Status": "🟢 健康", "Diagnosis": "状态稳定，就绪中。"}

            if "cooldown" in lowered or COOLDOWN_PROVIDER_RE.search(line):
                sticky_until = ts + dt.timedelta(minutes=cooldown_sticky_minutes) if ts else None
                apply_incident(
                    mid,
                    kind="cooldown",
                    base_status="🟡 瞬时限流",
                    diagnosis="Cooldown / 瞬时限流（窗口内曾出现）。",
                    sticky_until=sticky_until,
                    event=f"[{ts_local}] {mid}: cooldown",
                )
            elif "429" in lowered or "rate_limit" in lowered or CAPACITY_EXHAUSTED_RE.search(line):
                reset_info = _reset_after_from_text(line)
                reset_td = reset_info[0] if reset_info else None
                reset_raw = reset_info[1] if reset_info else ""
                recovery = f"预计 {reset_raw} 后重置" if reset_td and reset_raw else ""
                if reset_td and reset_td <= dt.timedelta(hours=1):
                    sticky_until = ts + reset_td if ts else None
                    status = "🟡 429 限流"
                    diag = f"429 限流（短期/RPM 可能）。{recovery}".strip()
                elif reset_td:
                    sticky_until = ts + reset_td if ts else None
                    status = "🔴 429 配额/容量限制"
                    diag = f"429 配额/容量限制（需等待重置）。{recovery}".strip()
                else:
                    sticky_until = ts + dt.timedelta(minutes=rate_limit_sticky_minutes) if ts else None
                    status = "🟡 429 限流"
                    diag = "429 限流（可能是 RPM/并发）。"
                apply_incident(
                    mid,
                    kind="429/rate_limit",
                    base_status=status,
                    diagnosis=diag,
                    sticky_until=sticky_until,
                    event=f"[{ts_local}] {mid}: 429/rate_limit {recovery}".strip(),
                )
            elif "timeout" in lowered or "etimedout" in lowered:
                sticky_until = ts + dt.timedelta(minutes=30) if ts else None
                apply_incident(
                    mid,
                    kind="timeout",
                    base_status="🟡 连接超时",
                    diagnosis="timeout / ETIMEDOUT（窗口内曾出现）。",
                    sticky_until=sticky_until,
                    event=f"[{ts_local}] {mid}: timeout",
                )
            elif CONTEXT_LIMIT_RE.search(line):
                sticky_until = ts + dt.timedelta(hours=6) if ts else None
                apply_incident(
                    mid,
                    kind="token/context limit",
                    base_status="🟡 Token/上下文上限",
                    diagnosis="Token/上下文上限触发（窗口内曾出现）。",
                    sticky_until=sticky_until,
                    event=f"[{ts_local}] {mid}: token/context limit",
                )

    # Convert to stable row list (provider, model)
    rows = sorted(
        (
            {
                "Provider": v["Provider"],
                "模型 (Model)": v["Model"],
                "状态": v["Status"],
                "详细诊断 / 恢复时间": v["Diagnosis"],
                "_model_id": mid,
            }
            for mid, v in matrix.items()
        ),
        key=lambda r: (r["Provider"], r["模型 (Model)"]),
    )
    for r in rows:
        r.pop("_model_id", None)
    seen_ev: set[str] = set()
    uniq_events: list[str] = []
    for e in events:
        if e in seen_ev:
            continue
        seen_ev.add(e)
        uniq_events.append(e)
    return rows, uniq_events[-30:]


def _read_watchdog_events(since_utc: dt.datetime) -> list[dict[str, Any]]:
    if not WATCHDOG_AUDIT.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with WATCHDOG_AUDIT.open("r") as f:
            for raw in f:
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                ts_raw = str(d.get("timestamp") or "").strip()
                if not ts_raw:
                    continue
                try:
                    # watchdog uses local naive isoformat; assume local and treat as UTC+0 is wrong.
                    # If it has offset, fromisoformat keeps it.
                    ts = dt.datetime.fromisoformat(ts_raw)
                except Exception:
                    continue
                # If naive, assume local time and approximate by treating it as UTC (best effort).
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt.timezone.utc)
                if ts >= since_utc:
                    out.append(d)
    except Exception:
        return []
    return out


def analyze_restarts(lines: Iterable[str], watchdog_events: list[dict[str, Any]], tz: dt.tzinfo) -> list[dict[str, str]]:
    restarts: list[tuple[dt.datetime, str]] = []

    wd_restart_times: list[dt.datetime] = []
    for e in watchdog_events:
        if e.get("type") != "gateway_restart":
            continue
        ts_raw = e.get("timestamp")
        try:
            ts = dt.datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        wd_restart_times.append(ts)

    for line in lines:
        if "received sigusr1; restarting" in line.lower():
            ts = _parse_ts_utc(line)
            if not ts:
                continue
            restarts.append((ts, "用户配置变更 (SIGUSR1)"))
        elif "received sigterm; shutting down" in line.lower():
            ts = _parse_ts_utc(line)
            if not ts:
                continue
            restarts.append((ts, "系统/服务重启 (SIGTERM)"))
        elif "uncaught exception" in line.lower() or "max reconnect attempts" in line.lower():
            ts = _parse_ts_utc(line)
            if not ts:
                continue
            restarts.append((ts, "异常退出/崩溃"))

    # Merge near-duplicates (within 90s).
    restarts.sort(key=lambda x: x[0], reverse=True)
    merged: list[tuple[dt.datetime, str]] = []
    for ts, reason in restarts:
        if merged and abs((merged[-1][0] - ts).total_seconds()) <= 90:
            # Keep the "more specific" reason if present.
            if "异常" in reason:
                merged[-1] = (merged[-1][0], reason)
            continue
        merged.append((ts, reason))

    # Attribute watchdog restarts (within 2 minutes) if we have evidence.
    out: list[dict[str, str]] = []
    for ts, reason in merged:
        for wts in wd_restart_times:
            if abs((wts - ts).total_seconds()) <= 120:
                reason = "Watchdog 自愈触发"
                break
        out.append({"timestamp": _fmt_local(ts, tz), "reason": reason})
    return out


def read_cron_jobs(tz: dt.tzinfo) -> list[dict[str, Any]]:
    data = _read_json(CRON_JOBS)
    if not data:
        return []
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        return []
    out: list[dict[str, Any]] = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        state = j.get("state") or {}
        next_ms = state.get("nextRunAtMs")
        next_local = None
        if isinstance(next_ms, int):
            next_utc = dt.datetime.fromtimestamp(next_ms / 1000, tz=dt.timezone.utc)
            next_local = next_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        out.append(
            {
                "name": j.get("name"),
                "enabled": bool(j.get("enabled", True)),
                "schedule": j.get("schedule"),
                "delivery": j.get("delivery"),
                "nextRunLocal": next_local,
            }
        )
    return out


def _resolve_runtime_log_paths(gateway_log_lines: list[str]) -> list[Path]:
    # Gateway log may include: "[gateway] log file: /tmp/openclaw/openclaw-YYYY-MM-DD.log"
    last = None
    for line in reversed(gateway_log_lines):
        if "log file:" in line:
            last = line
            break
    out: list[Path] = []
    if last:
        m = re.search(r"log file:\s*(?P<path>/\S+)", last)
        if m:
            out.append(Path(m.group("path")))

    # Fallback: standard location.
    tmp_dir = Path("/tmp/openclaw")
    if tmp_dir.exists():
        candidates = sorted(tmp_dir.glob("openclaw-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        out.extend(candidates[:2])

    # Dedup while preserving order.
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        ps = str(p)
        if ps in seen:
            continue
        seen.add(ps)
        uniq.append(p)
    return uniq


def render_markdown(report: dict[str, Any], *, discord: bool = False) -> str:
    tz_name = report.get("timezone") or ""
    hours = report.get("window_hours")
    now_local = report.get("generated_at_local")

    lines: list[str] = []
    lines.append(f"📊 OpenClaw 系统审计报告")
    lines.append(f"({now_local} | 过去 {hours} 小时 | 时区 {tz_name})")
    lines.append("")

    # 1) Infra
    if discord:
        lines.append("**🛰️ 基础设施状态**")
    else:
        lines.append("### 🛰️ 基础设施状态")
    gw = report.get("gateway") or {}
    restart_count = int(gw.get("restart_count", 0) or 0)
    lines.append(f"- Gateway 重启: {restart_count} 次。")
    details = gw.get("restart_details") or []
    if details:
        # Breakdown by reason.
        by_reason: dict[str, int] = {}
        for d in details:
            r = str(d.get("reason") or "").strip() or "未知原因"
            by_reason[r] = by_reason.get(r, 0) + 1
        breakdown = "，".join(f"{k} x{v}" for k, v in sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0])))
        if breakdown:
            lines.append(f"- 重启原因分布: {breakdown}。")

        lines.append("- 最近重启明细 (最多 5 条):")
        for d in details[:5]:
            lines.append(f"  - [{d.get('timestamp')}] {d.get('reason')}")
    wd = report.get("watchdog") or {}
    lines.append(f"- Watchdog: {wd.get('status')}（近 {hours} 小时事件 {wd.get('event_count', 0)} 条）。")
    lines.append("")

    # 2) LLM matrix
    if discord:
        lines.append("**🧠 LLM 状态矩阵**")
    else:
        lines.append("### 🧠 LLM 状态矩阵 (按模型)")
    rows = report.get("llm_health", {}).get("matrix_rows") or []
    if discord:
        # Discord doesn't render markdown tables; use bullet list instead.
        for r in rows:
            status = r.get("状态", "")
            provider = r.get("Provider", "")
            model = r.get("模型 (Model)", "")
            diag = r.get("详细诊断 / 恢复时间", "")
            lines.append(f"- {status} `{provider}/{model}` — {diag}")
    else:
        lines.append("| Provider | 模型 (Model) | 状态 | 详细诊断 / 恢复时间 |")
        lines.append("| :--- | :--- | :--- | :--- |")
        for r in rows:
            lines.append(f"| {r.get('Provider','')} | {r.get('模型 (Model)','')} | {r.get('状态','')} | {r.get('详细诊断 / 恢复时间','')} |")
    lines.append("")

    # 3) Deep dive
    if discord:
        lines.append("**🔍 异常深度穿透**")
    else:
        lines.append("### 🔍 异常深度穿透")
    events = report.get("llm_health", {}).get("events") or []
    if not events:
        lines.append("- 近窗口内未捕获到明确的限流/超时/模型错误事件。")
    else:
        # Show only the most recent events to avoid noisy spam.
        for e in events[-8:]:
            lines.append(f"- {e}")
    lines.append("")

    # 4) Cron
    if discord:
        lines.append("**🕒 定时任务追踪**")
    else:
        lines.append("### 🕒 定时任务追踪")
    cron_jobs = report.get("cron", {}).get("jobs") or []
    if not cron_jobs:
        lines.append("- 未检测到 Cron 任务。")
    else:
        for j in cron_jobs:
            name = j.get("name")
            enabled = "运行中" if j.get("enabled") else "已停用"
            nxt = j.get("nextRunLocal") or "未知"
            lines.append(f"- {name}: {enabled}，下次运行 {nxt}。")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=2.0, help="Lookback window in hours (default: 2)")
    ap.add_argument(
        "--llm-hours",
        type=float,
        default=None,
        help="LLM health lookback window in hours (default: max(--hours, 24)). Use a larger window to avoid missing long cooldown/quota events.",
    )
    ap.add_argument(
        "--cooldown-sticky-minutes",
        type=int,
        default=240,
        help="How long to treat a seen cooldown as still impacting (default: 240).",
    )
    ap.add_argument(
        "--rate-limit-sticky-minutes",
        type=int,
        default=30,
        help="How long to treat a 429/rate_limit without reset-after as still impacting (default: 30).",
    )
    ap.add_argument("--tz", type=str, default=None, help='Timezone name (default from config "agents.defaults.userTimezone")')
    ap.add_argument("--format", choices=["json", "md", "discord"], default="json", help="Output format (discord = md without tables)")
    args = ap.parse_args()

    config = _read_json(CONFIG_FILE) or {}
    tz = _resolve_tz(config, args.tz)
    now_utc = dt.datetime.now(tz=dt.timezone.utc)
    report_hours = max(0.1, float(args.hours))
    llm_hours = float(args.llm_hours) if args.llm_hours is not None else max(report_hours, 24.0)
    since_report_utc = now_utc - dt.timedelta(hours=report_hours)
    since_llm_utc = now_utc - dt.timedelta(hours=max(report_hours, llm_hours))

    models = get_configured_models(config)
    # Infra is based on the report window (short).
    gw_lines_report = get_recent_lines(GATEWAY_LOG, since_report_utc)
    err_lines_report = get_recent_lines(ERROR_LOG, since_report_utc)
    runtime_logs = _resolve_runtime_log_paths(gw_lines_report)
    runtime_lines_report: list[str] = []
    for p in runtime_logs:
        runtime_lines_report.extend(get_recent_lines(p, since_report_utc))
    watchdog_events = _read_watchdog_events(since_report_utc)
    infra_lines = gw_lines_report + err_lines_report + runtime_lines_report
    restart_details = analyze_restarts(infra_lines, watchdog_events, tz)

    # LLM health is based on a larger lookback so we don't miss long cooldown/quota events.
    err_lines_llm = get_recent_lines(ERROR_LOG, since_llm_utc)
    runtime_lines_llm: list[str] = []
    for p in runtime_logs:
        runtime_lines_llm.extend(get_recent_lines(p, since_llm_utc))
    llm_lines = err_lines_llm + runtime_lines_llm
    matrix_rows, events = parse_llm_status(
        llm_lines,
        models,
        tz,
        now_utc=now_utc,
        cooldown_sticky_minutes=int(args.cooldown_sticky_minutes),
        rate_limit_sticky_minutes=int(args.rate_limit_sticky_minutes),
    )
    # Deep-dive list should be recent, not the whole lookback window.
    _, events_recent = parse_llm_status(
        err_lines_report + runtime_lines_report,
        models,
        tz,
        now_utc=now_utc,
        cooldown_sticky_minutes=int(args.cooldown_sticky_minutes),
        rate_limit_sticky_minutes=int(args.rate_limit_sticky_minutes),
    )

    report: dict[str, Any] = {
        "generated_at_local": now_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
        "timezone": getattr(tz, "key", str(tz)),
        "window_hours": float(args.hours),
        "gateway": {"restart_count": len(restart_details), "restart_details": restart_details[:10]},
        "watchdog": {
            "status": "audit-present" if WATCHDOG_AUDIT.exists() else "audit-missing",
            "event_count": len(watchdog_events),
        },
        "llm_health": {
            "matrix_rows": matrix_rows,
            "events": events_recent,
            "_llm_lookback_hours": llm_hours,
            "_cooldown_sticky_minutes": int(args.cooldown_sticky_minutes),
        },
        "cron": {"jobs": read_cron_jobs(tz)},
    }

    if args.format in ("md", "discord"):
        print(render_markdown(report, discord=(args.format == "discord")), end="")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

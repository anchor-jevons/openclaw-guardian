---
name: system-watchdog
version: 0.1.0
description: >-
  深度审计 OpenClaw 系统健康状态，基于脚本与日志给出可复现的审计报告。
  Deep audit of OpenClaw system health with deterministic,
  script-based reports. No guessing, no hallucination.
author: Lucas
license: MIT

metadata:
  openclaw:
    emoji: "🐕"
    requires:
      bins: ["openclaw", "python3"]
      env: []
    skillKey: "system-watchdog"
    always: false

permissions:
  tools:
    allow: ["bash", "read"]
    deny: ["write"]
  sandbox: compatible
  elevated: false
---

# System Watchdog

本技能用于生成 **确定性** 的系统审计简报，目标是稳定、可复现、可追踪（相同输入日志应得到相同输出）。

## 🚨 安全红线 (Security Policy)

**严禁在报告中暴露以下敏感信息**：

| 敏感类型 | 示例 | 处理规则 |
|----------|------|----------|
| API Key / Token | `sk-...`, `Bearer ...`, bot token | ❌ 完全禁止 |
| Provider 账号标识 | `google-gemini-cli:email@...` | ❌ 禁止（只保留 provider 名称） |
| 密钥文件路径 | `~/.openclaw/credentials/...` | ❌ 完全禁止 |
| 模型名称 | `gemini-3-pro-preview` | ✅ 允许显示 |
| Provider 名称 | `google-gemini-cli`, `google-antigravity` | ✅ 允许显示 |

## 目标输出

输出一份中文 Markdown 简报，**顺序必须固定**：
1. `### 🛰️ 基础设施状态`
2. `### 🧠 LLM 状态矩阵 (按模型)`（表格必须含 `Provider` 列）
3. `### 🔍 异常深度穿透`（按时间列出关键事件）
4. `### 🕒 定时任务追踪`

## 数据来源 (必须基于这些“可查”的证据)

- `/Users/jevons/.openclaw/logs/gateway.log`
- `/Users/jevons/.openclaw/logs/gateway.err.log`
- `/Users/jevons/.openclaw/guardian/watchdog-audit.jsonl`（如存在）
- `/Users/jevons/.openclaw/cron/jobs.json`
- `/Users/jevons/.openclaw/openclaw.json`（仅用于读取模型清单/时区，不得输出 token）

## 强制工作流 (避免不稳定的 LLM 自由发挥)

1. 运行脚本生成**已排版**的 Markdown（不要自己重排，不要自行推断）：

```bash
python3 /Users/jevons/.openclaw/scripts/openclaw-guardian/health_fetcher.py --hours 2 --format md
```

2. 将脚本输出 **原样** 作为最终报告返回。

## 额外约束

- 不得声称“已推送到 Discord”，除非你确实执行了发送动作且拿到了成功回执；如果是由 Cron 的 `delivery.mode=announce` 自动推送，则只能描述为“本次输出将由 Cron 投递”，不要说“已投递成功”。
- 时间必须与用户时区对齐（脚本会读取 `openclaw.json` 的 `agents.defaults.userTimezone` 并格式化）。


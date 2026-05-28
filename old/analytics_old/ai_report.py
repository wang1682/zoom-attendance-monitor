"""ai_report.py — AI 报告生成器
通过 DeepSeek (sub2api) 生成日报/周报/风险摘要
失败不影响聚合服务（try/except 兜底）

Phase 8: 所有函数接受 tenant_id 参数，不再硬编码 default_tenant_id
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

from app.database import SyncSession
from app.settings import settings
from app.analytics.models import AiReport


MYT = timezone(timedelta(hours=8))


def _get_sub2api_url() -> str:
    """从 settings 获取 LLM 端点 URL
    优先级：sub2api_base_url > deepseek 官方 > openai 官方"""
    if settings.sub2api_base_url and settings.sub2api_api_key:
        base = settings.sub2api_base_url.rstrip("/")
        return f"{base}/chat/completions"
    if settings.deepseek_api_key:
        return "https://api.deepseek.com/v1/chat/completions"
    return "https://api.openai.com/v1/chat/completions"


def _get_sub2api_key() -> str:
    """获取 LLM API key，按优先级：sub2api > deepseek > openai"""
    return settings.sub2api_api_key or settings.deepseek_api_key or settings.openai_api_key


def _today_str() -> str:
    return datetime.now(MYT).strftime("%Y-%m-%d")


def _yesterday_str() -> str:
    return (datetime.now(MYT) - timedelta(days=1)).strftime("%Y-%m-%d")


def _call_llm(system: str, user: str, model: str = None) -> str | None:
    """调用 LLM 获取文本响应，失败返回 None"""
    api_key = _get_sub2api_key()
    if not api_key:
        return None

    model = model or settings.ai_report_model
    base_url = _get_sub2api_url()

    def _do_request(url, key):
        p = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": 2048,
            "temperature": 0.3,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=p, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {key}")
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("choices", [{}])[0].get("message", {}).get("content", "")

    providers = [
        ("sub2api", base_url, api_key),
        ("deepseek", "https://api.deepseek.com/v1/chat/completions", settings.deepseek_api_key),
        ("openai", "https://api.openai.com/v1/chat/completions", settings.openai_api_key),
    ]
    for provider_name, url, key in providers:
        if not key:
            continue
        try:
            content = _do_request(url, key)
            if content:
                return content, provider_name
        except (urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
            sys.stderr.write(f"[AI_REPORT] LLM call failed ({provider_name}): {e}\n")
            continue
    return None, None


def _send_telegram(message: str, chat_id: str | None = None) -> bool:
    """发送消息到 Telegram（纯文本，不依赖通知器）
    如果 chat_id 为 None，走 settings 全局私聊 chat_id
    """
    token = settings.telegram_bot_token
    cid = chat_id or settings.telegram_private_chat_id
    if not token or not cid:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": cid,
        "text": message,
        "parse_mode": "HTML",
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8")).get("ok", False)
    except Exception as e:
        sys.stderr.write(f"[AI_REPORT] Telegram send failed: {e}\n")
        return False


def _collect_daily_metrics(
    session: SyncSession,
    date_str: str,
    tenant_id: str | None = None,
) -> dict:
    """收集单日指标快照"""
    from sqlalchemy import text
    tenant = tenant_id or settings.default_tenant_id

    daily = session.execute(text("""
        SELECT date, total_persons, total_duration_minutes,
               earliest_entry, latest_entry, unique_emails
        FROM daily_stats WHERE tenant_id = :t AND date = :d
    """), {"t": tenant, "d": date_str}).fetchone()
    if daily:
        daily = {
            "date": daily[0], "total_persons": daily[1],
            "total_duration_minutes": daily[2], "earliest_entry": daily[3],
            "latest_entry": daily[4], "unique_emails": daily[5],
        }

    risks = session.execute(text("""
        SELECT risk_type, severity, COUNT(*) as cnt
        FROM risk_scores
        WHERE tenant_id = :t AND date = :d AND dismissed = 0
        GROUP BY risk_type, severity
    """), {"t": tenant, "d": date_str}).fetchall()

    top_stayers = session.execute(text("""
        SELECT pds.name,
               ROUND(pds.total_duration_minutes, 1) as duration,
               pds.late_entry,
               pds.short_stay,
               pds.night_entry
        FROM participant_daily_stats pds
        WHERE pds.tenant_id = :t AND pds.date = :d
        ORDER BY pds.total_duration_minutes DESC
        LIMIT 5
    """), {"t": tenant, "d": date_str}).fetchall()
    top_stayers_list = []
    for r in top_stayers:
        top_stayers_list.append({
            "name": r[0], "duration": r[1],
            "late": bool(r[2]), "short_stay": bool(r[3]), "night": bool(r[4]),
        })

    hourly = session.execute(text("""
        SELECT hour, person_count
        FROM hourly_activity
        WHERE tenant_id = :t AND date = :d
        ORDER BY hour
    """), {"t": tenant, "d": date_str}).fetchall()
    hourly_list = [{"hour": r[0], "count": r[1]} for r in hourly]

    # 7天趋势
    trend = session.execute(text("""
        SELECT date, total_persons
        FROM daily_stats
        WHERE tenant_id = :t AND date >= date(:d, '-7 days')
        ORDER BY date
    """), {"t": tenant, "d": date_str}).fetchall()
    trend_list = [{"date": r[0], "persons": r[1]} for r in trend]

    return {
        "daily": daily or {},
        "risks": [{"type": r[0], "severity": r[1], "count": r[2]} for r in risks],
        "top_stayers": top_stayers_list,
        "hourly": hourly_list,
        "trend": trend_list,
    }


def generate_daily_report(
    session: SyncSession = None,
    date_str: str = None,
    force: bool = False,
    send: bool = True,
    tenant_id: str | None = None,
    chat_id_override: str | None = None,
) -> dict | None:
    """生成 AI 日报"""
    close_session = False
    if session is None:
        session = SyncSession()
        close_session = True
    try:
        date_str = date_str or _yesterday_str()
        tenant = tenant_id or settings.default_tenant_id

        # 检查是否已生成
        if not force:
            existing = session.query(AiReport).filter(
                AiReport.tenant_id == tenant,
                AiReport.report_type == "daily",
                AiReport.date == date_str,
            ).first()
            if existing and existing.sent:
                return {"id": existing.id, "already_exists": True, "sent": existing.sent}

        metrics = _collect_daily_metrics(session, date_str, tenant_id=tenant)
        daily = metrics.get("daily", {})
        if not daily.get("total_persons"):
            return None  # 没有数据就不生成

        # 构造提示词
        system_prompt = "你是一个自习室考勤分析助手。用中文给出简洁、数据驱动的日报。不要客套话。"
        user_prompt = (
            f"请为 {date_str} 的自习室出勤情况生成一份日报。以下是指标：\n"
            f"- 总人数: {daily.get('total_persons', 0)}\n"
            f"- 唯一邮箱数: {daily.get('unique_emails', 0)}\n"
            f"- 总停留时长: {daily.get('total_duration_minutes', 0):.0f} 分钟\n"
            f"- 最早入场: {daily.get('earliest_entry', '-')}\n"
            f"- 最晚入场: {daily.get('latest_entry', '-')}\n"
        )

        if metrics["risks"]:
            user_prompt += "\n⚠️ 风险事件:\n"
            for r in metrics["risks"]:
                user_prompt += f"- [{r['severity'].upper()}] {r['type']}: {r['count']} 次\n"

        if metrics["top_stayers"]:
            user_prompt += "\n📊 停留最久 Top5:\n"
            for s in metrics["top_stayers"]:
                flags = []
                if s["late"]: flags.append("迟到")
                if s["short_stay"]: flags.append("挂机")
                if s["night"]: flags.append("深夜")
                tag = f" [{','.join(flags)}]" if flags else ""
                user_prompt += f"- {s['name']}: {s['duration']} 分钟{tag}\n"

        if metrics["hourly"]:
            peak = max(metrics["hourly"], key=lambda x: x["count"])
            user_prompt += f"\n⏰ 高峰时段: {peak['hour']}:00（{peak['count']} 人在线）\n"

        if metrics["trend"]:
            user_prompt += "\n📈 7天趋势:\n"
            for t in metrics["trend"]:
                user_prompt += f"- {t['date']}: {t['persons']} 人\n"

        user_prompt += "\n请给出简要总结（3-5句话），突出异常和亮点。"

        # 调用 AI
        content, ai_provider = _call_llm(system_prompt, user_prompt)
        if not content:
            sys.stderr.write("[AI_REPORT] LLM returned no content, falling back to template\n")
            content = _fallback_daily_report(metrics, date_str)
            ai_provider = "fallback"

        # 保存到 ai_reports（upsert: 已存在则更新）
        report = session.query(AiReport).filter(
            AiReport.tenant_id == tenant,
            AiReport.report_type == "daily",
            AiReport.date == date_str,
        ).first()

        if report:
            report.summary = content
            report.metrics = json.dumps(metrics, default=str, ensure_ascii=False)
            report.ai_provider = ai_provider
        else:
            report = AiReport(
                tenant_id=tenant,
                report_type="daily",
                date=date_str,
                title=f"自习室日报 {date_str}",
                summary=content,
                metrics=json.dumps(metrics, default=str, ensure_ascii=False),
                ai_provider=ai_provider,
            )
            session.add(report)

        session.commit()
        session.refresh(report)

        # 推送到 Telegram
        sent = False
        if send:
            tg_text = f"📋 <b>自习室日报 {date_str}</b>\n\n{content}"
            sent = _send_telegram(tg_text, chat_id=chat_id_override)
            if sent:
                report.sent = True
                report.sent_at = datetime.now(MYT)
                session.commit()

        return {
            "id": report.id,
            "date": date_str,
            "summary": content,
            "sent": sent,
            "ai_provider": ai_provider,
        }
    except Exception as e:
        sys.stderr.write(f"[AI_REPORT] generate_daily_report failed: {e}\n")
        return None
    finally:
        if close_session:
            session.close()


def _fallback_daily_report(metrics: dict, date_str: str) -> str:
    """当 AI 不可用时的模板日报"""
    daily = metrics.get("daily", {})
    parts = [f"📊 {date_str} 自习室出勤日报"]
    parts.append(f"总人数: {daily.get('total_persons', 0)} 人")
    parts.append(f"总时长: {daily.get('total_duration_minutes', 0):.0f} 分钟")
    parts.append(f"最早入场: {daily.get('earliest_entry', '-')}")

    if metrics["top_stayers"]:
        parts.append("\n停留最久:")
        for s in metrics["top_stayers"]:
            parts.append(f"  {s['name']}: {s['duration']} 分钟")

    if metrics["risks"]:
        parts.append("\n风险事件:")
        for r in metrics["risks"]:
            parts.append(f"  [{r['severity']}] {r['type']}: {r['count']} 次")

    return "\n".join(parts)


if __name__ == "__main__":
    result = generate_daily_report(force=True, send=False)
    if result:
        sys.stdout.write(f"[AI_REPORT] 日报生成成功: id={result.get('id')}, sent={result.get('sent')}\n")
        sys.stdout.write(f"---\n{result.get('summary', '')}\n")
    else:
        sys.stdout.write("[AI_REPORT] 无数据或生成失败\n")

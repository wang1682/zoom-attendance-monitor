"""engine.py — 分析引擎主循环
5 分钟周期，遍历所有 active tenant：
  ① aggregate_all (聚合昨日数据)
  ② detect_all (风险扫描)
  ③ generate_daily_report (AI 日报，每天 23:00 一次)

作为独立 systemd zoom-analytics 服务运行，不依赖 API/webhook 进程。

Tenant 间异常隔离：A 失败不阻塞 B
Tenant 专属 Telegram chat_id 从 tenant_configs.telegram_chat_id 读取
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from app.database import SyncSession
from app.settings import settings
from app.models import Tenant, TenantConfig
from analytics.aggregator import aggregate_all
from analytics.risk import detect_all
from analytics.ai_report import generate_daily_report

MYT = timezone(timedelta(hours=8))

_start_time = time.time()


def _get_telegram_chat_id(tenant_id: str) -> str | None:
    """读取 tenant 专属的 Telegram chat_id，无配置则返回 None（走全局默认）"""
    try:
        with SyncSession() as s:
            cfg = s.query(TenantConfig).filter(
                TenantConfig.tenant_id == tenant_id,
                TenantConfig.key == "telegram_chat_id",
            ).first()
            if cfg and cfg.value:
                return cfg.value.strip()
    except Exception as e:
        sys.stderr.write(f"[ENGINE] 读取 tenant_config telegram_chat_id 失败 (tenant={tenant_id}): {e}\n")
    return None


def _send_telegram(message: str, chat_id: str | None = None) -> bool:
    """直接发 Telegram 消息（不依赖外部模块）
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
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8")).get("ok", False)
    except Exception as e:
        sys.stderr.write(f"[ENGINE] Telegram send failed (chat_id={cid}): {e}\n")
        return False


def _today_myt() -> str:
    return datetime.now(MYT).strftime("%Y-%m-%d")


def _is_daily_report_hour() -> bool:
    now = datetime.now(MYT)
    return now.hour == settings.daily_report_hour


def _daily_report_already_sent(session: SyncSession, tenant_id: str, date_str: str) -> bool:
    from app.analytics.models import AiReport
    return session.query(AiReport).filter(
        AiReport.tenant_id == tenant_id,
        AiReport.date == date_str,
        AiReport.sent == True,  # noqa: E712
    ).first() is not None


def _log(msg: str):
    ts = datetime.now(MYT).strftime("%Y-%m-%d %H:%M:%S")
    sys.stdout.write(f"[ENGINE {ts}] {msg}\n")
    sys.stdout.flush()


def _run_tenant_cycle(session: SyncSession, tenant_id: str) -> dict:
    """执行一个 tenant 的完整周期"""
    result = {"tenant_id": tenant_id, "aggregation": None, "risks": None, "ai_report": None}
    today_str = _today_myt()

    # 获取 tenant 专属 Telegram chat_id
    chat_id = _get_telegram_chat_id(tenant_id)

    # 1. 聚合（回扫近 7 天，增量部分）
    try:
        agg = aggregate_all(session=session, days_back=7, tenant_id=tenant_id)
        result["aggregation"] = agg
        _log(f"tenant={tenant_id} aggregation done: daily={agg.get('daily', 0)}, "
             f"person={agg.get('person', 0)}, hourly={agg.get('hourly', 0)}, "
             f"participant={agg.get('participant', 0)}")
    except Exception as e:
        _log(f"tenant={tenant_id} aggregation failed: {e}")

    # 2. 风险扫描（今天 + 昨天）
    for scan_date in [today_str]:
        try:
            risks = detect_all(session=session, date_str=scan_date, tenant_id=tenant_id)
            count = len(risks.get("all", []))
            if count:
                _log(f"tenant={tenant_id} risk scan done: {scan_date} found {count} risks")
            result["risks"] = risks
        except Exception as e:
            _log(f"tenant={tenant_id} risk scan failed ({scan_date}): {e}")

    # 3. AI 日报（仅在 23:00 时段且未发送时触发）
    if _is_daily_report_hour() and not _daily_report_already_sent(session, tenant_id, today_str):
        try:
            report = generate_daily_report(
                session=session, force=False, send=True,
                tenant_id=tenant_id, chat_id_override=chat_id,
            )
            if report:
                _log(f"tenant={tenant_id} ai_report done: id={report.get('id', '?')}, "
                     f"provider={report.get('ai_provider', '?')}")
                result["ai_report"] = report
            else:
                _log(f"tenant={tenant_id} ai_report skipped (no data or already sent)")
        except Exception as e:
            _log(f"tenant={tenant_id} ai_report failed: {e}")

    return result


def _get_active_tenants(session: SyncSession) -> list[str]:
    """获取所有 active tenant 的 id 列表"""
    tenants = session.query(Tenant).filter(Tenant.active == True).all()  # noqa: E712
    ids = [t.id for t in tenants]
    _log(f"active tenants: {ids}")
    return ids


def main_loop(interval: int = 300):
    """主循环，默认 5 分钟一次"""
    _log(f"分析引擎启动，轮询间隔={interval}s，日报时段={settings.daily_report_hour}:00")

    last_heartbeat_hour = -1

    while True:
        try:
            session = SyncSession()
            try:
                # 获取所有 active tenant
                tenant_ids = _get_active_tenants(session)
                if not tenant_ids:
                    _log("无 active tenant，跳过此周期")

                for tid in tenant_ids:
                    try:
                        _run_tenant_cycle(session, tid)
                    except Exception as e:
                        _log(f"tenant={tid} cycle failed (isolated): {e}")
                        continue

                session.commit()
            finally:
                session.close()
        except Exception as e:
            _log(f"周期异常: {e}")

        # 心跳通知（每 6 小时，首次立即发）
        now = datetime.now(MYT)
        current_hour_block = now.hour // 6
        if current_hour_block != last_heartbeat_hour or last_heartbeat_hour == -1:
            last_heartbeat_hour = current_hour_block
            uptime = time.time() - _start_time
            _send_telegram(
                f"⏱ 分析引擎运行中\n"
                f"已运行 {int(uptime//3600)}h{int(uptime%3600//60)}m\n"
                f"下一日报排期 {settings.daily_report_hour}:00 MYT"
            )

        time.sleep(interval)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=300, help="轮询间隔（秒），默认 300")
    parser.add_argument("--once", action="store_true", help="单次运行后退出")
    args = parser.parse_args()

    if args.once:
        session = SyncSession()
        try:
            tenant_ids = _get_active_tenants(session)
            _log(f"单次运行，active tenants: {tenant_ids}")
            for tid in tenant_ids:
                try:
                    r = _run_tenant_cycle(session, tid)
                    sys.stdout.write(f"[once] tenant={tid}: "
                                     f"agg={r['aggregation'] is not None}, "
                                     f"risks={r['risks'] is not None}, "
                                     f"ai={r['ai_report'] is not None}\n")
                except Exception as e:
                    sys.stdout.write(f"[once] tenant={tid} failed: {e}\n")
            session.commit()
        finally:
            session.close()
    else:
        main_loop(interval=args.interval)

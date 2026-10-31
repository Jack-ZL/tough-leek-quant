"""多空辩论记录持久化存储 (P1-9)。

与个股分析报告 (stock_reports) 完全独立 —— 单独的文件、字段、上限。
存储机制委托给共享的 JsonReportStore (原子写 + 实例锁), 本模块只固化
辩论记录的 文件名 / 上限 / id 前缀。

存储位置: data/user_data/ai_debate_reports.json (数组, 按 created_at 降序)
保留最近 MAX_REPORTS 条; 超出自动裁剪最旧的。

每条记录结构 (由前端在辩论流结束后保存):
{
  "id": "deb_xxx",            # 唯一 id (debate)
  "symbol": "600519.SH",
  "name": "贵州茅台",
  "focus": "",                # 用户追加的关注点 (可为空)
  "bull_text": "...",         # 多方最终陈述 (重写过的为重写版)
  "bear_text": "...",         # 空方最终陈述
  "judge_text": "...",        # 风控主管终审全文 (已剥离裁决标记)
  "verdict": {"direction": "偏多", "reason": "..."} | null,  # 终审裁决 (解析失败为 null)
  "rewritten": false,         # 是否经历过打回重写
  "roles": {"bull": "deepseek-v3", ...},  # 各角色实际使用的模型名 (展示用)
  "summary": "当前价 ...",    # 价位摘要 (列表页展示)
  "close": 12.3,
  "created_at": "2026-10-31T18:00:00"
}
"""
from __future__ import annotations

from app.services.json_report_store import JsonReportStore

# 辩论一次要跑 3~5 次 LLM 调用, 成本高于普通分析, 上限收紧到 20 条
MAX_REPORTS = 20

_store = JsonReportStore("ai_debate_reports.json", MAX_REPORTS, id_prefix="deb")


def list_reports() -> list[dict]:
    """返回全部辩论记录 (按 created_at 降序)。"""
    return _store.list_reports()


def save_report(report: dict) -> dict:
    """新增一条辩论记录并持久化。返回保存后的记录 (含 id / created_at)。"""
    return _store.save_report(report)


def delete_report(report_id: str) -> bool:
    """删除指定记录。返回是否删除成功。"""
    return _store.delete_report(report_id)

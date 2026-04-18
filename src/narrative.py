"""Weekly narrative synthesis over recent daily reports."""
from __future__ import annotations

import json

from .analyzer import _call_claude, _extract_json
from .config import get_settings
from .logging_setup import get_logger
from .models import DailyReport, NarrativeInsight, StockAnalysis

log = get_logger(__name__)


def _load_system_prompt() -> str:
    settings = get_settings()
    return (settings.prompts_dir / "narrative_system.md").read_text(encoding="utf-8")


def _summarize_report(report: DailyReport) -> dict:
    return {
        "date": report.date,
        "gainers": [{"ticker": g.ticker, "name": g.name, "sector": g.sector,
                      "change_pct_nd": round(g.change_pct_nd, 2)} for g in report.gainers],
        "analyses": [{"ticker": a.ticker, "name": a.name, "pump_thesis": a.pump_thesis,
                       "sector_tags": a.sector_tags, "confidence": a.confidence}
                      for a in report.analyses],
    }


def synthesize_narrative(
    today_analyses: list[StockAnalysis],
    prior_reports: list[DailyReport],
) -> NarrativeInsight:
    today_summary = {
        "date": "today",
        "analyses": [{"ticker": a.ticker, "name": a.name, "pump_thesis": a.pump_thesis,
                       "sector_tags": a.sector_tags, "confidence": a.confidence}
                      for a in today_analyses],
    }
    history = [_summarize_report(r) for r in prior_reports]
    user_text = (
        "Here is the recent daily-report history (most recent first) followed "
        "by today's analyses. Produce the narrative JSON per the schema.\n\n"
        "IMPORTANT: Your response MUST start with `{` and contain ONLY a valid "
        "JSON object. No preamble, no markdown fences, no explanation.\n\n"
        + json.dumps({"today": today_summary, "history": history}, ensure_ascii=False, indent=2)
    )

    raw = ""
    try:
        raw = _call_claude(_load_system_prompt(), user_text)
        data = _extract_json(raw)
        return NarrativeInsight(
            current_narrative=data.get("current_narrative", ""),
            hot_sectors=list(data.get("hot_sectors") or []),
            cooling_sectors=list(data.get("cooling_sectors") or []),
            week_over_week_change=data.get("week_over_week_change", ""),
            investment_insight=data.get("investment_insight", ""),
        )
    except Exception as exc:  # noqa: BLE001
        # Log the raw response (truncated) for diagnosis on next run
        if raw:
            log.error(
                "narrative synthesis failed: %s | raw response (first 500 chars): %r",
                exc, raw[:500],
            )
        else:
            log.error("narrative synthesis failed before response: %s", exc)
        # Generate a minimal data-driven fallback narrative
        hot_from_today = []
        if today_analyses:
            from collections import Counter
            tag_counts = Counter()
            for a in today_analyses:
                for t in (a.sector_tags or []):
                    tag_counts[t] += 1
            hot_from_today = [t for t, _ in tag_counts.most_common(3)]

        fallback_narrative = (
            f"Dow 30 주요 상승 종목 {len(today_analyses)}개의 섹터 분포를 "
            f"기반으로 한 데이터 기반 요약입니다 (Claude 내러티브 합성 실패 시 fallback)."
        )
        return NarrativeInsight(
            current_narrative=fallback_narrative,
            hot_sectors=hot_from_today,
            cooling_sectors=[],
            week_over_week_change="이전 히스토리 비교 불가" if not prior_reports else "",
            investment_insight=(
                f"Claude synthesis error: {type(exc).__name__}: {str(exc)[:100]}. "
                f"다음 실행 시 자동 복구 예상."
            ),
        )

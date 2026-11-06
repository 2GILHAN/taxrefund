"""분석 결과를 워치리스트 회사와 매칭하고, 경정청구 기한을 계산합니다.

날짜 계산은 모델에 맡기지 않고 여기서 결정적으로 처리합니다.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterable

from .config import Company, NotifyConfig
from .models import Alert, Analysis, Match, Precedent

ALL_INDUSTRY_TOKENS = ("전업종", "전 업종", "모든 업종", "업종무관", "업종 무관", "업종불문")


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _haystack(prec: Precedent, analysis: Analysis) -> str:
    parts: list[str] = [
        prec.title,
        analysis.issue_summary,
        analysis.refund_rationale,
        analysis.generalizable_reason,
        *analysis.issue_tags,
        *analysis.applicable_industries,
        *analysis.check_points,
    ]
    return _norm(" ".join(p for p in parts if p))


def _is_all_industry(analysis: Analysis) -> bool:
    joined = _norm(" ".join(analysis.applicable_industries))
    return any(_norm(t) in joined for t in ALL_INDUSTRY_TOKENS)


def _ksic_hit(company: Company, analysis: Analysis) -> str | None:
    for mine in company.ksic:
        prefix = re.sub(r"\D", "", mine)
        if not prefix:
            continue
        for theirs in analysis.ksic_codes:
            other = re.sub(r"\D", "", str(theirs))
            if other and (other.startswith(prefix) or prefix.startswith(other)):
                return f"표준산업분류 {other} ↔ 관심 분류 {prefix}"
    return None


def match_companies(
    prec: Precedent, analysis: Analysis, watchlist: Iterable[Company]
) -> list[Match]:
    """이 판례로 검토할 만한 회사들을 골라냅니다."""
    hay = _haystack(prec, analysis)
    all_industry = _is_all_industry(analysis)
    matches: list[Match] = []

    for company in watchlist:
        reasons: list[str] = []
        bonus = 0

        ksic = _ksic_hit(company, analysis)
        if ksic:
            reasons.append(f"업종코드 일치 ({ksic})")
            bonus += 10

        hits = [kw for kw in company.keywords if kw and _norm(kw) in hay]
        if hits:
            reasons.append("키워드 일치: " + ", ".join(hits[:5]))
            bonus += min(15, 5 * len(hits))

        if company.industry and _norm(company.industry) in hay:
            reasons.append(f"업종명 언급: {company.industry}")
            bonus += 5

        if not reasons and all_industry:
            reasons.append("업종을 가리지 않는 일반 법리 (전업종 적용)")

        if reasons:
            matches.append(
                Match(
                    company=company.name,
                    industry=company.industry,
                    reasons=reasons,
                    score=min(100, analysis.refund_confidence + bonus),
                )
            )

    matches.sort(key=lambda m: m.score, reverse=True)
    return matches


def open_fiscal_years(today: date | None = None) -> list[int]:
    """오늘 기준으로 통상적 경정청구가 가능한 사업연도 목록 (12월 결산 법인 가정).

    12월 결산 법인의 사업연도 N 법정신고기한은 N+1년 3월 31일이고,
    통상적 경정청구는 그로부터 5년 이내이므로 N+6년 3월 31일까지 가능합니다.
    """
    today = today or date.today()
    years: list[int] = []
    for n in range(today.year - 7, today.year):
        if date(n + 6, 3, 31) >= today:
            years.append(n)
    return years


def compute_deadlines(analysis: Analysis, prec: Precedent, today: date | None = None) -> list[str]:
    """경정청구 기한 안내 문구 (12월 결산 법인 기준의 참고값)."""
    today = today or date.today()
    notes: list[str] = []

    years = open_fiscal_years(today)
    if years:
        notes.append(
            f"통상적 경정청구(국기법 §45조의2①): 12월 결산 법인 기준 "
            f"{years[0]}~{years[-1]} 사업연도가 아직 5년 이내입니다. "
            f"가장 먼저 만료되는 것은 {years[0]} 사업연도이며 기한은 "
            f"{date(years[0] + 6, 3, 31).isoformat()}입니다."
        )
        if (date(years[0] + 6, 3, 31) - today).days <= 90:
            notes.append(
                f"⚠ {years[0]} 사업연도 경정청구 기한이 "
                f"{(date(years[0] + 6, 3, 31) - today).days}일 남았습니다."
            )
    else:
        notes.append("통상적 경정청구 가능 사업연도를 계산하지 못했습니다. 직접 확인하세요.")

    if analysis.claim_basis == "후발적 사유" and prec.decided_on:
        month = prec.decided_on.month + 3
        year = prec.decided_on.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        day = min(prec.decided_on.day, 28)
        notes.append(
            f"후발적 사유(국기법 §45조의2②): 사유를 안 날부터 3개월 이내입니다. "
            f"선고일({prec.decided_str}) 기준 대략 {date(year, month, day).isoformat()}까지이나, "
            f"기산점은 선고일이 아니라 판결 확정일 등 '사유를 안 날'이므로 반드시 개별 확인하세요."
        )

    notes.append("결산월이 12월이 아니면 위 날짜는 그대로 적용되지 않습니다.")
    return notes


def tax_type_allowed(prec: Precedent, cfg: NotifyConfig) -> bool:
    """이 판례의 세목이 알림 대상인지.

    예전에는 분석의 is_corporate_tax로 걸렀습니다. 그러면 세목을 넓히려 해도
    분석을 다시 하지 않는 한 통과할 수가 없어서, 판례에 붙은 세목으로
    거릅니다. 세목은 국세청이 준 값이 1순위이고 없으면 사건명에서 읽습니다.

    cfg.tax_types가 비어 있으면 세목으로 거르지 않습니다.
    """
    allowed = [t.strip() for t in (cfg.tax_types or []) if t and t.strip()]
    if not allowed:
        return True
    return prec.resolved_tax_type in allowed


def build_alerts(
    items: Iterable[tuple[Precedent, Analysis]],
    watchlist: list[Company],
    cfg: NotifyConfig,
    today: date | None = None,
) -> list[Alert]:
    """알림 임계값을 통과한 건만 Alert로 만듭니다."""
    alerts: list[Alert] = []

    for prec, analysis in items:
        if not tax_type_allowed(prec, cfg):
            continue
        if not analysis.refund_claim_possible:
            continue
        if analysis.refund_confidence < cfg.min_confidence:
            continue
        if cfg.require_generalizable and not analysis.generalizable:
            continue

        matches = match_companies(prec, analysis, watchlist)
        if not matches:
            continue

        alerts.append(
            Alert(
                precedent=prec,
                analysis=analysis,
                matches=matches,
                deadlines=compute_deadlines(analysis, prec, today),
            )
        )

    alerts.sort(key=lambda a: a.score, reverse=True)
    return alerts

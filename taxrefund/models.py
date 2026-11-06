"""파이프라인 전체에서 오가는 데이터 구조."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any


def _parse_kdate(value: str | None) -> date | None:
    """'20250314', '2025.03.14', '2025-03-14' 형태를 date로."""
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) != 8:
        return None
    try:
        return datetime.strptime(digits, "%Y%m%d").date()
    except ValueError:
        return None


# 세목 태그로 쓰는 이름. 국세청이 쓰는 표기를 그대로 따릅니다.
# 여기 없는 세목이 나오면 국세청 값을 그대로 살려 둡니다(버리지 않습니다).
TAX_TYPES = (
    "법인세",
    "부가가치세",
    "소득세",
    "상속증여세",
    "종합부동산세",
    "지방세",
    "관세",
    "국제조세",
    "기타세목",
)

# 사건명에서 세목을 읽어낼 때 쓰는 단서. 국세청이 세목을 붙여 주지 않는
# 대법원 출처 건에만 씁니다. 위에서부터 먼저 맞는 것을 택하므로,
# '법인세(원천)' 같은 복합 표기가 '소득세'로 새지 않게 순서가 중요합니다.
_TAX_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("법인세", ("법인세", "법인지방소득세")),
    ("부가가치세", ("부가가치세", "부가세")),
    ("상속증여세", ("상속세", "증여세")),
    ("종합부동산세", ("종합부동산세", "종부세")),
    ("교육세", ("교육세",)),
    ("증권거래세", ("증권거래세",)),
    ("관세", ("관세", "덤핑방지관세")),
    ("지방세", ("지방세", "취득세", "재산세", "주민세", "등록면허세")),
    ("소득세", ("종합소득세", "양도소득세", "근로소득세", "소득세")),
)

# 국세청이 이 값을 주면 세목을 특정한 게 아닙니다. 교육세·증권거래세처럼
# 주요 세목 바깥의 건이 여기로 묶여 오는데, '기타'는 태그로 쓸모가 없어서
# 사건명에서 다시 읽어 봅니다.
VAGUE_TAX_TYPES = frozenset({"", "기타", "기타세목", "-"})

# 국세청이 같은 세목을 여러 표기로 내보냅니다('교육세'와 '교육세법').
# 그대로 두면 태그가 갈라져 세목별로 세면 숫자가 맞지 않습니다.
TAX_TYPE_ALIASES: dict[str, str] = {
    "교육세법": "교육세",
    "법인": "법인세",
    "소득": "소득세",
    "부가": "부가가치세",
    "상증": "상속증여세",
    "국기": "국세기본",
    "조특": "조세특례",
}


def normalize_tax_type(value: str | None) -> str:
    """세목 표기를 하나로 맞춥니다."""
    name = (value or "").strip()
    return TAX_TYPE_ALIASES.get(name, name)


def infer_tax_type(*texts: str) -> str:
    """사건명 등에서 세목을 추정합니다. 못 찾으면 빈 문자열.

    국세청 쪽에서 받은 세목이 있으면 그것을 씁니다. 이 함수는 세목을 주지
    않는 출처(대법원)를 메우는 용도이고, 추정이므로 확정 값보다 뒤에
    놓아야 합니다.
    """
    blob = " ".join(t for t in texts if t)
    if not blob:
        return ""
    # '법인(원천)세'처럼 괄호가 끼어들면 '법인세'가 걸리지 않습니다.
    # 괄호 안은 세목을 좁히는 부연이라 떼어 내고 봅니다.
    blob = re.sub(r"[(（][^)）]*[)）]", "", blob)
    for name, hints in _TAX_HINTS:
        if any(h in blob for h in hints):
            return name
    return ""


@dataclass
class Precedent:
    """국가법령정보센터에서 가져온 판례/해석례 1건."""

    source: str  # "prec" | "expc"
    doc_id: str  # 판례일련번호 / 해석례일련번호
    title: str  # 사건명 / 안건명
    case_no: str  # 사건번호 / 안건번호
    court: str  # 법원명 / 회신기관명
    decided_on: date | None  # 선고일자 / 회신일자
    summary: str  # 판시사항 + 판결요지 (목록 API 기준)
    body: str  # 판례내용 전문
    url: str
    # 세목. 국세법령정보시스템이 붙여 주는 값이 1순위이고, 없으면
    # 사건명에서 추정합니다. 대시보드 태그 검색의 축 하나입니다.
    tax_type: str = ""
    # 본문을 어디서 받아왔는지. "" 법령정보센터 / "nts" 국세법령정보시스템.
    # 본문 출처가 다르면 신뢰도와 확인 경로가 달라서 화면에 표시합니다.
    body_source: str = ""

    @property
    def key(self) -> str:
        return f"{self.source}:{self.doc_id}"

    @property
    def is_sample(self) -> bool:
        """동작 확인용 합성 데이터인지.

        실제 수집분과 섞였을 때 알림·대시보드 어디서든 구분되어야 하므로
        판단 기준을 여기 한 곳에 둡니다.
        """
        return self.doc_id.startswith("SAMPLE-")

    @property
    def decided_str(self) -> str:
        return self.decided_on.isoformat() if self.decided_on else "-"

    @property
    def resolved_tax_type(self) -> str:
        """화면·태그에 쓸 세목.

        국세청이 준 값이 1순위입니다. 다만 '기타'처럼 세목을 특정하지 못한
        값이면 사건명에서 읽어낸 쪽이 더 쓸모 있습니다(교육세 등).
        """
        name = normalize_tax_type(self.tax_type)
        if name not in VAGUE_TAX_TYPES:
            return name
        return infer_tax_type(self.title, self.case_no) or name

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["decided_on"] = self.decided_str if self.decided_on else None
        return d


@dataclass
class Analysis:
    """모델이 판례 1건을 읽고 내린 판단."""

    # 목록에 뜨는 한 줄 요지. 법원이 붙인 사건명은 '법인세부과처분취소'류라
    # 서로 구분되지 않아, 쟁점과 결론이 드러나는 제목을 따로 받습니다.
    headline: str = ""
    is_corporate_tax: bool = False
    outcome: str = "기타"  # 납세자승 | 과세관청승 | 일부승 | 기타
    issue_summary: str = ""
    issue_tags: list[str] = field(default_factory=list)
    applicable_industries: list[str] = field(default_factory=list)
    ksic_codes: list[str] = field(default_factory=list)
    generalizable: bool = False
    generalizable_reason: str = ""
    refund_claim_possible: bool = False
    refund_confidence: int = 0
    refund_rationale: str = ""
    claim_basis: str = ""  # 통상적 경정청구 | 후발적 사유 | 해당없음
    check_points: list[str] = field(default_factory=list)
    estimated_impact: str = "하"  # 상 | 중 | 하
    caveats: str = ""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Analysis":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def display_title(prec: Precedent, analysis: Analysis | None) -> str:
    """화면·알림에 쓸 제목. 제목 결정은 여기 한 곳에서만 합니다.

    법원이 붙인 사건명은 대부분 '법인세부과처분취소'라 서로 구분되지
    않습니다. 그래서 모델이 뽑은 한 줄 요지를 우선 쓰고, 그것이 없으면
    분석에서 뽑힌 쟁점 태그와 결론으로 만들어 씁니다. 사건명으로 물러서는
    것은 둘 다 없을 때뿐입니다.
    """
    if analysis and analysis.headline and analysis.headline.strip():
        return analysis.headline.strip()

    if analysis:
        tags = [t.strip() for t in analysis.issue_tags if t and t.strip()][:3]
        if tags:
            outcome = (analysis.outcome or "").strip()
            head = " · ".join(tags)
            return f"{head} ({outcome})" if outcome and outcome != "기타" else head

    return prec.title


@dataclass
class Match:
    """워치리스트 회사 1곳에 대한 매칭 결과."""

    company: str
    industry: str
    reasons: list[str]
    score: int


@dataclass
class Alert:
    """알림 1건 = 판례 + 분석 + 매칭된 회사들 + 기한 안내."""

    precedent: Precedent
    analysis: Analysis
    matches: list[Match]
    deadlines: list[str]

    @property
    def score(self) -> int:
        return max((m.score for m in self.matches), default=self.analysis.refund_confidence)

    @property
    def title(self) -> str:
        return display_title(self.precedent, self.analysis)

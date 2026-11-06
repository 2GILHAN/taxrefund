"""판례 1건을 읽고 '다른 회사도 경정청구가 가능한 건인지' 판단.

프롬프트·출력 스키마·예외는 여기서 공유하고, 실제 모델 호출은
provider별 모듈이 담당합니다.

    analyzer_gemini.py : Google Gemini (무료 등급 있음)
    analyzer_claude.py : Anthropic Claude
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from .config import AnalyzeConfig, Secrets
from .models import Analysis, Precedent

log = logging.getLogger(__name__)


class CredentialsError(RuntimeError):
    """모델 API 자격증명을 찾지 못했거나 인증에 실패한 경우.

    .env만 채우면 해결되는 상태이므로, serve는 이 예외를 받으면
    죽지 않고 '설정 대기' 상태로 들어갑니다.
    """


class QuotaExceededError(RuntimeError):
    """무료 등급 일일/분당 한도를 소진한 경우.

    이번 사이클만 중단하고 다음 일정에 이어서 진행합니다.
    """


class AnalyzerProtocol(Protocol):
    def analyze(self, prec: Precedent) -> Analysis | None: ...


SYSTEM_PROMPT = """당신은 한국 법인세 실무에 정통한 조세 전문가입니다. 대법원·각급법원 판례와
기획재정부·국세청 법령해석례를 읽고, **그 판단이 같은 업종의 다른 법인에도 적용되어
경정청구(국세기본법 제45조의2)로 세금을 돌려받을 여지가 있는지**를 평가합니다.

## 판단 기준

1) 법인세 관련성
   - 법인세 과세표준·세액 계산(익금/손금, 세액공제·감면, 부당행위계산부인, 이월결손금,
     국제조세 등)에 영향을 주는 쟁점이면 관련 있음으로 봅니다.
   - 부가가치세·소득세·지방세만 다루거나, 절차·형사·민사 쟁점만 있으면 관련 없음입니다.

2) 경정청구 실익 (핵심)
   다음이 모두 충족될 때만 실익이 있다고 봅니다.
   - 납세자에게 유리한 방향의 판단일 것 (전부승 또는 일부승).
   - 과세관청의 기존 해석·실무 관행을 뒤집거나 명확히 좁히는 판단일 것.
     (기존 실무를 그대로 확인한 판례는 이미 반영되어 있으므로 실익이 낮습니다.)
   - 같은 처리를 해온 다른 법인이 실제로 존재할 만한 쟁점일 것.

3) 일반화 가능성 (generalizable)
   - 법령 해석·법리에 관한 판단이면 일반화 가능성이 높습니다.
   - 특정 계약서 문언, 개별 거래의 사실인정, 증거 부족 등 그 사건 고유의
     사실관계에 좌우된 판단이면 일반화 가능성이 낮습니다.
   - 소수의견·방론(傍論)에 불과한 설시는 근거로 삼지 마십시오.

4) 청구 근거 (claim_basis)
   - "통상적 경정청구": 법정신고기한이 지난 후 5년 이내의 과세기간에 대해,
     법리 자체를 근거로 감액을 구할 수 있는 경우.
   - "후발적 사유": 판결의 확정 등으로 거래·행위의 존부나 법률효과가 달라진 경우
     (국세기본법 제45조의2 제2항). 사유를 안 날부터 3개월 이내 청구.
   - 둘 다 아니면 "해당없음".
   - 실제 기한 계산은 호출하는 프로그램이 별도로 하므로, 여기서 날짜를 계산하지 마십시오.

5) 적용 업종 (applicable_industries)
   - 업종을 가리지 않는 일반 법리이면 목록에 정확히 "전업종" 한 항목만 넣습니다.
   - 특정 업종에 한정되면 통계청 표준산업분류(KSIC) 명칭에 가깝게 적습니다.
     (예: "전자부품 제조업", "종합 건설업", "운수 및 창고업")
   - ksic_codes에는 해당 분류의 2자리 코드를 넣습니다. 확신이 없으면 빈 배열로 두십시오.

6) 점수 (refund_confidence, 0-100)
   - 80 이상: 법리가 명확하고 적용 대상이 넓으며 환급 실익이 큼.
   - 60-79 : 실익이 있으나 개별 사실관계 확인이 필요함.
   - 40-59 : 가능성은 있으나 근거가 약하거나 대상이 좁음.
   - 40 미만: 실익이 거의 없음.
   보수적으로 매기십시오. 근거가 본문에 없으면 점수를 올리지 마십시오.

## headline (목록에 뜨는 제목)

법원이 붙인 사건명은 대부분 "법인세부과처분취소", "법인세경정거부처분취소"라서
목록에서 서로 구분되지 않습니다. headline은 **그 사건만의 요지**를 한 줄로 담습니다.

- 무엇이 쟁점이었고 결론이 무엇인지가 드러나야 합니다.
- 사건명·법원명·사건번호를 반복하지 마십시오. 그 정보는 따로 표시됩니다.
- 25~45자 권장. 명사형으로 간결하게 끝내십시오.
- 좋은 예:
    "직원매장 할인액은 접대비 아닌 매출에누리"
    "기존 제품 개량도 연구·인력개발비 세액공제 대상"
    "사규에 따른 전 직원 균등 대여는 부당행위계산부인 제외"
    "해외 지분양도, 현지 정부 승인 없으면 이전 효력 부정"
- 나쁜 예:
    "법인세부과처분취소"            (사건명 반복, 내용 없음)
    "법인세 관련 대법원 판결"        (무엇이든 해당되는 말)
    "원고의 상고를 기각한 사건"      (절차만 있고 쟁점이 없음)

## 작성 규칙

- 판례 본문에 실제로 있는 내용만 근거로 삼고, 없는 사실을 지어내지 마십시오.
- 본문이 잘려 있거나 정보가 부족하면 그 사실을 caveats에 적고 점수를 낮추십시오.
- check_points에는 담당자가 자사 적용 여부를 확인할 때 실제로 들여다봐야 할 항목을
  구체적으로 적으십시오. (예: "2020~2024 사업연도 접대비 계정에 동일 유형 거래가
  계상되었는지", "해당 자산의 내용연수를 몇 년으로 신고했는지")
- 모든 서술은 한국어로 하고, 확정적 단정 대신 사실과 근거를 제시하십시오.
- 이 분석은 참고자료이며 최종 판단은 세무대리인의 검토를 전제로 한다는 점을
  caveats 마지막에 한 문장으로 덧붙이십시오."""

ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": (
                "판결/회신의 요지를 한 줄로. 목록에서 이 한 줄만 보고 무슨 건인지 "
                "알 수 있어야 합니다. 25~45자 권장."
            ),
        },
        "is_corporate_tax": {
            "type": "boolean",
            "description": "법인세 과세표준·세액 계산에 영향을 주는 쟁점인지",
        },
        "outcome": {
            "type": "string",
            "enum": ["납세자승", "과세관청승", "일부승", "기타"],
            "description": "결론의 방향",
        },
        "issue_summary": {
            "type": "string",
            "description": "쟁점과 결론을 3~5문장으로 요약",
        },
        "issue_tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "쟁점 키워드 (예: 부당행위계산부인, 접대비, 연구인력개발비 세액공제)",
        },
        "applicable_industries": {
            "type": "array",
            "items": {"type": "string"},
            "description": "적용 가능 업종. 업종 무관이면 ['전업종']",
        },
        "ksic_codes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "표준산업분류 2자리 코드. 불확실하면 빈 배열",
        },
        "generalizable": {
            "type": "boolean",
            "description": "다른 법인에도 일반적으로 적용 가능한 법리인지",
        },
        "generalizable_reason": {"type": "string", "description": "그렇게 본 이유"},
        "refund_claim_possible": {
            "type": "boolean",
            "description": "같은 업종 다른 법인이 경정청구를 검토할 실익이 있는지",
        },
        "refund_confidence": {
            "type": "integer",
            "description": "경정청구 실익 확신도 0-100",
        },
        "refund_rationale": {"type": "string", "description": "실익 판단의 근거"},
        "claim_basis": {
            "type": "string",
            "enum": ["통상적 경정청구", "후발적 사유", "해당없음"],
        },
        "check_points": {
            "type": "array",
            "items": {"type": "string"},
            "description": "자사 적용 여부 확인 시 점검할 구체적 항목",
        },
        "estimated_impact": {
            "type": "string",
            "enum": ["상", "중", "하"],
            "description": "환급 규모 영향도",
        },
        "caveats": {"type": "string", "description": "한계·유의사항 및 전문가 검토 필요 문구"},
    },
    "required": [
        "headline",
        "is_corporate_tax",
        "outcome",
        "issue_summary",
        "issue_tags",
        "applicable_industries",
        "ksic_codes",
        "generalizable",
        "generalizable_reason",
        "refund_claim_possible",
        "refund_confidence",
        "refund_rationale",
        "claim_basis",
        "check_points",
        "estimated_impact",
        "caveats",
    ],
    "additionalProperties": False,
}


def _build_user_content(prec: Precedent) -> str:
    return (
        f"# 분석 대상\n"
        f"- 구분: {'판례' if prec.source == 'prec' else '법령해석례'}\n"
        f"- 제목: {prec.title}\n"
        f"- 사건(안건)번호: {prec.case_no or '-'}\n"
        f"- 기관: {prec.court or '-'}\n"
        f"- 선고(회신)일: {prec.decided_str}\n"
        f"- 원문: {prec.url}\n\n"
        f"# 요지\n{prec.summary or '(제공되지 않음)'}\n\n"
        f"# 본문\n{prec.body or '(제공되지 않음)'}\n"
    )


def gemini_schema() -> dict[str, Any]:
    """Gemini용 스키마.

    Gemini의 구조화 출력은 additionalProperties를 지원하지 않습니다.
    Analysis.from_json이 모르는 필드를 무시하므로 빼도 안전합니다.
    """

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k != "additionalProperties"}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    return strip(ANALYSIS_SCHEMA)


def parse_analysis(text: str, key: str) -> Analysis | None:
    """모델이 돌려준 JSON 문자열을 Analysis로. 실패하면 None."""
    import json

    if not text or not text.strip():
        log.warning("빈 응답 (%s)", key)
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("JSON 파싱 실패 (%s): %s", key, text[:200])
        return None

    analysis = Analysis.from_json(data)
    analysis.refund_confidence = max(0, min(100, int(analysis.refund_confidence or 0)))
    return analysis


def make_analyzer(cfg: AnalyzeConfig, secrets: Secrets) -> AnalyzerProtocol:
    """설정의 provider에 맞는 분석기를 만듭니다."""
    provider = (cfg.provider or "gemini").strip().lower()

    if provider == "gemini":
        from .analyzer_gemini import GeminiAnalyzer

        return GeminiAnalyzer(cfg, secrets.gemini_api_key)
    if provider in ("claude", "anthropic"):
        from .analyzer_claude import ClaudeAnalyzer

        return ClaudeAnalyzer(cfg, secrets.anthropic_api_key)

    raise ValueError(
        f"알 수 없는 analyze.provider: {cfg.provider!r} (gemini 또는 claude)"
    )


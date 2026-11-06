"""국세법령정보시스템(taxlaw.nts.go.kr)에서 판결문 본문을 받아옵니다.

법령정보센터 OPEN API는 '데이터출처명: 국세법령정보시스템'인 건의 본문을
주지 않습니다. 목록만 연계받아 색인하고 본문은 보유하지 않기 때문입니다.
그 본문은 국세청 쪽에만 있고, 국세청은 OPEN API를 내놓지 않아서 화면이
쓰는 내부 엔드포인트(POST /action.do)를 그대로 호출합니다.

lawgo.py와 달리 이것은 공식 API가 아닙니다. 그래서:

* 국세청이 화면을 고치면 조용히 깨질 수 있습니다. 호출부는 실패를 예외로
  띄우지 말고 '본문 못 받음'으로 넘어가야 합니다.
* 사람이 브라우저로 보는 것보다 빠르게 두드리지 않습니다
  (`request_interval_sec` 기본 1초).
* 검색은 형태소 단위라 사건번호를 넣어도 엉뚱한 건이 섞여 나옵니다.
  받아온 사건번호를 정규화해서 대조가 맞는 건만 씁니다. 안 맞으면
  버립니다. 본문이 틀리면 분석 전체가 틀립니다.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

import requests

from .models import _parse_kdate

log = logging.getLogger(__name__)

BASE = "https://taxlaw.nts.go.kr"
ACTION_URL = f"{BASE}/action.do"
LIST_PAGE = f"{BASE}/pd/USEPDI001M.do"
DETAIL_PAGE = f"{BASE}/pd/USEPDA002P.do"

SEARCH_ACTION = "ASIPDI002PR01"
DETAIL_ACTION = "ASIQTB002PR01"

# 국세법령문서구분코드. 001_09 판례 / 001_08 심판 / 001_07 심사.
DOC_CLASS_PRECEDENT = "001_09"

# 검색 결과의 강조 표시. 본문·사건번호에 그대로 섞여 나옵니다.
_HILITE = re.compile(r"<!H[SE]>")

# '대법원-2026-두-30683', '대법원2026두30340', '2020구합86484' 어느 쪽이든
# 연도+사건부호+번호만 뽑습니다. 법원명 표기가 출처마다 달라서 그것으로는
# 대조할 수 없습니다.
_CASE_CORE = re.compile(r"(\d{4})\s*-?\s*([가-힣]{1,4})\s*-?\s*(\d+)")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class NtsError(RuntimeError):
    """국세청 엔드포인트가 응답을 주지 못한 경우."""


def clean(text: str | None) -> str:
    """강조 표시를 걷어냅니다."""
    if not text:
        return ""
    return _HILITE.sub("", str(text)).strip()


def html_to_text(raw: str | None) -> str:
    """판결문 HTML을 읽을 수 있는 평문으로.

    표(사건/원고/피고/주문)가 많아서 셀 경계를 살려야 문장이 붙지 않습니다.
    """
    if not raw:
        return ""
    text = _HILITE.sub("", str(raw))
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)</(td|th)>", "\t", text)
    text = re.sub(r"(?i)<(br\s*/?|/p|/div|/tr|/h[1-6]|/li)\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]*\t[ \t]*", "\t", text)
    text = re.sub(r"[ ]{2,}", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def case_keys(case_no: str | None) -> set[str]:
    """사건번호에서 대조용 열쇠를 뽑습니다.

    병합사건은 사건번호가 여러 개 붙어 있어서 집합으로 돌려줍니다.
    한쪽이라도 겹치면 같은 사건으로 봅니다.
    """
    if not case_no:
        return set()
    found = _CASE_CORE.findall(clean(case_no))
    return {f"{y}{mark}{int(num)}" for y, mark, num in found}


def normalize_case_no(case_no: str | None) -> str:
    """검색어로 쓸 대표 사건번호 하나. 없으면 빈 문자열."""
    found = _CASE_CORE.findall(clean(case_no))
    if not found:
        return ""
    year, mark, num = found[0]
    return f"{year}{mark}{num}"


@dataclass
class NtsRecord:
    """검색 결과 1건 (본문 제외)."""

    doc_id: str
    title: str
    case_no: str
    tax_type: str  # 법인세 | 부가가치세 | 소득세 | 상속증여세 ...
    gist: str  # 판시요지
    registered_on: date | None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def keys(self) -> set[str]:
        return case_keys(self.case_no)


@dataclass
class NtsDocument:
    """본문까지 받아 온 1건."""

    doc_id: str
    title: str
    case_no: str
    tax_type: str
    gist: str
    body: str

    @property
    def url(self) -> str:
        return f"{DETAIL_PAGE}?ntstDcmId={self.doc_id}"


class NtsClient:
    def __init__(
        self,
        *,
        request_interval_sec: float = 1.0,
        timeout: int = 30,
        max_retries: int = 2,
        session: requests.Session | None = None,
    ) -> None:
        self.request_interval_sec = request_interval_sec
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = session
        self._primed = session is not None
        self._last_call = 0.0

    # -- 하부 ------------------------------------------------------------
    def _ensure_session(self) -> requests.Session:
        """세션 쿠키(JSESSIONID)가 없으면 검색이 0건으로 나옵니다."""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(
                {
                    "User-Agent": USER_AGENT,
                    "Referer": LIST_PAGE,
                    "X-Requested-With": "XMLHttpRequest",
                }
            )
        if not self._primed:
            try:
                self._session.get(LIST_PAGE, timeout=self.timeout)
            except requests.RequestException as exc:
                log.debug("국세청 목록 페이지 예열 실패: %s", exc)
            self._primed = True
        return self._session

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_call
        if gap < self.request_interval_sec:
            time.sleep(self.request_interval_sec - gap)
        self._last_call = time.monotonic()

    def _call(self, action: str, param: dict[str, Any]) -> dict[str, Any]:
        session = self._ensure_session()
        payload = {"actionId": action, "paramData": json.dumps(param, ensure_ascii=False)}
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = session.post(ACTION_URL, data=payload, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                last = exc
                if attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                continue
            if data.get("status") != "SUCCESS":
                raise NtsError(f"{action}: status={data.get('status')} {data.get('message')}")
            return (data.get("data") or {}).get(action) or {}
        raise NtsError(f"{action}: 응답을 받지 못했습니다 ({last})")

    # -- 검색 ------------------------------------------------------------
    def search(
        self,
        keyword: str,
        *,
        size: int = 10,
        start: int = 0,
        doc_classes: Iterable[str] = (DOC_CLASS_PRECEDENT,),
    ) -> list[NtsRecord]:
        param: dict[str, Any] = {
            "collectionName": "precedent,precedent_gr",
            # 이 값이 'RANK' 같은 이름이면 서버가 조용히 0건을 돌려줍니다.
            "sortField": "DCM_RGT_DTM/DESC",
            "startCount": start,
            "viewCount": size,
            "dcmClCdCtl": list(doc_classes),
        }
        if keyword:
            param["icldVcbCtl"] = [keyword]  # 포함어
        result = self._call(SEARCH_ACTION, param)
        return [_to_record(item.get("dcm") or {}) for item in (result.get("body") or [])]

    def _search_quietly(self, keyword: str, size: int) -> list[NtsRecord]:
        try:
            return self.search(keyword, size=size)
        except NtsError as exc:
            log.warning("국세청 검색 실패 (%s): %s", keyword, exc)
            return []

    def find_by_case_no(self, case_no: str, title: str = "") -> NtsRecord | None:
        """사건번호로 1건을 찾습니다. 대조가 맞지 않으면 None.

        검색어를 바꿔 가며 후보를 모으되, **채택 기준은 언제나 사건번호
        대조**입니다. 질의 방법이 느슨해져도 잘못된 본문이 붙지 않습니다.

        사건번호가 '2025두1'처럼 짧으면 검색이 이를 '2025', '두', '1'로
        쪼개서 엉뚱한 2025두○○○○○ 사건이 앞자리를 채웁니다. 그래서
        사건명의 특징적인 낱말로 한 번 더 찾아봅니다.
        """
        wanted = case_keys(case_no)
        if not wanted:
            return None

        queries = [normalize_case_no(case_no)]
        queries.extend(_title_queries(title))

        for i, query in enumerate(queries):
            if not query:
                continue
            # 첫 질의는 짧게, 이후는 넓게 봅니다.
            for rec in self._search_quietly(query, size=10 if i == 0 else 30):
                if rec.keys & wanted:
                    if i:
                        log.debug("사건명으로 찾음: %s (검색어 %r)", case_no, query)
                    return rec
        log.debug("국세청에서 못 찾음: %s", case_no)
        return None

    # -- 본문 ------------------------------------------------------------
    def fetch_document(self, doc_id: str) -> NtsDocument | None:
        try:
            result = self._call(DETAIL_ACTION, {"dcmDVO": {"ntstDcmId": doc_id}})
        except NtsError as exc:
            log.warning("국세청 본문 조회 실패 (%s): %s", doc_id, exc)
            return None
        dvo = result.get("dcmDVO") or {}
        # 본문은 첨부된 한글 문서를 HTML로 변환한 것이고, 목록에 여러 조각이
        # 들어옵니다. 조각 하나만 쓰면 '이유'가 통째로 빠질 수 있습니다.
        chunks = [
            html_to_text(e.get("dcmFleByte"))
            for e in (result.get("dcmHwpEditorDVOList") or [])
            if e.get("dcmFleByte")
        ]
        body = "\n\n".join(c for c in chunks if c).strip()
        return NtsDocument(
            doc_id=str(dvo.get("ntstDcmId") or doc_id),
            title=clean(dvo.get("ntstDcmTtl")),
            case_no=clean(dvo.get("ntstDcmDscmCntn")),
            tax_type=clean(dvo.get("ntstTlawClNm")),
            gist=clean(dvo.get("ntstDcmGistCntn")),
            body=body,
        )

    def fetch_by_case_no(self, case_no: str, title: str = "") -> NtsDocument | None:
        """사건번호 하나로 검색부터 본문까지. 못 찾으면 None."""
        rec = self.find_by_case_no(case_no, title)
        if rec is None:
            return None
        doc = self.fetch_document(rec.doc_id)
        if doc is None:
            return None
        # 상세 응답에 없는 값은 목록 쪽 값으로 채웁니다.
        doc.tax_type = doc.tax_type or rec.tax_type
        doc.gist = doc.gist or rec.gist
        doc.title = doc.title or rec.title
        doc.case_no = doc.case_no or rec.case_no
        return doc


# 사건명 어디에나 나와서 검색어로는 쓸모가 없는 낱말.
_STOPWORDS = frozenset(
    {
        "법인세", "부가가치세", "소득세", "종합소득세", "상속세", "증여세",
        "부과처분취소", "경정거부처분취소", "부과처분", "경정거부", "취소",
        "심리불속행", "해당하는지", "해당함", "해당하지", "여부", "관하여",
        "대하여", "경우에는", "것으로", "봄이", "타당함", "있는지", "없음",
    }
)


def _title_queries(title: str, limit: int = 2) -> list[str]:
    """사건명에서 검색어로 쓸 만한 낱말을 뽑습니다.

    긴 낱말일수록 그 사건에만 나올 가능성이 높습니다('발급사일일분담금').
    흔한 세목·처분 이름은 어느 사건에나 있어 후보를 좁히지 못합니다.

    '법인세부과처분취소'처럼 흔한 말이 띄어쓰기 없이 붙어 한 덩어리로
    잡히는 사건명이 많아서, 낱말이 같은지가 아니라 **흔한 말을 품고
    있는지**로 걸러냅니다.
    """
    words = re.findall(r"[가-힣A-Za-z]{4,}", clean(title))
    useful = {w for w in words if not any(s in w for s in _STOPWORDS)}
    return sorted(useful, key=len, reverse=True)[:limit]


def _to_record(dcm: dict[str, Any]) -> NtsRecord:
    return NtsRecord(
        doc_id=str(dcm.get("DOC_ID") or ""),
        title=clean(dcm.get("NTST_DCM_TTL") or dcm.get("TTL")),
        case_no=clean(dcm.get("NTST_DCM_DSCM_CNTN")),
        tax_type=clean(dcm.get("NTST_TLAW_CL_NM")),
        gist=clean(dcm.get("GIST_CNTN")),
        registered_on=_parse_kdate(str(dcm.get("NTST_DCM_RGT_DT") or "")[:8]),
        raw=dcm,
    )

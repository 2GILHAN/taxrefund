"""국가법령정보센터(law.go.kr) OPEN API 클라이언트.

공식 OPEN API만 사용합니다. HTML 스크래핑은 하지 않습니다.
사용 전 https://open.law.go.kr/LSO/openApi/cuAskList.do 에서
활용 신청(무료)을 하고, 봇을 돌릴 서버의 공인 IP를 등록해야 합니다.
"""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import date, timedelta
from typing import Any, Iterator

import requests

from .models import Precedent, _parse_kdate

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"
SERVICE_URL = "https://www.law.go.kr/DRF/lawService.do"

# 목록/본문 응답의 필드명이 target마다 달라서 별칭으로 흡수합니다.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "doc_id": ("판례일련번호", "판례정보일련번호", "법령해석례일련번호", "해석례일련번호", "id", "ID"),
    "title": ("사건명", "안건명", "제목"),
    "case_no": ("사건번호", "안건번호", "질의기관코드"),
    "court": ("법원명", "회신기관명", "해석기관명", "기관명"),
    "decided_on": ("선고일자", "회신일자", "해석일자", "등록일자"),
    "url": ("판례상세링크", "법령해석례상세링크", "상세링크"),
}

BODY_FIELDS = ("판례내용", "이유", "회답", "질의요지", "판결요지", "판시사항")
SUMMARY_FIELDS = ("판시사항", "판결요지", "질의요지", "회답", "요지")


class LawApiError(RuntimeError):
    """OPEN API가 사용자 검증 실패 등 오류를 돌려준 경우."""


def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _pick(data: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = data.get(name)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""


def _find_records(payload: Any) -> list[dict[str, Any]]:
    """응답 JSON 어디에 있든 레코드 리스트를 찾아냅니다.

    target(prec/expc)마다 루트 키가 달라서(PrecSearch/ExpcSearch...)
    키 이름에 의존하지 않고 구조로 탐색합니다.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key, value in payload.items():
        if key in ("result", "msg", "totalCnt", "page", "numOfRows"):
            continue
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
        if isinstance(value, dict):
            found = _find_records(value)
            if found:
                return found
            # 단건 응답: 레코드 자체가 dict인 경우
            if any(k in value for k in FIELD_ALIASES["title"] + FIELD_ALIASES["doc_id"]):
                return [value]
    return []


class LawGoKrClient:
    def __init__(self, oc: str, interval_sec: float = 0.6, timeout: int = 30):
        if not oc:
            raise LawApiError(
                "LAW_API_OC가 비어 있습니다. .env에 국가법령정보 OPEN API 신청 시 사용한 "
                "이메일 아이디(@ 앞부분)를 LAW_API_OC로 설정하세요."
            )
        self.oc = oc
        self.interval = interval_sec
        self.timeout = timeout
        self.session = requests.Session()
        # Referer가 없으면 OC와 IP가 모두 정상이어도
        # "사용자 정보 검증에 실패하였습니다 / 서버장비의 IP주소 및 도메인주소를
        # 등록해 주세요"를 돌려줍니다. 메시지가 IP 문제처럼 보이지만 실제로는
        # 헤더 검사입니다. 이 한 줄이 없으면 모든 호출이 거부됩니다.
        self.session.headers.update(
            {
                "Referer": "https://www.law.go.kr/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            }
        )
        self._last_call = 0.0

    # -- 내부 ---------------------------------------------------------
    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_call
        if gap < self.interval:
            time.sleep(self.interval - gap)
        self._last_call = time.monotonic()

    def _get(self, url: str, params: dict[str, Any]) -> Any:
        self._throttle()
        params = {"OC": self.oc, "type": "JSON", **params}
        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()

        text = resp.text.strip()
        if not text:
            return {}
        try:
            payload = resp.json()
        except ValueError:
            # 미등록 IP 등에서 HTML 오류 페이지를 돌려주는 경우
            raise LawApiError(
                "OPEN API가 JSON이 아닌 응답을 반환했습니다. OC 값과 서버 IP 등록 상태를 "
                f"확인하세요. 응답 앞부분: {text[:200]}"
            ) from None

        if isinstance(payload, dict) and "result" in payload and "msg" in payload:
            raise LawApiError(
                f"OPEN API 오류: {payload.get('result')} / {payload.get('msg')}\n"
                "-> https://open.law.go.kr/LSO/openApi/cuAskList.do 에서 활용 신청 후 "
                "이 봇을 실행하는 서버의 공인 IP를 등록해야 합니다."
            )
        return payload

    # -- 공개 API ------------------------------------------------------
    def search(self, target: str, query: str, display: int = 50, page: int = 1) -> list[dict[str, Any]]:
        payload = self._get(
            SEARCH_URL,
            {
                "target": target,
                "query": query,
                "display": min(max(display, 1), 100),
                "page": page,
                "sort": "ddes",  # 선고일자 내림차순
            },
        )
        return _find_records(payload)

    def fetch(self, target: str, doc_id: str) -> dict[str, Any]:
        payload = self._get(SERVICE_URL, {"target": target, "ID": doc_id})
        records = _find_records(payload)
        return records[0] if records else {}

    def collect(
        self,
        targets: list[str],
        queries: list[str],
        since_days: int,
        max_per_query: int,
        max_body_chars: int,
        skip_keys: set[str] | None = None,
        max_pages: int = 50,
    ) -> Iterator[Precedent]:
        """조건에 맞는 판례/해석례를 중복 없이 순회합니다.


        본문을 받지 못한 건은 내보내지 않습니다. 이 API의 판례는 출처가
        '대법원'이면 판시사항·판결요지·판례내용이 오지만, '국세법령정보시스템'
        출처는 상세 조회가 되지 않아 제목·사건번호만 남습니다. 제목만으로
        경정청구 실익을 판단하게 하면 근거 없는 추측이 나오므로 제외합니다.

        결과는 선고일 내림차순이므로, 기간 밖 건만 담긴 페이지를 만나면 그
        검색어는 더 볼 것이 없습니다. skip_keys에 이미 본문까지 받아 둔 키를
        넘기면 상세 조회를 건너뛰어, 과거 구간을 채우는 동안 같은 건을 매일
        다시 긁지 않습니다.
        """
        cutoff = date.today() - timedelta(days=since_days)
        seen: set[str] = set()
        already = skip_keys or set()
        counters = {"no_body": 0, "known": 0}

        for target in targets:
            for query in queries:
                for page in range(1, max_pages + 1):
                    try:
                        records = self.search(
                            target, query, display=max_per_query, page=page
                        )
                    except LawApiError:
                        raise
                    except requests.RequestException as exc:
                        log.warning("검색 실패 (%s/%s p%d): %s", target, query, page, exc)
                        break

                    if not records:
                        break

                    parsed = [
                        d for d in (
                            _parse_kdate(_pick(r, FIELD_ALIASES["decided_on"]))
                            for r in records
                        ) if d
                    ]
                    fresh = [d for d in parsed if d >= cutoff]
                    log.info(
                        "[%s] '%s' p%d: %d건 (기간 내 %d건)",
                        target, query, page, len(records), len(fresh),
                    )

                    yield from self._emit(
                        target, records, cutoff, max_body_chars, seen, already, counters
                    )

                    # 내림차순이므로 이 페이지가 통째로 기간 밖이면 끝입니다.
                    # 날짜를 못 읽은 건은 판단 근거가 아닙니다. parsed가 비어
                    # 있는데 멈추면, 날짜 형식이 다른 페이지 하나 때문에 그
                    # 뒤 구간을 통째로 놓칩니다.
                    if parsed and not fresh:
                        break
                    if len(records) < max_per_query:
                        break

        if counters["known"]:
            log.info("이미 받아 둔 %d건은 상세 조회를 건너뛰었습니다.", counters["known"])
        if counters["no_body"]:
            log.info(
                "본문을 받지 못한 건: %d건 (분석 대상에서 빠집니다) "
                "(국세법령정보시스템 출처는 이 API로 본문이 제공되지 않습니다)",
                counters["no_body"],
            )

    def _emit(
        self,
        target: str,
        records: list[dict[str, Any]],
        cutoff: date,
        max_body_chars: int,
        seen: set[str],
        already: set[str],
        counters: dict[str, int],
    ) -> Iterator[Precedent]:
        """검색 결과 한 페이지를 Precedent로 바꿉니다."""
        for rec in records:
            doc_id = _pick(rec, FIELD_ALIASES["doc_id"])
            if not doc_id:
                continue
            key = f"{target}:{doc_id}"
            if key in seen:
                continue
            seen.add(key)
            if key in already:
                counters["known"] += 1
                continue

            decided = _parse_kdate(_pick(rec, FIELD_ALIASES["decided_on"]))
            if decided and decided < cutoff:
                continue  # sort=ddes라 이후 건은 더 오래됨 -> 계속 훑되 필터만

            try:
                detail = self.fetch(target, doc_id)
            except LawApiError:
                raise
            except requests.RequestException as exc:
                log.warning("본문 조회 실패 (%s): %s", key, exc)
                detail = {}

            merged = {**rec, **detail}
            decided = decided or _parse_kdate(_pick(merged, FIELD_ALIASES["decided_on"]))
            if decided and decided < cutoff:
                continue

            body = ""
            for f in BODY_FIELDS:
                chunk = _strip_html(str(merged.get(f, "") or ""))
                if chunk:
                    body += f"\n\n### {f}\n{chunk}"
            body = body.strip()[:max_body_chars]

            summary = "\n".join(
                _strip_html(str(merged.get(f, "") or "")) for f in SUMMARY_FIELDS
            ).strip()

            if not body and not summary:
                # 내보내긴 합니다. 저장해 두지 않으면 매일 다시 상세 조회하게
                # 됩니다. 본문이 비어 있으므로 분석 큐에는 들어가지 않습니다.
                counters["no_body"] += 1
                log.debug(
                    "본문 없음: %s (%s) 출처=%s",
                    key,
                    _pick(merged, FIELD_ALIASES["title"])[:40],
                    merged.get("데이터출처명", "?"),
                )

            url = _pick(merged, FIELD_ALIASES["url"])
            if url and url.startswith("/"):
                url = "https://www.law.go.kr" + url
            if not url:
                url = (
                    f"https://www.law.go.kr/DRF/lawService.do?OC={self.oc}"
                    f"&target={target}&ID={doc_id}&type=HTML"
                )

            yield Precedent(
                source=target,
                doc_id=doc_id,
                title=_pick(merged, FIELD_ALIASES["title"]) or "(제목없음)",
                case_no=_pick(merged, FIELD_ALIASES["case_no"]),
                court=_pick(merged, FIELD_ALIASES["court"]),
                decided_on=decided,
                summary=summary[:4000],
                body=body,
                url=url,
            )

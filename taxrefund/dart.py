"""DART(금융감독원 전자공시) OPEN API 클라이언트.

목적: 판례가 걸리는 **업종**을 알아냈을 때 그 업종의 외부감사대상 법인을
찾아내는 것. DART에는 업종으로 검색하는 API가 없어서, 기업별 업종코드를
한 번 긁어 로컬에 색인해 두고 그걸 조회합니다.

색인 절차
    1) corpCode.xml  : 전체 고유번호 목록 (ZIP 1회 다운로드)
    2) list.json     : 최근 사업보고서 제출 기업만 추림
                       (전체 10만여 곳 중 실제 외감/상장 대상만 남김)
    3) company.json  : 기업별 업종코드·사업자번호·결산월 수집
"""

from __future__ import annotations

import io
import logging
import time
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterator
from xml.etree import ElementTree

import requests

log = logging.getLogger(__name__)

BASE = "https://opendart.fss.or.kr/api"

# 법인구분
CORP_CLS = {"Y": "유가증권", "K": "코스닥", "N": "코넥스", "E": "기타(외감 등)"}


class DartError(RuntimeError):
    """DART API가 오류를 돌려준 경우."""


# DART가 돌려주는 상태코드
STATUS_MESSAGE = {
    "010": "등록되지 않은 인증키입니다.",
    "011": "사용할 수 없는 인증키입니다. (사용중지/폐기)",
    "012": "접근할 수 없는 IP입니다.",
    "013": "조회된 데이터가 없습니다.",
    "020": "요청 제한을 초과했습니다. (일 20,000건)",
    "021": "조회 가능한 회사 개수가 초과했습니다.",
    "100": "필드의 부적절한 값입니다.",
    "101": "부적절한 접근입니다.",
    "800": "시스템 점검 중입니다.",
    "900": "정의되지 않은 오류입니다.",
    "901": "사용자 계정의 개인정보보유기간이 만료되었습니다.",
}


@dataclass
class Company:
    corp_code: str
    corp_name: str
    corp_cls: str = ""  # Y/K/N/E
    stock_code: str = ""
    bizr_no: str = ""  # 사업자등록번호
    jurir_no: str = ""  # 법인등록번호
    induty_code: str = ""  # 업종코드 (한국표준산업분류)
    adres: str = ""
    est_dt: str = ""  # 설립일 YYYYMMDD
    ceo_nm: str = ""
    acc_mt: str = ""  # 결산월 (경정청구 기한 계산에 씁니다)

    @property
    def is_listed(self) -> bool:
        return self.corp_cls in ("Y", "K", "N")

    @property
    def cls_label(self) -> str:
        return CORP_CLS.get(self.corp_cls, self.corp_cls or "-")


class DartClient:
    def __init__(self, api_key: str, interval_sec: float = 0.12, timeout: int = 30):
        if not api_key:
            raise DartError(
                "DART API 키가 없습니다. https://opendart.fss.or.kr/ 에서 무료로 발급받아 "
                ".env의 DART_API_KEY에 넣으세요."
            )
        self.api_key = api_key
        self.interval = interval_sec
        self.timeout = timeout
        self.session = requests.Session()
        self._last_call = 0.0

    # -- 내부 ---------------------------------------------------------
    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_call
        if gap < self.interval:
            time.sleep(self.interval - gap)
        self._last_call = time.monotonic()

    def _check(self, payload: dict) -> dict:
        status = str(payload.get("status", "000"))
        if status in ("000", "013"):  # 013 = 데이터 없음 (오류 아님)
            return payload
        detail = STATUS_MESSAGE.get(status, payload.get("message", ""))
        raise DartError(f"DART 오류 {status}: {detail}")

    def _get_json(self, path: str, **params) -> dict:
        self._throttle()
        resp = self.session.get(
            f"{BASE}/{path}", params={"crtfc_key": self.api_key, **params},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        try:
            return self._check(resp.json())
        except ValueError:
            raise DartError(f"JSON이 아닌 응답: {resp.text[:200]}") from None

    # -- 공개 API ------------------------------------------------------
    def corp_codes(self) -> list[Company]:
        """전체 고유번호 목록. ZIP 안의 XML을 파싱합니다."""
        self._throttle()
        resp = self.session.get(
            f"{BASE}/corpCode.xml", params={"crtfc_key": self.api_key}, timeout=120
        )
        resp.raise_for_status()

        # 인증 오류 등은 ZIP이 아니라 XML로 옵니다.
        if not resp.content[:2] == b"PK":
            try:
                root = ElementTree.fromstring(resp.content)
                status = (root.findtext("status") or "").strip()
                raise DartError(
                    f"DART 오류 {status}: "
                    f"{STATUS_MESSAGE.get(status, root.findtext('message') or '')}"
                )
            except ElementTree.ParseError:
                raise DartError(f"예상치 못한 응답: {resp.content[:200]!r}") from None

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
            root = ElementTree.fromstring(zf.read(name))

        out: list[Company] = []
        for item in root.iterfind("list"):
            code = (item.findtext("corp_code") or "").strip()
            if not code:
                continue
            out.append(
                Company(
                    corp_code=code,
                    corp_name=(item.findtext("corp_name") or "").strip(),
                    stock_code=(item.findtext("stock_code") or "").strip(),
                )
            )
        log.info("DART 고유번호 %d건 확보", len(out))
        return out

    def annual_report_filers(self, years: int = 2) -> set[str]:
        """최근 N년 사업보고서를 제출한 회사의 고유번호.

        corpCode 전체(10만여 곳)에는 폐업·휴면 법인이 많이 섞여 있습니다.
        사업보고서를 실제로 낸 곳만 추리면 외감/상장 대상만 남고, 이후
        기업개황 호출 수도 크게 줄어듭니다.
        """
        end = date.today()
        start = end - timedelta(days=365 * years)
        codes: set[str] = set()
        page = 1

        while True:
            data = self._get_json(
                "list.json",
                bgn_de=start.strftime("%Y%m%d"),
                end_de=end.strftime("%Y%m%d"),
                pblntf_detail_ty="A001",  # 사업보고서
                page_no=page,
                page_count=100,
            )
            if str(data.get("status")) == "013":
                break
            for item in data.get("list") or []:
                if item.get("corp_code"):
                    codes.add(item["corp_code"])

            total_page = int(data.get("total_page") or 1)
            if page % 20 == 0 or page == total_page:
                log.info("사업보고서 제출기업 수집 %d/%d 페이지 (%d곳)",
                         page, total_page, len(codes))
            if page >= total_page:
                break
            page += 1

        return codes

    def company(self, corp_code: str) -> Company | None:
        """기업개황. 업종코드·사업자번호·결산월이 여기 있습니다."""
        data = self._get_json("company.json", corp_code=corp_code)
        if str(data.get("status")) == "013" or not data.get("corp_name"):
            return None
        return Company(
            corp_code=corp_code,
            corp_name=(data.get("corp_name") or "").strip(),
            corp_cls=(data.get("corp_cls") or "").strip(),
            stock_code=(data.get("stock_code") or "").strip(),
            bizr_no=(data.get("bizr_no") or "").strip(),
            jurir_no=(data.get("jurir_no") or "").strip(),
            induty_code=(data.get("induty_code") or "").strip(),
            adres=(data.get("adres") or "").strip(),
            est_dt=(data.get("est_dt") or "").strip(),
            ceo_nm=(data.get("ceo_nm") or "").strip(),
            acc_mt=(data.get("acc_mt") or "").strip(),
        )

    def profiles(self, corp_codes: list[str]) -> Iterator[Company]:
        """여러 회사의 기업개황을 순차 수집. 일일 한도에 걸리면 중단합니다."""
        for i, code in enumerate(corp_codes, 1):
            try:
                company = self.company(code)
            except DartError as exc:
                if "20,000" in str(exc) or "020" in str(exc):
                    log.warning("DART 일일 한도 도달 (%d건 처리). 내일 이어서 진행하세요.", i - 1)
                    return
                log.debug("기업개황 실패 %s: %s", code, exc)
                continue
            except requests.RequestException as exc:
                log.debug("기업개황 통신 실패 %s: %s", code, exc)
                continue
            if company:
                yield company
            if i % 500 == 0:
                log.info("기업개황 %d/%d 수집", i, len(corp_codes))

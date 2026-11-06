"""국세법령정보시스템 본문 보충."""

from __future__ import annotations

import json

import pytest

from taxrefund import nts
from taxrefund.nts import _title_queries
from taxrefund.models import Precedent, infer_tax_type
from taxrefund.store import MAX_BODY_TRIES, Store


# ---------------------------------------------------------------- 사건번호

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("대법원-2026-두-30683", {"2026두30683"}),
        ("대법원2026두30340", {"2026두30340"}),
        ("서울행정법원-2020-구합-86484", {"2020구합86484"}),
        ("<!HS>서울행정법원<!HE>-<!HS>2020<!HE>-<!HS>구합<!HE>-<!HS>86484<!HE>", {"2020구합86484"}),
        ("2020구합86484, 2020구합86491", {"2020구합86484", "2020구합86491"}),
        ("", set()),
        ("사건번호 없음", set()),
    ],
)
def test_case_keys(raw, expected):
    assert nts.case_keys(raw) == expected


def test_case_keys_ignores_court_name_differences():
    """출처마다 법원 표기가 달라도 같은 사건으로 봐야 합니다."""
    assert nts.case_keys("대법원-2026-두-30683") & nts.case_keys("2026두30683")


def test_normalize_case_no_picks_first():
    assert nts.normalize_case_no("2020구합86484, 2020구합86491") == "2020구합86484"
    assert nts.normalize_case_no("숫자없음") == ""


def test_case_keys_strips_leading_zeros_consistently():
    """한쪽에만 0이 붙어 와도 대조가 되어야 합니다."""
    assert nts.case_keys("2020구합0086484") == nts.case_keys("2020구합86484")


# ---------------------------------------------------------------- 본문 변환

def test_html_to_text_keeps_table_cells_apart():
    out = nts.html_to_text(
        "<table><tr><td>사 건</td><td>2020구합86484</td></tr></table>"
    )
    assert "사 건" in out and "2020구합86484" in out
    assert "사 건2020" not in out  # 셀이 붙으면 안 됩니다


def test_html_to_text_drops_script_and_unescapes():
    out = nts.html_to_text("<script>var a=1;</script><p>&lt;주문&gt;&nbsp;기각</p>")
    assert "var a" not in out
    assert "<주문> 기각" in out


def test_html_to_text_handles_none():
    assert nts.html_to_text(None) == ""
    assert nts.clean(None) == ""


# ---------------------------------------------------------------- 클라이언트

class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeSession:
    """action.do 응답을 흉내 냅니다."""

    def __init__(self, search_body=None, detail=None):
        self.search_body = search_body if search_body is not None else []
        self.detail = detail or {}
        self.calls = []
        self.headers = {}

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return FakeResponse({})

    def post(self, url, data=None, **kw):
        action = data["actionId"]
        self.calls.append((action, json.loads(data["paramData"])))
        payload = self.search_body if action == nts.SEARCH_ACTION else self.detail
        return FakeResponse({"status": "SUCCESS", "data": {action: payload}})


def make_client(session):
    return nts.NtsClient(request_interval_sec=0, session=session, max_retries=0)


def hit(case_no="서울행정법원-2020-구합-86484", doc_id="D1", tax="법인세"):
    return {
        "dcm": {
            "DOC_ID": doc_id,
            "NTST_DCM_DSCM_CNTN": case_no,
            "NTST_TLAW_CL_NM": tax,
            "GIST_CNTN": "요지입니다",
            "NTST_DCM_RGT_DT": "20230629000000",
            "TTL": "제목",
        }
    }


def detail_payload(body_html="<p>주 문</p><p>기각한다</p>", **dvo):
    base = {
        "ntstDcmId": "D1",
        "ntstDcmTtl": "사용료소득에 해당함",
        "ntstDcmDscmCntn": "서울행정법원-2020-구합-86484",
        "ntstDcmGistCntn": "판시요지",
    }
    base.update(dvo)
    return {
        "dcmDVO": base,
        "dcmHwpEditorDVOList": [
            {"dcmFleId": "x"},  # 본문 없는 조각도 섞여 옵니다
            {"dcmFleByte": body_html},
        ],
    }


def test_search_sends_sort_field_that_works():
    """sortField가 'RANK' 같은 값이면 서버가 조용히 0건을 돌려줍니다."""
    session = FakeSession(search_body={"body": [hit()]})
    make_client(session).search("2020구합86484")
    _, param = session.calls[0]
    assert param["sortField"] == "DCM_RGT_DTM/DESC"
    assert param["dcmClCdCtl"] == [nts.DOC_CLASS_PRECEDENT]
    assert param["icldVcbCtl"] == ["2020구합86484"]


def test_find_by_case_no_matches():
    session = FakeSession(search_body={"body": [hit()]})
    rec = make_client(session).find_by_case_no("서울행정법원-2020-구합-86484")
    assert rec is not None
    assert rec.doc_id == "D1"
    assert rec.tax_type == "법인세"


def test_find_by_case_no_rejects_mismatch():
    """검색이 형태소 단위라 엉뚱한 건이 섞여 옵니다. 대조가 안 맞으면 버립니다."""
    session = FakeSession(search_body={"body": [hit(case_no="대구지법-2019-구합-1111")]})
    assert make_client(session).find_by_case_no("서울행정법원-2020-구합-86484") is None


def test_find_by_case_no_picks_correct_hit_among_others():
    session = FakeSession(
        search_body={"body": [hit(case_no="대구지법-2019-구합-1111", doc_id="X"), hit()]}
    )
    rec = make_client(session).find_by_case_no("2020구합86484")
    assert rec.doc_id == "D1"


def test_find_by_case_no_without_number():
    session = FakeSession(search_body={"body": [hit()]})
    assert make_client(session).find_by_case_no("사건번호없음") is None
    assert session.calls == []  # 검색조차 하지 않습니다


def test_fetch_document_joins_all_chunks():
    """조각 하나만 쓰면 '이유'가 통째로 빠질 수 있습니다."""
    payload = detail_payload()
    payload["dcmHwpEditorDVOList"].append({"dcmFleByte": "<p>이 유</p><p>원고 주장</p>"})
    doc = make_client(FakeSession(detail=payload)).fetch_document("D1")
    assert "기각한다" in doc.body
    assert "원고 주장" in doc.body


def test_fetch_by_case_no_fills_missing_fields_from_search():
    """상세 응답에 세목이 없어 목록 쪽 값으로 채워야 합니다."""
    session = FakeSession(search_body={"body": [hit(tax="부가가치세")]},
                          detail=detail_payload())
    doc = make_client(session).fetch_by_case_no("2020구합86484")
    assert doc.tax_type == "부가가치세"
    assert doc.gist == "판시요지"


def test_fetch_by_case_no_returns_none_when_not_found():
    session = FakeSession(search_body={"body": []})
    assert make_client(session).fetch_by_case_no("2020구합86484") is None


def test_call_raises_on_error_status():
    class Failing(FakeSession):
        def post(self, url, data=None, **kw):
            return FakeResponse({"status": "ERROR", "message": "실패"})

    with pytest.raises(nts.NtsError):
        make_client(Failing()).search("x")


def test_session_is_primed_once():
    """세션 쿠키가 없으면 검색이 0건이 됩니다. 예열은 한 번이면 됩니다."""
    client = nts.NtsClient(request_interval_sec=0)
    session = FakeSession(search_body={"body": []})
    client._session = session
    client._primed = False
    client.search("a")
    client.search("b")
    assert [c for c in session.calls if c[0] == "GET"] == [("GET", nts.LIST_PAGE)]


# ---------------------------------------------------------------- 세목 추론

@pytest.mark.parametrize(
    "title, expected",
    [
        ("법인세부과처분취소", "법인세"),
        ("부가가치세경정거부처분취소", "부가가치세"),
        ("종합소득세부과처분취소", "소득세"),
        ("증여세부과처분취소", "상속증여세"),
        ("취득세등부과처분취소", "지방세"),
        ("손해배상청구", ""),
        ("", ""),
    ],
)
def test_infer_tax_type(title, expected):
    assert infer_tax_type(title) == expected


def test_infer_tax_type_prefers_corporate_over_income():
    """'법인(원천)세'가 소득세로 새면 세목 태그가 통째로 틀어집니다."""
    assert infer_tax_type("법인(원천)세 징수처분 취소청구") == "법인세"


# ---------------------------------------------------------------- 저장소

def make_prec(key_id="1", body="", case_no="2020구합86484", title="법인세부과처분취소"):
    return Precedent(
        source="prec", doc_id=key_id, title=title, case_no=case_no,
        court="", decided_on=None, summary="", body=body, url="",
    )


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_bodiless_precedents_lists_only_empty(store):
    store.save_precedent(make_prec("1"))
    store.save_precedent(make_prec("2", body="본문 있음"))
    keys = {p.doc_id for p in store.bodiless_precedents()}
    assert keys == {"1"}


def test_attach_body_marks_source_and_tax(store):
    store.save_precedent(make_prec("1"))
    store.attach_body("prec:1", "판결 본문", source="nts", tax_type="부가가치세")
    prec = store.analyzable_precedents()[0]
    assert prec.body == "판결 본문"
    assert prec.body_source == "nts"
    assert prec.tax_type == "부가가치세"
    assert store.bodiless_precedents() == []


def test_attach_body_keeps_existing_tax_when_blank(store):
    store.save_precedent(make_prec("1"))
    store.attach_body("prec:1", "본문", source="nts", tax_type="법인세")
    store.attach_body("prec:1", "본문2", source="nts", tax_type="")
    assert store.analyzable_precedents()[0].tax_type == "법인세"


def test_gives_up_after_repeated_misses(store):
    """찾아도 안 나오는 건이 매일 국세청을 다시 두드리면 안 됩니다."""
    store.save_precedent(make_prec("1"))
    for _ in range(MAX_BODY_TRIES):
        store.mark_body_missing("prec:1")
    assert store.bodiless_precedents() == []
    assert store.body_stats()["gave_up"] == 1


def test_save_precedent_preserves_body_tries(store):
    """재크롤링으로 덮어써도 시도 횟수가 0으로 돌아가면 안 됩니다."""
    store.save_precedent(make_prec("1"))
    store.mark_body_missing("prec:1")
    store.save_precedent(make_prec("1"))
    row = store.conn.execute(
        "SELECT body_tries FROM precedents WHERE key='prec:1'"
    ).fetchone()
    assert row["body_tries"] == 1


def test_save_precedent_infers_tax_type_from_title(store):
    store.save_precedent(make_prec("1", title="부가가치세부과처분취소"))
    assert store.bodiless_precedents()[0].tax_type == "부가가치세"


def test_resolved_tax_type_prefers_stored_value():
    """국세청이 준 확정 값이 사건명 추정보다 앞서야 합니다."""
    prec = make_prec("1", title="법인세부과처분취소")
    prec.tax_type = "부가가치세"
    assert prec.resolved_tax_type == "부가가치세"


def test_vague_tax_type_falls_back_to_title():
    """국세청 '기타'는 세목을 특정한 게 아닙니다. 사건명이 더 쓸모 있습니다."""
    prec = make_prec("1", title="할인비용분담액은 교육세 과세표준에서 제외되지 않음")
    prec.tax_type = "기타"
    assert prec.resolved_tax_type == "교육세"


def test_vague_tax_type_kept_when_title_has_no_clue():
    prec = make_prec("1", title="알 수 없는 사건")
    prec.tax_type = "기타"
    assert prec.resolved_tax_type == "기타"


def test_specific_tax_type_beats_title():
    prec = make_prec("1", title="교육세 어쩌고")
    prec.tax_type = "법인세"
    assert prec.resolved_tax_type == "법인세"


# ---------------------------------------------------------------- 파이프라인

class FakeNtsClient:
    """fetch_bodies가 부르는 만큼만 흉내 냅니다."""

    def __init__(self, answers=None, raises=False):
        self.answers = answers or {}
        self.raises = raises
        self.asked = []

    def fetch_by_case_no(self, case_no, title=""):
        self.asked.append(case_no)
        if self.raises:
            raise RuntimeError("국세청 화면이 바뀌었습니다")
        return self.answers.get(case_no)


def doc(body="판결 본문입니다", tax="법인세", gist="요지"):
    return nts.NtsDocument(
        doc_id="D1", title="제목", case_no="2024구합1",
        tax_type=tax, gist=gist, body=body,
    )


def fetch_cfg(tmp_path):
    from taxrefund.config import CrawlConfig

    class Cfg:
        crawl = CrawlConfig()
        db_path = tmp_path / "t.db"

    return Cfg()


def test_fetch_bodies_attaches_and_counts(tmp_path):
    from taxrefund.pipeline import fetch_bodies

    cfg = fetch_cfg(tmp_path)
    store = Store(cfg.db_path)
    store.save_precedent(make_prec("1", case_no="2024구합1"))
    store.save_precedent(make_prec("2", case_no="2024구합2"))

    client = FakeNtsClient({"2024구합1": doc()})
    assert fetch_bodies(cfg, store=store, client=client) == 1
    assert store.body_stats() == {"from_nts": 1, "bodiless": 1, "gave_up": 0}
    store.close()


def test_fetch_bodies_uses_gist_when_body_empty(tmp_path):
    """심리불속행처럼 본문이 없어도 요지가 있으면 분석 큐에 올립니다."""
    from taxrefund.pipeline import fetch_bodies

    cfg = fetch_cfg(tmp_path)
    store = Store(cfg.db_path)
    store.save_precedent(make_prec("1", case_no="2024구합1"))
    fetch_bodies(cfg, store=store, client=FakeNtsClient({"2024구합1": doc(body="")}))
    assert len(store.unanalyzed_precedents()) == 1
    store.close()


def test_fetch_bodies_survives_client_errors(tmp_path):
    """공식 API가 아니라 언제든 깨질 수 있습니다. 파이프라인은 계속 가야 합니다."""
    from taxrefund.pipeline import fetch_bodies

    cfg = fetch_cfg(tmp_path)
    store = Store(cfg.db_path)
    store.save_precedent(make_prec("1", case_no="2024구합1"))
    assert fetch_bodies(cfg, store=store, client=FakeNtsClient(raises=True)) == 0
    assert store.body_stats()["bodiless"] == 1
    store.close()


def test_fetch_bodies_skips_records_without_case_no(tmp_path):
    from taxrefund.pipeline import fetch_bodies

    cfg = fetch_cfg(tmp_path)
    store = Store(cfg.db_path)
    store.save_precedent(make_prec("1", case_no=""))
    client = FakeNtsClient()
    fetch_bodies(cfg, store=store, client=client)
    assert client.asked == []
    store.close()


def test_fetch_bodies_honours_limit(tmp_path):
    from taxrefund.pipeline import fetch_bodies

    cfg = fetch_cfg(tmp_path)
    store = Store(cfg.db_path)
    for i in range(5):
        store.save_precedent(make_prec(str(i), case_no=f"2024구합{i}"))
    client = FakeNtsClient()
    fetch_bodies(cfg, store=store, client=client, limit=2)
    assert len(client.asked) == 2
    store.close()


def test_fetch_bodies_truncates_to_max_body_chars(tmp_path):
    from taxrefund.pipeline import fetch_bodies

    cfg = fetch_cfg(tmp_path)
    cfg.crawl.max_body_chars = 10
    store = Store(cfg.db_path)
    store.save_precedent(make_prec("1", case_no="2024구합1"))
    fetch_bodies(cfg, store=store, client=FakeNtsClient({"2024구합1": doc(body="가" * 100)}))
    assert len(store.analyzable_precedents()[0].body) == 10
    store.close()


# ---------------------------------------------------------------- 제목 폴백

def test_title_queries_prefers_distinctive_long_words():
    """'법인세부과처분취소' 같은 낱말은 어느 사건에나 있어 후보를 못 좁힙니다."""
    got = _title_queries(
        "(심리불속행) 해외카드사에 지급한 분담금 중 발급사일일분담금 부분에 관한 "
        "법인세 원천세율 그로스업 상당액은 부가가치세 과세표준에서 제외되어야 함"
    )
    assert "발급사일일분담금" in got
    assert "법인세" not in got
    assert "부가가치세" not in got


def test_title_queries_empty_for_boilerplate_title():
    assert _title_queries("법인세부과처분취소") == []


def test_find_by_case_no_falls_back_to_title():
    """'2025두1'처럼 짧은 번호는 검색이 쪼개서 엉뚱한 건만 돌려줍니다."""

    class TwoPass(FakeSession):
        def post(self, url, data=None, **kw):
            param = json.loads(data["paramData"])
            action = data["actionId"]
            self.calls.append((action, param))
            word = (param.get("icldVcbCtl") or [""])[0]
            if word == "발급사일일분담금":
                body = [hit(case_no="대법원-2025-두-1", doc_id="RIGHT")]
            else:
                body = [hit(case_no="대법원-2025-두-34577", doc_id="WRONG")]
            return FakeResponse({"status": "SUCCESS", "data": {action: {"body": body}}})

    rec = make_client(TwoPass()).find_by_case_no(
        "대법원-2025-두-1", "발급사일일분담금 관련 법인세부과처분취소"
    )
    assert rec is not None and rec.doc_id == "RIGHT"


def test_title_fallback_still_requires_case_no_match():
    """질의를 넓혀도 채택 기준은 사건번호 대조여야 합니다."""
    session = FakeSession(search_body={"body": [hit(case_no="대법원-2025-두-99999")]})
    assert make_client(session).find_by_case_no(
        "대법원-2025-두-1", "발급사일일분담금 사건"
    ) is None


def test_find_by_case_no_skips_title_search_when_number_matches():
    """첫 질의에서 맞으면 국세청을 더 두드리지 않습니다."""
    session = FakeSession(search_body={"body": [hit()]})
    make_client(session).find_by_case_no("2020구합86484", "발급사일일분담금 사건")
    assert len([c for c in session.calls if c[0] == nts.SEARCH_ACTION]) == 1

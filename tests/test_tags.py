"""태그 색인과 대시보드의 태그·검색 필터."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from taxrefund import web
from taxrefund.models import Analysis, Precedent
from taxrefund.store import Store


def make_prec(doc_id="1", title="법인세부과처분취소", case_no="2024구합1", body="본문"):
    return Precedent(
        source="prec", doc_id=doc_id, title=title, case_no=case_no,
        court="서울행정법원", decided_on=None, summary="", body=body, url="",
    )


def make_analysis(**kw):
    base = dict(
        headline="접대비 손금 인정",
        is_corporate_tax=True,
        outcome="납세자승",
        issue_summary="접대비 한도 계산",
        issue_tags=["접대비", "손금산입"],
        applicable_industries=["제조업", "도소매업"],
        claim_basis="통상적 경정청구",
        refund_confidence=70,
    )
    base.update(kw)
    return Analysis(**base)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


# ---------------------------------------------------------------- 색인

def test_save_analysis_indexes_tags(store):
    store.save_precedent(make_prec())
    store.save_analysis("prec:1", make_analysis())
    tags = store.tags_for("prec:1")
    assert tags["tax"] == ["법인세"]
    assert set(tags["industry"]) == {"제조업", "도소매업"}
    assert set(tags["issue"]) == {"접대비", "손금산입"}
    assert tags["outcome"] == ["납세자승"]
    assert tags["basis"] == ["통상적 경정청구"]


def test_reanalysis_replaces_old_tags(store):
    """지우지 않고 넣기만 하면 옛 분석의 태그가 유령으로 남습니다."""
    store.save_precedent(make_prec())
    store.save_analysis("prec:1", make_analysis(issue_tags=["접대비"]))
    store.save_analysis("prec:1", make_analysis(issue_tags=["기부금"]))
    assert store.tags_for("prec:1")["issue"] == ["기부금"]


def test_no_basis_tag_when_not_applicable(store):
    store.save_precedent(make_prec())
    store.save_analysis("prec:1", make_analysis(claim_basis="해당없음"))
    assert "basis" not in store.tags_for("prec:1")


def test_blank_tag_values_are_dropped(store):
    store.save_precedent(make_prec())
    store.save_analysis("prec:1", make_analysis(issue_tags=["", "  ", "접대비"]))
    assert store.tags_for("prec:1")["issue"] == ["접대비"]


def test_tax_tag_uses_stored_value_over_title(store):
    prec = make_prec(title="법인세부과처분취소")
    prec.tax_type = "부가가치세"
    store.save_precedent(prec)
    store.save_analysis("prec:1", make_analysis())
    assert store.tags_for("prec:1")["tax"] == ["부가가치세"]


def test_tag_counts_sorted_by_frequency(store):
    for i in (1, 2, 3):
        store.save_precedent(make_prec(str(i)))
        industries = ["제조업"] if i < 3 else ["제조업", "건설업"]
        store.save_analysis(f"prec:{i}", make_analysis(applicable_industries=industries))
    assert store.tag_counts("industry") == [("제조업", 3), ("건설업", 1)]


def test_tag_counts_respects_limit(store):
    store.save_precedent(make_prec())
    store.save_analysis("prec:1", make_analysis(issue_tags=["가", "나", "다"]))
    assert len(store.tag_counts("issue", limit=2)) == 2


def test_keys_with_tags_is_and_across_kinds(store):
    store.save_precedent(make_prec("1"))
    store.save_analysis("prec:1", make_analysis(applicable_industries=["제조업"]))
    store.save_precedent(make_prec("2"))
    store.save_analysis("prec:2", make_analysis(applicable_industries=["건설업"]))

    assert store.keys_with_tags([("industry", "제조업")]) == {"prec:1"}
    assert store.keys_with_tags(
        [("industry", "제조업"), ("outcome", "납세자승")]
    ) == {"prec:1"}
    assert store.keys_with_tags(
        [("industry", "제조업"), ("outcome", "과세관청승")]
    ) == set()


def test_keys_with_tags_none_when_nothing_picked(store):
    assert store.keys_with_tags([]) is None


def test_rebuild_tags_recovers_index(store):
    store.save_precedent(make_prec())
    store.save_analysis("prec:1", make_analysis())
    store.conn.execute("DELETE FROM tags")
    store.conn.commit()
    assert store.tags_for("prec:1") == {}
    assert store.rebuild_tags() == 1
    assert store.tags_for("prec:1")["issue"]


def test_all_tags_groups_by_key(store):
    for i in (1, 2):
        store.save_precedent(make_prec(str(i)))
        store.save_analysis(f"prec:{i}", make_analysis())
    everything = store.all_tags()
    assert set(everything) == {"prec:1", "prec:2"}
    assert everything["prec:1"]["tax"] == ["법인세"]


# ---------------------------------------------------------------- 필터

def row(passed=True, pending=False, tags=None, haystack=""):
    return {
        "prec": make_prec(),
        "analysis": make_analysis(),
        "passed": passed,
        "pending": pending,
        "reason": "",
        "tags": tags or {},
        "haystack": haystack.lower(),
    }


def test_filter_by_single_tag():
    rows = [
        row(tags={"industry": ["제조업"]}),
        row(tags={"industry": ["건설업"]}),
    ]
    got = web._filter_rows(rows, "all", picked={"industry": ["제조업"]})
    assert len(got) == 1


def test_same_kind_tags_are_or():
    """같은 종류를 여러 개 고르면 넓어져야 합니다."""
    rows = [
        row(tags={"industry": ["제조업"]}),
        row(tags={"industry": ["건설업"]}),
        row(tags={"industry": ["금융업"]}),
    ]
    got = web._filter_rows(rows, "all", picked={"industry": ["제조업", "건설업"]})
    assert len(got) == 2


def test_different_kinds_are_and():
    """다른 종류끼리는 좁아져야 합니다."""
    rows = [
        row(tags={"industry": ["제조업"], "tax": ["법인세"]}),
        row(tags={"industry": ["제조업"], "tax": ["부가가치세"]}),
    ]
    got = web._filter_rows(
        rows, "all", picked={"industry": ["제조업"], "tax": ["법인세"]}
    )
    assert len(got) == 1


def test_empty_tag_values_do_not_filter():
    rows = [row(tags={"industry": ["제조업"]})]
    assert len(web._filter_rows(rows, "all", picked={"industry": []})) == 1


def test_search_terms_are_and():
    rows = [
        row(haystack="접대비 손금 제조업"),
        row(haystack="기부금 손금 건설업"),
    ]
    assert len(web._filter_rows(rows, "all", text="손금")) == 2
    assert len(web._filter_rows(rows, "all", text="손금 접대비")) == 1
    assert len(web._filter_rows(rows, "all", text="손금 없는말")) == 0


def test_search_is_case_insensitive():
    rows = [row(haystack="lg전자 실질과세")]
    assert len(web._filter_rows(rows, "all", text="LG전자")) == 1


def test_mode_and_tags_combine():
    rows = [
        row(passed=True, tags={"tax": ["법인세"]}),
        row(passed=False, tags={"tax": ["법인세"]}),
    ]
    got = web._filter_rows(rows, "alert", picked={"tax": ["법인세"]})
    assert len(got) == 1


# ---------------------------------------------------------------- 링크

def params(url: str) -> dict:
    return parse_qs(urlparse(url).query)


def test_query_string_keeps_other_filters_when_toggling():
    """태그 하나를 켜도 검색어와 다른 태그가 살아 있어야 합니다."""
    url = web._query_string(
        "tok", "alert", {"tax": ["법인세"]}, "손금", add=("industry", "제조업")
    )
    got = params(url)
    assert got["t"] == ["tok"]
    assert got["f"] == ["alert"]
    assert got["tax"] == ["법인세"]
    assert got["industry"] == ["제조업"]
    assert got["q"] == ["손금"]


def test_query_string_drops_the_tag_being_turned_off():
    url = web._query_string(
        "tok", "all", {"tax": ["법인세", "부가가치세"]}, "", drop=("tax", "법인세")
    )
    assert params(url)["tax"] == ["부가가치세"]


def test_query_string_omits_default_mode():
    assert "f=" not in web._query_string("tok", "all", {}, "")


def test_query_string_empty_without_anything():
    assert web._query_string(None, "all", {}, "") == ""


def test_query_string_encodes_korean_safely():
    url = web._query_string(None, "all", {}, "", add=("issue", "부당행위계산 부인"))
    assert " " not in url
    assert params(url)["issue"] == ["부당행위계산 부인"]


# ---------------------------------------------------------------- 렌더링

def fake_data(rows, facets=None):
    return {
        "rows": rows,
        "facets": facets or {"tax": [("법인세", 2)], "industry": [("제조업", 1)],
                             "issue": [], "outcome": []},
    }


def test_render_all_shows_search_box_and_chips():
    html = web.render_all(fake_data([row()]), "tok").decode()
    assert 'type="search"' in html
    assert "tchip" in html
    assert "법인세" in html


def test_render_all_marks_picked_tag_as_on():
    data = fake_data([row(tags={"tax": ["법인세"]})])
    html = web.render_all(data, "tok", "all", {"tax": ["법인세"]}, "").decode()
    assert 'class="tchip on"' in html
    assert "필터 해제" in html


def test_render_all_shows_picked_tag_outside_top_list():
    """상위 목록에 없는 태그를 골랐어도 눌러서 끌 수 있어야 합니다."""
    data = fake_data([row(tags={"issue": ["희귀쟁점"]})],
                     facets={"tax": [], "industry": [], "issue": [("흔한쟁점", 9)],
                             "outcome": []})
    html = web.render_all(data, None, "all", {"issue": ["희귀쟁점"]}, "").decode()
    assert "희귀쟁점" in html


def test_render_all_empty_message_mentions_filters():
    data = fake_data([row(tags={"tax": ["법인세"]})])
    html = web.render_all(data, None, "all", {"tax": ["부가가치세"]}, "").decode()
    assert "태그를 줄이거나" in html


def test_render_all_escapes_search_text():
    html = web.render_all(fake_data([]), None, "all", {}, '"><script>x</script>').decode()
    assert "<script>x" not in html


def test_card_shows_tax_and_industry_tags():
    html = web.render_all(
        fake_data([row(tags={"tax": ["법인세"], "industry": ["제조업"]})]), None
    ).decode()
    assert 'class="tag tx"' in html
    assert 'class="tag ind"' in html


def test_card_marks_nts_body_source():
    r = row(tags={"tax": ["법인세"]})
    r["prec"].body_source = "nts"
    html = web.render_all(fake_data([r]), None).decode()
    assert "국세청 본문" in html


def test_haystack_covers_title_tags_and_case_no():
    prec = make_prec(case_no="2024구합55")
    analysis = make_analysis()
    hay = web._haystack(prec, analysis, {"industry": ["제조업"]})
    assert "접대비 손금 인정" in hay
    assert "2024구합55" in hay
    assert "제조업" in hay


def test_body_source_label():
    prec = make_prec()
    prec.body_source = "nts"
    assert "국세청" in web._body_source_label(prec)
    assert web._body_source_label(make_prec()) == "국가법령정보센터"
    assert "본문 없음" in web._body_source_label(make_prec(body=""))


def test_detail_tag_links_point_to_filtered_list():
    links = web._detail_tag_links(make_prec(), make_analysis(), "tok")
    assert 'href="/all?' in links
    assert "제조업" in links


# ---------------------------------------------------------------- 중복 판결

def save_pair(store, doc_id, case_no, body="", summary="", source="prec",
              decided="2025-01-01", body_source=""):
    prec = Precedent(
        source=source, doc_id=doc_id, title="법인세부과처분취소", case_no=case_no,
        court="", decided_on=None, summary=summary, body=body, url="",
        body_source=body_source,
    )
    store.save_precedent(prec)
    store.conn.execute(
        "UPDATE precedents SET decided_on=? WHERE key=?", (decided, prec.key)
    )
    store.conn.commit()
    store.save_analysis(prec.key, make_analysis())
    return prec.key


def test_same_case_from_two_sources_is_one_group(store):
    """법령정보센터와 국세청이 같은 판결을 각자 다른 번호로 내놓습니다."""
    a = save_pair(store, "1", "2025두1234", summary="요지")
    b = save_pair(store, "2", "대법원-2025-두-1234", body="본문" * 100, body_source="nts")
    groups = store.duplicate_groups()
    assert list(groups) == ["2025두1234"]
    assert set(groups["2025두1234"]) == {a, b}


def test_longer_body_becomes_the_primary(store):
    """대표는 분석에 쓸 정보가 많은 쪽이어야 합니다."""
    short = save_pair(store, "1", "2025두1234", summary="요지")
    long = save_pair(store, "2", "2025두1234", body="본문" * 100)
    assert store.duplicate_groups()["2025두1234"][0] == long
    assert store.duplicate_secondaries() == {short}


def test_same_case_no_different_date_is_not_duplicate(store):
    """법원이 달라 사건번호만 우연히 겹치는 경우를 걸러야 합니다."""
    save_pair(store, "1", "2025구합100", decided="2025-01-01")
    save_pair(store, "2", "2025구합100", decided="2024-06-30")
    assert store.duplicate_groups() == {}


def test_no_duplicates_when_case_numbers_differ(store):
    save_pair(store, "1", "2025두1")
    save_pair(store, "2", "2025두2")
    assert store.duplicate_secondaries() == set()


def test_records_without_case_no_are_never_duplicates(store):
    save_pair(store, "1", "")
    save_pair(store, "2", "")
    assert store.duplicate_groups() == {}


def test_analyzed_pairs_dedupe_drops_secondary(store):
    save_pair(store, "1", "2025두1234", summary="요지")
    save_pair(store, "2", "2025두1234", body="본문" * 100)
    assert len(store.analyzed_pairs()) == 2
    assert len(store.analyzed_pairs(dedupe=True)) == 1


def test_analyzed_pairs_carries_tax_type_and_source(store):
    """세목·본문출처가 실리지 않으면 화면에 잘못 표시됩니다."""
    prec = Precedent(
        source="prec", doc_id="1", title="부가가치세부과처분취소", case_no="2025두9",
        court="", decided_on=None, summary="", body="본문", url="",
        tax_type="부가가치세", body_source="nts",
    )
    store.save_precedent(prec)
    store.save_analysis(prec.key, make_analysis())
    got, _ = store.analyzed_pairs()[0]
    assert got.tax_type == "부가가치세"
    assert got.body_source == "nts"


def test_card_marks_duplicate_row():
    r = row(tags={"tax": ["법인세"]})
    r["duplicate"] = True
    html = web.render_all(fake_data([r]), None).decode()
    assert "중복(같은 판결)" in html


# ---------------------------------------------------------------- 세목 정규화

def test_tax_type_aliases_merge():
    """국세청이 '교육세'와 '교육세법'을 섞어 내보내 태그가 갈라졌습니다."""
    from taxrefund.models import normalize_tax_type
    assert normalize_tax_type("교육세법") == "교육세"
    assert normalize_tax_type("법인") == "법인세"
    assert normalize_tax_type("교육세") == "교육세"
    assert normalize_tax_type(None) == ""


def test_resolved_tax_type_applies_alias():
    prec = make_prec()
    prec.tax_type = "교육세법"
    assert prec.resolved_tax_type == "교육세"


# ---------------------------------------------------------------- 세목 게이트

def test_tax_type_allowed_respects_list():
    from taxrefund.config import NotifyConfig
    from taxrefund.matcher import tax_type_allowed
    prec = make_prec(title="법인세부과처분취소")
    assert tax_type_allowed(prec, NotifyConfig(tax_types=["법인세"]))
    assert not tax_type_allowed(prec, NotifyConfig(tax_types=["부가가치세"]))


def test_tax_type_allowed_when_list_empty():
    """빈 목록은 '거르지 않음'이지 '아무것도 통과 못 함'이 아닙니다."""
    from taxrefund.config import NotifyConfig
    from taxrefund.matcher import tax_type_allowed
    assert tax_type_allowed(make_prec(title="증여세부과처분취소"),
                            NotifyConfig(tax_types=[]))


def test_tax_type_allowed_ignores_blank_entries():
    from taxrefund.config import NotifyConfig
    from taxrefund.matcher import tax_type_allowed
    cfg = NotifyConfig(tax_types=["", "  ", "법인세"])
    assert tax_type_allowed(make_prec(title="법인세부과처분취소"), cfg)
    assert not tax_type_allowed(make_prec(title="증여세부과처분취소"), cfg)


# ---------------------------------------------------------------- 카드·화면

def test_axis_chips_are_links_to_filtered_list():
    """카드의 세목·업종·주제는 눌러서 걸러 볼 수 있어야 합니다."""
    prec = make_prec(title="법인세부과처분취소")
    html = web._axis_chips(prec, make_analysis(), "tok")
    assert 'href="/all?' in html
    assert "법인세" in html and "제조업" in html and "접대비" in html


def test_card_has_no_nested_links():
    """카드 전체를 <a>로 감싸면 안의 태그 링크가 깨집니다."""
    import re
    html = web._card(make_prec(), make_analysis(), "tok", score=70,
                     companies=["제조업"])
    assert re.search(r"<a\b[^>]*>(?:(?!</a>).)*<a\b", html, re.S) is None
    assert html.count("<a ") == html.count("</a>")
    assert 'class="ttl"' in html


def test_card_shows_matched_industry_group():
    html = web._card(make_prec(), make_analysis(), None, score=70,
                     companies=["제조업", "건설·부동산업"])
    assert "해당 업종군" in html
    assert "제조업" in html


def test_card_omits_company_line_when_none():
    html = web._card(make_prec(), make_analysis(), None, score=70)
    assert "해당 업종군" not in html


def test_row_tag_html_no_longer_duplicates_axis_tags():
    """세목·업종은 칩으로 나가므로 배지에서는 빠져야 합니다."""
    r = row(tags={"tax": ["법인세"], "industry": ["제조업"]})
    html = web._row_tag_html(r)
    assert "법인세" not in html and "제조업" not in html


def test_row_tag_html_keeps_source_and_duplicate():
    r = row()
    r["prec"].body_source = "nts"
    r["duplicate"] = True
    html = web._row_tag_html(r)
    assert "국세청 본문" in html and "중복(같은 판결)" in html


def browse_data(rows, alerts=None, tags=None):
    return {
        "rows": rows,
        "alerts": alerts or [],
        "tags": tags or {},
        "facets": {k: [] for k in web.TAG_KINDS},
    }


def test_browse_lists_every_axis():
    r = row(tags={"tax": ["법인세"], "industry": ["제조업"],
                  "issue": ["접대비"], "outcome": ["납세자승"]})
    html = web.render_browse(browse_data([r]), "tok").decode()
    for label in ("세목", "업종", "쟁점", "결과"):
        assert f"<h3>{label}" in html
    assert 'href="/all?' in html


def test_browse_rows_link_to_filtered_list():
    r = row(tags={"industry": ["제조업"]})
    html = web.render_browse(browse_data([r]), None).decode()
    assert "industry=%EC%A0%9C%EC%A1%B0%EC%97%85" in html


def test_browse_empty_is_graceful():
    html = web.render_browse(browse_data([]), None).decode()
    assert "아직 분류할 분석이 없습니다" in html


def test_quick_axes_degrades_without_tags():
    """장식 때문에 알림 목록이 안 그려지면 안 됩니다."""
    assert web._quick_axes({"alerts": [], "tags": {}}, None, "alert") == ""
    assert web._quick_axes({}, None, "alert") == ""


def test_nav_has_four_menus():
    html = web._page("t", "", "home", None).decode()
    nav = html.split("<nav>")[1].split("</nav>")[0]
    for label in ("알림", "전체", "분류", "상태"):
        assert f">{label}</a>" in nav

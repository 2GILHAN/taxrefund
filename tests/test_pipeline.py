"""Claude API 호출을 모킹해 파이프라인 전 구간을 검증합니다.

실행: python -m tests.test_pipeline   (프로젝트 루트에서)
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from taxrefund.analyzer import ANALYSIS_SCHEMA
from taxrefund.analyzer_claude import ClaudeAnalyzer as Analyzer  # noqa: E402
from taxrefund.config import Company, load_config  # noqa: E402
from taxrefund.lawgo import LawGoKrClient, _find_records, _strip_html  # noqa: E402
from taxrefund.matcher import (  # noqa: E402
    build_alerts,
    compute_deadlines,
    match_companies,
    open_fiscal_years,
)
from taxrefund.models import Analysis  # noqa: E402
from taxrefund.notifier import render_report_md  # noqa: E402
from taxrefund.pipeline import load_sample_precedents, run  # noqa: E402

# 샘플 판례 1(연구개발비, 전자부품)에 대응하는 가짜 모델 출력
FAKE_ANALYSIS = {
    "is_corporate_tax": True,
    "outcome": "납세자승",
    "issue_summary": "기존 제품의 성능 개선을 위한 반복적 시험·평가도 연구·인력개발비 세액공제 대상인 연구개발에 해당한다고 본 사안.",
    "issue_tags": ["연구인력개발비 세액공제", "조세특례제한법 제10조"],
    "applicable_industries": ["전자부품 제조업", "기타 제조업"],
    "ksic_codes": ["26", "27"],
    "generalizable": True,
    "generalizable_reason": "개별 사실인정이 아니라 '연구개발' 개념의 법령 해석에 관한 판단이다.",
    "refund_claim_possible": True,
    "refund_confidence": 82,
    "refund_rationale": "과세관청이 '기존 제품 개량은 연구개발이 아니다'라는 전제로 공제를 부인해 온 사례가 많아 실익이 크다.",
    "claim_basis": "통상적 경정청구",
    "check_points": [
        "2021~2025 사업연도 연구개발비 중 기존 제품 개량 과제로 분류해 공제에서 제외한 금액이 있는지",
        "해당 과제의 연구노트·시험성적서 등 기술적 불확실성 해소 근거가 남아 있는지",
    ],
    "estimated_impact": "상",
    "caveats": "본문이 일부만 제공되었습니다. 최종 판단은 세무대리인의 검토를 전제로 합니다.",
}


def stub_init(self, cfg, api_key=None):
    """Anthropic 클라이언트를 만들지 않는 Analyzer.__init__ 대체."""
    self.cfg = cfg
    self._fallback_enabled = False


def fake_response(payload: dict, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=None,
        content=[SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False))],
        usage=SimpleNamespace(
            input_tokens=1000, output_tokens=500, cache_read_input_tokens=800
        ),
    )


def routed_response(self, user_content: str, use_fallback: bool) -> SimpleNamespace:
    """샘플 1건은 알림 대상, 나머지 1건은 실익 없음으로 응답하는 가짜 모델."""
    if "학자금" in user_content:
        return fake_response(
            {
                **FAKE_ANALYSIS,
                "refund_claim_possible": False,
                "refund_confidence": 25,
                "applicable_industries": ["전업종"],
                "ksic_codes": [],
            }
        )
    return fake_response(FAKE_ANALYSIS)


class TestSchema(unittest.TestCase):
    def test_schema_meets_structured_output_rules(self):
        """구조화 출력이 요구하는 제약을 지키는지."""
        props = set(ANALYSIS_SCHEMA["properties"])
        self.assertEqual(props, set(ANALYSIS_SCHEMA["required"]), "모든 필드가 required여야 합니다")
        self.assertFalse(ANALYSIS_SCHEMA["additionalProperties"])

        unsupported = {"minimum", "maximum", "minLength", "maxLength", "multipleOf", "pattern"}
        for name, spec in ANALYSIS_SCHEMA["properties"].items():
            self.assertFalse(
                unsupported & set(spec), f"{name}에 지원되지 않는 제약이 있습니다"
            )

    def test_analysis_ignores_unknown_fields(self):
        a = Analysis.from_json({**FAKE_ANALYSIS, "존재하지않는필드": 1})
        self.assertEqual(a.refund_confidence, 82)


class TestDeadlines(unittest.TestCase):
    def test_open_fiscal_years(self):
        # 2026-08-16 기준: 2020 사업연도 기한(2026-03-31)은 지났고 2021부터 유효
        self.assertEqual(open_fiscal_years(date(2026, 8, 16)), [2021, 2022, 2023, 2024, 2025])
        # 2026-03-30 기준: 2020 사업연도가 아직 하루 남음
        self.assertIn(2020, open_fiscal_years(date(2026, 3, 30)))
        self.assertNotIn(2020, open_fiscal_years(date(2026, 4, 1)))

    def test_후발적_사유_3개월_안내(self):
        analysis = Analysis.from_json({**FAKE_ANALYSIS, "claim_basis": "후발적 사유"})
        prec = load_sample_precedents(ROOT)[0]
        notes = " ".join(compute_deadlines(analysis, prec, date(2026, 8, 16)))
        self.assertIn("후발적 사유", notes)
        self.assertIn("2025-09-12", notes)  # 선고일 2025-06-12 + 3개월

    def test_임박_경고(self):
        analysis = Analysis.from_json(FAKE_ANALYSIS)
        prec = load_sample_precedents(ROOT)[0]
        notes = " ".join(compute_deadlines(analysis, prec, date(2027, 2, 1)))
        self.assertIn("⚠", notes)


class TestMatcher(unittest.TestCase):
    def setUp(self):
        self.prec = load_sample_precedents(ROOT)[0]
        self.analysis = Analysis.from_json(FAKE_ANALYSIS)
        self.watchlist = [
            Company(name="전자부품사", industry="전자부품 제조업", ksic=["26"], keywords=["반도체"]),
            Company(name="건설사", industry="종합 건설업", ksic=["41"], keywords=["하자보수충당금"]),
        ]

    def test_업종_일치만_매칭(self):
        matches = match_companies(self.prec, self.analysis, self.watchlist)
        self.assertEqual([m.company for m in matches], ["전자부품사"])
        self.assertGreater(matches[0].score, self.analysis.refund_confidence)

    def test_전업종이면_전부_매칭(self):
        analysis = Analysis.from_json({**FAKE_ANALYSIS, "applicable_industries": ["전업종"], "ksic_codes": []})
        matches = match_companies(self.prec, analysis, self.watchlist)
        self.assertEqual(len(matches), 2)

    def test_임계값_미달은_제외(self):
        cfg = load_config().notify
        cfg.min_confidence = 90
        alerts = build_alerts([(self.prec, self.analysis)], self.watchlist, cfg)
        self.assertEqual(alerts, [])

    def test_과세관청승_건은_제외(self):
        analysis = Analysis.from_json(
            {**FAKE_ANALYSIS, "refund_claim_possible": False, "outcome": "과세관청승"}
        )
        alerts = build_alerts([(self.prec, analysis)], self.watchlist, load_config().notify)
        self.assertEqual(alerts, [])

    def test_알림_세목이_아니면_제외(self):
        """세목으로 거릅니다. is_corporate_tax로 걸면 세목을 넓힐 수가 없습니다."""
        notify = load_config().notify
        notify.tax_types = ["부가가치세"]          # 이 판례는 법인세
        analysis = Analysis.from_json(FAKE_ANALYSIS)
        alerts = build_alerts([(self.prec, analysis)], self.watchlist, notify)
        self.assertEqual(alerts, [])

    def test_알림_세목이면_통과(self):
        notify = load_config().notify
        notify.tax_types = ["법인세", "부가가치세", "상속증여세"]
        analysis = Analysis.from_json(FAKE_ANALYSIS)
        alerts = build_alerts([(self.prec, analysis)], self.watchlist, notify)
        self.assertTrue(alerts, "법인세 판례는 통과해야 합니다")

    def test_세목_목록이_비면_거르지_않는다(self):
        notify = load_config().notify
        notify.tax_types = []
        analysis = Analysis.from_json(FAKE_ANALYSIS)
        self.assertTrue(build_alerts([(self.prec, analysis)], self.watchlist, notify))


class TestLawGoParsing(unittest.TestCase):
    def test_strip_html(self):
        self.assertEqual(_strip_html("<p>가<br/>나</p>&amp;"), "가\n나&")

    def test_find_records_prec(self):
        payload = {"PrecSearch": {"totalCnt": 2, "prec": [{"판례일련번호": "1"}, {"판례일련번호": "2"}]}}
        self.assertEqual(len(_find_records(payload)), 2)

    def test_find_records_single_detail(self):
        payload = {"PrecService": {"판례정보일련번호": "1", "사건명": "테스트"}}
        records = _find_records(payload)
        self.assertEqual(records[0]["사건명"], "테스트")


class TestLawGoHeaders(unittest.TestCase):
    """Referer가 빠지면 OC/IP가 정상이어도 모든 호출이 거부됩니다.

    오류 메시지가 'IP주소를 등록해 주세요'로 나와 원인을 찾기 매우 어려웠던
    문제라, 헤더가 사라지지 않도록 여기서 고정해 둡니다.
    """

    def test_referer_헤더가_항상_붙는다(self):
        client = LawGoKrClient("someoc")
        self.assertIn("Referer", client.session.headers)
        self.assertIn("law.go.kr", client.session.headers["Referer"])

    def test_user_agent가_봇처럼_보이지_않는다(self):
        ua = LawGoKrClient("someoc").session.headers.get("User-Agent", "")
        self.assertTrue(ua)
        self.assertNotIn("bot", ua.lower())

    def test_실제_요청에_헤더가_실린다(self):
        client = LawGoKrClient("someoc", interval_sec=0)
        captured: dict[str, Any] = {}

        def fake_get(url, params=None, timeout=None):
            captured["headers"] = dict(client.session.headers)
            return SimpleNamespace(
                text='{"PrecSearch":{"prec":[]}}',
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: {"PrecSearch": {"prec": []}},
            )

        with mock.patch.object(client.session, "get", fake_get):
            client.search("prec", "법인세")
        self.assertIn("Referer", captured["headers"])


class TestBodyFiltering(unittest.TestCase):
    """본문 없는 판례(국세법령정보시스템 출처)는 분석에 넘기지 않습니다.

    수집 자체는 합니다. 저장해 두지 않으면 매일 같은 건을 다시 상세 조회하게
    되기 때문입니다. 대신 본문이 비어 있어 분석 큐(Store)에 들어가지 않습니다.
    """

    def _client_with(self, search_records, detail):
        client = LawGoKrClient("someoc", interval_sec=0)
        client.search = lambda target, query, display=50, page=1: search_records
        client.fetch = lambda target, doc_id: detail
        return client

    def test_본문_없으면_제외(self):
        record = {
            "판례일련번호": "621195",
            "사건명": "법인세 추계결정 가능 여부",
            "사건번호": "대법원-2026-두-30683",
            "선고일자": "20260625",
            "데이터출처명": "국세법령정보시스템",
        }
        client = self._client_with([record], {})  # 상세 조회 실패 = 빈 dict
        got = list(
            client.collect(["prec"], ["법인세"], since_days=36500,
                           max_per_query=10, max_body_chars=60000)
        )
        self.assertEqual(len(got), 1, "다시 긁지 않으려면 기록은 남겨야 합니다")
        self.assertEqual(got[0].body, "")
        self.assertEqual(got[0].summary, "")

        # 핵심: 본문이 없으므로 분석 큐에는 들어가지 않습니다.
        import tempfile

        from taxrefund.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            with Store(Path(tmp) / "t.db") as store:
                store.save_precedent(got[0])
                self.assertEqual(store.unanalyzed_precedents(), [])
                self.assertEqual(store.precedent_stats()["with_body"], 0)

    def test_본문_있으면_통과(self):
        record = {
            "판례일련번호": "622113",
            "사건명": "법인세경정거부처분취소",
            "사건번호": "2022두34425",
            "선고일자": "20260529",
            "데이터출처명": "대법원",
        }
        detail = {
            "판시사항": "쟁점 요약",
            "판결요지": "요지 본문",
            "판례내용": "【원고】 ○○○ 주식회사 ...",
            "법원명": "대법원",
        }
        client = self._client_with([record], detail)
        got = list(
            client.collect(["prec"], ["법인세"], since_days=36500,
                           max_per_query=10, max_body_chars=60000)
        )
        self.assertEqual(len(got), 1)
        self.assertIn("원고", got[0].body)
        self.assertIn("쟁점 요약", got[0].summary)
        self.assertEqual(got[0].court, "대법원")


class TestPipelineEndToEnd(unittest.TestCase):
    def test_run_with_mocked_claude(self):
        cfg = load_config()
        cfg.analyze.provider = "claude"  # 이 테스트는 Claude 경로를 모킹합니다
        cfg.secrets.anthropic_api_key = "test-key"
        cfg.watchlist = [
            Company(name="한국전자부품(주)", industry="전자부품 제조업", ksic=["26"], keywords=["반도체"])
        ]
        cfg.notify.channels = ["file"]

        with tempfile.TemporaryDirectory() as tmp:
            cfg.root = Path(tmp)
            (Path(tmp) / "data").mkdir()
            (Path(tmp) / "data" / "sample_precedents.json").write_text(
                (ROOT / "data" / "sample_precedents.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            cfg.notify.report_dir = str(Path(tmp) / "reports")

            with mock.patch.object(Analyzer, "__init__", stub_init), \
                 mock.patch.object(Analyzer, "_request", routed_response):
                alerts = run(cfg, sample=True)

            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0].matches[0].company, "한국전자부품(주)")
            reports = list((Path(tmp) / "reports").glob("*.md"))
            self.assertEqual(len(reports), 1)
            body = reports[0].read_text(encoding="utf-8")
            self.assertIn("연구인력개발비", body)
            self.assertIn("한국전자부품(주)", body)

            # 두 번째 실행: 이미 처리한 건이라 새 알림/리포트가 없어야 함
            with mock.patch.object(Analyzer, "__init__", stub_init), \
                 mock.patch.object(Analyzer, "_request", routed_response):
                again = run(cfg, sample=True)
            self.assertEqual(again, [], "중복 처리가 걸러지지 않았습니다")
            self.assertEqual(len(list((Path(tmp) / "reports").glob("*.md"))), 1)

    def test_refusal_returns_none(self):
        cfg = load_config()
        prec = load_sample_precedents(ROOT)[0]
        with mock.patch.object(Analyzer, "__init__", stub_init), mock.patch.object(
            Analyzer, "_request", lambda self, u, f: fake_response({}, stop_reason="refusal")
        ):
            analyzer = Analyzer(cfg.analyze)
            self.assertIsNone(analyzer.analyze(prec))

    def test_malformed_json_returns_none(self):
        cfg = load_config()
        prec = load_sample_precedents(ROOT)[0]
        broken = SimpleNamespace(
            stop_reason="end_turn",
            stop_details=None,
            content=[SimpleNamespace(type="text", text="이건 JSON이 아닙니다")],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0),
        )
        with mock.patch.object(Analyzer, "__init__", stub_init), mock.patch.object(
            Analyzer, "_request", lambda self, u, f: broken
        ):
            self.assertIsNone(Analyzer(cfg.analyze).analyze(prec))


class TestRendering(unittest.TestCase):
    def test_report_renders(self):
        prec = load_sample_precedents(ROOT)[0]
        analysis = Analysis.from_json(FAKE_ANALYSIS)
        watchlist = [Company(name="전자부품사", industry="전자부품 제조업", ksic=["26"])]
        alerts = build_alerts([(prec, analysis)], watchlist, load_config().notify)
        md = render_report_md(alerts)
        self.assertIn("경정청구 후보 리포트", md)
        self.assertIn("전자부품사", md)
        self.assertIn("확인할 것", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)

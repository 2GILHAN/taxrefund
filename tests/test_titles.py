"""제목 결정 검증.

법원 사건명은 대부분 '법인세부과처분취소'라 서로 구분되지 않습니다.
목록에서 그 이름이 그대로 보이면 안 됩니다.

실행: python -m tests.test_titles
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from taxrefund.config import load_config  # noqa: E402
from taxrefund.models import Analysis, Precedent, display_title  # noqa: E402
from taxrefund.pipeline import fill_headlines  # noqa: E402
from taxrefund.analyzer import QuotaExceededError  # noqa: E402
from taxrefund.store import Store  # noqa: E402

PREC = Precedent(
    source="prec", doc_id="1", title="법인세부과처분취소", case_no="2025두34772",
    court="대법원", decided_on=date(2026, 2, 12), summary="", body="본문",
    url="https://www.law.go.kr/",
)


class TestDisplayTitle(unittest.TestCase):
    def test_모델이_뽑은_제목이_최우선(self):
        a = Analysis(headline="직원매장 할인액은 매출에누리", issue_tags=["접대비"])
        self.assertEqual(display_title(PREC, a), "직원매장 할인액은 매출에누리")

    def test_제목이_없으면_쟁점태그와_결론으로(self):
        """API 한도로 제목을 못 받아도 사건명으로 물러서지 않습니다."""
        a = Analysis(
            issue_tags=["접대비", "판매부대비용", "매출에누리"], outcome="납세자승"
        )
        self.assertEqual(
            display_title(PREC, a), "접대비 · 판매부대비용 · 매출에누리 (납세자승)"
        )
        self.assertNotIn("법인세부과처분취소", display_title(PREC, a))

    def test_태그는_세_개까지만(self):
        a = Analysis(issue_tags=["가", "나", "다", "라", "마"], outcome="납세자승")
        self.assertEqual(display_title(PREC, a), "가 · 나 · 다 (납세자승)")

    def test_결론이_기타면_붙이지_않는다(self):
        a = Analysis(issue_tags=["접대비"], outcome="기타")
        self.assertEqual(display_title(PREC, a), "접대비")

    def test_빈_태그는_걸러진다(self):
        a = Analysis(issue_tags=["", "  ", "접대비"], outcome="납세자승")
        self.assertEqual(display_title(PREC, a), "접대비 (납세자승)")

    def test_둘_다_없을_때만_사건명(self):
        self.assertEqual(display_title(PREC, Analysis()), "법인세부과처분취소")
        self.assertEqual(display_title(PREC, None), "법인세부과처분취소")

    def test_공백뿐인_제목은_무시(self):
        a = Analysis(headline="   ", issue_tags=["접대비"], outcome="납세자승")
        self.assertEqual(display_title(PREC, a), "접대비 (납세자승)")


class TestFillHeadlines(unittest.TestCase):
    def _store(self, tmp):
        cfg = load_config()
        cfg.root = Path(tmp)
        return Store(cfg.db_path)

    def _targets(self, n: int):
        out = []
        for i in range(n):
            prec = Precedent(
                source="prec", doc_id=str(i), title="법인세부과처분취소",
                case_no=f"2025두{i}", court="대법원", decided_on=date(2026, 2, 12),
                summary="요약", body="본문", url="https://www.law.go.kr/",
            )
            out.append((prec, Analysis(outcome="납세자승", issue_summary="쟁점")))
        return out

    def test_채운_만큼_저장된다(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                targets = self._targets(3)
                for prec, _ in targets:
                    store.save_precedent(prec)
                done = fill_headlines(store, lambda p, a: f"제목 {p.doc_id}", targets)
                self.assertEqual(done, 3)
                self.assertEqual(store.get_analysis("prec:0").headline, "제목 0")

    def test_한도가_떨어지면_거기까지_남긴다(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                targets = self._targets(5)
                for prec, _ in targets:
                    store.save_precedent(prec)

                def flaky(prec, analysis):
                    if int(prec.doc_id) >= 2:
                        raise QuotaExceededError("일일 한도 소진")
                    return f"제목 {prec.doc_id}"

                done = fill_headlines(store, flaky, targets)
                self.assertEqual(done, 2, "한도 전까지 채운 것은 살아야 합니다")
                self.assertEqual(store.get_analysis("prec:1").headline, "제목 1")
                self.assertIsNone(store.get_analysis("prec:2"))

    def test_생성_실패한_건은_건너뛰고_계속(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                targets = self._targets(3)
                for prec, _ in targets:
                    store.save_precedent(prec)
                done = fill_headlines(
                    store, lambda p, a: None if p.doc_id == "1" else f"제목 {p.doc_id}",
                    targets,
                )
                self.assertEqual(done, 2)
                self.assertIsNone(store.get_analysis("prec:1"))


class TestCallBudget(unittest.TestCase):
    """예산은 '건수'가 아니라 '실제 요청 수'로 세야 합니다.

    503 재시도도 무료 등급 일일 한도를 먹습니다. 건수로만 막으면 조용히
    한도를 넘겨 뒤따르는 분석이 0건이 됩니다(2026-08-19에 실제로 발생).
    """

    def _store(self, tmp):
        cfg = load_config()
        cfg.root = Path(tmp)
        return Store(cfg.db_path)

    def _targets(self, n: int):
        out = []
        for i in range(n):
            prec = Precedent(
                source="prec", doc_id=str(i), title="법인세부과처분취소",
                case_no=f"2025두{i}", court="대법원", decided_on=date(2026, 2, 12),
                summary="요약", body="본문", url="https://www.law.go.kr/",
            )
            out.append((prec, Analysis(outcome="납세자승", issue_summary="쟁점")))
        return out

    class Counting:
        """항목당 요청을 여러 번 쓰는 분석기(재시도 흉내)."""

        def __init__(self, per_item: int = 3):
            self.api_calls = 0
            self.per_item = per_item

        def generate_headline(self, prec, analysis):
            self.api_calls += self.per_item
            return f"제목 {prec.doc_id}"

    def test_재시도가_많으면_적은_건수에서_멈춘다(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                targets = self._targets(10)
                for prec, _ in targets:
                    store.save_precedent(prec)
                analyzer = self.Counting(per_item=3)
                done = fill_headlines(
                    store, analyzer.generate_headline, targets,
                    analyzer=analyzer, max_calls=6,
                )
                self.assertEqual(done, 2, "요청 6회면 항목 3회짜리는 2건까지입니다")
                self.assertLessEqual(analyzer.api_calls, 6 + 3)

    def test_재시도가_없으면_건수만큼_처리한다(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                targets = self._targets(10)
                for prec, _ in targets:
                    store.save_precedent(prec)
                analyzer = self.Counting(per_item=1)
                done = fill_headlines(
                    store, analyzer.generate_headline, targets,
                    analyzer=analyzer, max_calls=6,
                )
                self.assertEqual(done, 6)

    def test_카운터가_없는_분석기는_제한하지_않는다(self):
        """Claude 등 카운터가 없는 구현에서도 동작해야 합니다."""
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                targets = self._targets(4)
                for prec, _ in targets:
                    store.save_precedent(prec)
                done = fill_headlines(
                    store, lambda p, a: f"제목 {p.doc_id}", targets, max_calls=2
                )
                self.assertEqual(done, 4)


class TestHeadlineStability(unittest.TestCase):
    """한 번 붙은 제목은 다시 만들지 않습니다.

    매 사이클마다 다시 생성하면 (1) 같은 판례 제목이 날마다 바뀌어
    목록이 흔들리고 (2) 무료 등급 한도를 밀린 건 대신 이미 끝난 건에
    써 버립니다.
    """

    def _cfg(self, tmp):
        cfg = load_config()
        cfg.root = Path(tmp)
        cfg.notify.channels = []
        cfg.analyze.per_run_limit = 10
        return cfg

    def _seed(self, store):
        """제목 있는 건 1개 + 없는 건 1개."""
        for i, headline in enumerate(("이미 붙은 제목", "")):
            prec = Precedent(
                source="prec", doc_id=str(i), title="법인세부과처분취소",
                case_no=f"2025두{i}", court="대법원", decided_on=date(2026, 2, 12),
                summary="요약", body="본문", url="https://www.law.go.kr/",
            )
            store.save_precedent(prec)
            store.save_analysis(
                prec.key, Analysis(headline=headline, outcome="납세자승",
                                   issue_summary="쟁점", issue_tags=["접대비"])
            )

    def test_run은_이미_붙은_제목을_건드리지_않는다(self):
        from unittest import mock

        from taxrefund import pipeline

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            with Store(cfg.db_path) as store:
                self._seed(store)

            asked: list[str] = []

            class Fake:
                def analyze(self, prec):
                    return None

                def generate_headline(self, prec, analysis):
                    asked.append(prec.key)
                    return f"새 제목 {prec.doc_id}"

            with mock.patch.object(pipeline, "iter_precedents",
                                   lambda c, s, skip_keys=None: iter([])),                  mock.patch.object(pipeline, "make_analyzer", lambda *a, **k: Fake()),                  mock.patch.object(pipeline, "get_public_ip", lambda *a, **k: None):
                pipeline.run(cfg)

            self.assertEqual(asked, ["prec:1"], "빈 것만 물어봐야 합니다")
            with Store(cfg.db_path) as store:
                self.assertEqual(
                    store.get_analysis("prec:0").headline, "이미 붙은 제목"
                )
                self.assertEqual(store.get_analysis("prec:1").headline, "새 제목 1")

    def test_두_번_돌려도_다시_만들지_않는다(self):
        from unittest import mock

        from taxrefund import pipeline

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            with Store(cfg.db_path) as store:
                self._seed(store)

            calls = {"n": 0}

            class Fake:
                def analyze(self, prec):
                    return None

                def generate_headline(self, prec, analysis):
                    calls["n"] += 1
                    return f"제목 {calls['n']}"

            with mock.patch.object(pipeline, "iter_precedents",
                                   lambda c, s, skip_keys=None: iter([])),                  mock.patch.object(pipeline, "make_analyzer", lambda *a, **k: Fake()),                  mock.patch.object(pipeline, "get_public_ip", lambda *a, **k: None):
                pipeline.run(cfg)
                first = calls["n"]
                pipeline.run(cfg)

            self.assertEqual(calls["n"], first, "두 번째 사이클은 호출이 없어야 합니다")

    def test_headlines_명령도_기본은_빈_것만(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            with Store(cfg.db_path) as store:
                self._seed(store)
                pairs = list(store.analyzed_pairs())

            empty = [p.key for p, a in pairs if not (a.headline or "").strip()]
            forced = [p.key for p, a in pairs]
            self.assertEqual(empty, ["prec:1"])
            self.assertEqual(sorted(forced), ["prec:0", "prec:1"], "--force일 때만 전체")


if __name__ == "__main__":
    unittest.main(verbosity=2)

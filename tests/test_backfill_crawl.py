"""과거 구간 수집(페이지 순회)과 수집·분석 분리 검증.

실행: python -m tests.test_backfill_crawl
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from taxrefund.config import load_config  # noqa: E402
from taxrefund.lawgo import LawGoKrClient  # noqa: E402
from taxrefund.models import Precedent  # noqa: E402
from taxrefund.store import MAX_ANALYSIS_ATTEMPTS, Store  # noqa: E402

TODAY = date.today()


def rec(doc_id: str, days_ago: int) -> dict:
    d = TODAY - timedelta(days=days_ago)
    return {
        "판례일련번호": doc_id,
        "사건명": f"법인세부과처분취소 {doc_id}",
        "사건번호": f"2020구합{doc_id}",
        "법원명": "대법원",
        "선고일자": d.strftime("%Y.%m.%d"),
    }


def detail(doc_id: str) -> dict:
    return {"판례내용": f"<p>본문 {doc_id}</p>", "판시사항": f"쟁점 {doc_id}"}


class FakeClient(LawGoKrClient):
    """네트워크 대신 준비된 페이지를 돌려줍니다."""

    def __init__(self, pages: dict[int, list[dict]]):
        self.oc = "test"
        self.pages = pages
        self.searched: list[int] = []
        self.fetched: list[str] = []

    def search(self, target, query, display=50, page=1):
        self.searched.append(page)
        return self.pages.get(page, [])

    def fetch(self, target, doc_id):
        self.fetched.append(doc_id)
        return detail(doc_id)


class TestPagination(unittest.TestCase):
    def _collect(self, client: FakeClient, since_days: int = 1825, **kw):
        return list(
            client.collect(
                targets=["prec"], queries=["법인세"], since_days=since_days,
                max_per_query=2, max_body_chars=1000, **kw
            )
        )

    def test_페이지를_끝까지_넘긴다(self):
        client = FakeClient({
            1: [rec("A", 10), rec("B", 20)],
            2: [rec("C", 30), rec("D", 40)],
            3: [rec("E", 50)],  # 마지막(요청보다 적음)
        })
        got = self._collect(client)
        self.assertEqual([p.doc_id for p in got], ["A", "B", "C", "D", "E"])
        self.assertEqual(client.searched, [1, 2, 3])

    def test_기간_밖_페이지를_만나면_멈춘다(self):
        """내림차순이므로 더 볼 것이 없습니다."""
        old = 365 * 6  # 6년 전 = 5년 창 밖
        client = FakeClient({
            1: [rec("A", 10), rec("B", 20)],
            2: [rec("C", old), rec("D", old + 10)],
            3: [rec("E", old + 20)],
        })
        got = self._collect(client, since_days=1825)
        self.assertEqual([p.doc_id for p in got], ["A", "B"])
        self.assertEqual(client.searched, [1, 2], "3페이지는 보지 않아야 합니다")

    def test_날짜를_못_읽는_페이지에서_멈추지_않는다(self):
        """형식이 다른 페이지 하나 때문에 그 뒤 구간을 통째로 놓치면 안 됩니다."""
        no_date = [
            {"판례일련번호": "X", "사건명": "날짜없음", "법원명": "대법원"},
            {"판례일련번호": "Y", "사건명": "날짜없음2", "법원명": "대법원"},
        ]
        client = FakeClient({
            1: [rec("A", 10), rec("B", 20)],
            2: no_date,
            3: [rec("C", 30), rec("D", 40)],
            4: [rec("E", 50)],
        })
        got = self._collect(client)
        self.assertEqual([p.doc_id for p in got], ["A", "B", "X", "Y", "C", "D", "E"])
        self.assertEqual(client.searched, [1, 2, 3, 4])

    def test_max_pages_안전장치(self):
        pages = {n: [rec(f"P{n}a", n), rec(f"P{n}b", n)] for n in range(1, 20)}
        client = FakeClient(pages)
        self._collect(client, max_pages=3)
        self.assertEqual(client.searched, [1, 2, 3])

    def test_이미_받아_둔_건은_상세조회를_건너뛴다(self):
        client = FakeClient({1: [rec("A", 10), rec("B", 20)]})
        got = self._collect(client, skip_keys={"prec:A"})
        self.assertEqual([p.doc_id for p in got], ["B"])
        self.assertEqual(client.fetched, ["B"], "A는 본문을 다시 받지 않아야 합니다")

    def test_빈_페이지면_멈춘다(self):
        client = FakeClient({1: [rec("A", 10), rec("B", 20)], 2: []})
        self._collect(client)
        self.assertEqual(client.searched, [1, 2])


class TestStoreQueue(unittest.TestCase):
    def _store(self, tmp):
        cfg = load_config()
        cfg.root = Path(tmp)
        return Store(cfg.db_path)

    def _prec(self, doc_id: str, days_ago: int = 1, body: str = "본문") -> Precedent:
        return Precedent(
            source="prec", doc_id=doc_id, title=f"제목{doc_id}", case_no="2020구합1",
            court="대법원", decided_on=TODAY - timedelta(days=days_ago),
            summary="요약", body=body, url="https://www.law.go.kr/",
        )

    def test_본문을_보관하고_되살린다(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                store.save_precedent(self._prec("A", body="아주 긴 본문"))
                got = store.unanalyzed_precedents()
                self.assertEqual(len(got), 1)
                self.assertEqual(got[0].body, "아주 긴 본문")
                self.assertEqual(got[0].decided_on, TODAY - timedelta(days=1))

    def test_최신순으로_꺼낸다(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                store.save_precedent(self._prec("OLD", days_ago=900))
                store.save_precedent(self._prec("NEW", days_ago=3))
                self.assertEqual(
                    [p.doc_id for p in store.unanalyzed_precedents()], ["NEW", "OLD"]
                )

    def test_본문이_없으면_큐에_넣지_않는다(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                p = self._prec("A", body="")
                p.summary = ""
                store.save_precedent(p)
                self.assertEqual(store.unanalyzed_precedents(), [])

    def test_반복_실패한_건은_큐에서_빠진다(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                store.save_precedent(self._prec("BAD"))
                for _ in range(MAX_ANALYSIS_ATTEMPTS):
                    self.assertEqual(len(store.unanalyzed_precedents()), 1)
                    store.mark_analysis_failed("prec:BAD")
                self.assertEqual(
                    store.unanalyzed_precedents(), [],
                    "계속 실패하는 건이 매일 한도를 먼저 먹으면 안 됩니다",
                )

    def test_재크롤링해도_실패기록이_지워지지_않는다(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                store.save_precedent(self._prec("BAD"))
                for _ in range(MAX_ANALYSIS_ATTEMPTS):
                    store.mark_analysis_failed("prec:BAD")
                store.save_precedent(self._prec("BAD"))  # 다시 수집
                self.assertEqual(store.unanalyzed_precedents(), [])

    def test_본문없는_건도_최근이면_다시_긁지_않는다(self):
        """매번 다시 조회하면 하루 수백 건이 헛돕니다."""
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                store.save_precedent(self._prec("HAS"))
                empty = self._prec("EMPTY", body="")
                empty.summary = ""
                store.save_precedent(empty)
                self.assertEqual(store.stored_keys(), {"prec:HAS", "prec:EMPTY"})

    def test_본문없는_건은_기간이_지나면_다시_확인한다(self):
        """나중에 본문이 붙는 경우가 있으므로 영구히 포기하지는 않습니다."""
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                store.save_precedent(self._prec("HAS"))
                empty = self._prec("EMPTY", body="")
                empty.summary = ""
                store.save_precedent(empty)
                old_ts = (datetime.now() - timedelta(days=90)).isoformat(timespec="seconds")
                store.conn.execute("UPDATE precedents SET fetched_at = ?", (old_ts,))
                store.conn.commit()
                self.assertEqual(
                    store.stored_keys(recheck_days=30), {"prec:HAS"},
                    "본문 있는 건만 계속 건너뜁니다",
                )

    def test_진행_상황_집계(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                store.save_precedent(self._prec("A"))
                store.save_precedent(self._prec("B"))
                empty = self._prec("C", body="")
                empty.summary = ""
                store.save_precedent(empty)
                st = store.precedent_stats()
                self.assertEqual(st["total"], 3)
                self.assertEqual(st["with_body"], 2)
                self.assertEqual(st["pending"], 2)
                self.assertEqual(st["analyzed"], 0)

    def test_force는_이미_분석한_것도_다시_꺼낸다(self):
        from taxrefund.models import Analysis

        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                store.save_precedent(self._prec("DONE"))
                store.save_precedent(self._prec("TODO", days_ago=2))
                store.save_analysis("prec:DONE", Analysis(headline="이미 봄"))
                self.assertEqual(
                    [p.doc_id for p in store.unanalyzed_precedents()], ["TODO"]
                )
                self.assertEqual(
                    [p.doc_id for p in store.analyzable_precedents()], ["DONE", "TODO"]
                )

    def test_한도만큼만_꺼낸다(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                for i in range(10):
                    store.save_precedent(self._prec(f"P{i}", days_ago=i + 1))
                self.assertEqual(len(store.unanalyzed_precedents(limit=3)), 3)


class TestRunSplit(unittest.TestCase):
    """수집이 모델 한도에 묶이지 않아야 합니다."""

    def test_한도를_넘겨도_수집분은_모두_남는다(self):
        from taxrefund import pipeline
        from taxrefund.analyzer import QuotaExceededError

        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config()
            cfg.root = Path(tmp)
            cfg.analyze.per_run_limit = 2
            cfg.notify.channels = []

            crawled = [
                Precedent(
                    source="prec", doc_id=f"P{i}", title=f"판례{i}", case_no="2020구합1",
                    court="대법원", decided_on=TODAY - timedelta(days=i + 1),
                    summary="요약", body="본문", url="https://www.law.go.kr/",
                )
                for i in range(6)
            ]

            class Broke:
                def analyze(self, prec):
                    raise QuotaExceededError("일일 한도 소진")

            with mock.patch.object(pipeline, "iter_precedents",
                                   lambda c, s, skip_keys=None: iter(crawled)), \
                 mock.patch.object(pipeline, "make_analyzer", lambda *a, **k: Broke()), \
                 mock.patch.object(pipeline, "get_public_ip", lambda *a, **k: None):
                pipeline.run(cfg)

            with Store(cfg.db_path) as store:
                st = store.precedent_stats()
            self.assertEqual(st["total"], 6, "분석이 막혀도 수집분은 남아야 합니다")
            self.assertEqual(st["analyzed"], 0)
            self.assertEqual(st["pending"], 6)


class TestOldDatabaseMigration(unittest.TestCase):
    """뒤늦게 컬럼이 추가된 뒤에도 옛 DB가 열려야 합니다.

    CREATE TABLE IF NOT EXISTS는 이미 있는 표에 컬럼을 붙이지 못합니다.
    그래서 새 컬럼을 가리키는 CREATE INDEX를 마이그레이션보다 먼저 돌리면
    옛 DB가 'no such column'으로 열리지 않고, 봇 전체가 멈춥니다.
    """

    def _make_old_db(self, path: str) -> None:
        import sqlite3

        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE precedents (
                key         TEXT PRIMARY KEY,
                source      TEXT NOT NULL,
                doc_id      TEXT NOT NULL,
                title       TEXT NOT NULL,
                case_no     TEXT,
                court       TEXT,
                decided_on  TEXT,
                url         TEXT,
                fetched_at  TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO precedents VALUES (?,?,?,?,?,?,?,?,?)",
            ("prec:1", "prec", "1", "법인세부과처분취소",
             "2020두1", "대법원", "2024-01-01", "http://x", "2024-01-01"),
        )
        conn.commit()
        conn.close()

    def test_컬럼이_없는_옛_DB도_열린다(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "old.db")
            self._make_old_db(path)

            with Store(path) as store:          # 여기서 터지면 안 됩니다
                stats = store.precedent_stats()

            self.assertEqual(stats["total"], 1)

    def test_새_컬럼과_색인이_실제로_붙는다(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "old.db")
            self._make_old_db(path)

            with Store(path) as store:
                cols = {
                    r["name"]
                    for r in store.conn.execute("PRAGMA table_info(precedents)")
                }
                idx = {
                    r["name"]
                    for r in store.conn.execute("PRAGMA index_list(precedents)")
                }

            for column in ("tax_type", "body", "body_source", "attempts"):
                self.assertIn(column, cols)
            self.assertIn("idx_precedents_tax", idx)


if __name__ == "__main__":
    unittest.main(verbosity=2)

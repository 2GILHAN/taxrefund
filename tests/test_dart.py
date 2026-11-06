"""DART 클라이언트·기업 색인 검증 (네트워크 호출은 모킹).

실행: python -m tests.test_dart
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from taxrefund.config import load_config  # noqa: E402
from taxrefund.dart import Company, DartClient, DartError  # noqa: E402
from taxrefund.store import Store  # noqa: E402

CORP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<result>
  <list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>
        <stock_code>005930</stock_code><modify_date>20260101</modify_date></list>
  <list><corp_code>00111111</corp_code><corp_name>비상장외감(주)</corp_name>
        <stock_code></stock_code><modify_date>20260101</modify_date></list>
</result>"""


def corp_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", CORP_XML.encode("utf-8"))
    return buf.getvalue()


def resp(*, content=b"", json_body=None, status=200):
    return SimpleNamespace(
        content=content,
        text=content.decode("utf-8", "replace") if content else "",
        status_code=status,
        raise_for_status=lambda: None,
        json=lambda: json_body if json_body is not None else {},
    )


class TestClientBasics(unittest.TestCase):
    def test_키가_없으면_에러(self):
        with self.assertRaises(DartError) as ctx:
            DartClient("")
        self.assertIn("opendart", str(ctx.exception))

    def test_상태코드를_사람이_읽을_수_있게(self):
        client = DartClient("k", interval_sec=0)
        with self.assertRaises(DartError) as ctx:
            client._check({"status": "020", "message": ""})
        self.assertIn("요청 제한", str(ctx.exception))

        with self.assertRaises(DartError) as ctx:
            client._check({"status": "010", "message": ""})
        self.assertIn("등록되지 않은", str(ctx.exception))

    def test_데이터없음은_오류가_아니다(self):
        client = DartClient("k", interval_sec=0)
        self.assertEqual(client._check({"status": "013"})["status"], "013")


class TestCorpCodes(unittest.TestCase):
    def test_ZIP_파싱(self):
        client = DartClient("k", interval_sec=0)
        with mock.patch.object(client.session, "get", lambda *a, **kw: resp(content=corp_zip())):
            companies = client.corp_codes()
        self.assertEqual(len(companies), 2)
        self.assertEqual(companies[0].corp_name, "삼성전자")
        self.assertEqual(companies[0].stock_code, "005930")

    def test_인증오류는_XML로_와도_잡는다(self):
        """ZIP이 아니라 XML 오류가 오는 경우가 있습니다."""
        err = b'<?xml version="1.0"?><result><status>010</status><message>x</message></result>'
        client = DartClient("k", interval_sec=0)
        with mock.patch.object(client.session, "get", lambda *a, **kw: resp(content=err)):
            with self.assertRaises(DartError) as ctx:
                client.corp_codes()
        self.assertIn("010", str(ctx.exception))


class TestFilers(unittest.TestCase):
    def test_페이지를_끝까지_넘긴다(self):
        pages = {
            1: {"status": "000", "total_page": 2, "list": [{"corp_code": "A"}, {"corp_code": "B"}]},
            2: {"status": "000", "total_page": 2, "list": [{"corp_code": "B"}, {"corp_code": "C"}]},
        }
        client = DartClient("k", interval_sec=0)
        client._get_json = lambda path, **p: pages[p["page_no"]]
        self.assertEqual(client.annual_report_filers(), {"A", "B", "C"})

    def test_결과없음이면_빈집합(self):
        client = DartClient("k", interval_sec=0)
        client._get_json = lambda path, **p: {"status": "013"}
        self.assertEqual(client.annual_report_filers(), set())


class TestProfiles(unittest.TestCase):
    def _client(self):
        client = DartClient("k", interval_sec=0)
        return client

    def test_기업개황_파싱(self):
        client = self._client()
        client._get_json = lambda path, **p: {
            "status": "000", "corp_name": "테스트(주)", "corp_cls": "E",
            "bizr_no": "1234567890", "induty_code": "26410", "acc_mt": "12",
            "adres": "서울시", "est_dt": "20100101", "ceo_nm": "홍길동",
        }
        c = client.company("00111111")
        self.assertEqual(c.corp_name, "테스트(주)")
        self.assertEqual(c.induty_code, "26410")
        self.assertEqual(c.acc_mt, "12")
        self.assertFalse(c.is_listed)
        self.assertIn("외감", c.cls_label)

    def test_일일한도에_걸리면_중단하고_수집분은_보존(self):
        client = self._client()
        calls = {"n": 0}

        def limited(code):
            calls["n"] += 1
            if calls["n"] > 2:
                raise DartError("DART 오류 020: 요청 제한을 초과했습니다. (일 20,000건)")
            return Company(corp_code=code, corp_name=f"회사{calls['n']}", induty_code="26")

        client.company = limited
        got = list(client.profiles(["A", "B", "C", "D"]))
        self.assertEqual(len(got), 2, "한도 전까지 모은 것은 살아야 합니다")

    def test_개별_실패는_건너뛰고_계속(self):
        client = self._client()

        def flaky(code):
            if code == "B":
                raise DartError("DART 오류 100: 필드의 부적절한 값입니다.")
            return Company(corp_code=code, corp_name=code)

        client.company = flaky
        self.assertEqual([c.corp_code for c in client.profiles(["A", "B", "C"])], ["A", "C"])


class TestStore(unittest.TestCase):
    def _store(self, tmp):
        cfg = load_config()
        cfg.root = Path(tmp)
        return Store(cfg.db_path)

    def test_업종코드_접두사_조회(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                store.save_companies([
                    Company("1", "전자부품사", corp_cls="E", induty_code="26410", acc_mt="12"),
                    Company("2", "반도체사", corp_cls="Y", induty_code="26120", acc_mt="12"),
                    Company("3", "건설사", corp_cls="E", induty_code="41220", acc_mt="03"),
                ])
                # 판례는 2자리 KSIC를 내놓고 DART는 5자리 -> 접두사 일치
                names = {r["corp_name"] for r in store.companies_by_industry(["26"])}
                self.assertEqual(names, {"전자부품사", "반도체사"})

                self.assertEqual(
                    [r["corp_name"] for r in store.companies_by_industry(["41"])], ["건설사"]
                )

    def test_외감법인만_필터(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                store.save_companies([
                    Company("1", "외감사", corp_cls="E", induty_code="26410"),
                    Company("2", "상장사", corp_cls="Y", induty_code="26120"),
                ])
                rows = store.companies_by_industry(["26"], audited_only=True)
                self.assertEqual([r["corp_name"] for r in rows], ["외감사"])

    def test_잘못된_업종코드는_빈결과(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                store.save_companies([Company("1", "회사", induty_code="26410")])
                self.assertEqual(store.companies_by_industry([]), [])
                self.assertEqual(store.companies_by_industry(["전업종"]), [])

    def test_이어서_수집하기_위한_기존목록(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                store.save_companies([Company("A", "가"), Company("B", "나")])
                self.assertEqual(store.indexed_corp_codes(), {"A", "B"})

    def test_통계(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                store.save_companies([
                    Company("1", "가", corp_cls="E", induty_code="26410"),
                    Company("2", "나", corp_cls="Y", induty_code="26120"),
                    Company("3", "다", corp_cls="E", induty_code=""),
                ])
                stats = store.company_stats()
                self.assertEqual(stats["total"], 3)
                self.assertEqual(stats["with_industry"], 2)
                self.assertEqual(stats["E"], 2)

    def test_같은_기업_재저장은_갱신(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._store(tmp) as store:
                store.save_companies([Company("1", "옛이름", induty_code="26410")])
                store.save_companies([Company("1", "새이름", induty_code="26410")])
                rows = store.companies_by_industry(["26"])
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["corp_name"], "새이름")


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""웹 대시보드 렌더링·인증과 공인 IP 변동 감지 검증.

실행: python -m tests.test_web_netcheck
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from taxrefund import netcheck  # noqa: E402
from taxrefund.config import Company, load_config  # noqa: E402
from taxrefund.models import Analysis  # noqa: E402
from taxrefund.notifier import Notifier  # noqa: E402
from taxrefund.pipeline import load_sample_precedents  # noqa: E402
from taxrefund.serve import Server  # noqa: E402
from taxrefund.store import META_LAW_OK_IP, Store  # noqa: E402
from taxrefund.web import (  # noqa: E402
    _reject_reason,
    access_urls,
    alert_by_key,
    detail_copy_text,
    exposure_warning,
    gather,
    is_private,
    make_server,
    render_all,
    render_detail,
    render_home,
    render_status,
    resolve_host,
)
from tests.test_pipeline import FAKE_ANALYSIS  # noqa: E402


def seeded_cfg(tmp: str, *, token: str | None = None):
    cfg = load_config()
    cfg.root = Path(tmp)
    cfg.secrets.web_token = token
    cfg.web.host = "127.0.0.1"
    cfg.web.port = 0  # 임의 포트
    cfg.watchlist = [Company(name="전자부품사", industry="전자부품 제조업", ksic=["26"])]
    prec = load_sample_precedents(ROOT)[0]
    with Store(cfg.db_path) as store:
        store.save_precedent(prec)
        store.save_analysis(prec.key, Analysis.from_json(FAKE_ANALYSIS))
    return cfg, prec


class TestNetcheck(unittest.TestCase):
    def setUp(self):
        netcheck.reset_cache()

    def tearDown(self):
        netcheck.reset_cache()

    def test_첫_제공자_성공(self):
        def ok(url, timeout=None):
            return SimpleNamespace(text=" 1.2.3.4 \n", raise_for_status=lambda: None)

        with mock.patch("taxrefund.netcheck.requests.get", ok):
            self.assertEqual(netcheck.get_public_ip(), "1.2.3.4")

    def test_첫_제공자_실패시_다음으로(self):
        import requests

        calls: list[str] = []

        def flaky(url, timeout=None):
            calls.append(url)
            if len(calls) == 1:
                raise requests.RequestException("down")
            return SimpleNamespace(text="5.6.7.8", raise_for_status=lambda: None)

        with mock.patch("taxrefund.netcheck.requests.get", flaky):
            self.assertEqual(netcheck.get_public_ip(), "5.6.7.8")
        self.assertEqual(len(calls), 2)

    def test_전부_실패하면_None(self):
        import requests

        def dead(url, timeout=None):
            raise requests.RequestException("down")

        with mock.patch("taxrefund.netcheck.requests.get", dead):
            self.assertIsNone(netcheck.get_public_ip())

    def test_이상한_응답은_거부(self):
        def html(url, timeout=None):
            return SimpleNamespace(text="<html>error</html>", raise_for_status=lambda: None)

        with mock.patch("taxrefund.netcheck.requests.get", html):
            self.assertIsNone(netcheck.get_public_ip())

    def test_캐시가_재조회를_막는다(self):
        calls = {"n": 0}

        def counting(url, timeout=None):
            calls["n"] += 1
            return SimpleNamespace(text="9.9.9.9", raise_for_status=lambda: None)

        with mock.patch("taxrefund.netcheck.requests.get", counting):
            netcheck.get_public_ip()
            netcheck.get_public_ip()
        self.assertEqual(calls["n"], 1)

    def test_안내문구는_변동을_명시(self):
        msg = netcheck.registration_hint("2.2.2.2", "1.1.1.1")
        self.assertIn("1.1.1.1", msg)
        self.assertIn("2.2.2.2", msg)
        self.assertIn("바뀌었습니다", msg)

    def test_등록이력_없으면_현재IP만_안내(self):
        msg = netcheck.registration_hint("2.2.2.2", None)
        self.assertIn("2.2.2.2", msg)
        self.assertNotIn("바뀌었습니다", msg)


class TestIpChangeAlert(unittest.TestCase):
    def _server(self, tmp: str):
        cfg, _ = seeded_cfg(tmp)
        cfg.notify.startup_ping = False
        with Store(cfg.db_path) as store:
            store.set_meta(META_LAW_OK_IP, "1.1.1.1")
        return Server(cfg, sample=True)

    def test_IP가_바뀌면_한_번만_알린다(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(tmp)
            notices: list[str] = []
            server.notifier.notice = lambda t, m, c=None: notices.append(t) or True

            with mock.patch("taxrefund.serve.get_public_ip", lambda: "2.2.2.2"):
                server.check_ip_change()
                server.check_ip_change()

            self.assertEqual(len(notices), 1)
            self.assertIn("IP", notices[0])

    def test_IP가_같으면_조용(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(tmp)
            notices: list[str] = []
            server.notifier.notice = lambda t, m, c=None: notices.append(t) or True
            with mock.patch("taxrefund.serve.get_public_ip", lambda: "1.1.1.1"):
                server.check_ip_change()
            self.assertEqual(notices, [])

    def test_등록이력_없으면_조용(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = seeded_cfg(tmp)
            cfg.notify.startup_ping = False
            server = Server(cfg, sample=True)
            notices: list[str] = []
            server.notifier.notice = lambda t, m, c=None: notices.append(t) or True
            with mock.patch("taxrefund.serve.get_public_ip", lambda: "2.2.2.2"):
                server.check_ip_change()
            self.assertEqual(notices, [])

    def test_IP조회_실패해도_조용(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(tmp)
            notices: list[str] = []
            server.notifier.notice = lambda t, m, c=None: notices.append(t) or True
            with mock.patch("taxrefund.serve.get_public_ip", lambda: None):
                server.check_ip_change()
            self.assertEqual(notices, [])


class TestRendering(unittest.TestCase):
    def test_제목은_한줄요지를_쓴다(self):
        """사건명은 '법인세부과처분취소'류라 목록에서 구분이 안 됩니다."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg, prec = seeded_cfg(tmp)
            with Store(cfg.db_path) as store:
                analysis = Analysis.from_json(
                    {**FAKE_ANALYSIS, "headline": "직원매장 할인액은 접대비 아닌 매출에누리"}
                )
                store.save_analysis(prec.key, analysis)

            data = gather(cfg)
            for page in (
                render_home(data, None).decode("utf-8"),
                render_all(data, None, "all").decode("utf-8"),
            ):
                self.assertIn("직원매장 할인액은 접대비 아닌 매출에누리", page)
                # 사건명은 부제로 남아 식별은 가능해야 합니다
                self.assertIn(prec.title, page)

    def test_한줄요지가_없으면_사건명으로_물러선다(self):
        """스키마 추가 전에 분석한 건도 깨지지 않아야 합니다."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg, prec = seeded_cfg(tmp)  # FAKE_ANALYSIS에는 headline이 없음
            page = render_home(gather(cfg), None).decode("utf-8")
            self.assertIn(prec.title, page)

    def test_홈에_알림이_보인다(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = seeded_cfg(tmp)
            page = render_home(gather(cfg), None).decode("utf-8")
            self.assertIn("전자부품사", page)
            self.assertIn("법인세부과처분취소", page)
            self.assertIn("viewport", page, "모바일 뷰포트 메타가 있어야 합니다")

    def test_IP변동_배너(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = seeded_cfg(tmp)
            with Store(cfg.db_path) as store:
                store.set_meta(META_LAW_OK_IP, "1.1.1.1")
                store.set_meta("public_ip", "2.2.2.2")
            page = render_home(gather(cfg), None).decode("utf-8")
            self.assertIn("공인 IP가 바뀌었습니다", page)
            self.assertIn("2.2.2.2", page)

    def test_통계타일이_클릭가능한_링크다(self):
        """div면 눌러도 아무 일도 안 일어납니다. a 태그여야 합니다."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = seeded_cfg(tmp)
            page = render_home(gather(cfg), None).decode("utf-8")
            self.assertNotIn('<div class="stat">', page, "통계 타일이 링크가 아닙니다")
            for label, href in (
                ("수집", "/all?f=all"),
                ("알림 대상", "/all?f=alert"),
                ("미발송", "/all?f=pending"),
            ):
                self.assertIn(f'href="{href}"', page, f"'{label}' 타일 링크 없음")

    def test_토큰이_타일_링크에_유지된다(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = seeded_cfg(tmp, token="s3cret")
            page = render_home(gather(cfg), "s3cret").decode("utf-8")
            self.assertIn("/all?t=s3cret&f=alert", page)

    def test_필터가_실제로_걸러낸다(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, prec = seeded_cfg(tmp)
            data = gather(cfg)

            alert_page = render_all(data, None, "alert").decode("utf-8")
            self.assertIn(prec.title, alert_page)

            # 점수 미달로 만들면 알림 필터에서 빠지고 제외됨 필터에 잡혀야 합니다
            cfg.notify.min_confidence = 99
            data2 = gather(cfg)
            self.assertNotIn(prec.title, render_all(data2, None, "alert").decode("utf-8"))
            self.assertIn(prec.title, render_all(data2, None, "rejected").decode("utf-8"))

    def test_미발송_필터(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, prec = seeded_cfg(tmp)
            data = gather(cfg)
            self.assertIn(prec.title, render_all(data, None, "pending").decode("utf-8"))

            with Store(cfg.db_path) as store:
                for ch in cfg.notify.channels:
                    store.mark_notified(prec.key, ch)
            self.assertNotIn(
                prec.title, render_all(gather(cfg), None, "pending").decode("utf-8")
            )

    def test_필터칩이_현재_선택을_표시(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = seeded_cfg(tmp)
            page = render_all(gather(cfg), None, "pending").decode("utf-8")
            self.assertIn('class="chip on">미발송', page)

    def test_전체목록에_탈락사유(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = seeded_cfg(tmp)
            cfg.notify.min_confidence = 99  # 일부러 탈락시킴
            page = render_all(gather(cfg), None).decode("utf-8")
            self.assertIn("점수 미달", page)

    def test_상세페이지(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, prec = seeded_cfg(tmp)
            alert = alert_by_key(cfg, prec.key)
            self.assertIsNotNone(alert)
            page = render_detail(alert, None).decode("utf-8")
            for expected in ("쟁점 요약", "경정청구 실익", "확인할 것", "청구 기한"):
                self.assertIn(expected, page)

    def test_복사버튼은_하나뿐이다(self):
        """버튼이 많으면 오히려 방해됩니다. 첫 박스에 하나만 둡니다."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg, prec = seeded_cfg(tmp)
            page = render_detail(alert_by_key(cfg, prec.key), None).decode("utf-8")
            buttons = re.findall(r'class="copy[^"]*" data-target="([^"]+)"', page)
            self.assertEqual(buttons, ["cp-full"], f"복사 버튼이 하나가 아님: {buttons}")
            self.assertIn('id="cp-full"', page)

    def test_복사버튼이_첫_박스_안에_있다(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, prec = seeded_cfg(tmp)
            page = render_detail(alert_by_key(cfg, prec.key), None).decode("utf-8")
            first_section = page.split("<section>")[1].split("</section>")[0]
            self.assertIn('data-target="cp-full"', first_section)
            self.assertIn("사건번호", first_section, "첫 박스가 정보 박스가 아닙니다")

    def test_복사_스크립트가_비보안_컨텍스트를_대비(self):
        """HTTP로 서비스하므로 navigator.clipboard만으로는 폰에서 실패합니다."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg, prec = seeded_cfg(tmp)
            page = render_detail(alert_by_key(cfg, prec.key), None).decode("utf-8")
            self.assertIn("isSecureContext", page)
            self.assertIn("execCommand", page, "데스크톱 HTTP 폴백이 없습니다")
            self.assertIn('id="tr-manual"', page, "수동 복사 영역이 없습니다")

    def test_iOS에서는_execCommand를_시도하지_않는다(self):
        """iOS는 execCommand가 true를 반환해도 실제로 복사되지 않습니다.

        시도하면 '복사됨'이라고 거짓 안내를 하게 되므로, iOS + 비보안 컨텍스트에서는
        곧바로 수동 복사 경로로 가야 합니다.
        """
        with tempfile.TemporaryDirectory() as tmp:
            cfg, prec = seeded_cfg(tmp)
            page = render_detail(alert_by_key(cfg, prec.key), None).decode("utf-8")
            script = page.split("<script>")[-1]
            self.assertIn("trIsIOS", script)
            # iOS 판정 직후 execCommand 없이 reject 하는 분기가 있어야 합니다
            self.assertIn("ios-insecure", script)
            ios_branch = script.split("if (trIsIOS())")[1].split("}")[0]
            self.assertIn("reject", ios_branch)
            self.assertNotIn("execCommand", ios_branch)

    def test_수동복사_영역에_전체선택_버튼(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, prec = seeded_cfg(tmp)
            page = render_detail(alert_by_key(cfg, prec.key), None).decode("utf-8")
            self.assertIn('id="tr-manual-all"', page)
            self.assertIn('id="tr-manual-close"', page)
            self.assertIn("길게 눌러", page)

    def test_복사본문은_정보박스와_쟁점실익_링크까지(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, prec = seeded_cfg(tmp)
            alert = alert_by_key(cfg, prec.key)
            text = detail_copy_text(alert)

            # 첫 박스에 보이는 항목
            self.assertIn(prec.title, text)
            self.assertIn(prec.case_no, text)
            self.assertIn(prec.decided_str, text)
            self.assertIn("결론:", text)
            self.assertIn("청구 근거:", text)
            self.assertIn("적용 업종:", text)
            # 쟁점 + 실익
            self.assertIn(alert.analysis.issue_summary, text)
            self.assertIn(alert.analysis.refund_rationale, text)
            # 원문 링크
            self.assertIn(prec.url, text)

    def test_복사본문에_체크리스트_기한은_넣지_않는다(self):
        """화면에서 보는 용도라 복사 분량을 늘리지 않습니다."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg, prec = seeded_cfg(tmp)
            text = detail_copy_text(alert_by_key(cfg, prec.key))
            self.assertNotIn("확인할 것", text)
            self.assertNotIn("청구 기한", text)

    def test_복사데이터는_JSON으로_안전하게_직렬화(self):
        """줄바꿈·따옴표·태그가 섞여도 문서를 깨뜨리지 않아야 합니다."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg, prec = seeded_cfg(tmp)
            with Store(cfg.db_path) as store:
                store.conn.execute(
                    "UPDATE precedents SET title=? WHERE key=?",
                    ('제목"인용" <script>alert(1)</script>\n둘째줄', prec.key),
                )
                store.conn.commit()
            page = render_detail(alert_by_key(cfg, prec.key), None).decode("utf-8")
            self.assertNotIn("<script>alert(1)</script>", page)
            payload = re.search(
                r'<script type="application/json" id="cp-full">(.*?)</script>', page, re.S
            )
            self.assertIsNotNone(payload, "복사 데이터 블록이 없습니다")
            restored = json.loads(payload.group(1).replace("\\u003c", "<"))
            self.assertIn("둘째줄", restored, "줄바꿈이 보존되어야 합니다")
            self.assertIn('"인용"', restored)

    def test_상태페이지(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = seeded_cfg(tmp)
            page = render_status(gather(cfg), None).decode("utf-8")
            self.assertIn("워치리스트", page)
            self.assertIn("전자부품사", page)

    def test_HTML_이스케이프(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, prec = seeded_cfg(tmp)
            with Store(cfg.db_path) as store:
                store.conn.execute(
                    "UPDATE precedents SET title=? WHERE key=?",
                    ("<script>alert(1)</script>", prec.key),
                )
                store.conn.commit()
            page = render_home(gather(cfg), None).decode("utf-8")
            self.assertNotIn("<script>alert(1)</script>", page)
            self.assertIn("&lt;script&gt;", page)

    def test_탈락사유_분류(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, prec = seeded_cfg(tmp)
            cases = [
                ({"refund_claim_possible": False}, "실익"),
                ({"refund_confidence": 10}, "점수 미달"),
                ({"generalizable": False}, "일반화"),
            ]
            for patch, expected in cases:
                analysis = Analysis.from_json({**FAKE_ANALYSIS, **patch})
                self.assertIn(expected, _reject_reason(prec, analysis, cfg))
            self.assertEqual(_reject_reason(prec, Analysis.from_json(FAKE_ANALYSIS), cfg), "")

    def test_알림_세목이_아니면_그_이유가_나온다(self):
        """세목 게이트가 is_corporate_tax를 대신합니다."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg, prec = seeded_cfg(tmp)
            cfg.notify.tax_types = ["부가가치세"]      # 법인세를 뺍니다
            analysis = Analysis.from_json(FAKE_ANALYSIS)
            reason = _reject_reason(prec, analysis, cfg)
            self.assertIn("알림 세목 아님", reason)
            self.assertIn(prec.resolved_tax_type, reason)

    def test_세목_목록이_비면_세목으로_거르지_않는다(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, prec = seeded_cfg(tmp)
            cfg.notify.tax_types = []
            self.assertEqual(_reject_reason(prec, Analysis.from_json(FAKE_ANALYSIS), cfg), "")


class TestHostResolution(unittest.TestCase):
    def _cfg(self, host: str):
        cfg = load_config()
        cfg.web.host = host
        return cfg

    def test_tailscale_별칭(self):
        with mock.patch("taxrefund.web.tailscale_ip", lambda: "100.106.34.95"):
            self.assertEqual(resolve_host(self._cfg("tailscale")), "100.106.34.95")

    def test_tailscale_없으면_로컬로_안전하게(self):
        with mock.patch("taxrefund.web.tailscale_ip", lambda: None):
            self.assertEqual(resolve_host(self._cfg("tailscale")), "127.0.0.1")

    def test_local_별칭(self):
        self.assertEqual(resolve_host(self._cfg("local")), "127.0.0.1")

    def test_그_외는_그대로(self):
        self.assertEqual(resolve_host(self._cfg("192.168.0.5")), "192.168.0.5")

    def test_사설_대역_판정(self):
        for ip in ("10.0.0.1", "192.168.1.2", "172.16.0.1", "127.0.0.1", "100.106.34.95"):
            self.assertTrue(is_private(ip), ip)
        for ip in ("182.221.152.12", "8.8.8.8", "1.1.1.1"):
            self.assertFalse(is_private(ip), ip)

    def test_공인IP_직결이면_0000_바인드를_경고(self):
        cfg = self._cfg("0.0.0.0")
        with mock.patch("taxrefund.web.outbound_ip", lambda: "182.221.152.12"):
            warning = exposure_warning(cfg)
        self.assertIsNotNone(warning)
        self.assertIn("인터넷", warning)

    def test_NAT_뒤면_경고없음(self):
        cfg = self._cfg("0.0.0.0")
        with mock.patch("taxrefund.web.outbound_ip", lambda: "192.168.0.10"):
            self.assertIsNone(exposure_warning(cfg))

    def test_tailscale_바인드는_경고없음(self):
        cfg = self._cfg("tailscale")
        with mock.patch("taxrefund.web.tailscale_ip", lambda: "100.106.34.95"):
            self.assertIsNone(exposure_warning(cfg))

    def test_접속주소에_토큰이_붙는다(self):
        cfg = self._cfg("tailscale")
        cfg.secrets.web_token = "abc"
        with mock.patch("taxrefund.web.tailscale_ip", lambda: "100.106.34.95"), \
             mock.patch("taxrefund.web.tailscale_https_url", lambda p: None):
            urls = access_urls(cfg)
        self.assertTrue(any("100.106.34.95" in u and "t=abc" in u for u in urls))

    def test_HTTPS_프록시가_있으면_그_주소를_먼저_안내(self):
        """127.0.0.1에만 묶인 상태에서 localhost만 안내하면 폰에서 못 씁니다."""
        cfg = self._cfg("local")
        cfg.secrets.web_token = "abc"
        with mock.patch(
            "taxrefund.web.tailscale_https_url", lambda p: "https://home.tailnet.ts.net/"
        ):
            urls = access_urls(cfg)
        self.assertIn("https://home.tailnet.ts.net/?t=abc", urls[0])
        self.assertIn("HTTPS", urls[0])

    def test_HTTPS_프록시가_없으면_안내하지_않는다(self):
        cfg = self._cfg("local")
        with mock.patch("taxrefund.web.tailscale_https_url", lambda p: None):
            urls = access_urls(cfg)
        self.assertFalse(any("https://" in u for u in urls))


class TestHttpServer(unittest.TestCase):
    """실제로 HTTP 서버를 띄워 요청해 봅니다."""

    def _run(self, cfg):
        server = make_server(cfg)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, port

    def _get(self, port: int, path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def test_페이지들이_200을_준다(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, prec = seeded_cfg(tmp)
            server, port = self._run(cfg)
            try:
                for path in ("/", "/all", "/status", "/health", f"/d/{prec.key}"):
                    status, body = self._get(port, path)
                    self.assertEqual(status, 200, f"{path} -> {status}")
                    self.assertTrue(body)
            finally:
                server.shutdown()
                server.server_close()

    def test_JSON_API(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = seeded_cfg(tmp)
            server, port = self._run(cfg)
            try:
                status, body = self._get(port, "/api/alerts.json")
                self.assertEqual(status, 200)
                data = json.loads(body)
                self.assertEqual(len(data), 1)
                self.assertEqual(data[0]["companies"], ["전자부품사"])
                self.assertIn("deadlines", data[0])
            finally:
                server.shutdown()
                server.server_close()

    def test_없는_경로는_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = seeded_cfg(tmp)
            server, port = self._run(cfg)
            try:
                self.assertEqual(self._get(port, "/nope")[0], 404)
                self.assertEqual(self._get(port, "/d/prec:9999")[0], 404)
            finally:
                server.shutdown()
                server.server_close()

    def test_토큰_인증(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = seeded_cfg(tmp, token="s3cret")
            server, port = self._run(cfg)
            try:
                self.assertEqual(self._get(port, "/")[0], 401)
                self.assertEqual(self._get(port, "/?t=wrong")[0], 401)
                status, body = self._get(port, "/?t=s3cret")
                self.assertEqual(status, 200)
                self.assertIn("전자부품사", body)
                # health는 토큰 없이도 열려 있어야 모니터링이 가능합니다
                self.assertEqual(self._get(port, "/health")[0], 200)
            finally:
                server.shutdown()
                server.server_close()


class TestStatTileConsistency(unittest.TestCase):
    """타일 숫자와 그 타일을 눌렀을 때 나오는 목록이 일치해야 합니다.

    미발송을 채널별 합계로 세면 한 판례가 채널 수만큼 곱해져서, 옆의
    '수집·분석·알림 대상'(모두 판례 개수)과 기준이 어긋납니다.
    """

    def _data(self, pending_keys, rows_pending, channels):
        return {
            "alerts": [],
            "rows": [{"pending": True} for _ in range(rows_pending)],
            "counts": {"precedents": 100, "analyses": 50},
            "last_run": None,
            "public_ip": None,
            "law_ok_ip": None,
            "pending": {ch: len(pending_keys) for ch in channels},
            "pending_keys": set(pending_keys),
            "cfg": None,
        }

    def test_미발송은_채널수만큼_곱해지지_않는다(self):
        keys = {f"prec:{i}" for i in range(18)}
        data = self._data(keys, 18, ["console", "file", "discord"])
        html = render_home(data, token=None).decode("utf-8")
        self.assertIn(">18<", html, "판례 18건이어야 합니다")
        self.assertNotIn(">54<", html, "채널 수(3)를 곱하면 안 됩니다")

    def test_채널이_하나여도_같은_값(self):
        keys = {f"prec:{i}" for i in range(7)}
        one = render_home(self._data(keys, 7, ["console"]), None).decode("utf-8")
        three = render_home(
            self._data(keys, 7, ["console", "file", "discord"]), None
        ).decode("utf-8")
        self.assertIn(">7<", one)
        self.assertIn(">7<", three, "채널 수가 늘어도 미발송 판례 수는 같습니다")

    def test_미발송이_없으면_0(self):
        html = render_home(self._data(set(), 0, ["console"]), None).decode("utf-8")
        self.assertIn(">0<", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)


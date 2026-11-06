"""Tailscale Serve 매핑 자가복구 검증 (CLI 호출은 모킹).

Serve 설정은 프로젝트별이 아니라 노드 전체에 하나뿐이라, 다른 프로젝트가
덮어쓰면 대시보드 매핑만 조용히 사라집니다. 그 상황을 재현합니다.

실행: python -m tests.test_tailscale_serve
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from taxrefund import web  # noqa: E402
from taxrefund.config import load_config  # noqa: E402
from taxrefund.serve import Server  # noqa: E402

HOST = "home.tailc06b21.ts.net"


def conf(*entries: tuple[str, str]) -> dict:
    """(호스트:포트, 프록시대상) 목록으로 serve status --json 모양을 만듭니다."""
    web_conf: dict = {}
    for host_port, proxy in entries:
        web_conf[host_port] = {"Handlers": {"/": {"Proxy": proxy}}}
    return {"Web": web_conf}


DASHBOARD = (f"{HOST}:443", "http://127.0.0.1:8787")
OTHER_APP = (f"{HOST}:8080", "http://127.0.0.1:3000")


class TestUrlLookup(unittest.TestCase):
    def test_443이면_포트를_붙이지_않는다(self):
        with mock.patch.object(web, "_tailscale_serve_config", lambda: conf(DASHBOARD)):
            self.assertEqual(web.tailscale_https_url(8787), f"https://{HOST}/")

    def test_다른_포트는_주소에_드러난다(self):
        entry = (f"{HOST}:8443", "http://127.0.0.1:8787")
        with mock.patch.object(web, "_tailscale_serve_config", lambda: conf(entry)):
            self.assertEqual(web.tailscale_https_url(8787), f"https://{HOST}:8443/")

    def test_매핑이_없으면_None(self):
        with mock.patch.object(web, "_tailscale_serve_config", lambda: conf(OTHER_APP)):
            self.assertIsNone(web.tailscale_https_url(8787))

    def test_tailscale이_없으면_None(self):
        with mock.patch.object(web, "_tailscale_serve_config", lambda: None):
            self.assertIsNone(web.tailscale_https_url(8787))


class TestEnsure(unittest.TestCase):
    def _run(self, states: list[dict], returncode: int = 0):
        """states를 순서대로 돌려주는 _tailscale_serve_config + run 캡처."""
        seq = list(states)
        calls: list[list[str]] = []

        def next_conf():
            return seq.pop(0) if len(seq) > 1 else seq[0]

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return SimpleNamespace(returncode=returncode, stdout="", stderr="거부됨")

        return next_conf, fake_run, calls

    def test_이미_붙어있으면_아무것도_하지_않는다(self):
        next_conf, fake_run, calls = self._run([conf(DASHBOARD)])
        with mock.patch.object(web, "_tailscale_serve_config", next_conf), \
             mock.patch.object(web.subprocess, "run", fake_run):
            url, restored = web.ensure_tailscale_serve(8787)
        self.assertEqual(url, f"https://{HOST}/")
        self.assertFalse(restored)
        self.assertEqual(calls, [], "멀쩡한데 명령을 부르면 안 됩니다")

    def test_사라졌으면_다시_붙인다(self):
        # 처음엔 없고(다른 프로젝트가 덮어씀), 명령 후에는 있음
        next_conf, fake_run, calls = self._run([conf(OTHER_APP), conf(DASHBOARD, OTHER_APP)])
        with mock.patch.object(web, "_tailscale_serve_config", next_conf), \
             mock.patch.object(web.subprocess, "run", fake_run):
            url, restored = web.ensure_tailscale_serve(8787)
        self.assertEqual(url, f"https://{HOST}/")
        self.assertTrue(restored)
        self.assertEqual(len(calls), 1)
        self.assertIn("--https=443", calls[0])
        self.assertIn("http://127.0.0.1:8787", calls[0])

    def test_443을_남이_쓰면_빼앗지_않는다(self):
        taken = (f"{HOST}:443", "http://127.0.0.1:3000")
        next_conf, fake_run, calls = self._run([conf(taken)])
        with mock.patch.object(web, "_tailscale_serve_config", next_conf), \
             mock.patch.object(web.subprocess, "run", fake_run):
            url, restored = web.ensure_tailscale_serve(8787)
        self.assertIsNone(url)
        self.assertFalse(restored)
        self.assertEqual(calls, [], "남의 매핑을 덮어쓰면 다른 프로젝트가 죽습니다")

    def test_명령이_실패하면_복구했다고_보고하지_않는다(self):
        next_conf, fake_run, _ = self._run([conf(OTHER_APP)], returncode=1)
        with mock.patch.object(web, "_tailscale_serve_config", next_conf), \
             mock.patch.object(web.subprocess, "run", fake_run):
            url, restored = web.ensure_tailscale_serve(8787)
        self.assertIsNone(url)
        self.assertFalse(restored)

    def test_명령은_성공했지만_안_붙었으면_실패로_본다(self):
        """returncode 0만 믿으면 안 됩니다. 실제 매핑을 다시 확인합니다."""
        next_conf, fake_run, _ = self._run([conf(OTHER_APP)])
        with mock.patch.object(web, "_tailscale_serve_config", next_conf), \
             mock.patch.object(web.subprocess, "run", fake_run):
            url, restored = web.ensure_tailscale_serve(8787)
        self.assertIsNone(url)
        self.assertFalse(restored)


class TestServerHook(unittest.TestCase):
    def _server(self, host: str = "local"):
        cfg = load_config()
        cfg.web.host = host
        cfg.web.enabled = True
        server = Server(cfg)
        server.dashboard = object()
        server.notifier = SimpleNamespace(sent=[], notice=lambda *a, **k: server.notifier.sent.append(a))
        return server

    def test_복구하면_디스코드로_알린다(self):
        server = self._server()
        with mock.patch.object(
            web, "ensure_tailscale_serve", lambda p, **k: (f"https://{HOST}/", True)
        ), mock.patch("taxrefund.serve.ensure_tailscale_serve",
                      lambda p, **k: (f"https://{HOST}/", True)):
            server.check_dashboard_reachable()
        self.assertEqual(len(server.notifier.sent), 1)
        self.assertIn("복구", server.notifier.sent[0][0])

    def test_멀쩡하면_알리지_않는다(self):
        server = self._server()
        with mock.patch("taxrefund.serve.ensure_tailscale_serve",
                        lambda p, **k: (f"https://{HOST}/", False)):
            server.check_dashboard_reachable()
        self.assertEqual(server.notifier.sent, [])

    def test_0000에_바인드된_구성은_건드리지_않는다(self):
        """직접 바인드된 경우 Serve는 통로가 아니라서 확인할 이유가 없습니다."""
        server = self._server(host="0.0.0.0")
        called = []
        with mock.patch("taxrefund.serve.ensure_tailscale_serve",
                        lambda p, **k: called.append(p) or (None, False)):
            server.check_dashboard_reachable()
        self.assertEqual(called, [])

    def test_대시보드가_안_떠있으면_건드리지_않는다(self):
        server = self._server()
        server.dashboard = None
        called = []
        with mock.patch("taxrefund.serve.ensure_tailscale_serve",
                        lambda p, **k: called.append(p) or (None, False)):
            server.check_dashboard_reachable()
        self.assertEqual(called, [])

    def test_복구_실패_경고는_반복하지_않는다(self):
        server = self._server()
        with mock.patch("taxrefund.serve.ensure_tailscale_serve", lambda p, **k: (None, False)):
            server.check_dashboard_reachable()
            self.assertTrue(server._serve_gone_logged)
            server.check_dashboard_reachable()
        self.assertEqual(server.notifier.sent, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""디스코드 전송과 서버 스케줄러 검증 (실제 네트워크 호출은 모킹).

실행: python -m tests.test_discord_serve
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from taxrefund.config import Company, load_config, reload_secrets  # noqa: E402
from taxrefund.discord_client import (  # noqa: E402
    EMBEDS_PER_MESSAGE,
    MAX_FIELD_VALUE,
    MAX_TITLE,
    DiscordError,
    DiscordSender,
    alert_to_embed,
)
from taxrefund.lawgo import LawApiError  # noqa: E402
from taxrefund.matcher import build_alerts  # noqa: E402
from taxrefund.models import Analysis  # noqa: E402
from taxrefund.notifier import FAILED, SENT, SKIPPED, Notifier  # noqa: E402
from taxrefund.pipeline import backfill, load_sample_precedents  # noqa: E402
from taxrefund.store import Store  # noqa: E402
from taxrefund.serve import Server, next_run_at  # noqa: E402
from taxrefund.cli import _make_output_safe  # noqa: E402

# 실제 실행은 항상 cli.main()을 거치고, 거기서 콘솔 인코딩을 안전하게
# 만듭니다. 여기서는 serve()를 직접 부르므로 같은 조건을 맞춰 줍니다.
# (cp949 콘솔에서 이모지 출력이 UnicodeEncodeError를 냅니다)
_make_output_safe()
from tests.test_pipeline import FAKE_ANALYSIS  # noqa: E402


def make_alerts(n: int = 1):
    prec = load_sample_precedents(ROOT)[0]
    analysis = Analysis.from_json(FAKE_ANALYSIS)
    watchlist = [Company(name="전자부품사", industry="전자부품 제조업", ksic=["26"])]
    alerts = build_alerts([(prec, analysis)], watchlist, load_config().notify)
    return alerts * n


def fake_post(status: int = 204, body: dict | None = None):
    calls: list[dict] = []

    def _post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return SimpleNamespace(
            status_code=status,
            text="",
            json=lambda: body or {},
            headers={},
            raise_for_status=lambda: None,
        )

    return _post, calls


class TestSenderConfig(unittest.TestCase):
    def test_설정_없으면_에러(self):
        with self.assertRaises(DiscordError):
            DiscordSender()

    def test_봇모드_우선(self):
        s = DiscordSender(bot_token="tok", channel_id="123", webhook_url="https://x")
        self.assertEqual(s.mode, "bot")

    def test_웹훅모드(self):
        self.assertEqual(DiscordSender(webhook_url="https://x").mode, "webhook")


class TestEmbed(unittest.TestCase):
    def setUp(self):
        self.alert = make_alerts()[0]

    def test_임베드_제한_준수(self):
        e = alert_to_embed(self.alert)
        self.assertLessEqual(len(e["title"]), MAX_TITLE)
        self.assertLessEqual(len(e["description"]), 1200)
        self.assertLessEqual(len(e["fields"]), 25)
        for f in e["fields"]:
            self.assertLessEqual(len(f["value"]), MAX_FIELD_VALUE, f["name"])
            self.assertTrue(f["value"].strip(), f"{f['name']} 값이 비어 있음")
        total = len(e["title"]) + len(e["description"]) + sum(
            len(f["name"]) + len(f["value"]) for f in e["fields"]
        ) + len(e["footer"]["text"])
        self.assertLess(total, 6000, "임베드 전체 길이 초과")

    def test_내용_포함(self):
        e = alert_to_embed(self.alert)
        joined = e["title"] + e["description"] + str(e["fields"])
        self.assertIn("전자부품사", joined)
        self.assertIn("연구인력개발비", joined + e["footer"]["text"])

    def test_점수별_색상(self):
        self.assertEqual(alert_to_embed(self.alert)["color"], 0xE74C3C)  # 82점 -> 빨강

    def test_잘못된_URL은_embed_url에_넣지_않는다(self):
        """공백·한글이 섞인 링크를 embed.url에 넣으면 디스코드가 400으로 거부합니다."""
        for bad in (
            "https://www.law.go.kr/ (샘플 - 실제 링크 아님)",
            "판례상세링크없음",
            "https://한글도메인.kr/판례",
            "",
        ):
            alert = make_alerts()[0]
            alert.precedent.url = bad
            e = alert_to_embed(alert)
            self.assertNotIn("url", e, f"거부되어야 할 URL: {bad!r}")
            if bad:
                names = [f["name"] for f in e["fields"]]
                self.assertIn("원문", names, "링크가 아니어도 원문 위치는 남겨야 합니다")

    def test_정상_URL은_유지(self):
        for good in ("https://www.law.go.kr/", "http://law.go.kr/DRF/lawService.do?ID=1"):
            alert = make_alerts()[0]
            alert.precedent.url = good
            self.assertEqual(alert_to_embed(alert)["url"], good)

    def test_긴_텍스트_잘림(self):
        alert = make_alerts()[0]
        alert.analysis.refund_rationale = "가" * 5000
        alert.analysis.issue_summary = "나" * 5000
        e = alert_to_embed(alert)
        self.assertLessEqual(len(e["description"]), 1200)
        for f in e["fields"]:
            self.assertLessEqual(len(f["value"]), MAX_FIELD_VALUE)


class TestSending(unittest.TestCase):
    def test_봇_헤더와_엔드포인트(self):
        post, calls = fake_post()
        sender = DiscordSender(bot_token="tok", channel_id="999")
        with mock.patch("taxrefund.discord_client.requests.post", post):
            sender.send_alerts(make_alerts())
        self.assertEqual(len(calls), 1)
        self.assertIn("/channels/999/messages", calls[0]["url"])
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bot tok")

    def test_웹훅은_인증헤더_없음(self):
        post, calls = fake_post()
        sender = DiscordSender(webhook_url="https://discord.com/api/webhooks/1/abc")
        with mock.patch("taxrefund.discord_client.requests.post", post):
            sender.send_alerts(make_alerts())
        self.assertNotIn("Authorization", calls[0]["headers"])

    def test_임베드_분할전송(self):
        post, calls = fake_post()
        n = EMBEDS_PER_MESSAGE * 2 + 1
        sender = DiscordSender(bot_token="t", channel_id="1")
        with mock.patch("taxrefund.discord_client.requests.post", post), \
             mock.patch("taxrefund.discord_client.time.sleep", lambda s: None):
            sender.send_alerts(make_alerts(n))
        self.assertEqual(len(calls), 3)
        for call in calls:
            self.assertLessEqual(len(call["json"]["embeds"]), EMBEDS_PER_MESSAGE)

    def test_401은_친절한_에러(self):
        post, _ = fake_post(status=401)
        sender = DiscordSender(bot_token="bad", channel_id="1")
        with mock.patch("taxrefund.discord_client.requests.post", post):
            with self.assertRaises(DiscordError) as ctx:
                sender.send_alerts(make_alerts())
        self.assertIn("권한", str(ctx.exception))

    def test_레이트리밋_재시도(self):
        attempts = {"n": 0}

        def _post(url, json=None, headers=None, timeout=None):
            attempts["n"] += 1
            code = 429 if attempts["n"] == 1 else 204
            return SimpleNamespace(
                status_code=code,
                text="",
                json=lambda: {"retry_after": 0.01},
                headers={},
                raise_for_status=lambda: None,
            )

        sender = DiscordSender(bot_token="t", channel_id="1")
        with mock.patch("taxrefund.discord_client.requests.post", _post), \
             mock.patch("taxrefund.discord_client.time.sleep", lambda s: None):
            sender.send_alerts(make_alerts())
        self.assertEqual(attempts["n"], 2)

    def test_notifier_채널_연결(self):
        cfg = load_config()
        cfg.secrets.discord_bot_token = "tok"
        cfg.secrets.discord_channel_id = "1"
        post, calls = fake_post()
        with mock.patch("taxrefund.discord_client.requests.post", post):
            results = Notifier(cfg).send(make_alerts(), ["discord"])
        self.assertEqual(results["discord"], SENT)
        self.assertEqual(len(calls), 1)

    def test_미설정_채널은_실패가_아니라_건너뜀(self):
        cfg = load_config()
        cfg.secrets.discord_bot_token = None
        cfg.secrets.discord_channel_id = None
        cfg.secrets.discord_webhook_url = None
        results = Notifier(cfg).send(make_alerts(), ["discord", "console"])
        self.assertEqual(results["discord"], SKIPPED)
        self.assertEqual(results["console"], SENT)

    def test_전송_예외는_실패로_기록(self):
        cfg = load_config()
        cfg.secrets.discord_bot_token = "tok"
        cfg.secrets.discord_channel_id = "1"

        def boom(*a, **kw):
            raise requests.RequestException("네트워크 끊김")

        with mock.patch("taxrefund.discord_client.requests.post", boom):
            results = Notifier(cfg).send(make_alerts(), ["discord"])
        self.assertEqual(results["discord"], FAILED)


class TestSchedule(unittest.TestCase):
    def test_daily_at_오늘_아직_안_지남(self):
        cfg = load_config()
        cfg.serve.daily_at = "23:00"
        now = datetime(2026, 8, 16, 8, 0)
        self.assertEqual(next_run_at(cfg, now), datetime(2026, 8, 16, 23, 0))

    def test_daily_at_이미_지남이면_내일(self):
        cfg = load_config()
        cfg.serve.daily_at = "08:00"
        now = datetime(2026, 8, 16, 9, 0)
        self.assertEqual(next_run_at(cfg, now), datetime(2026, 8, 17, 8, 0))

    def test_interval_모드(self):
        cfg = load_config()
        cfg.serve.daily_at = None
        cfg.serve.interval_minutes = 90
        now = datetime(2026, 8, 16, 8, 0)
        self.assertEqual(next_run_at(cfg, now), datetime(2026, 8, 16, 9, 30))

    def test_잘못된_daily_at은_interval로_대체(self):
        cfg = load_config()
        cfg.serve.daily_at = "말도안되는값"
        cfg.serve.interval_minutes = 60
        now = datetime(2026, 8, 16, 8, 0)
        self.assertEqual(next_run_at(cfg, now), datetime(2026, 8, 16, 9, 0))


class TestServer(unittest.TestCase):
    def _server(self, cfg=None):
        cfg = cfg or load_config()
        cfg.notify.startup_ping = False
        return Server(cfg, sample=True)

    def test_성공하면_실패카운터_초기화(self):
        server = self._server()
        server.consecutive_failures = 2
        with mock.patch("taxrefund.serve.run", lambda cfg, **kw: make_alerts(2)):
            self.assertEqual(server.run_once(), 2)
        self.assertEqual(server.consecutive_failures, 0)
        self.assertEqual(server.total_alerts, 2)

    def test_예외는_삼키고_계속(self):
        server = self._server()

        def boom(cfg, **kw):
            raise RuntimeError("일시적 오류")

        with mock.patch("taxrefund.serve.run", boom):
            self.assertEqual(server.run_once(), -1)
        self.assertEqual(server.consecutive_failures, 1)
        self.assertFalse(server.stop_event.is_set(), "일반 오류로 서버가 멈추면 안 됩니다")

    def test_연속실패시_디스코드_경고(self):
        server = self._server()
        notices: list[str] = []
        server.notifier.notice = lambda t, m, c=None: notices.append(t) or True

        def boom(cfg, **kw):
            raise RuntimeError("계속 실패")

        with mock.patch("taxrefund.serve.run", boom):
            for _ in range(3):
                server.run_once()
        self.assertEqual(len(notices), 1, "임계값 도달 시 한 번만 경고해야 합니다")
        self.assertIn("오류", notices[0])

    def _raiser(self, exc):
        def _fn(cfg, **kw):
            raise exc

        return _fn

    def test_자격증명_없으면_멈추지_않고_대기(self):
        from taxrefund.analyzer import CredentialsError

        server = self._server()
        notices: list[str] = []
        server.notifier.notice = lambda t, m, c=None: notices.append(t) or True

        with mock.patch("taxrefund.serve.run", self._raiser(CredentialsError("키 없음"))):
            server.run_once()
            server.run_once()  # 두 번째 사이클

        self.assertFalse(server.stop_event.is_set(), "키가 없다고 서버가 죽으면 안 됩니다")
        self.assertIn("Claude", server.waiting_for_setup)
        self.assertEqual(len(notices), 1, "대기 알림은 한 번만 보내야 합니다")

    def test_법령API_미설정도_대기상태(self):
        server = self._server()
        notices: list[str] = []
        server.notifier.notice = lambda t, m, c=None: notices.append(t) or True

        with mock.patch("taxrefund.serve.run", self._raiser(LawApiError("OC 없음"))):
            server.run_once()

        self.assertFalse(server.stop_event.is_set())
        self.assertIn("법령정보", server.waiting_for_setup)
        self.assertEqual(server.consecutive_failures, 0, "설정 대기는 실패로 세지 않습니다")

    def test_대기사유가_바뀌면_다시_알림(self):
        from taxrefund.analyzer import CredentialsError

        server = self._server()
        notices: list[str] = []
        server.notifier.notice = lambda t, m, c=None: notices.append(t) or True

        with mock.patch("taxrefund.serve.run", self._raiser(LawApiError("OC 없음"))):
            server.run_once()
        with mock.patch("taxrefund.serve.run", self._raiser(CredentialsError("키 없음"))):
            server.run_once()

        self.assertEqual(len(notices), 2, "사유가 달라지면 새로 알려야 합니다")

    def test_설정이_채워지면_자동_재개(self):
        from taxrefund.analyzer import CredentialsError

        server = self._server()
        notices: list[str] = []
        server.notifier.notice = lambda t, m, c=None: notices.append(t) or True

        with mock.patch("taxrefund.serve.run", self._raiser(CredentialsError("키 없음"))):
            server.run_once()
        self.assertIsNotNone(server.waiting_for_setup)

        with mock.patch("taxrefund.serve.run", lambda cfg, **kw: make_alerts(1)):
            self.assertEqual(server.run_once(), 1)

        self.assertIsNone(server.waiting_for_setup)
        self.assertEqual(len(notices), 2)
        self.assertIn("재개", notices[1])

    def test_중지신호_걸려있으면_루프_진입안함(self):
        server = self._server()
        server.cfg.serve.run_on_start = False
        server.stop_event.set()
        with mock.patch("taxrefund.serve.run", lambda cfg, **kw: make_alerts()):
            self.assertEqual(server.serve(), 0)
        self.assertEqual(server.cycles, 0)


class TestBackfill(unittest.TestCase):
    """디스코드 봇을 나중에 붙이는 시나리오 전체."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config()
        self.cfg.root = Path(self._tmp.name)
        self.cfg.watchlist = [
            Company(name="전자부품사", industry="전자부품 제조업", ksic=["26"])
        ]
        self.cfg.notify.report_dir = str(Path(self._tmp.name) / "reports")
        self.cfg.secrets.discord_bot_token = None
        self.cfg.secrets.discord_channel_id = None
        self.cfg.secrets.discord_webhook_url = None

        prec = load_sample_precedents(ROOT)[0]
        with Store(self.cfg.db_path) as store:
            store.save_precedent(prec)
            store.save_analysis(prec.key, Analysis.from_json(FAKE_ANALYSIS))
        self.key = prec.key

    def tearDown(self):
        self._tmp.cleanup()

    def test_저장된_분석에서_알림을_복원한다(self):
        pending = backfill(self.cfg, channels=["console"], dry_run=True)
        self.assertEqual(len(pending["console"]), 1)
        alert = pending["console"][0]
        self.assertEqual(alert.precedent.title, "[샘플] 법인세부과처분취소")
        self.assertEqual(alert.matches[0].company, "전자부품사")
        self.assertTrue(alert.deadlines, "기한 안내가 다시 계산되어야 합니다")

    def test_dry_run은_발송기록을_남기지_않는다(self):
        backfill(self.cfg, channels=["console"], dry_run=True)
        with Store(self.cfg.db_path) as store:
            self.assertFalse(store.was_notified(self.key, "console"))

    def test_발송하면_중복되지_않는다(self):
        self.assertEqual(len(backfill(self.cfg, channels=["console"])["console"]), 1)
        self.assertEqual(backfill(self.cfg, channels=["console"])["console"], [])

    def test_미설정_기간에_쌓였다가_설정_후_받는다(self):
        # 1) 봇이 없는 상태에서 알림 시도 -> 건너뜀, 기록 없음
        results = Notifier(self.cfg).send(
            backfill(self.cfg, channels=["discord"], dry_run=True)["discord"], ["discord"]
        )
        self.assertEqual(results["discord"], SKIPPED)
        with Store(self.cfg.db_path) as store:
            self.assertFalse(store.was_notified(self.key, "discord"))

        # 2) 나중에 봇 토큰을 채움
        self.cfg.secrets.discord_bot_token = "tok"
        self.cfg.secrets.discord_channel_id = "123"
        self.assertEqual(self.cfg.discord_mode, "bot")

        post, calls = fake_post()
        with mock.patch("taxrefund.discord_client.requests.post", post):
            sent = backfill(self.cfg, channels=["discord"])
        self.assertEqual(len(sent["discord"]), 1, "밀린 알림이 발송되어야 합니다")
        self.assertEqual(len(calls), 1)
        with Store(self.cfg.db_path) as store:
            self.assertTrue(store.was_notified(self.key, "discord"))

        # 3) 다시 실행해도 중복 발송 없음
        post2, calls2 = fake_post()
        with mock.patch("taxrefund.discord_client.requests.post", post2):
            self.assertEqual(backfill(self.cfg, channels=["discord"])["discord"], [])
        self.assertEqual(len(calls2), 0)

    def test_limit_적용(self):
        pending = backfill(self.cfg, channels=["console"], limit=0, dry_run=True)
        self.assertEqual(pending["console"], [])


class TestDiscordMode(unittest.TestCase):
    def test_봇이_웹훅보다_우선(self):
        cfg = load_config()
        cfg.secrets.discord_bot_token = "t"
        cfg.secrets.discord_channel_id = "1"
        cfg.secrets.discord_webhook_url = "https://x"
        self.assertEqual(cfg.discord_mode, "bot")

    def test_미설정(self):
        cfg = load_config()
        cfg.secrets.discord_bot_token = None
        cfg.secrets.discord_channel_id = None
        cfg.secrets.discord_webhook_url = None
        self.assertIsNone(cfg.discord_mode)
        self.assertFalse(cfg.discord_ready)
        self.assertIsNone(cfg.discord_partial)

    def test_절반만_채운_경우_감지(self):
        cfg = load_config()
        cfg.secrets.discord_bot_token = "t"
        cfg.secrets.discord_channel_id = None
        cfg.secrets.discord_webhook_url = None
        self.assertFalse(cfg.discord_ready)
        self.assertEqual(cfg.discord_partial, "DISCORD_CHANNEL_ID")


class TestAutoDetect(unittest.TestCase):
    """서버가 돌아가는 중에 .env에 봇 토큰이 추가된 경우."""

    def _server(self):
        cfg = load_config()
        cfg.notify.startup_ping = False
        cfg.secrets.discord_bot_token = None
        cfg.secrets.discord_channel_id = None
        cfg.secrets.discord_webhook_url = None
        return Server(cfg, sample=True)

    def test_봇이_새로_켜지면_밀린알림_발송(self):
        server = self._server()
        calls: list[dict] = []
        notices: list[str] = []

        def turn_on(cfg):
            cfg.secrets.discord_bot_token = "tok"
            cfg.secrets.discord_channel_id = "123"
            return True

        with mock.patch("taxrefund.serve.reload_secrets", turn_on), \
             mock.patch("taxrefund.serve.backfill", lambda cfg, **kw: calls.append(kw)), \
             mock.patch.object(Notifier, "notice", lambda self, t, m, c=None: notices.append(t)):
            server.refresh_secrets()

        self.assertEqual(calls, [{"channels": ["discord"]}])
        self.assertIn("디스코드", notices[0])

    def test_변화없으면_아무것도_안함(self):
        server = self._server()
        calls: list[dict] = []
        with mock.patch("taxrefund.serve.reload_secrets", lambda cfg: False), \
             mock.patch("taxrefund.serve.backfill", lambda cfg, **kw: calls.append(kw)):
            server.refresh_secrets()
        self.assertEqual(calls, [])

    def test_이미_켜져있으면_재발송_안함(self):
        server = self._server()
        server.cfg.secrets.discord_bot_token = "tok"
        server.cfg.secrets.discord_channel_id = "123"
        calls: list[dict] = []
        with mock.patch("taxrefund.serve.reload_secrets", lambda cfg: True), \
             mock.patch("taxrefund.serve.backfill", lambda cfg, **kw: calls.append(kw)):
            server.refresh_secrets()
        self.assertEqual(calls, [], "이미 연결된 상태면 backfill을 다시 돌리지 않습니다")

    def test_backfill_실패해도_서버는_계속(self):
        server = self._server()

        def turn_on(cfg):
            cfg.secrets.discord_bot_token = "tok"
            cfg.secrets.discord_channel_id = "123"
            return True

        def boom(cfg, **kw):
            raise RuntimeError("디스코드 다운")

        with mock.patch("taxrefund.serve.reload_secrets", turn_on), \
             mock.patch("taxrefund.serve.backfill", boom), \
             mock.patch.object(Notifier, "notice", lambda self, t, m, c=None: True):
            server.refresh_secrets()  # 예외가 밖으로 나오면 안 됨
        self.assertFalse(server.stop_event.is_set())


class TestReloadSecrets(unittest.TestCase):
    def test_env파일_수정이_반영된다(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / ".env"
            env.write_text("DISCORD_BOT_TOKEN=\nDISCORD_CHANNEL_ID=\n", encoding="utf-8")

            cfg = load_config()
            cfg.root = root
            cfg.secrets.discord_bot_token = None
            cfg.secrets.discord_channel_id = None
            cfg.secrets.discord_webhook_url = None
            reload_secrets(cfg)
            self.assertFalse(cfg.discord_ready)

            env.write_text(
                "DISCORD_BOT_TOKEN=abc123\nDISCORD_CHANNEL_ID=999\n", encoding="utf-8"
            )
            self.assertTrue(reload_secrets(cfg), "추가를 감지해야 합니다")
            self.assertEqual(cfg.secrets.discord_bot_token, "abc123")
            self.assertEqual(cfg.discord_mode, "bot")

            # 값을 지운 것도 감지되어야 합니다 (os.environ에 남은 값이 살아나면 안 됨)
            env.write_text("DISCORD_BOT_TOKEN=\nDISCORD_CHANNEL_ID=\n", encoding="utf-8")
            self.assertTrue(reload_secrets(cfg), "삭제를 감지해야 합니다")
            self.assertIsNone(cfg.secrets.discord_bot_token)
            self.assertFalse(cfg.discord_ready)


if __name__ == "__main__":
    unittest.main(verbosity=2)

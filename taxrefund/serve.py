"""상시 실행 서버 모드.

일정에 맞춰 파이프라인을 반복 실행하고, 시작/종료/오류를 디스코드로 알립니다.
Ctrl+C(SIGINT) 또는 SIGTERM으로 안전하게 멈춥니다.
"""

from __future__ import annotations

import logging
import logging.handlers
import signal
import threading
import time
from datetime import datetime, timedelta

from typing import Any

from .analyzer import CredentialsError
from .config import Config, reload_secrets
from .discord_client import COLOR_HIGH, COLOR_INFO
from .lawgo import LawApiError
from .netcheck import get_public_ip, registration_hint
from .notifier import Notifier
from .pipeline import backfill, run
from .store import META_LAW_OK_IP, Store
from .web import access_urls, ensure_tailscale_serve, start_in_thread

log = logging.getLogger(__name__)

# 연속 실패가 이만큼 쌓이면 디스코드로 경고합니다.
FAILURE_ALERT_THRESHOLD = 3

# 설정이 채워지기를 기다리는 동안은 정규 일정 대신 이 주기로 .env를 다시 확인합니다.
SETUP_RETRY_SECONDS = 60


def setup_file_logging(cfg: Config, verbose: bool = False) -> None:
    """콘솔 + 회전 파일 로깅을 붙입니다."""
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        cfg.log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def next_run_at(cfg: Config, now: datetime | None = None) -> datetime:
    """다음 실행 시각. daily_at이 있으면 그 시각, 없으면 interval_minutes 후."""
    now = now or datetime.now()
    daily = (cfg.serve.daily_at or "").strip()
    if daily:
        try:
            hour, minute = (int(x) for x in daily.split(":", 1))
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target
        except (ValueError, TypeError):
            log.warning("serve.daily_at 값이 잘못되었습니다(%r). interval_minutes를 씁니다.", daily)
    return now + timedelta(minutes=max(1, cfg.serve.interval_minutes))


class Server:
    def __init__(self, cfg: Config, **run_kwargs):
        self.cfg = cfg
        self.run_kwargs = run_kwargs
        self.notifier = Notifier(cfg)
        self.stop_event = threading.Event()
        self.cycles = 0
        self.total_alerts = 0
        self.consecutive_failures = 0
        self.started_at = datetime.now()
        # 설정이 채워지기를 기다리는 중이면 그 사유. 아니면 None.
        # API 키나 OPEN API 등록은 .env 수정 / 포털 신청으로 언제든 해결되므로
        # 서버를 죽이지 않고 짧은 주기로 재확인합니다.
        self.waiting_for_setup: str | None = None
        self._setup_dedupe: str | None = None
        self._last_ip_warned: str | None = None
        self._serve_gone_logged = False
        self.dashboard: Any = None

    # -- 수명주기 ------------------------------------------------------
    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            log.info("종료 신호(%s)를 받았습니다. 현재 작업을 마치고 멈춥니다.", signum)
            self.stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError, AttributeError):
                pass  # 메인 스레드가 아니거나 지원하지 않는 플랫폼

    def _sleep_until(self, target: datetime) -> None:
        """중간에 멈출 수 있도록 잘게 나눠 잡니다."""
        while not self.stop_event.is_set():
            remaining = (target - datetime.now()).total_seconds()
            if remaining <= 0:
                return
            self.stop_event.wait(min(remaining, 30))

    # -- 설정 변화 감지 --------------------------------------------------
    def refresh_secrets(self) -> None:
        """.env를 다시 읽어, 서버를 돌리는 중에 추가된 설정을 반영합니다.

        디스코드 봇을 나중에 붙이는 경우가 여기에 해당합니다. 새로 켜지면
        그동안 쌓여 있던 알림을 자동으로 몰아서 보냅니다.
        """
        was_ready = self.cfg.discord_ready
        if not reload_secrets(self.cfg):
            return

        if self.cfg.discord_ready and not was_ready:
            log.info("디스코드 설정이 감지되었습니다(%s 모드). 밀린 알림을 보냅니다.",
                     self.cfg.discord_mode)
            self.notifier = Notifier(self.cfg)
            self.notifier.notice(
                "🟢 디스코드 연결됨",
                "이제부터 이 채널로 알림을 보냅니다. 그동안 쌓인 알림을 이어서 발송합니다.",
                COLOR_INFO,
            )
            try:
                backfill(self.cfg, channels=["discord"])
            except Exception as exc:
                log.warning("밀린 알림 발송 실패: %s", exc)
        elif not self.cfg.discord_ready and was_ready:
            log.warning("디스코드 설정이 사라졌습니다. 알림은 나머지 채널로만 나갑니다.")
            self.notifier = Notifier(self.cfg)
        else:
            self.notifier = Notifier(self.cfg)

    # -- 한 사이클 ------------------------------------------------------
    def run_once(self) -> int:
        """한 번 실행하고 알림 건수를 돌려줍니다. 실패하면 -1."""
        self.refresh_secrets()
        self.cycles += 1
        started = time.monotonic()
        log.info("===== 사이클 #%d 시작 =====", self.cycles)
        try:
            alerts = run(self.cfg, **self.run_kwargs)
        except CredentialsError as exc:
            # 설정만 채우면 해결되는 상태입니다. 서버를 죽이지 않습니다.
            return self._wait_for_setup(
                "Claude API 키 대기 중",
                str(exc),
                "`.env`의 `ANTHROPIC_API_KEY`를 채우면 자동으로 재개합니다.",
            )
        except LawApiError as exc:
            # IP 검증 실패가 가장 흔한 원인이라, 현재 IP를 함께 알려 줍니다.
            current_ip = get_public_ip()
            registered_ip = self._registered_ip()
            return self._wait_for_setup(
                "법령정보 API 설정 대기 중",
                str(exc),
                registration_hint(current_ip, registered_ip),
                # IP가 바뀌면 사유가 바뀐 것으로 보고 다시 알립니다.
                dedupe=f"law:{current_ip}",
            )
        except Exception as exc:
            self.consecutive_failures += 1
            log.exception("사이클 실패: %s", exc)
            self._maybe_alert_failure(f"{type(exc).__name__}: {exc}")
            return -1

        if self.waiting_for_setup:
            log.info("설정이 확인되었습니다(%s 해소). 정상 일정으로 돌아갑니다.",
                     self.waiting_for_setup)
            self.notifier.notice(
                "▶️ 재개",
                f"'{self.waiting_for_setup}' 상태가 해소되어 분석을 시작합니다.",
                COLOR_INFO,
            )
            self.waiting_for_setup = None
            self._setup_dedupe = None

        self.check_ip_change()
        self.check_dashboard_reachable()
        self.consecutive_failures = 0
        self.total_alerts += len(alerts)
        log.info(
            "===== 사이클 #%d 완료: 알림 %d건 (%.1f초) =====",
            self.cycles,
            len(alerts),
            time.monotonic() - started,
        )
        return len(alerts)

    def _registered_ip(self) -> str | None:
        """법령 API가 마지막으로 성공했을 때의 IP."""
        try:
            if self.cfg.db_path.exists():
                with Store(self.cfg.db_path) as store:
                    return store.get_meta(META_LAW_OK_IP)
        except Exception as exc:
            log.debug("등록 IP 조회 실패: %s", exc)
        return None

    def check_ip_change(self) -> None:
        """수집이 되고 있어도 IP가 바뀌면 곧 끊기므로 미리 알립니다."""
        registered = self._registered_ip()
        if not registered:
            return
        current = get_public_ip()
        if not current or current == registered:
            return
        if self._last_ip_warned == current:
            return
        log.warning("공인 IP 변동 감지: %s → %s", registered, current)
        self.notifier.notice(
            "🌐 공인 IP가 바뀌었습니다",
            registration_hint(current, registered),
            COLOR_HIGH,
        )
        self._last_ip_warned = current

    def check_dashboard_reachable(self) -> None:
        """Tailscale Serve 매핑이 사라졌으면 다시 붙입니다.

        Serve 설정은 이 PC 전체에 하나뿐이라, 같은 PC의 다른 프로젝트가
        설정을 덮어쓰면 대시보드 매핑만 조용히 없어집니다. 서버 자체는
        멀쩡해서 로그로는 티가 안 나고, 폰에서 접속할 때야 알게 됩니다.
        """
        if not (self.cfg.web.enabled and self.dashboard):
            return
        # 127.0.0.1에만 묶은 구성에서만 Serve가 유일한 통로입니다.
        if self.cfg.web.host not in ("local", "127.0.0.1", "localhost"):
            return

        url, restored = ensure_tailscale_serve(self.cfg.web.port)
        if restored:
            self._serve_gone_logged = False
            self.notifier.notice(
                "🔧 대시보드 접속을 복구했습니다",
                (
                    "Tailscale Serve 매핑이 사라져 폰에서 접속되지 않던 것을 "
                    "다시 붙였습니다. 같은 PC의 다른 프로젝트가 Serve 설정을 "
                    f"덮어썼을 때 생깁니다.\n\n**주소**: {url}"
                ),
                COLOR_INFO,
            )
        elif url is None and not self._serve_gone_logged:
            self._serve_gone_logged = True
            log.warning("대시보드 Tailscale 매핑이 없고 자동 복구도 되지 않았습니다.")

    def _wait_for_setup(self, kind: str, detail: str, guide: str,
                        dedupe: str | None = None) -> int:
        """설정 미비 상태로 전환. 사유가 바뀔 때만 알립니다."""
        kind_key = dedupe or kind
        if self._setup_dedupe != kind_key:
            self._setup_dedupe = kind_key
            self.waiting_for_setup = kind
            log.error("%s — %d초마다 다시 확인하며 대기합니다.", detail, SETUP_RETRY_SECONDS)
            self.notifier.notice(
                f"⏸️ {kind}",
                f"{detail}\n\n{guide} 서버를 다시 시작할 필요는 없습니다.",
                COLOR_HIGH,
            )
            self.waiting_for_setup = kind
        else:
            # 같은 사유면 알림은 반복하지 않되, 로그에는 살아있음을 남깁니다.
            log.info("%s — 아직 해소되지 않았습니다. %d초 후 다시 확인합니다.",
                     kind, SETUP_RETRY_SECONDS)
        return -1

    def _maybe_alert_failure(self, detail: str) -> None:
        if self.consecutive_failures < FAILURE_ALERT_THRESHOLD:
            return
        self.notifier.notice(
            "⚠️ 봇 오류가 반복되고 있습니다",
            f"연속 {self.consecutive_failures}회 실패했습니다.\n```{detail[:800]}```",
            COLOR_HIGH,
        )

    # -- 메인 루프 ------------------------------------------------------
    def serve(self) -> int:
        self._install_signal_handlers()

        schedule = (
            f"매일 {self.cfg.serve.daily_at}"
            if self.cfg.serve.daily_at
            else f"{self.cfg.serve.interval_minutes}분마다"
        )
        channels = ", ".join(self.cfg.notify.channels)
        log.info("서버 시작 | 일정: %s | 채널: %s | 로그: %s", schedule, channels, self.cfg.log_path)

        dashboard_urls: list[str] = []
        if self.cfg.web.enabled:
            self.dashboard = start_in_thread(self.cfg)
            if self.dashboard:
                self.check_dashboard_reachable()
                dashboard_urls = access_urls(self.cfg)
                print("\n  📱 폰에서 볼 대시보드:")
                for url in dashboard_urls:
                    print(f"     {url}")
                print()

        if "discord" in self.cfg.notify.channels and not self.cfg.discord_ready:
            log.warning(
                "디스코드가 아직 설정되지 않았습니다. 알림은 나머지 채널로만 나가고 "
                "발송 기록은 남기지 않습니다. 서버를 켜 둔 채로 .env에 DISCORD_BOT_TOKEN과 "
                "DISCORD_CHANNEL_ID를 채우면 다음 사이클에 자동으로 감지해서 "
                "그동안 쌓인 알림까지 보냅니다."
            )

        if self.cfg.notify.startup_ping:
            self.notifier.notice(
                "🟢 법인세 판례 봇 시작",
                (
                    f"**일정**: {schedule}\n"
                    f"**감시 대상**: {len(self.cfg.watchlist)}개 회사\n"
                    f"**최소 점수**: {self.cfg.notify.min_confidence}점\n"
                    f"**알림 채널**: {channels}"
                    + (
                        "\n**대시보드**: " + dashboard_urls[-1].split("   ")[0]
                        if dashboard_urls
                        else ""
                    )
                ),
                COLOR_INFO,
            )

        if self.cfg.serve.run_on_start:
            self.run_once()

        while not self.stop_event.is_set():
            if self.waiting_for_setup:
                # 설정을 기다리는 중에는 정규 일정(예: 매일 08:00)까지 놀지 않고
                # 짧게 재확인해서, 값을 채우는 즉시 작동하게 합니다.
                target = datetime.now() + timedelta(seconds=SETUP_RETRY_SECONDS)
            else:
                target = next_run_at(self.cfg)
                log.info("다음 실행: %s", target.strftime("%Y-%m-%d %H:%M:%S"))
            self._sleep_until(target)
            if self.stop_event.is_set():
                break
            self.run_once()

        if self.dashboard:
            self.dashboard.shutdown()
            self.dashboard.server_close()

        uptime = datetime.now() - self.started_at
        log.info(
            "서버 종료 | 가동 %s | 사이클 %d회 | 누적 알림 %d건",
            str(uptime).split(".")[0],
            self.cycles,
            self.total_alerts,
        )
        if self.cfg.notify.startup_ping:
            self.notifier.notice(
                "🔴 법인세 판례 봇 종료",
                (
                    f"**가동 시간**: {str(uptime).split('.')[0]}\n"
                    f"**실행 횟수**: {self.cycles}회\n"
                    f"**누적 알림**: {self.total_alerts}건"
                ),
                COLOR_INFO,
            )
        return 0

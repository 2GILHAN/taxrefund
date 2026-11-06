"""알림 발송: 콘솔 / 마크다운 파일 / Slack / 이메일."""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import requests

from .config import Config
from .discord_client import DiscordSender
from .models import Alert

log = logging.getLogger(__name__)

# 채널별 처리 결과. '건너뜀'과 '실패'를 구분해야 미설정 채널의 알림이
# 발송된 것으로 잘못 기록되지 않고, 나중에 backfill로 다시 보낼 수 있습니다.
SENT = "sent"
SKIPPED = "skipped"
FAILED = "failed"

STATUS_LABEL = {SENT: "발송", SKIPPED: "건너뜀(미설정)", FAILED: "실패"}


# ---------------------------------------------------------------- 렌더링

def render_alert_md(alert: Alert) -> str:
    p, a = alert.precedent, alert.analysis
    kind = "판례" if p.source == "prec" else "법령해석례"
    lines = [
        f"## [{alert.score}점] {alert.title}",
        "",
        f"- **사건명**: {p.title}",
        f"- **구분**: {kind} / {p.court or '-'} / {p.case_no or '-'}",
        f"- **선고(회신)일**: {p.decided_str}",
        f"- **결론**: {a.outcome}  |  **환급 영향도**: {a.estimated_impact}  |  **청구 근거**: {a.claim_basis}",
        f"- **원문**: {p.url}",
        "",
        "### 쟁점 요약",
        a.issue_summary or "-",
        "",
        "### 경정청구 실익",
        f"{a.refund_rationale or '-'}",
        "",
        f"- 일반화 가능성: {'예' if a.generalizable else '아니오'} — {a.generalizable_reason or '-'}",
        f"- 적용 업종: {', '.join(a.applicable_industries) or '-'}"
        + (f" (KSIC {', '.join(a.ksic_codes)})" if a.ksic_codes else ""),
        f"- 쟁점 태그: {', '.join(a.issue_tags) or '-'}",
        "",
        "### 매칭된 회사",
    ]
    for m in alert.matches:
        lines.append(f"- **{m.company}** ({m.industry or '업종 미기재'}) — {m.score}점")
        for r in m.reasons:
            lines.append(f"  - {r}")

    if a.check_points:
        lines += ["", "### 확인할 것"]
        lines += [f"- [ ] {c}" for c in a.check_points]

    lines += ["", "### 청구 기한"]
    lines += [f"- {d}" for d in alert.deadlines]

    if a.caveats:
        lines += ["", "### 유의사항", a.caveats]

    lines.append("")
    lines.append("---")
    return "\n".join(lines)


def render_report_md(alerts: list[Alert]) -> str:
    header = [
        f"# 법인세 경정청구 후보 리포트",
        "",
        f"생성 시각: {datetime.now().isoformat(timespec='seconds')}",
        f"알림 건수: {len(alerts)}건",
        "",
        "> 자동 분석 결과입니다. 실제 경정청구 전에 반드시 세무대리인의 검토를 받으세요.",
        "",
        "---",
        "",
    ]
    return "\n".join(header) + "\n".join(render_alert_md(a) for a in alerts)


def render_alert_text(alert: Alert) -> str:
    p, a = alert.precedent, alert.analysis
    companies = ", ".join(m.company for m in alert.matches)
    return (
        f"[{alert.score}점] {alert.title}\n"
        f"{p.title} · {p.court or '-'} {p.case_no or '-'} ({p.decided_str}) / 결론: {a.outcome}\n"
        f"대상 회사: {companies}\n"
        f"{a.issue_summary}\n"
        f"실익: {a.refund_rationale}\n"
        f"원문: {p.url}"
    )


# ---------------------------------------------------------------- 채널

class Notifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def readiness(self, channel: str) -> tuple[bool, str]:
        """채널을 지금 쓸 수 있는지와, 못 쓴다면 그 이유."""
        s = self.cfg.secrets
        if channel == "discord" and not self.cfg.discord_ready:
            return False, "DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID 미설정"
        if channel == "slack" and not s.slack_webhook_url:
            return False, "SLACK_WEBHOOK_URL 미설정"
        if channel == "email" and not (s.smtp_host and s.smtp_user and s.smtp_to):
            return False, "SMTP_HOST / SMTP_USER / SMTP_TO 미설정"
        return True, ""

    def send(self, alerts: list[Alert], channels: list[str] | None = None) -> dict[str, str]:
        """채널별 결과를 돌려줍니다: sent | skipped | failed."""
        channels = channels or self.cfg.notify.channels
        results: dict[str, str] = {}
        if not alerts:
            log.info("알림 대상이 없습니다.")
            return results

        for channel in channels:
            handler = getattr(self, f"_send_{channel}", None)
            if handler is None:
                log.warning("알 수 없는 알림 채널: %s", channel)
                results[channel] = FAILED
                continue

            ready, reason = self.readiness(channel)
            if not ready:
                # 아직 설정 전이라도 오류가 아닙니다. 발송 기록을 남기지 않으므로
                # 나중에 `backfill`로 밀린 알림을 그대로 받을 수 있습니다.
                log.info(
                    "[%s] %s — 건너뜁니다. 설정 후 `python -m taxrefund backfill`로 "
                    "밀린 알림 %d건을 받을 수 있습니다.",
                    channel,
                    reason,
                    len(alerts),
                )
                results[channel] = SKIPPED
                continue

            try:
                handler(alerts)
                results[channel] = SENT
            except Exception as exc:  # 한 채널이 죽어도 나머지는 계속
                log.error("[%s] 발송 실패: %s", channel, exc)
                results[channel] = FAILED
        return results

    # -- console ------------------------------------------------------
    def _send_console(self, alerts: list[Alert]) -> None:
        print()
        print("=" * 72)
        print(f" 법인세 경정청구 후보 {len(alerts)}건")
        print("=" * 72)
        for alert in alerts:
            print()
            print(render_alert_text(alert))
            print("-" * 72)

    # -- file ---------------------------------------------------------
    def _send_file(self, alerts: list[Alert]) -> None:
        out_dir: Path = self.cfg.report_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"report_{datetime.now():%Y%m%d_%H%M%S}.md"
        path.write_text(render_report_md(alerts), encoding="utf-8")
        log.info("리포트 저장: %s", path)
        print(f"\n[file] 리포트 저장: {path}")

    # -- discord ------------------------------------------------------
    def discord(self) -> DiscordSender:
        s = self.cfg.secrets
        return DiscordSender(
            bot_token=s.discord_bot_token,
            channel_id=s.discord_channel_id,
            webhook_url=s.discord_webhook_url,
        )

    def _send_discord(self, alerts: list[Alert]) -> None:
        self.discord().send_alerts(alerts)

    def notice(self, title: str, message: str, color: int | None = None) -> bool:
        """디스코드로 상태 알림. 설정이 없거나 실패하면 조용히 False."""
        if not self.cfg.discord_ready:
            return False
        try:
            sender = self.discord()
            if color is None:
                sender.send_notice(title, message)
            else:
                sender.send_notice(title, message, color)
            return True
        except Exception as exc:
            log.warning("디스코드 상태 알림 실패: %s", exc)
            return False

    # -- slack --------------------------------------------------------
    def _send_slack(self, alerts: list[Alert]) -> None:
        url = self.cfg.secrets.slack_webhook_url
        if not url:
            raise RuntimeError("SLACK_WEBHOOK_URL이 설정되지 않았습니다.")

        for alert in alerts:
            p, a = alert.precedent, alert.analysis
            companies = ", ".join(f"*{m.company}*" for m in alert.matches)
            checks = "\n".join(f"• {c}" for c in a.check_points[:5])
            payload = {
                "text": f"[경정청구 후보 {alert.score}점] {p.title}",
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": f"경정청구 후보 ({alert.score}점)",
                            "emoji": True,
                        },
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"*<{p.url}|{p.title}>*\n"
                                f"{p.court or '-'} {p.case_no or '-'} · {p.decided_str} · 결론 *{a.outcome}*\n"
                                f"청구근거: {a.claim_basis} · 영향도: {a.estimated_impact}"
                            ),
                        },
                    },
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"*쟁점*\n{a.issue_summary}"},
                    },
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"*실익*\n{a.refund_rationale}"},
                    },
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"*대상 회사*\n{companies}"},
                    },
                ],
            }
            if checks:
                payload["blocks"].append(
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*확인할 것*\n{checks}"}}
                )
            if alert.deadlines:
                payload["blocks"].append(
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": alert.deadlines[0]}],
                    }
                )

            resp = requests.post(url, json=payload, timeout=15)
            resp.raise_for_status()
        log.info("Slack 알림 %d건 발송", len(alerts))

    # -- email --------------------------------------------------------
    def _send_email(self, alerts: list[Alert]) -> None:
        s = self.cfg.secrets
        missing = [
            name
            for name, value in (
                ("SMTP_HOST", s.smtp_host),
                ("SMTP_USER", s.smtp_user),
                ("SMTP_PASSWORD", s.smtp_password),
                ("SMTP_TO", s.smtp_to),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"이메일 설정 누락: {', '.join(missing)}")

        recipients = [addr.strip() for addr in str(s.smtp_to).split(",") if addr.strip()]
        msg = EmailMessage()
        msg["Subject"] = f"[법인세 봇] 경정청구 후보 {len(alerts)}건"
        msg["From"] = s.smtp_from or s.smtp_user
        msg["To"] = ", ".join(recipients)
        msg.set_content(render_report_md(alerts))

        with smtplib.SMTP(str(s.smtp_host), s.smtp_port, timeout=30) as server:
            server.starttls()
            server.login(str(s.smtp_user), str(s.smtp_password))
            server.send_message(msg)
        log.info("이메일 발송 완료: %s", ", ".join(recipients))

"""디스코드 전송 (봇 토큰 REST 방식, 웹훅 대체 지원).

게이트웨이(WebSocket) 연결 없이 REST API만 씁니다. 알림은 단방향이라
상시 접속이 필요 없고, 봇 토큰을 쓰면 메시지가 봇 계정 이름으로 나갑니다.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from .models import Alert

log = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"

# 디스코드 임베드 제한
MAX_TITLE = 256
MAX_DESC = 4096
MAX_FIELD_VALUE = 1024
MAX_FOOTER = 2048
EMBEDS_PER_MESSAGE = 4  # 10까지 되지만 임베드 총량(6000자) 여유를 둡니다

COLOR_URGENT = 0xE74C3C  # 80점 이상
COLOR_HIGH = 0xE67E22  # 70-79
COLOR_NORMAL = 0x3498DB  # 그 외
COLOR_INFO = 0x95A5A6  # 상태 메시지


class DiscordError(RuntimeError):
    pass


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _valid_url(url: str) -> bool:
    """디스코드 임베드에 넣어도 되는 URL인지.

    판례 링크는 외부 API에서 오는 값이라 공백이나 한글 주석이 섞여 있을 수 있고,
    그런 값을 embed.url에 넣으면 디스코드가 메시지 전체를 400으로 거부합니다.
    """
    if not url or any(ch.isspace() for ch in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc) and parts.netloc.isascii()


def _color(score: int) -> int:
    if score >= 80:
        return COLOR_URGENT
    if score >= 70:
        return COLOR_HIGH
    return COLOR_NORMAL


def alert_to_embed(alert: Alert) -> dict[str, Any]:
    p, a = alert.precedent, alert.analysis
    kind = "판례" if p.source == "prec" else "법령해석례"

    fields: list[dict[str, Any]] = [
        {"name": "결론", "value": _clip(a.outcome or "-", MAX_FIELD_VALUE), "inline": True},
        {"name": "환급 영향도", "value": _clip(a.estimated_impact or "-", MAX_FIELD_VALUE), "inline": True},
        {"name": "청구 근거", "value": _clip(a.claim_basis or "-", MAX_FIELD_VALUE), "inline": True},
    ]

    companies = "\n".join(
        f"• **{m.company}** ({m.industry or '업종 미기재'}) — {m.score}점\n　{'; '.join(m.reasons)}"
        for m in alert.matches[:5]
    )
    if companies:
        fields.append({"name": "🏢 대상 회사", "value": _clip(companies, MAX_FIELD_VALUE)})

    if a.refund_rationale:
        fields.append({"name": "💰 경정청구 실익", "value": _clip(a.refund_rationale, MAX_FIELD_VALUE)})

    if a.check_points:
        checks = "\n".join(f"☐ {c}" for c in a.check_points[:5])
        fields.append({"name": "✅ 확인할 것", "value": _clip(checks, MAX_FIELD_VALUE)})

    if alert.deadlines:
        fields.append({"name": "⏰ 청구 기한", "value": _clip("\n".join(alert.deadlines[:2]), MAX_FIELD_VALUE)})

    industries = ", ".join(a.applicable_industries) or "-"
    # 사건명은 '법인세부과처분취소'류라 제목으로는 못 쓰고, 식별용으로 푸터에 둡니다.
    footer = f"{p.title}\n{kind} · {p.court or '-'} · {p.case_no or '-'}\n적용업종: {industries}"
    if a.issue_tags:
        footer += f"\n쟁점: {', '.join(a.issue_tags)}"
    if p.is_sample:
        # 실제 판례가 아니라는 것이 알림만 봐도 드러나야 합니다.
        footer = "⚠ 동작 확인용 샘플 데이터 (실제 판례 아님)\n" + footer

    title = f"[{alert.score}점] {alert.title}"
    if p.is_sample and "샘플" not in title:
        title = f"[샘플] {title}"

    embed: dict[str, Any] = {
        "title": _clip(title, MAX_TITLE),
        "description": _clip(a.issue_summary or "", MAX_DESC if MAX_DESC < 1200 else 1200),
        "color": _color(alert.score),
        "fields": fields[:25],
        "footer": {"text": _clip(footer, MAX_FOOTER)},
    }
    if _valid_url(p.url):
        embed["url"] = p.url
    elif p.url:
        # 링크로는 못 쓰지만 원문 위치는 알려줍니다.
        embed["fields"].append({"name": "원문", "value": _clip(p.url, MAX_FIELD_VALUE)})
    if p.decided_on:
        embed["timestamp"] = f"{p.decided_on.isoformat()}T00:00:00.000Z"
    return embed


class DiscordSender:
    def __init__(
        self,
        bot_token: str | None = None,
        channel_id: str | None = None,
        webhook_url: str | None = None,
        timeout: int = 20,
    ):
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.webhook_url = webhook_url
        self.timeout = timeout
        if not self.webhook_url and not (self.bot_token and self.channel_id):
            raise DiscordError(
                "디스코드 설정이 없습니다. .env에 DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID "
                "(또는 DISCORD_WEBHOOK_URL)를 설정하세요."
            )

    @property
    def mode(self) -> str:
        return "bot" if (self.bot_token and self.channel_id) else "webhook"

    def _post(self, payload: dict[str, Any]) -> None:
        if self.mode == "bot":
            url = f"{API_BASE}/channels/{self.channel_id}/messages"
            headers = {
                "Authorization": f"Bot {self.bot_token}",
                "Content-Type": "application/json",
                "User-Agent": "taxrefund-bot/0.1",
            }
        else:
            url = str(self.webhook_url)
            headers = {"Content-Type": "application/json"}

        for attempt in range(3):
            resp = requests.post(url, json=payload, headers=headers, timeout=self.timeout)

            if resp.status_code == 429:  # 레이트리밋
                try:
                    wait = float(resp.json().get("retry_after", 1))
                except Exception:
                    wait = float(resp.headers.get("Retry-After", 1))
                log.warning("디스코드 레이트리밋, %.1f초 대기", wait)
                time.sleep(min(wait + 0.5, 30))
                continue

            if resp.status_code in (401, 403):
                raise DiscordError(
                    f"디스코드 인증 실패({resp.status_code}). 봇 토큰이 맞는지, 그리고 "
                    f"봇이 해당 채널에 '메시지 보내기' 권한을 가진 채로 서버에 초대되었는지 "
                    f"확인하세요. 응답: {resp.text[:200]}"
                )
            if resp.status_code == 404:
                raise DiscordError(
                    f"채널을 찾을 수 없습니다(404). DISCORD_CHANNEL_ID를 확인하세요. "
                    f"응답: {resp.text[:200]}"
                )
            if resp.status_code >= 400:
                raise DiscordError(f"디스코드 전송 실패({resp.status_code}): {resp.text[:300]}")
            return

        raise DiscordError("디스코드 레이트리밋으로 3회 재시도 후 실패했습니다.")

    # -- 공개 API ------------------------------------------------------
    def send_alerts(self, alerts: list[Alert]) -> None:
        if not alerts:
            return
        embeds = [alert_to_embed(a) for a in alerts]

        for i in range(0, len(embeds), EMBEDS_PER_MESSAGE):
            chunk = embeds[i : i + EMBEDS_PER_MESSAGE]
            content = (
                f"📢 **법인세 경정청구 후보 {len(alerts)}건**"
                if i == 0
                else f"(이어서 {i + 1}–{i + len(chunk)}번째)"
            )
            self._post({"content": content, "embeds": chunk})
            time.sleep(0.4)  # 연속 전송 시 레이트리밋 여유

        log.info("디스코드 알림 %d건 발송 (%s 모드)", len(alerts), self.mode)

    def verify(self) -> str:
        """메시지를 보내지 않고 자격증명만 확인. 봇/웹훅 이름을 돌려줍니다."""
        if self.mode == "bot":
            resp = requests.get(
                f"{API_BASE}/users/@me",
                headers={"Authorization": f"Bot {self.bot_token}"},
                timeout=self.timeout,
            )
            if resp.status_code == 401:
                raise DiscordError("봇 토큰이 유효하지 않습니다(401). DISCORD_BOT_TOKEN 확인.")
            resp.raise_for_status()
            me = resp.json()
            name = f"{me.get('username')}#{me.get('discriminator', '0')}"

            # 채널 접근 권한까지 확인
            ch = requests.get(
                f"{API_BASE}/channels/{self.channel_id}",
                headers={"Authorization": f"Bot {self.bot_token}"},
                timeout=self.timeout,
            )
            if ch.status_code in (403, 404):
                raise DiscordError(
                    f"봇이 채널 {self.channel_id}에 접근할 수 없습니다({ch.status_code}). "
                    "봇을 서버에 초대했는지, 해당 채널 권한이 있는지 확인하세요."
                )
            ch.raise_for_status()
            return f"{name} → #{ch.json().get('name', self.channel_id)}"

        resp = requests.get(str(self.webhook_url), timeout=self.timeout)
        if resp.status_code >= 400:
            raise DiscordError(f"웹훅 URL이 유효하지 않습니다({resp.status_code}).")
        hook = resp.json()
        return f"webhook {hook.get('name')} → 채널 {hook.get('channel_id')}"

    def send_notice(self, title: str, message: str, color: int = COLOR_INFO) -> None:
        """서버 시작/종료/오류 같은 상태 알림."""
        self._post(
            {
                "embeds": [
                    {
                        "title": _clip(title, MAX_TITLE),
                        "description": _clip(message, 1200),
                        "color": color,
                    }
                ]
            }
        )

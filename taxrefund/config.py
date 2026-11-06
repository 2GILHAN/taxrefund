"""config.yaml + .env 로딩."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values, load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# 모듈이 로드되는 시점(= .env를 읽기 전)의 실제 환경변수 스냅샷.
# load_dotenv()는 os.environ을 오염시키므로, 이후 .env를 다시 읽을 때
# 이 스냅샷을 기준으로 삼아야 '.env에서 값을 지운 것'도 제대로 감지됩니다.
_SHELL_ENV: dict[str, str] = dict(os.environ)


@dataclass
class CrawlConfig:
    targets: list[str] = field(default_factory=lambda: ["prec"])
    queries: list[str] = field(default_factory=lambda: ["법인세"])
    since_days: int = 30
    max_per_query: int = 50
    # 검색어당 넘길 최대 페이지 수. 기간 밖 페이지를 만나면 그전에 멈춥니다.
    max_pages: int = 50
    request_interval_sec: float = 0.6
    max_body_chars: int = 60000

    # 법령정보센터가 본문을 주지 않는 건(국세법령정보시스템 출처)을
    # 국세청에서 보충할지. 국세청 쪽은 공식 OPEN API가 아니라 화면이 쓰는
    # 내부 엔드포인트라, 끄고 쓸 수 있어야 합니다.
    nts_enabled: bool = True
    # 국세청은 사람이 브라우저로 보는 속도를 넘지 않게 둡니다.
    nts_interval_sec: float = 1.0
    # 한 회차에 보충할 최대 건수. 밀린 물량은 매일 조금씩 소화합니다.
    nts_per_run_limit: int = 60


@dataclass
class AnalyzeConfig:
    # gemini(무료 등급 있음) | claude(유료)
    provider: str = "gemini"
    gemini_model: str = "gemini-3.7-flash"
    claude_model: str = "claude-opus-5"
    # 예전 설정 파일 호환용. 값이 있으면 provider와 무관하게 이걸 씁니다.
    model: str | None = None

    max_tokens: int = 16000

    # 한 번 실행에서 분석할 최대 건수. 무료 등급 일일 한도를 넘겨 봐야
    # 429만 받으므로, 밀린 물량은 다음 실행으로 넘깁니다.
    per_run_limit: int = 18

    # --- Gemini 무료 등급 대응 ---
    # 분당 요청수(RPM) 한도에 걸리지 않도록 호출 사이에 두는 간격(초).
    request_interval_sec: float = 6.0
    max_retries: int = 3

    # --- Claude 전용 ---
    effort: str = "high"
    server_side_fallback: bool = True

    def resolved_model(self) -> str:
        if self.model:
            return self.model
        return (
            self.gemini_model
            if (self.provider or "").lower() == "gemini"
            else self.claude_model
        )


@dataclass
class NotifyConfig:
    min_confidence: int = 60
    require_generalizable: bool = True
    # 알림을 보낼 세목. 여기 없는 세목은 대시보드에는 남되 알림에서 빠집니다.
    # 비워 두면 세목으로 거르지 않습니다(모든 세목 알림).
    tax_types: list[str] = field(
        default_factory=lambda: ["법인세", "부가가치세", "상속증여세"]
    )
    channels: list[str] = field(default_factory=lambda: ["console", "file"])
    report_dir: str = "reports"
    # 서버가 뜨고 질 때 디스코드에 상태 메시지를 보냅니다.
    startup_ping: bool = True


@dataclass
class ServeConfig:
    """상시 실행(serve) 모드 설정."""

    # 매일 이 시각에 실행 (HH:MM, 24시간제). 지정하면 interval_minutes보다 우선합니다.
    daily_at: str | None = "08:00"
    # daily_at이 없을 때 사용할 반복 주기(분).
    interval_minutes: int = 360
    # 서버 시작 직후 한 번 바로 실행할지.
    run_on_start: bool = True
    log_file: str = "logs/taxrefund.log"


@dataclass
class WebConfig:
    """폰에서 볼 수 있는 웹 대시보드."""

    # serve 실행 시 대시보드도 함께 띄울지
    enabled: bool = True
    # 0.0.0.0이면 같은 와이파이의 다른 기기(폰)에서도 접속 가능
    host: str = "0.0.0.0"
    port: int = 8787


@dataclass
class Company:
    name: str
    industry: str = ""
    ksic: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


@dataclass
class Secrets:
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    law_api_oc: str | None = None
    dart_api_key: str | None = None
    slack_webhook_url: str | None = None
    discord_bot_token: str | None = None
    discord_channel_id: str | None = None
    discord_webhook_url: str | None = None
    web_token: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_to: str | None = None


@dataclass
class Config:
    crawl: CrawlConfig
    analyze: AnalyzeConfig
    notify: NotifyConfig
    serve: ServeConfig
    web: WebConfig
    watchlist: list[Company]
    secrets: Secrets
    root: Path = ROOT

    @property
    def report_dir(self) -> Path:
        p = Path(self.notify.report_dir)
        return p if p.is_absolute() else self.root / p

    @property
    def log_path(self) -> Path:
        p = Path(self.serve.log_file)
        return p if p.is_absolute() else self.root / p

    @property
    def db_path(self) -> Path:
        return self.root / "data" / "taxrefund.db"

    # 국세청 본문 보충 설정은 수집의 일부라 crawl 아래 두었습니다.
    # 파이프라인에서 자주 읽어서 짧은 이름으로 꺼내 씁니다.
    @property
    def nts_enabled(self) -> bool:
        return self.crawl.nts_enabled

    @property
    def nts_interval_sec(self) -> float:
        return self.crawl.nts_interval_sec

    @property
    def nts_per_run_limit(self) -> int:
        return self.crawl.nts_per_run_limit

    @property
    def discord_mode(self) -> str | None:
        """'bot' | 'webhook' | None. 봇 토큰이 있으면 봇 모드가 우선입니다."""
        s = self.secrets
        if s.discord_bot_token and s.discord_channel_id:
            return "bot"
        if s.discord_webhook_url:
            return "webhook"
        return None

    @property
    def discord_ready(self) -> bool:
        return self.discord_mode is not None

    @property
    def discord_partial(self) -> str | None:
        """봇 설정을 절반만 채운 경우 무엇이 빠졌는지 알려줍니다."""
        s = self.secrets
        if s.discord_bot_token and not s.discord_channel_id:
            return "DISCORD_CHANNEL_ID"
        if s.discord_channel_id and not s.discord_bot_token:
            return "DISCORD_BOT_TOKEN"
        return None


def _env_map(root: Path = ROOT) -> dict[str, str]:
    """셸 환경변수 + .env를 합칩니다. .env의 비어있지 않은 값이 우선입니다.

    매번 파일을 다시 읽으므로, 서버가 도는 중에 .env를 고쳐도 다음 사이클에
    반영됩니다. 값을 지운 경우도 마찬가지입니다.
    """
    values: dict[str, str] = dict(_SHELL_ENV)
    for key, value in (dotenv_values(root / ".env") or {}).items():
        if value and value.strip():
            values[key] = value
    return values


def _build_secrets(env: dict[str, str]) -> Secrets:
    def get(name: str) -> str | None:
        value = env.get(name)
        return value.strip() if value and value.strip() else None

    try:
        smtp_port = int(get("SMTP_PORT") or 587)
    except ValueError:
        smtp_port = 587

    return Secrets(
        anthropic_api_key=get("ANTHROPIC_API_KEY"),
        gemini_api_key=get("GEMINI_API_KEY") or get("GOOGLE_API_KEY"),
        law_api_oc=get("LAW_API_OC"),
        dart_api_key=get("DART_API_KEY"),
        slack_webhook_url=get("SLACK_WEBHOOK_URL"),
        discord_bot_token=get("DISCORD_BOT_TOKEN"),
        discord_channel_id=get("DISCORD_CHANNEL_ID"),
        discord_webhook_url=get("DISCORD_WEBHOOK_URL"),
        web_token=get("WEB_TOKEN"),
        smtp_host=get("SMTP_HOST"),
        smtp_port=smtp_port,
        smtp_user=get("SMTP_USER"),
        smtp_password=get("SMTP_PASSWORD"),
        smtp_from=get("SMTP_FROM"),
        smtp_to=get("SMTP_TO"),
    )


def reload_secrets(cfg: "Config") -> bool:
    """.env를 다시 읽어 cfg.secrets를 갱신합니다. 바뀌었으면 True."""
    fresh = _build_secrets(_env_map(cfg.root))
    changed = fresh != cfg.secrets
    cfg.secrets = fresh
    return changed


def load_config(path: str | Path | None = None) -> Config:
    # os.environ을 직접 읽는 서드파티 라이브러리를 위해 한 번은 반영해 둡니다.
    # (_SHELL_ENV 스냅샷은 이미 위에서 떠 두었으므로 재로딩에 영향이 없습니다.)
    load_dotenv(ROOT / ".env")
    env = _env_map(ROOT)

    cfg_path = Path(path) if path else ROOT / "config.yaml"
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    companies = [
        Company(
            name=c.get("name", "이름없음"),
            industry=c.get("industry", ""),
            ksic=[str(k) for k in (c.get("ksic") or [])],
            keywords=[str(k) for k in (c.get("keywords") or [])],
        )
        for c in (raw.get("watchlist") or [])
    ]

    return Config(
        crawl=CrawlConfig(**(raw.get("crawl") or {})),
        analyze=AnalyzeConfig(**(raw.get("analyze") or {})),
        notify=NotifyConfig(**(raw.get("notify") or {})),
        serve=ServeConfig(**(raw.get("serve") or {})),
        web=WebConfig(**(raw.get("web") or {})),
        watchlist=companies,
        secrets=_build_secrets(env),
    )

"""Anthropic Claude 분석기.

무료 등급이 없으므로 유료 키가 필요합니다. 무료로 시작하려면
analyze.provider를 gemini로 두세요.
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic

from .analyzer import (
    ANALYSIS_SCHEMA,
    SYSTEM_PROMPT,
    CredentialsError,
    QuotaExceededError,
    _build_user_content,
    parse_analysis,
)
from .config import AnalyzeConfig
from .models import Analysis, Precedent

log = logging.getLogger(__name__)

# 서버사이드 폴백: 안전 분류기가 요청을 거절하면 다른 모델이 같은 호출 안에서 이어받습니다.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeAnalyzer:
    def __init__(self, cfg: AnalyzeConfig, api_key: str | None = None):
        self.cfg = cfg
        # api_key가 없으면 SDK가 환경변수/ant 프로필에서 자격증명을 찾습니다.
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self._fallback_enabled = cfg.server_side_fallback

    def _call(self, user_content: str, use_fallback: bool):
        """_request를 감싸 자격증명 오류를 알아보기 쉬운 예외로 바꿉니다."""
        try:
            return self._request(user_content, use_fallback)
        except anthropic.AuthenticationError as exc:
            raise CredentialsError(
                "Claude API 인증에 실패했습니다. .env의 ANTHROPIC_API_KEY를 확인하세요."
            ) from exc
        except anthropic.RateLimitError as exc:
            raise QuotaExceededError(f"Claude API 한도에 걸렸습니다: {exc}") from exc
        except TypeError as exc:
            if "authentication" in str(exc).lower():
                raise CredentialsError(
                    "Claude API 키를 찾지 못했습니다. .env의 ANTHROPIC_API_KEY를 채우거나, "
                    "config.yaml의 analyze.provider를 gemini로 바꾸세요."
                ) from exc
            raise

    def _request(self, user_content: str, use_fallback: bool):
        params: dict[str, Any] = {
            "model": self.cfg.resolved_model(),
            "max_tokens": self.cfg.max_tokens,
            "thinking": {"type": "adaptive"},
            "output_config": {
                "effort": self.cfg.effort,
                "format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA},
            },
            "system": [
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    # 판례마다 동일한 프리픽스라 캐시 적중률이 높습니다.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user_content}],
        }
        if use_fallback:
            return self.client.beta.messages.create(
                betas=[FALLBACK_BETA], fallbacks="default", **params
            )
        return self.client.messages.create(**params)

    def analyze(self, prec: Precedent) -> Analysis | None:
        """분석 결과를 반환. 거부되거나 파싱에 실패하면 None."""
        user_content = _build_user_content(prec)

        try:
            resp = self._call(user_content, self._fallback_enabled)
        except anthropic.BadRequestError as exc:
            message = str(exc).lower()
            if self._fallback_enabled and ("fallback" in message or "beta" in message):
                # 계정에서 서버사이드 폴백 베타를 못 쓰면 표준 엔드포인트로 강등.
                log.warning("서버사이드 폴백 사용 불가 -> 표준 엔드포인트로 전환합니다.")
                self._fallback_enabled = False
                resp = self._call(user_content, False)
            else:
                log.error("분석 요청 거절됨 (%s): %s", prec.key, exc)
                return None

        if resp.stop_reason == "refusal":
            detail = getattr(resp, "stop_details", None)
            log.warning(
                "안전 분류기가 분석을 거부했습니다 (%s): %s",
                prec.key,
                getattr(detail, "category", None),
            )
            return None

        if resp.stop_reason == "max_tokens":
            log.warning("응답이 max_tokens에서 잘렸습니다 (%s). 본문을 더 줄여보세요.", prec.key)

        text = next((b.text for b in resp.content if b.type == "text"), "")
        analysis = parse_analysis(text, prec.key)
        if analysis is None:
            return None

        usage = resp.usage
        log.debug(
            "분석 완료 %s | in=%s cache_read=%s out=%s",
            prec.key,
            usage.input_tokens,
            getattr(usage, "cache_read_input_tokens", 0),
            usage.output_tokens,
        )
        return analysis

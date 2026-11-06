"""Google Gemini 분석기 (무료 등급으로 시작할 때 사용).

무료 등급은 분당 요청수(RPM)와 일일 요청수(RPD)가 제한됩니다. 한도를 넘으면
429가 오므로 호출 간격을 두고, 429는 재시도하되 일일 한도로 판단되면
이번 사이클을 중단하고 다음 일정에 이어갑니다.
"""

from __future__ import annotations

import logging
import random
import re
import time

from .analyzer import (
    SYSTEM_PROMPT,
    CredentialsError,
    QuotaExceededError,
    _build_user_content,
    gemini_schema,
    parse_analysis,
)
from .config import AnalyzeConfig
from .models import Analysis, Precedent

log = logging.getLogger(__name__)

# 429 응답에 이 표현이 있으면 분당이 아니라 하루 한도로 봅니다.
DAILY_QUOTA_MARKERS = ("per day", "PerDay", "GenerateRequestsPerDay", "daily")


class GeminiAnalyzer:
    def __init__(self, cfg: AnalyzeConfig, api_key: str | None = None):
        self.cfg = cfg
        if not api_key:
            raise CredentialsError(
                "Gemini API 키가 없습니다. https://aistudio.google.com/apikey 에서 "
                "무료로 발급받아 .env의 GEMINI_API_KEY에 넣으세요."
            )
        try:
            from google import genai
            from google.genai import errors, types
        except ImportError as exc:
            raise CredentialsError(
                "google-genai 패키지가 없습니다. `python -m pip install google-genai`"
            ) from exc

        # 메서드 안에서 매번 import하면 테스트에서 모킹하기 어렵고,
        # 모듈 최상단에서 import하면 패키지가 없을 때 안내를 못 합니다.
        self._errors = errors
        self._types = types
        self.client = genai.Client(api_key=api_key)
        self._last_call = 0.0
        # 재시도까지 포함한 실제 요청 수. 무료 등급 일일 한도가
        # 건수가 아니라 요청 수로 걸리므로 이걸 기준으로 배분합니다.
        self.api_calls = 0

    # -- 내부 ---------------------------------------------------------
    def _throttle(self) -> None:
        """무료 등급 분당 한도에 걸리지 않게 호출 간격을 둡니다."""
        gap = time.monotonic() - self._last_call
        wait = self.cfg.request_interval_sec - gap
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _config(self):
        types = self._types

        return types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_json_schema=gemini_schema(),
            max_output_tokens=self.cfg.max_tokens,
            temperature=0.2,
        )

    def _quota_info(self, exc: Exception) -> tuple[bool, str | None]:
        """(일일 한도인가?, 한도값) 판정.

        429 응답에는 google.rpc.QuotaFailure가 구조화되어 들어옵니다.
        quotaId가 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'처럼
        PerDay/PerMinute를 명시하므로, 메시지 문자열을 넘겨짚는 것보다
        훨씬 정확합니다. 구조가 없을 때만 문자열 판단으로 물러섭니다.
        """
        payload = getattr(exc, "details", None)
        if isinstance(payload, dict):
            for detail in payload.get("error", {}).get("details", []):
                if not str(detail.get("@type", "")).endswith("QuotaFailure"):
                    continue
                for violation in detail.get("violations", []):
                    quota_id = str(violation.get("quotaId", ""))
                    limit = violation.get("quotaValue")
                    if "PerDay" in quota_id:
                        return True, limit
                    if "PerMinute" in quota_id:
                        return False, limit

        message = str(exc)
        is_daily = any(m.lower() in message.lower() for m in DAILY_QUOTA_MARKERS)
        return is_daily, None

    def _retry_delay(self, message: str, attempt: int) -> float:
        """응답이 알려준 대기 시간이 있으면 그걸, 없으면 지수 백오프."""
        m = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+)", message)
        if m:
            return min(float(m.group(1)) + 1, 90.0)
        return min(2**attempt + random.uniform(0, 1), 60.0)

    # -- 공개 API ------------------------------------------------------
    def analyze(self, prec: Precedent) -> Analysis | None:
        errors = self._errors

        user_content = _build_user_content(prec)
        model = self.cfg.resolved_model()

        for attempt in range(self.cfg.max_retries + 1):
            self._throttle()
            try:
                self.api_calls += 1
                resp = self.client.models.generate_content(
                    model=model, contents=user_content, config=self._config()
                )
            except errors.ClientError as exc:
                message = str(exc)
                status = getattr(exc, "code", None) or getattr(exc, "status_code", None)

                # 상태 코드를 먼저 봅니다. 응답 본문 문자열로 먼저 판단하면
                # 403 응답에 우연히 RESOURCE_EXHAUSTED가 섞였을 때 오분류됩니다.
                if status in (401, 403):
                    raise CredentialsError(
                        f"Gemini 인증에 실패했습니다. .env의 GEMINI_API_KEY를 확인하세요. ({exc})"
                    ) from exc

                if status == 404:
                    # API가 돌려준 설명을 그대로 살립니다. 목록에는 있지만
                    # '신규 사용자에게 제공 중단'된 모델처럼, 원문 메시지가
                    # 결정적인 단서인 경우가 많습니다.
                    raise CredentialsError(
                        f"Gemini 모델 '{model}'을 쓸 수 없습니다.\n"
                        f"  API 응답: {getattr(exc, 'message', None) or message}\n"
                        f"  config.yaml의 analyze.gemini_model을 바꾸세요. "
                        f"(사용 가능 목록: python -m taxrefund models)"
                    ) from exc

                if status == 429 or (status is None and "RESOURCE_EXHAUSTED" in message):
                    is_daily, limit = self._quota_info(exc)
                    if is_daily:
                        cap = f"(하루 {limit}건)" if limit else ""
                        raise QuotaExceededError(
                            f"Gemini 무료 등급 일일 한도를 소진했습니다{cap}. "
                            f"모델 '{model}'. 내일 이어서 자동으로 분석합니다."
                        ) from exc
                    if attempt < self.cfg.max_retries:
                        delay = self._retry_delay(message, attempt)
                        log.warning("Gemini 분당 한도, %.0f초 대기 후 재시도", delay)
                        time.sleep(delay)
                        continue
                    raise QuotaExceededError(
                        "Gemini 분당 한도 재시도에 모두 실패했습니다. "
                        "config.yaml의 analyze.request_interval_sec를 늘려보세요."
                    ) from exc

                if "API key" in message or "PERMISSION_DENIED" in message:
                    raise CredentialsError(
                        f"Gemini 인증에 실패했습니다. .env의 GEMINI_API_KEY를 확인하세요. ({exc})"
                    ) from exc

                log.error("Gemini 요청 실패 (%s): %s", prec.key, exc)
                return None

            except errors.ServerError as exc:
                if attempt < self.cfg.max_retries:
                    delay = self._retry_delay(str(exc), attempt)
                    log.warning("Gemini 서버 오류, %.0f초 후 재시도: %s", delay, exc)
                    time.sleep(delay)
                    continue
                log.error("Gemini 서버 오류로 포기 (%s): %s", prec.key, exc)
                return None

            # -- 응답 처리 --------------------------------------------
            blocked = getattr(getattr(resp, "prompt_feedback", None), "block_reason", None)
            if blocked:
                log.warning("Gemini 안전 필터가 분석을 거부했습니다 (%s): %s", prec.key, blocked)
                return None

            finish = None
            candidates = getattr(resp, "candidates", None) or []
            if candidates:
                finish = str(getattr(candidates[0], "finish_reason", "") or "")
            if "MAX_TOKENS" in finish:
                log.warning(
                    "응답이 max_output_tokens에서 잘렸습니다 (%s). "
                    "crawl.max_body_chars를 줄이거나 analyze.max_tokens를 늘리세요.",
                    prec.key,
                )

            analysis = parse_analysis(resp.text or "", prec.key)
            if analysis is None:
                return None

            usage = getattr(resp, "usage_metadata", None)
            if usage:
                log.debug(
                    "분석 완료 %s | in=%s out=%s",
                    prec.key,
                    getattr(usage, "prompt_token_count", "?"),
                    getattr(usage, "candidates_token_count", "?"),
                )
            return analysis

        return None


    def generate_headline(self, prec: Precedent, analysis: Analysis) -> str | None:
        """저장된 요약만으로 한 줄 제목을 만듭니다 (본문을 다시 넣지 않아 가볍습니다)."""
        errors = self._errors
        prompt = (
            "아래 법인세 판례/해석례의 요지를 한 줄 제목으로 만드십시오.\n"
            "- 쟁점과 결론이 드러나야 합니다.\n"
            "- 사건명·법원명·사건번호를 반복하지 마십시오.\n"
            "- 25~45자, 명사형으로 간결하게. 제목만 출력하십시오.\n\n"
            f"사건명: {prec.title}\n"
            f"결론: {analysis.outcome}\n"
            f"쟁점 요약: {analysis.issue_summary}\n"
        )
        # 분당 한도(429)와 일시적 과부하(503)는 잠깐 기다리면 풀립니다.
        # 재시도가 없으면 무료 등급에서 대부분이 실패로 남습니다.
        for attempt in range(self.cfg.max_retries + 1):
            self._throttle()
            try:
                self.api_calls += 1
                resp = self.client.models.generate_content(
                    model=self.cfg.resolved_model(),
                    contents=prompt,
                    config=self._types.GenerateContentConfig(max_output_tokens=2048),
                )
            except errors.ClientError as exc:
                if getattr(exc, "code", None) == 429:
                    is_daily, limit = self._quota_info(exc)
                    if is_daily:
                        cap = f"(하루 {limit}건)" if limit else ""
                        raise QuotaExceededError(
                            f"Gemini 무료 등급 일일 한도를 소진했습니다{cap}. "
                            "내일 이어서 진행하세요."
                        ) from exc
                    if attempt < self.cfg.max_retries:
                        delay = self._retry_delay(str(exc), attempt)
                        log.info("분당 한도, %.0f초 대기 후 재시도 (%s)", delay, prec.key)
                        time.sleep(delay)
                        continue
                log.warning("한 줄 제목 생성 실패 (%s): %s", prec.key, exc)
                return None
            except errors.ServerError as exc:
                if attempt < self.cfg.max_retries:
                    delay = self._retry_delay(str(exc), attempt)
                    log.info("서버 과부하, %.0f초 후 재시도 (%s)", delay, prec.key)
                    time.sleep(delay)
                    continue
                log.warning("한 줄 제목 생성 실패 (%s): %s", prec.key, exc)
                return None
            except Exception as exc:
                log.warning("한 줄 제목 생성 실패 (%s): %s", prec.key, exc)
                return None

            # 줄을 먼저 자르고 따옴표를 벗깁니다. 순서가 반대면 여는 따옴표만
            # 사라지고 첫 줄 끝의 닫는 따옴표가 남습니다.
            lines = (resp.text or "").strip().splitlines()
            if not lines:
                return None
            return lines[0].strip().strip('"').strip("'").strip() or None

        return None


def verify_model(api_key: str | None, model: str) -> str:
    """모델을 실제로 한 번 호출해 봅니다.

    목록에 있어도 '신규 사용자에게 제공 중단'된 모델이 있어서, 목록 확인만으로는
    쓸 수 있는지 알 수 없습니다. 아주 짧은 요청으로 실제 가용성을 확인합니다.
    """
    if not api_key:
        raise CredentialsError("GEMINI_API_KEY가 없습니다.")
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=model,
        contents="ping",
        config=types.GenerateContentConfig(max_output_tokens=8),
    )
    return (resp.text or "").strip() or "(빈 응답)"


def list_models(api_key: str | None) -> list[str]:
    """이 키로 쓸 수 있는 생성형 모델 이름 목록."""
    if not api_key:
        raise CredentialsError("GEMINI_API_KEY가 없습니다.")
    from google import genai

    client = genai.Client(api_key=api_key)
    names: list[str] = []
    for m in client.models.list():
        actions = getattr(m, "supported_actions", None) or []
        if not actions or "generateContent" in actions:
            names.append((m.name or "").replace("models/", ""))
    return sorted(names)

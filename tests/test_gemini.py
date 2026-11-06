"""Gemini 분석기 검증 (실제 API 호출은 모킹).

실행: python -m tests.test_gemini
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from taxrefund.analyzer import (  # noqa: E402
    ANALYSIS_SCHEMA,
    CredentialsError,
    QuotaExceededError,
    gemini_schema,
    make_analyzer,
    parse_analysis,
)
from taxrefund.analyzer_gemini import GeminiAnalyzer  # noqa: E402
from taxrefund.config import AnalyzeConfig, Secrets, load_config  # noqa: E402
from taxrefund.pipeline import load_sample_precedents  # noqa: E402
from tests.test_pipeline import FAKE_ANALYSIS  # noqa: E402


from google.genai import errors as genai_errors  # noqa: E402
from google.genai import types as genai_types  # noqa: E402


def stub_init(self, cfg, api_key=None):
    """실제 genai.Client를 만들지 않는 __init__ 대체."""
    self.cfg = cfg
    self._last_call = 0.0
    self.api_calls = 0
    self._errors = genai_errors
    self._types = genai_types


STATUS_BY_CODE = {429: "RESOURCE_EXHAUSTED", 403: "PERMISSION_DENIED", 404: "NOT_FOUND"}


def client_error(message: str, code: int = 429) -> genai_errors.ClientError:
    return genai_errors.ClientError(
        code, {"error": {"message": message, "status": STATUS_BY_CODE.get(code, "UNKNOWN")}}
    )


def server_error(message: str = "internal") -> genai_errors.ServerError:
    return genai_errors.ServerError(500, {"error": {"message": message}})


def ok_response(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        text=json.dumps(payload, ensure_ascii=False),
        prompt_feedback=None,
        candidates=[SimpleNamespace(finish_reason="STOP")],
        usage_metadata=SimpleNamespace(prompt_token_count=1200, candidates_token_count=400),
    )


class TestSchemaConversion(unittest.TestCase):
    def test_additionalProperties가_제거된다(self):
        """Gemini 구조화 출력은 additionalProperties를 지원하지 않습니다."""
        schema = gemini_schema()

        def has_ap(node) -> bool:
            if isinstance(node, dict):
                return "additionalProperties" in node or any(has_ap(v) for v in node.values())
            if isinstance(node, list):
                return any(has_ap(v) for v in node)
            return False

        self.assertFalse(has_ap(schema))
        self.assertIn("additionalProperties", ANALYSIS_SCHEMA, "원본은 그대로여야 합니다")

    def test_필드는_그대로_유지된다(self):
        schema = gemini_schema()
        self.assertEqual(set(schema["properties"]), set(ANALYSIS_SCHEMA["properties"]))
        self.assertEqual(schema["required"], ANALYSIS_SCHEMA["required"])
        self.assertEqual(schema["properties"]["outcome"]["enum"], ["납세자승", "과세관청승", "일부승", "기타"])


class TestParse(unittest.TestCase):
    def test_점수는_0에서_100으로_고정(self):
        for raw, expected in ((150, 100), (-5, 0), (82, 82)):
            a = parse_analysis(json.dumps({**FAKE_ANALYSIS, "refund_confidence": raw}), "k")
            self.assertEqual(a.refund_confidence, expected)

    def test_JSON이_아니면_None(self):
        self.assertIsNone(parse_analysis("이건 JSON이 아닙니다", "k"))
        self.assertIsNone(parse_analysis("", "k"))


class TestFactory(unittest.TestCase):
    def test_provider가_gemini면_GeminiAnalyzer(self):
        cfg = AnalyzeConfig(provider="gemini")
        with mock.patch.object(GeminiAnalyzer, "__init__", stub_init):
            self.assertIsInstance(
                make_analyzer(cfg, Secrets(gemini_api_key="k")), GeminiAnalyzer
            )

    def test_provider가_claude면_ClaudeAnalyzer(self):
        from taxrefund.analyzer_claude import ClaudeAnalyzer

        cfg = AnalyzeConfig(provider="claude")
        with mock.patch.object(
            ClaudeAnalyzer, "__init__", lambda self, c, k=None: setattr(self, "cfg", c)
        ):
            self.assertIsInstance(
                make_analyzer(cfg, Secrets(anthropic_api_key="k")), ClaudeAnalyzer
            )

    def test_모르는_provider는_에러(self):
        with self.assertRaises(ValueError):
            make_analyzer(AnalyzeConfig(provider="gpt"), Secrets())

    def test_키가_없으면_CredentialsError(self):
        with self.assertRaises(CredentialsError) as ctx:
            make_analyzer(AnalyzeConfig(provider="gemini"), Secrets(gemini_api_key=None))
        self.assertIn("aistudio", str(ctx.exception))


class TestModelResolution(unittest.TestCase):
    def test_provider에_맞는_모델(self):
        self.assertEqual(
            AnalyzeConfig(provider="gemini", gemini_model="gemini-3.7-flash").resolved_model(),
            "gemini-3.7-flash",
        )
        self.assertEqual(
            AnalyzeConfig(provider="claude", claude_model="claude-opus-5").resolved_model(),
            "claude-opus-5",
        )

    def test_예전_model_설정도_동작(self):
        cfg = AnalyzeConfig(provider="gemini", model="gemini-3.5-flash")
        self.assertEqual(cfg.resolved_model(), "gemini-3.5-flash")

    def test_기본값은_gemini(self):
        self.assertEqual(load_config().analyze.provider, "gemini")


class TestAnalyze(unittest.TestCase):
    def setUp(self):
        self.prec = load_sample_precedents(ROOT)[0]
        self.cfg = AnalyzeConfig(provider="gemini", request_interval_sec=0, max_retries=2)

    def _analyzer(self, generate):
        with mock.patch.object(GeminiAnalyzer, "__init__", stub_init):
            a = GeminiAnalyzer(self.cfg)
        a.client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        return a

    def test_정상_응답(self):
        a = self._analyzer(lambda **kw: ok_response(FAKE_ANALYSIS))
        result = a.analyze(self.prec)
        self.assertIsNotNone(result)
        self.assertEqual(result.refund_confidence, 82)
        self.assertTrue(result.generalizable)

    def test_스키마와_시스템프롬프트가_전달된다(self):
        captured = {}

        def generate(**kw):
            captured.update(kw)
            return ok_response(FAKE_ANALYSIS)

        a = self._analyzer(generate)
        a.analyze(self.prec)
        self.assertEqual(captured["model"], "gemini-3.7-flash")
        cfg = captured["config"]
        self.assertIn("조세 전문가", cfg.system_instruction)
        self.assertEqual(cfg.response_mime_type, "application/json")
        self.assertIsNotNone(cfg.response_json_schema)

    def test_분당한도는_재시도(self):
        calls = {"n": 0}

        def flaky(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise client_error("429 RESOURCE_EXHAUSTED: quota per minute", 429)
            return ok_response(FAKE_ANALYSIS)

        a = self._analyzer(flaky)
        with mock.patch("taxrefund.analyzer_gemini.time.sleep", lambda s: None):
            result = a.analyze(self.prec)
        self.assertIsNotNone(result)
        self.assertEqual(calls["n"], 2)

    def _quota_error(self, quota_id: str, limit: str = "20"):
        """실제 429 응답과 같은 구조의 QuotaFailure를 만듭니다."""
        return genai_errors.ClientError(
            429,
            {
                "error": {
                    "code": 429,
                    "message": "You exceeded your current quota.",
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                            "violations": [{"quotaId": quota_id, "quotaValue": limit}],
                        }
                    ],
                }
            },
        )

    def test_일일한도는_즉시_QuotaExceeded(self):
        """구조화된 quotaId로 판정합니다 (메시지 문자열에 의존하지 않음)."""

        def daily(**kw):
            raise self._quota_error("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "20")

        a = self._analyzer(daily)
        with mock.patch("taxrefund.analyzer_gemini.time.sleep", lambda s: None):
            with self.assertRaises(QuotaExceededError) as ctx:
                a.analyze(self.prec)
        message = str(ctx.exception)
        self.assertIn("일일", message)
        self.assertIn("20", message, "한도값을 알려줘야 합니다")

    def test_분당한도는_구조로_판정해_재시도(self):
        """메시지가 모호해도 quotaId가 PerMinute면 재시도해야 합니다."""
        calls = {"n": 0}

        def flaky(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise self._quota_error(
                    "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "10"
                )
            return ok_response(FAKE_ANALYSIS)

        a = self._analyzer(flaky)
        with mock.patch("taxrefund.analyzer_gemini.time.sleep", lambda s: None):
            self.assertIsNotNone(a.analyze(self.prec))
        self.assertEqual(calls["n"], 2)

    def test_메시지_문자열로_대체판정(self):
        """구조화된 상세가 없으면 메시지로 판단합니다."""

        def daily(**kw):
            raise client_error("429 RESOURCE_EXHAUSTED: quota per day exceeded", 429)

        a = self._analyzer(daily)
        with mock.patch("taxrefund.analyzer_gemini.time.sleep", lambda s: None):
            with self.assertRaises(QuotaExceededError):
                a.analyze(self.prec)

    def test_분당한도_재시도_소진시_QuotaExceeded(self):
        def always(**kw):
            raise client_error("429 RESOURCE_EXHAUSTED: per minute", 429)

        a = self._analyzer(always)
        with mock.patch("taxrefund.analyzer_gemini.time.sleep", lambda s: None):
            with self.assertRaises(QuotaExceededError):
                a.analyze(self.prec)

    def test_인증실패는_CredentialsError(self):
        def denied(**kw):
            raise client_error("403 PERMISSION_DENIED: API key not valid", 403)

        a = self._analyzer(denied)
        with self.assertRaises(CredentialsError):
            a.analyze(self.prec)

    def test_없는_모델은_안내와_함께_CredentialsError(self):
        def missing(**kw):
            raise client_error("404 NOT_FOUND: model not found", 404)

        a = self._analyzer(missing)
        with self.assertRaises(CredentialsError) as ctx:
            a.analyze(self.prec)
        self.assertIn("models", str(ctx.exception))

    def test_안전필터_차단은_None(self):
        blocked = SimpleNamespace(
            text="",
            prompt_feedback=SimpleNamespace(block_reason="SAFETY"),
            candidates=[],
            usage_metadata=None,
        )
        a = self._analyzer(lambda **kw: blocked)
        self.assertIsNone(a.analyze(self.prec))

    def test_깨진_JSON은_None(self):
        broken = SimpleNamespace(
            text="설명을 곁들인 답변입니다",
            prompt_feedback=None,
            candidates=[SimpleNamespace(finish_reason="STOP")],
            usage_metadata=None,
        )
        a = self._analyzer(lambda **kw: broken)
        self.assertIsNone(a.analyze(self.prec))

    def test_서버오류는_재시도_후_None(self):
        def down(**kw):
            raise server_error("500 internal")

        a = self._analyzer(down)
        with mock.patch("taxrefund.analyzer_gemini.time.sleep", lambda s: None):
            self.assertIsNone(a.analyze(self.prec))

    def test_호출간격이_지켜진다(self):
        slept: list[float] = []
        cfg = AnalyzeConfig(provider="gemini", request_interval_sec=6.0)
        with mock.patch.object(GeminiAnalyzer, "__init__", stub_init):
            a = GeminiAnalyzer(cfg)
        a.client = SimpleNamespace(
            models=SimpleNamespace(generate_content=lambda **kw: ok_response(FAKE_ANALYSIS))
        )
        with mock.patch(
            "taxrefund.analyzer_gemini.time.sleep", lambda s: slept.append(s)
        ):
            a.analyze(self.prec)
            a.analyze(self.prec)
        self.assertTrue(any(s > 0 for s in slept), "두 번째 호출 전에 대기해야 합니다")


class TestHeadline(unittest.TestCase):
    """한 줄 요지 보정도 analyze와 같은 재시도를 받아야 합니다.

    재시도가 없으면 무료 등급에서 503/429로 대부분이 실패로 남습니다.
    """

    def setUp(self):
        self.prec = load_sample_precedents(ROOT)[0]
        self.analysis = parse_analysis(json.dumps(FAKE_ANALYSIS, ensure_ascii=False), "t")
        self.cfg = AnalyzeConfig(provider="gemini", request_interval_sec=0, max_retries=2)

    def _analyzer(self, generate):
        with mock.patch.object(GeminiAnalyzer, "__init__", stub_init):
            a = GeminiAnalyzer(self.cfg)
        a.client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        return a

    def _text(self, value: str) -> SimpleNamespace:
        return SimpleNamespace(text=value)

    def test_정상이면_한_줄만_남긴다(self):
        a = self._analyzer(lambda **kw: self._text('  "매출에누리 해당 여부"\n부연설명  '))
        self.assertEqual(a.generate_headline(self.prec, self.analysis), "매출에누리 해당 여부")

    def test_과부하_503은_재시도(self):
        calls = {"n": 0}

        def flaky(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise genai_errors.ServerError(503, {"error": {"message": "high demand"}})
            return self._text("적격합병 시 세액공제 이월액 승계 가부")

        a = self._analyzer(flaky)
        with mock.patch("taxrefund.analyzer_gemini.time.sleep", lambda s: None):
            headline = a.generate_headline(self.prec, self.analysis)
        self.assertEqual(headline, "적격합병 시 세액공제 이월액 승계 가부")
        self.assertEqual(calls["n"], 2)

    def test_분당한도_429는_재시도(self):
        calls = {"n": 0}

        def flaky(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise client_error("429 RESOURCE_EXHAUSTED: per minute", 429)
            return self._text("제목")

        a = self._analyzer(flaky)
        with mock.patch("taxrefund.analyzer_gemini.time.sleep", lambda s: None):
            self.assertEqual(a.generate_headline(self.prec, self.analysis), "제목")
        self.assertEqual(calls["n"], 2)

    def test_일일한도는_재시도하지_않고_중단(self):
        calls = {"n": 0}

        def daily(**kw):
            calls["n"] += 1
            raise genai_errors.ClientError(
                429,
                {
                    "error": {
                        "message": "quota",
                        "status": "RESOURCE_EXHAUSTED",
                        "details": [
                            {
                                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                "violations": [
                                    {
                                        "quotaId": "GenerateRequestsPerDayPerProject-FreeTier",
                                        "quotaValue": "20",
                                    }
                                ],
                            }
                        ],
                    }
                },
            )

        a = self._analyzer(daily)
        with mock.patch("taxrefund.analyzer_gemini.time.sleep", lambda s: None):
            with self.assertRaises(QuotaExceededError):
                a.generate_headline(self.prec, self.analysis)
        self.assertEqual(calls["n"], 1, "하루 한도는 기다려도 안 풀립니다")

    def test_재시도_소진되면_None(self):
        def down(**kw):
            raise genai_errors.ServerError(503, {"error": {"message": "high demand"}})

        a = self._analyzer(down)
        with mock.patch("taxrefund.analyzer_gemini.time.sleep", lambda s: None):
            self.assertIsNone(a.generate_headline(self.prec, self.analysis))


if __name__ == "__main__":
    unittest.main(verbosity=2)


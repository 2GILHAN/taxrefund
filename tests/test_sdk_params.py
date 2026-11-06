"""analyzer가 쓰는 파라미터를 설치된 anthropic SDK가 실제로 지원하는지 확인.

SDK 업그레이드로 인자가 바뀌면 여기서 먼저 잡힙니다.
실행: python -m tests.test_sdk_params
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402


class TestSdkSignatures(unittest.TestCase):
    def setUp(self):
        self.client = anthropic.Anthropic(api_key="sk-ant-dummy-for-signature-check")

    def test_standard_endpoint_params(self):
        params = inspect.signature(self.client.messages.create).parameters
        for name in ("model", "max_tokens", "thinking", "output_config", "system", "messages"):
            self.assertIn(name, params, f"messages.create가 {name}를 받지 않습니다")

    def test_beta_endpoint_fallback_params(self):
        params = inspect.signature(self.client.beta.messages.create).parameters
        for name in ("betas", "fallbacks", "output_config", "thinking"):
            self.assertIn(name, params, f"beta.messages.create가 {name}를 받지 않습니다")

    def test_bad_request_error_exists(self):
        self.assertTrue(issubclass(anthropic.BadRequestError, Exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)

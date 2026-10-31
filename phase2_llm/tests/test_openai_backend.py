# -*- coding: utf-8 -*-
"""Test backend OpenAI phát triển — KHÔNG gọi mạng thật.

Mục đích:
    Kiểm chứng các điểm sau mà không tốn quota API và không cần key thật:
      1. ``load_dotenv`` đọc đúng file .env mẫu (model, base_url, các
         tham số từ curl) và không ghi đè biến môi trường đã có sẵn.
      2. Thiếu key thì ``OpenAIConfig`` raise RuntimeError rõ ràng, và
         thông báo lỗi không chứa key.
      3. ``generate_json`` trích đúng văn bản từ phản hồi Responses API
         (cả dạng ``output_text`` tổng hợp lẫn dạng mảng ``output``),
         và dùng ``json_schema`` thay cho ``text`` như curl gốc.
      4. Lỗi HTTP 401 được chuyển thành RuntimeError có status code,
         không lộ key, không retry vô hạn.

Cách mock mạng: thay ``urllib.request.urlopen`` bằng hàm giả trả về một
object giống file (có ``read()`` và hỗ trợ ``with``), và ghi lại request
đã gửi để kiểm tra body/header. Không có test nào trong file này chạm
tới api.openai.com thật.
"""

import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openai_backend import (  # noqa: E402
    OpenAIConfig,
    OpenAIResponsesBackend,
    load_dotenv,
)


class _FakeResponse:
    """Object giả lập phản hồi HTTP: chỉ cần read() và __enter__/__exit__."""

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestLoadDotenv(unittest.TestCase):
    """Test đọc file .env mẫu."""

    def test_loads_example_values_without_overwriting_existing_env(self):
        # Biến đã có sẵn trong môi trường phải thắng file .env.
        with mock.patch.dict(os.environ, {"OPENAI_MODEL": "model-tu-moi-truong"}, clear=False):
            # Ghi một file .env tạm (không phải file thật trong repo).
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as handle:
                handle.write("# comment\nOPENAI_MODEL=gpt-4o-mini\nOPENAI_BASE_URL=https://api.openai.com/v1\n")
                tmp_path = handle.name
            try:
                # Xóa OPENAI_BASE_URL khỏi môi trường tạm để test nạp từ file.
                os.environ.pop("OPENAI_BASE_URL", None)
                load_dotenv(tmp_path)
                self.assertEqual(os.environ["OPENAI_MODEL"], "model-tu-moi-truong")
                self.assertEqual(os.environ["OPENAI_BASE_URL"], "https://api.openai.com/v1")
            finally:
                Path(tmp_path).unlink(missing_ok=True)
                os.environ.pop("OPENAI_BASE_URL", None)


class TestOpenAIConfig(unittest.TestCase):
    """Test cấu hình: thiếu key phải báo lỗi, có key thì giữ đúng giá trị curl."""

    def test_missing_key_raises_without_leaking_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                OpenAIConfig(env_file="/duong/dan/khong/ton/tai/.env")
            self.assertIn("OPENAI_API_KEY", str(caught.exception))

    def test_defaults_match_user_curl(self):
        config = OpenAIConfig(api_key="key-gia-cho-test")
        self.assertEqual(config.model, "gpt-4o-mini")
        self.assertEqual(config.base_url, "https://api.openai.com/v1")
        self.assertEqual(config.service_tier, "default")
        self.assertEqual(config.temperature, 1.0)
        self.assertEqual(config.max_output_tokens, 2048)
        self.assertEqual(config.top_p, 1.0)
        self.assertTrue(config.store)

    def test_repr_does_not_leak_key(self):
        config = OpenAIConfig(api_key="sk-bi-mat-khong-duoc-in")
        self.assertNotIn("sk-bi-mat-khong-duoc-in", repr(config))


class TestOpenAIResponsesBackend(unittest.TestCase):
    """Test gọi API bằng mock, không gọi mạng thật."""

    def setUp(self):
        self.config = OpenAIConfig(api_key="key-gia-cho-test")
        self.backend = OpenAIResponsesBackend(config=self.config)
        self.schema = {
            "type": "object",
            "properties": {"measure": {"type": "string"}},
            "required": ["measure"],
        }
        self.expected = {"measure": "revenue"}

    def test_generate_json_uses_json_schema_and_extracts_output_text(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["auth"] = request.headers.get("Authorization")
            return _FakeResponse({"output_text": json.dumps(self.expected)})

        with mock.patch("openai_backend.urllib.request.urlopen", side_effect=fake_urlopen):
            result = self.backend.generate_json("prompt gia", self.schema)
        self.assertEqual(result, self.expected)
        self.assertEqual(captured["url"], "https://api.openai.com/v1/responses")
        # Điểm khác curl gốc có chủ ý: parser cần json_schema, không phải text.
        self.assertEqual(captured["body"]["text"]["format"]["type"], "json_schema")
        self.assertEqual(captured["body"]["text"]["format"]["schema"], self.schema)
        self.assertEqual(captured["body"]["model"], "gpt-4o-mini")
        # Parser không bật web search, nên body không có trường include.
        self.assertNotIn("include", captured["body"])
        self.assertEqual(captured["auth"], "Bearer key-gia-cho-test")

    def test_generate_json_extracts_from_output_array(self):
        payload = {
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps(self.expected)}],
            }]
        }
        with mock.patch("openai_backend.urllib.request.urlopen",
                         return_value=_FakeResponse(payload)):
            self.assertEqual(self.backend.generate_json("prompt gia", self.schema), self.expected)

    def test_http_401_becomes_runtime_error_without_key(self):
        error = urllib.error.HTTPError(
            url="https://api.openai.com/v1/responses", code=401,
            msg="Unauthorized", hdrs=None, fp=io.BytesIO(b"{}"))
        with mock.patch("openai_backend.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                self.backend.generate_json("prompt gia", self.schema)
            self.assertIn("401", str(caught.exception))
            self.assertNotIn("key-gia-cho-test", str(caught.exception))

    def test_non_json_output_raises(self):
        with mock.patch("openai_backend.urllib.request.urlopen",
                         return_value=_FakeResponse({"output_text": "khong phai json"})):
            with self.assertRaises(RuntimeError):
                self.backend.generate_json("prompt gia", self.schema)


if __name__ == "__main__":
    unittest.main()

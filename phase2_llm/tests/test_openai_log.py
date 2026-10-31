# -*- coding: utf-8 -*-
"""Test log_openai — KHÔNG gọi mạng thật, KHÔNG cần key thật.

Mục đích:
    Kiểm chứng 3 điểm của tính năng log_openai, mà không tốn quota API:
      1. Backend OpenAI ghi lại từng lần gọi vào ``call_log``: đủ
         system/user prompt, raw output, JSON đã parse, tham số, lỗi
         (khi lỗi), và KHÔNG chứa key API.
      2. ``build_openai_log_record`` ráp đúng một bản ghi câu hỏi:
         câu hỏi, context shop đã gửi cho model, từng lần gọi model,
         kết quả parse, đáp án cuối — và bản ghi ghi được bằng json.
      3. ``OpenAICallLogger`` ghi JSONL: dòng run + dòng question đọc
         lại được bằng json.loads, theo đúng thứ tự ghi.

Cách mock mạng: giống test_openai_backend.py — thay
``urllib.request.urlopen`` bằng hàm giả trả về phản hồi Chat
Completions mẫu, ghi lại request để kiểm tra body. Key trong test là
chuỗi giả, và test assert rõ ràng là nó không xuất hiện trong call_log
hay trong file log_openai.
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
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openai_backend import OpenAIChatBackend, OpenAIConfig  # noqa: E402
from run_phase2_openai import (  # noqa: E402
    OpenAICallLogger,
    build_openai_log_record,
)

FAKE_KEY = "sk-test-khong-phai-key-that-12345"


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


def _chat_payload(content):
    """Phản hồi Chat Completions mẫu với content là chuỗi JSON."""
    return {
        "id": "chatcmpl-gia",
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }


class TestBackendCallLog(unittest.TestCase):
    """Backend phải ghi lại lần gọi, đủ prompt + raw output, không lộ key."""

    def test_call_log_ghi_du_prompt_raw_va_parsed_khong_lo_key(self):
        config = OpenAIConfig(api_key=FAKE_KEY)
        backend = OpenAIChatBackend(config=config)
        prompt_mau = "Câu hỏi gốc: doanh thu tháng 5 là bao nhiêu?"
        schema_mau = {"type": "object", "properties": {"measure": {"type": "string"}}}
        with mock.patch(
            "urllib.request.urlopen",
            return_value=_FakeResponse(
                _chat_payload('{"measure": "revenue", "answer_type": "number"}')),
        ):
            result = backend.generate_json(prompt_mau, schema_mau)
        self.assertEqual(result, {"measure": "revenue", "answer_type": "number"})
        self.assertEqual(len(backend.call_log), 1)
        record = backend.call_log[0]
        # Đủ context: system + user prompt, raw output, JSON đã parse.
        self.assertIn("DataMate", record["system_message"])
        self.assertEqual(record["user_prompt"], prompt_mau)
        self.assertEqual(
            record["raw_content"], '{"measure": "revenue", "answer_type": "number"}')
        self.assertEqual(
            record["parsed_json"], {"measure": "revenue", "answer_type": "number"})
        self.assertIsNone(record["error"])
        self.assertEqual(record["model"], "gpt-4o-mini")
        self.assertEqual(record["endpoint"], "/chat/completions")
        # Không lộ key ở bất kỳ đâu trong bản ghi.
        self.assertNotIn(FAKE_KEY, json.dumps(record, ensure_ascii=False))

    def test_call_log_ghi_ca_lan_goi_loi(self):
        config = OpenAIConfig(api_key=FAKE_KEY)
        backend = OpenAIChatBackend(config=config)
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                "https://api.openai.com/v1/chat/completions", 401,
                "Unauthorized", {}, io.BytesIO(b"")),
        ):
            with self.assertRaises(RuntimeError):
                backend.generate_json("prompt", {"type": "object"})
        self.assertEqual(len(backend.call_log), 1)
        self.assertIn("401", backend.call_log[0]["error"])
        self.assertIsNone(backend.call_log[0]["parsed_json"])
        self.assertNotIn(FAKE_KEY, json.dumps(backend.call_log[0], ensure_ascii=False))


class TestBuildOpenaiLogRecord(unittest.TestCase):
    """Bản ghi câu hỏi phải đủ 4 phần: câu hỏi, context, output model, đáp án cuối."""

    def test_ban_ghi_day_du_va_ghi_duoc_bang_json(self):
        question = {
            "id": "q_gia", "business_id": "biz_gia",
            "question": "doanh thu tháng 5 là bao nhiêu?",
            "as_of_date": "2026-06-16",
        }
        context = {
            "question_text": "doanh thu tháng 5 là bao nhiêu?",
            "as_of": "2026-06-16",
            "products": ["Sản phẩm A 100g", "Sản phẩm B"],
            "customers": ["Khách A"],
            "calls": [{
                "model": "gpt-4o-mini", "endpoint": "/chat/completions",
                "base_url": "https://api.openai.com/v1",
                "temperature": 0, "top_p": 1, "max_tokens": 2048,
                "response_format": {"type": "json_object"},
                "system_message": "Chỉ trả JSON",
                "user_prompt": "Câu hỏi gốc: doanh thu tháng 5 là bao nhiêu?",
                "raw_content": '{"measure": "revenue"}',
                "cleaned_text": '{"measure": "revenue"}',
                "parsed_json": {"measure": "revenue"},
                "error": None, "elapsed_ms": 123.0,
            }],
            "parse_source": "llm",
            "parse_confidence": 0.9,
            "parse_notes": ["ghi chú mẫu"],
            "parsed": None,
            "parse_ms": 200.0,
            "parse_error": None,
        }
        entry = {"id": "q_gia", "type": "number", "answer": 1000}
        info = {"measure": "revenue", "status": "answered",
                "error_group": None, "confidence": 0.9}
        record = build_openai_log_record(
            question, context, entry, info, question_seconds=0.5)
        # Cấu trúc cố định, đủ 4 phần.
        self.assertEqual(record["record_type"], "question")
        self.assertEqual(record["question_id"], "q_gia")
        self.assertEqual(record["question"], "doanh thu tháng 5 là bao nhiêu?")
        self.assertEqual(record["shop_entities"]["products"],
                         ["Sản phẩm A 100g", "Sản phẩm B"])
        self.assertEqual(len(record["model_calls"]), 1)
        self.assertEqual(record["model_calls"][0]["kind"], "initial")
        self.assertEqual(record["model_calls"][0]["raw_content"],
                         '{"measure": "revenue"}')
        self.assertEqual(record["model_calls"][0]["parsed_json"],
                         {"measure": "revenue"})
        self.assertEqual(record["parse_result"]["source"], "llm")
        self.assertEqual(record["final_answer"]["entry"], entry)
        self.assertEqual(record["final_answer"]["status"], "answered")
        # Ghi được bằng json (không còn object lạ).
        json.dumps(record, ensure_ascii=False)

    def test_lan_goi_thu_hai_duoc_danh_dau_repair_va_khong_co_key(self):
        question = {"id": "q_gia", "business_id": "biz_gia",
                    "question": "câu hỏi", "as_of_date": "2026-06-16"}
        context = {
            "question_text": "câu hỏi", "as_of": "2026-06-16",
            "products": [], "customers": [],
            "calls": [
                {"model": "m", "raw_content": "{sai", "parsed_json": None,
                 "error": "JSON sai", "system_message": "s", "user_prompt": "u"},
                {"model": "m", "raw_content": '{"measure": "revenue"}',
                 "parsed_json": {"measure": "revenue"}, "error": None,
                 "system_message": "s", "user_prompt": "u"},
            ],
            "parse_source": "repair", "parse_confidence": 0.8,
            "parse_notes": [], "parsed": None, "parse_ms": 1.0,
            "parse_error": None,
        }
        record = build_openai_log_record(question, context, None, None)
        self.assertEqual(
            [call["kind"] for call in record["model_calls"]],
            ["initial", "repair"])
        self.assertNotIn(FAKE_KEY, json.dumps(record, ensure_ascii=False))


class TestOpenAICallLogger(unittest.TestCase):
    """File log_openai phải đọc lại được theo từng dòng JSON."""

    def test_ghi_run_va_question_roi_doc_lai_duoc(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "log_openai.jsonl"
            logger = OpenAICallLogger(log_path=path)
            try:
                logger.write_run_record({"model": "gpt-4o-mini", "shops": ["biz_gia"]})
                logger.write_question(build_openai_log_record(
                    {"id": "q_gia", "business_id": "biz_gia",
                     "question": "câu hỏi", "as_of_date": "2026-06-16"},
                    {"question_text": "câu hỏi", "as_of": "2026-06-16",
                     "products": [], "customers": [], "calls": [],
                     "parse_source": "template_fallback", "parse_confidence": 0.5,
                     "parse_notes": [], "parsed": None, "parse_ms": 1.0,
                     "parse_error": None},
                    {"id": "q_gia", "type": "abstain"}, None))
            finally:
                logger.close()
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            run_record = json.loads(lines[0])
            question_record = json.loads(lines[1])
            self.assertEqual(run_record["record_type"], "run")
            self.assertEqual(run_record["model"], "gpt-4o-mini")
            self.assertEqual(question_record["record_type"], "question")
            self.assertEqual(question_record["final_answer"]["entry"]["type"], "abstain")
            # File không chứa key giả (test này không dùng key thật, nhưng
            # assert để khóa quy tắc: logger không bao giờ ghi key).
            self.assertNotIn(FAKE_KEY, path.read_text(encoding="utf-8"))

    def test_tat_log_thi_khong_ghi_file(self):
        logger = OpenAICallLogger(log_path=None)
        self.assertIsNone(logger.log_path)
        # Ghi khi tắt thì không crash, không làm gì cả.
        logger.write_run_record({"model": "m"})
        logger.write_question({"record_type": "question"})
        logger.close()


if __name__ == "__main__":
    unittest.main()

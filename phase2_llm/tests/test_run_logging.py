# -*- coding: utf-8 -*-
"""Test tầng log của runner OpenAI — KHÔNG gọi mạng thật, KHÔNG cần key thật.

Mục đích:
    Kiểm chứng các điểm sau mà không tốn quota API:
      1. ``ConsoleLogger`` ghi ra cả console (qua print) và file log,
         và chế độ quiet chỉ giữ lại dòng quan trọng.
      2. ``LoggingBackend`` gọi đúng backend thật, trả nguyên kết quả,
         và khi backend lỗi thì log lỗi rồi raise lại nguyên vẹn (không
         nuốt lỗi để pipeline tưởng đã parse xong).
      3. ``run_phase2_llm`` chấp nhận hai callback ``on_question`` và
         ``on_shop`` (chữ ký mới thêm cho tầng log) mà không bắt buộc
         caller cũ phải truyền chúng.

    Test này KHÔNG chạy pipeline thật (pipeline thật cần dữ liệu train
    và cache), nên phần callback của pipeline chỉ kiểm chứng ở mức chữ
    ký; việc callback được gọi thật trong một đợt chạy sẽ được kiểm
    chứng bằng đợt chạy --limit 1 với key giả (xem ghi chú trong
    run_phase2_openai.py), vì đợt chạy đó không tốn quota thật.
"""

import inspect
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from run_phase2_openai import ConsoleLogger, LoggingBackend  # noqa: E402
import run_phase2_llm  # noqa: E402


class _FakeBackend:
    """Backend giả: trả dict cố định, hoặc raise lỗi cố định khi được yêu cầu."""

    def __init__(self, fail_with=None):
        self.fail_with = fail_with
        self.calls = []

    def generate_json(self, prompt, schema):
        self.calls.append((prompt, schema))
        if self.fail_with is not None:
            raise self.fail_with
        return {"measure": "revenue"}


class TestConsoleLogger(unittest.TestCase):
    """Test ghi log ra console và file."""

    def test_logs_to_file_and_quiet_keeps_only_important(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "run.log"
            logger = ConsoleLogger(log_file=log_path, quiet=True)
            try:
                # Dòng thường bị quiet chặn, dòng quan trọng vẫn ghi.
                with mock.patch("sys.stdout", new_callable=io.StringIO):
                    logger.log("dòng thường")
                    logger.log("dòng quan trọng", important=True)
            finally:
                logger.close()
            content = log_path.read_text(encoding="utf-8")
            self.assertNotIn("dòng thường", content)
            self.assertIn("dòng quan trọng", content)

    def test_log_without_file_does_not_crash(self):
        logger = ConsoleLogger(log_file=None, quiet=False)
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            logger.log("chỉ in console")
        logger.close()


class TestLoggingBackend(unittest.TestCase):
    """Test lớp bọc log không đổi kết quả và không nuốt lỗi."""

    def test_success_is_logged_and_result_passed_through(self):
        logger = ConsoleLogger(log_file=None, quiet=False)
        fake = _FakeBackend()
        wrapped = LoggingBackend(fake, logger)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as captured:
            result = wrapped.generate_json("prompt gia", {"type": "object"})
        self.assertEqual(result, {"measure": "revenue"})
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(wrapped.calls, 1)
        self.assertEqual(wrapped.failures, 0)
        self.assertIn("LLM OK", captured.getvalue())

    def test_failure_is_logged_and_reraised(self):
        logger = ConsoleLogger(log_file=None, quiet=False)
        fake = _FakeBackend(fail_with=RuntimeError("OpenAI Responses API trả lời HTTP 401"))
        wrapped = LoggingBackend(fake, logger)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as captured:
            with self.assertRaises(RuntimeError):
                wrapped.generate_json("prompt gia", {"type": "object"})
        self.assertEqual(wrapped.failures, 1)
        self.assertIn("LLM ERROR", captured.getvalue())
        self.assertIn("401", captured.getvalue())


class TestRunPhase2LlmCallbacks(unittest.TestCase):
    """Test pipeline chấp nhận callback log (mức chữ ký, không chạy pipeline thật)."""

    def test_run_phase2_llm_accepts_on_question_and_on_shop(self):
        params = inspect.signature(run_phase2_llm.run_phase2_llm).parameters
        self.assertIn("on_question", params)
        self.assertIn("on_shop", params)
        # Mặc định phải là None để caller cũ (không truyền callback) chạy
        # đúng như trước khi có tầng log.
        self.assertIsNone(params["on_question"].default)
        self.assertIsNone(params["on_shop"].default)


if __name__ == "__main__":
    unittest.main()

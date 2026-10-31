# -*- coding: utf-8 -*-
"""Test LLM-only: LLM là đường parse duy nhất, lỗi thì raise để abstain, KHÔNG fallback template.

Mục đích:
    Chứng minh bằng test (không gọi API thật, không cần key thật) rằng
    ``parse_with_llm_only`` trong ``run_phase2_openai.py`` tuân thủ yêu
    cầu của người dùng ngày 2026-10-31 (bỏ fallback template vì nó dựa
    trên cụm từ của train nên rất hên xui trên test):
      1. LLM thành công -> trả kết quả LLM ngay.
      2. LLM sau repair vẫn thành công -> trả kết quả repair.
      3. LLM raise bất kỳ lỗi nào (API, mạng, dựng prompt...) -> hàm
         raise lại (để tầng solve_one_question chuyển câu đó thành
         abstain, không crash shop, không bịa đáp án, không template).
      4. LLM trả về kết quả không dùng được (None / không có parsed)
         -> hàm raise RuntimeError, giống trường hợp LLM lỗi.
      5. Hàm này KHÔNG nhận bất kỳ hàm template nào: chữ ký không còn
         tham số template_fallback_fn, nên không có đường nào gọi
         template thừa được nữa.

    Cách test: tiêm hàm LLM giả, thay vì dùng backend thật hay parser
    template thật.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from run_phase2_openai import parse_with_llm_only  # noqa: E402


def _llm_result(source="llm"):
    """Tạo một kết quả parse giả dạng ParseResult (parsed/source/confidence/notes)."""
    parsed = SimpleNamespace(measure="revenue", confidence_parse=0.9)
    return SimpleNamespace(parsed=parsed, source=source, confidence=0.9, notes=[])


class TestLlmOnly(unittest.TestCase):
    """Test LLM duy nhất, không fallback template."""

    def setUp(self):
        # Các tham số parse giả, đủ để hàm chạy mà không cần dữ liệu thật.
        self.kwargs = {
            "question_text": "doanh thu thang nay la bao nhieu",
            "as_of": None,
            "calendar": None,
            "rulebook": None,
            "products": [],
            "customers": [],
            "backend": object(),
            "logger": None,
        }

    def test_llm_success_tra_ngay(self):
        calls = {"llm": 0}

        def fake_llm(*args, **kwargs):
            calls["llm"] += 1
            return _llm_result("llm")

        result = parse_with_llm_only(**self.kwargs, llm_parse_fn=fake_llm)
        self.assertEqual(result.source, "llm")
        self.assertEqual(calls, {"llm": 1})

    def test_llm_repair_success_tra_ngay(self):
        result = parse_with_llm_only(
            **self.kwargs,
            llm_parse_fn=lambda *a, **k: _llm_result("repair"))
        self.assertEqual(result.source, "repair")

    def test_llm_error_thi_raise_de_abstain(self):
        def fake_llm(*args, **kwargs):
            raise RuntimeError("OpenAI API trả lời HTTP 401")

        with self.assertRaises(RuntimeError):
            parse_with_llm_only(**self.kwargs, llm_parse_fn=fake_llm)

    def test_llm_unusable_result_thi_raise(self):
        with self.assertRaises(RuntimeError):
            parse_with_llm_only(
                **self.kwargs,
                llm_parse_fn=lambda *a, **k: SimpleNamespace(
                    parsed=None, source="llm", confidence=0.0, notes=[]))
        with self.assertRaises(RuntimeError):
            parse_with_llm_only(
                **self.kwargs, llm_parse_fn=lambda *a, **k: None)

    def test_khong_con_tham_so_template(self):
        """Chữ ký không còn template_fallback_fn: truyền tham số đó phải lỗi TypeError."""
        with self.assertRaises(TypeError):
            parse_with_llm_only(
                **self.kwargs,
                llm_parse_fn=lambda *a, **k: _llm_result("llm"),
                template_fallback_fn=lambda *a, **k: _llm_result("llm"))


if __name__ == "__main__":
    unittest.main()

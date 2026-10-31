# -*- coding: utf-8 -*-
"""Test thứ tự bắt buộc: LLM trước, lỗi thì fallback template.

Mục đích:
    Chứng minh bằng test (không gọi API thật, không cần key thật) rằng
    ``parse_with_llm_first`` trong ``run_phase2_openai.py`` tuân thủ đúng
    thứ tự người dùng yêu cầu, và không có trường hợp nào template chạy
    trước LLM hay template chạy thừa:
      1. LLM thành công -> trả kết quả LLM ngay, template KHÔNG được gọi.
      2. LLM raise bất kỳ lỗi nào (API, mạng, dựng prompt...) -> template
         được gọi đúng một lần, kết quả trả về là template_fallback và
         notes ghi rõ lý do lỗi LLM.
      3. LLM trả về kết quả không dùng được (None / không có parsed) ->
         template được gọi, giống trường hợp LLM lỗi.
      4. LLM đã tự fallback bên trong (trả template_fallback) -> trả ngay,
         template KHÔNG được gọi lần thứ hai.
      5. Cả LLM lẫn template đều lỗi -> hàm raise lại (để tầng
         solve_one_question chuyển câu đó thành abstain, không crash shop,
         không bịa đáp án). Test này chỉ kiểm chứng việc raise, việc
         chuyển thành abstain thuộc về pipeline (đã có test riêng).

    Cách test: tiêm hàm LLM giả và hàm template giả có đếm số lần gọi,
    thay vì dùng backend thật hay parser template thật.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from run_phase2_openai import parse_with_llm_first  # noqa: E402


def _llm_result(source="llm"):
    """Tạo một kết quả parse giả dạng ParseResult (parsed/source/confidence/notes)."""
    parsed = SimpleNamespace(measure="revenue", confidence_parse=0.9)
    return SimpleNamespace(parsed=parsed, source=source, confidence=0.9, notes=[])


def _template_result():
    """Tạo một kết quả template giả, source bắt buộc là template_fallback."""
    parsed = SimpleNamespace(measure="revenue", confidence_parse=0.7)
    return SimpleNamespace(
        parsed=parsed, source="template_fallback", confidence=0.7,
        notes=["Fallback ve parser template."])


class TestLlmFirstFallback(unittest.TestCase):
    """Test thứ tự LLM trước, template sau."""

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

    def test_llm_success_template_never_called(self):
        calls = {"llm": 0, "template": 0}

        def fake_llm(*args, **kwargs):
            calls["llm"] += 1
            return _llm_result("llm")

        def fake_template(*args, **kwargs):
            calls["template"] += 1
            return _template_result()

        result = parse_with_llm_first(
            **self.kwargs, llm_parse_fn=fake_llm, template_fallback_fn=fake_template)
        self.assertEqual(result.source, "llm")
        self.assertEqual(calls, {"llm": 1, "template": 0})

    def test_llm_repair_success_template_never_called(self):
        # LLM sau repair vẫn là thành công: template cũng không được gọi.
        calls = {"template": 0}

        def fake_template(*args, **kwargs):
            calls["template"] += 1
            return _template_result()

        result = parse_with_llm_first(
            **self.kwargs,
            llm_parse_fn=lambda *a, **k: _llm_result("repair"),
            template_fallback_fn=fake_template)
        self.assertEqual(result.source, "repair")
        self.assertEqual(calls["template"], 0)

    def test_llm_error_calls_template_once(self):
        calls = {"llm": 0, "template": 0}
        reasons = []

        def fake_llm(*args, **kwargs):
            calls["llm"] += 1
            raise RuntimeError("OpenAI Responses API tra loi HTTP 401")

        def fake_template(*args, reason=None, **kwargs):
            calls["template"] += 1
            reasons.append(reason)
            return _template_result()

        result = parse_with_llm_first(
            **self.kwargs, llm_parse_fn=fake_llm, template_fallback_fn=fake_template)
        self.assertEqual(result.source, "template_fallback")
        self.assertEqual(calls, {"llm": 1, "template": 1})
        # Lý do fallback phải ghi rõ lỗi LLM, để log truy vết được.
        self.assertTrue(any("401" in (reason or "") for reason in reasons))

    def test_llm_unusable_result_calls_template(self):
        calls = {"template": 0}

        def fake_template(*args, **kwargs):
            calls["template"] += 1
            return _template_result()

        result = parse_with_llm_first(
            **self.kwargs,
            llm_parse_fn=lambda *a, **k: SimpleNamespace(parsed=None, source="llm", confidence=0.0, notes=[]),
            template_fallback_fn=fake_template)
        self.assertEqual(result.source, "template_fallback")
        self.assertEqual(calls["template"], 1)

    def test_llm_internal_fallback_not_called_twice(self):
        # llm_parser đã tự fallback bên trong (trả template_fallback):
        # runner không được gọi template lần thứ hai.
        calls = {"template": 0}

        def fake_template(*args, **kwargs):
            calls["template"] += 1
            return _template_result()

        result = parse_with_llm_first(
            **self.kwargs,
            llm_parse_fn=lambda *a, **k: _template_result(),
            template_fallback_fn=fake_template)
        self.assertEqual(result.source, "template_fallback")
        self.assertEqual(calls["template"], 0)

    def test_both_llm_and_template_fail_reraises(self):
        def fake_llm(*args, **kwargs):
            raise RuntimeError("LLM loi")

        def fake_template(*args, **kwargs):
            raise RuntimeError("Template cung loi")

        with self.assertRaises(RuntimeError):
            parse_with_llm_first(
                **self.kwargs, llm_parse_fn=fake_llm, template_fallback_fn=fake_template)


if __name__ == "__main__":
    unittest.main()

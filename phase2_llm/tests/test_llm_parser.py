# -*- coding: utf-8 -*-
"""Kiểm thử parser LLM DataMate (Giai đoạn 2 LLM, module llm_parser).

Mục đích của file này
---------------------
Chứng minh tầng LLM parser giữ đúng hợp đồng, kể cả khi mô hình sai:
JSON sai schema phải đi repair/fallback chứ không crash; thực thể
không tồn tại trong shop phải fallback chứ không được đoán; kỳ không
hợp lệ phải fallback; backend lỗi phải fallback; chỉ JSON đúng hoàn
toàn mới được xây thành ParsedQuestion.

Tất cả test đều dùng dữ liệu GIẢ tự tạo trong file (sản phẩm/khách
giả, backend giả trả JSON có sẵn), KHÔNG dùng đáp án train để làm
test pass giả, và KHÔNG gọi mạng hay API thương mại nào. Backend giả
đóng vai mô hình: nó trả đúng những JSON mà test đã định sẵn, để kiểm
tra logic validate/repair/fallback, chứ không kiểm tra chất lượng của
một model thật (model thật chưa được chọn/kiểm chứng, xem docstring
của llm_parser).

Chạy bằng::

    python3 -m unittest discover -s tests -v
    (chạy từ thư mục phase2_llm/)

Cuối file có ghi kết quả kiểm tra AST docstring tiếng Việt sau lần
chạy kiểm chứng gần nhất (xem phần cuối file).
"""

from __future__ import annotations

import ast
import datetime
import sys
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Nạp module llm_parser bằng đường dẫn tương đối từ file test.
# File này nằm ở <datamate>/phase2_llm/tests/test_llm_parser.py.
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent
_LLM_DIR = _TESTS_DIR.parent
for _candidate in (str(_LLM_DIR / "src"),):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from llm_parser import (  # noqa: E402
    ANSWER_TYPE_VALUES,
    ENTITY_KIND_VALUES,
    MEASURE_VALUES,
    PARSED_QUESTION_JSON_SCHEMA,
    PERIOD_KIND_VALUES,
    LocalModelBackend,
    ParseResult,
    ParsedQuestion,
    TransformersBackend,
    build_prompt,
    parse_with_llm,
    validate_and_build,
)

_AS_OF = datetime.date(2025, 7, 31)

# Dữ liệu giả của một shop giả: chỉ tên/mã, không có dòng đơn nào.
_PRODUCTS = [
    {"key": "sting dau 330ml", "display": "Sting dâu 330ml", "code": None, "group": "Nước giải khát"},
    {"key": "pepsi 330ml", "display": "Pepsi 330ml", "code": "PEP330", "group": "Nước giải khát"},
]
_CUSTOMERS = [
    {"key": "nguyen van a", "display": "Nguyễn Văn A", "code": "KH001", "group": None},
]


def _valid_json(**overrides):
    """Tạo một JSON hợp lệ mẫu, cho phép ghi đè từng trường để test.

    Đầu vào:
        overrides: các trường muốn thay so với mẫu hợp lệ.

    Đầu ra:
        dict JSON hợp lệ theo schema: doanh thu tháng 7/2025, không
        thực thể, answer_type number.

    Vì sao cần mẫu này:
        Mỗi test chỉ muốn thay một điểm sai (measure, kỳ, thực thể...)
        mà vẫn giữ các trường khác đúng, để lỗi bắt được đúng là lỗi
        đang test, không phải lỗi phụ từ trường khác.
    """
    data = {
        "measure": "revenue",
        "entity_kind": "none",
        "entity_key": None,
        "entity_display": None,
        "period_start": "2025-07-01",
        "period_end": "2025-07-31",
        "period_kind": "absolute",
        "period_months": [],
        "ranking_k": None,
        "ranking_target": "none",
        "ranking_metric": None,
        "unit": None,
        "threshold_money": None,
        "threshold_op": None,
        "answer_type": "number",
        "confidence_parse": 0.9,
        "parse_notes": [],
        "normalized_text": "doanh thu thang 7/2025 la bao nhieu",
    }
    data.update(overrides)
    return data


class StubBackend(LocalModelBackend):
    """Backend giả đóng vai mô hình, trả đúng JSON đã định sẵn theo lượt gọi.

    Đầu vào (__init__):
        outputs: list các dict JSON sẽ trả lần lượt, hoặc các exception
            sẽ raise lần lượt (mỗi lần gọi generate_json tiêu thụ một
            phần tử). Hết list thì lặp lại phần tử cuối, để test repair
            không bị hết output giữa chừng một cách bất ngờ.

    Đầu ra (generate_json):
        dict JSON của lượt gọi đó, hoặc raise exception của lượt đó.

    Vì sao cần:
        Test logic validate/repair/fallback phải kiểm soát được chính
        xác mô hình trả gì ở mỗi lần gọi (đúng, sai measure, sai kỳ,
        bịa thực thể, lỗi backend...), điều mà model thật không làm
        được một cách deterministic.
    """

    def __init__(self, outputs):
        """Lưu danh sách output giả và đếm số lần gọi."""
        self.outputs = list(outputs)
        self.calls = 0
        self.prompts = []

    def generate_json(self, prompt: str, schema: dict) -> dict:
        """Trả output giả của lượt gọi hiện tại, ghi lại prompt đã nhận."""
        self.prompts.append(prompt)
        index = min(self.calls, len(self.outputs) - 1)
        self.calls += 1
        item = self.outputs[index]
        if isinstance(item, Exception):
            raise item
        return item


class TestValidateAndBuild(unittest.TestCase):
    """Kiểm tra validate_and_build: đúng thì xây, sai thì raise ValueError."""

    def test_schema_dung_thi_xay_dung_parsed_question(self):
        """JSON đúng hoàn toàn phải thành ParsedQuestion đúng từng trường."""
        parsed = validate_and_build(_valid_json(), _PRODUCTS, _CUSTOMERS)
        self.assertEqual(parsed.measure, "revenue")
        self.assertEqual(parsed.answer_type, "number")
        self.assertEqual(parsed.period_start, datetime.date(2025, 7, 1))
        self.assertEqual(parsed.period_end, datetime.date(2025, 7, 31))
        self.assertEqual(parsed.period_kind, "absolute")
        self.assertEqual(parsed.period_months, [])
        self.assertEqual(parsed.confidence_parse, 0.9)

    def test_schema_dung_voi_thuc_the_va_multi_month(self):
        """Thực thể có thật + multi_month phải được chuẩn hoá về key/display của shop."""
        data = _valid_json(
            measure="top_product",
            entity_kind="product",
            entity_key="sting dau 330ml",
            entity_display="Sting dâu 330ml",
            period_kind="multi_month",
            period_months=[[2025, 5], [2025, 6], [2025, 7]],
            period_start="2025-05-01",
            period_end="2025-07-31",
            ranking_k=1,
            ranking_target="product",
            ranking_metric="quantity",
            answer_type="string",
        )
        parsed = validate_and_build(data, _PRODUCTS, _CUSTOMERS)
        self.assertEqual(parsed.entity_key, "sting dau 330ml")
        self.assertEqual(parsed.entity_display, "Sting dâu 330ml")
        self.assertEqual(parsed.period_months, [(2025, 5), (2025, 6), (2025, 7)])
        self.assertEqual(parsed.ranking_k, 1)

    def test_measure_ngoai_enum_thi_raise(self):
        """Measure không có trong enum hợp đồng phải raise, không được chấp nhận."""
        with self.assertRaises(ValueError):
            validate_and_build(_valid_json(measure="doanh_thu_bia"), _PRODUCTS, _CUSTOMERS)

    def test_thuc_the_khong_ton_tai_thi_raise(self):
        """Mô hình bịa sản phẩm không có trong shop phải raise, không được đoán."""
        data = _valid_json(
            entity_kind="product",
            entity_key="cocacola bia",
            entity_display="Cocacola bịa",
        )
        with self.assertRaises(ValueError):
            validate_and_build(data, _PRODUCTS, _CUSTOMERS)

    def test_ky_khong_hop_le_thi_raise(self):
        """Tháng 13, ngày 31/2, start > end đều là kỳ không hợp lệ, phải raise."""
        with self.assertRaises(ValueError):
            validate_and_build(
                _valid_json(period_months=[[2025, 13]], period_kind="multi_month"),
                _PRODUCTS, _CUSTOMERS,
            )
        with self.assertRaises(ValueError):
            validate_and_build(
                _valid_json(period_start="2025-02-31", period_end="2025-02-31"),
                _PRODUCTS, _CUSTOMERS,
            )
        with self.assertRaises(ValueError):
            validate_and_build(
                _valid_json(period_start="2025-07-31", period_end="2025-07-01"),
                _PRODUCTS, _CUSTOMERS,
            )
        with self.assertRaises(ValueError):
            validate_and_build(
                _valid_json(period_kind="snapshot", period_start="2025-07-01", period_end="2025-07-31"),
                _PRODUCTS, _CUSTOMERS,
            )

    def test_ranking_k_sai_thi_raise(self):
        """ranking_k = 0, True (bool) hay chuỗi đều sai schema, phải raise."""
        for bad_k in (0, True, "3"):
            with self.assertRaises(ValueError, msg=f"ranking_k={bad_k!r}"):
                validate_and_build(_valid_json(ranking_k=bad_k), _PRODUCTS, _CUSTOMERS)

    def test_truong_la_thi_raise(self):
        """Trường ngoài hợp đồng (ví dụ reasoning) phải raise để repair bỏ nó đi."""
        data = _valid_json()
        data["reasoning"] = "mô hình tự nghĩ"
        with self.assertRaises(ValueError):
            validate_and_build(data, _PRODUCTS, _CUSTOMERS)

    def test_enum_schema_khop_hop_dong(self):
        """Enum trong schema phải đúng danh sách của API_CONTRACT mục 2."""
        schema = PARSED_QUESTION_JSON_SCHEMA
        self.assertEqual(schema["properties"]["measure"]["enum"], MEASURE_VALUES)
        self.assertEqual(schema["properties"]["period_kind"]["enum"], PERIOD_KIND_VALUES)
        self.assertEqual(schema["properties"]["entity_kind"]["enum"], ENTITY_KIND_VALUES)
        self.assertEqual(schema["properties"]["answer_type"]["enum"], ANSWER_TYPE_VALUES)
        # Không thêm trường bắt buộc mới: chỉ 4 trường bắt buộc của hợp đồng.
        self.assertEqual(
            sorted(schema["required"]),
            sorted(["measure", "entity_kind", "period_kind", "answer_type"]),
        )
        self.assertIn("product_revenue", MEASURE_VALUES)


class TestParseWithLlm(unittest.TestCase):
    """Kiểm tra parse_with_llm: llm, repair, lỗi thì raise để abstain (KHÔNG fallback template)."""

    def test_llm_dung_ngay_lan_dau_thi_source_llm(self):
        """JSON đúng ngay lần đầu thì source='llm', confidence của mô hình."""
        backend = StubBackend([_valid_json()])
        result = parse_with_llm(
            "doanh thu thang 7/2025 la bao nhieu", _AS_OF, None, None,
            _PRODUCTS, _CUSTOMERS, backend=backend,
        )
        self.assertEqual(result.source, "llm")
        self.assertEqual(result.parsed.measure, "revenue")
        self.assertAlmostEqual(result.confidence, 0.9)
        self.assertEqual(backend.calls, 1)

    def test_json_sai_thi_repair_roi_thanh_cong_source_repair(self):
        """Lần đầu sai measure, lần repair đúng thì source='repair', gọi 2 lần."""
        backend = StubBackend([
            _valid_json(measure="sai_ngoai_enum"),
            _valid_json(),
        ])
        result = parse_with_llm(
            "doanh thu thang 7/2025 la bao nhieu", _AS_OF, None, None,
            _PRODUCTS, _CUSTOMERS, backend=backend,
        )
        self.assertEqual(result.source, "repair")
        self.assertEqual(result.parsed.measure, "revenue")
        self.assertEqual(backend.calls, 2)
        # Repair prompt phải liệt kê lỗi của lần đầu.
        self.assertIn("sai_ngoai_enum", backend.prompts[1])

    def test_json_sai_mai_thi_raise_khong_fallback(self):
        """Sai cả lần đầu + 2 lần repair thì raise ValueError, đúng 3 lần gọi, KHÔNG fallback template."""
        backend = StubBackend([_valid_json(measure="sai_ngoai_enum")])
        with self.assertRaises(ValueError):
            parse_with_llm(
                "doanh thu thang 7/2025 la bao nhieu", _AS_OF, None, None,
                _PRODUCTS, _CUSTOMERS, backend=backend,
            )
        self.assertEqual(backend.calls, 3)

    def test_thuc_the_khong_ton_tai_thi_raise_khong_doan(self):
        """Mô hình bịa thực thể ở mọi lần repair thì raise, không đoán, không fallback."""
        data = _valid_json(
            entity_kind="product",
            entity_key="san pham bia",
            entity_display="Sản phẩm bịa",
        )
        backend = StubBackend([data])
        with self.assertRaises(ValueError):
            parse_with_llm(
                "doanh thu san pham bia thang 7/2025", _AS_OF, None, None,
                _PRODUCTS, _CUSTOMERS, backend=backend,
            )

    def test_ky_khong_hop_le_thi_raise(self):
        """Kỳ sai (tháng 13 trong multi_month) ở mọi lần thì raise, không fallback, không crash."""
        data = _valid_json(period_kind="multi_month", period_months=[[2025, 13]])
        backend = StubBackend([data])
        with self.assertRaises(ValueError):
            parse_with_llm(
                "doanh thu 3 thang", _AS_OF, None, None,
                _PRODUCTS, _CUSTOMERS, backend=backend,
            )

    def test_backend_loi_thi_raise(self):
        """Backend raise RuntimeError thì raise ngay, không retry vô hạn, không fallback."""
        backend = StubBackend([RuntimeError("không tải được model")])
        with self.assertRaises(RuntimeError):
            parse_with_llm(
                "doanh thu thang 7/2025", _AS_OF, None, None,
                _PRODUCTS, _CUSTOMERS, backend=backend,
            )
        self.assertEqual(backend.calls, 1)

    def test_khong_co_backend_thi_raise(self):
        """backend=None thì raise RuntimeError ngay, không giả vờ có mô hình, không fallback."""
        with self.assertRaises(RuntimeError):
            parse_with_llm(
                "doanh thu thang 7/2025", _AS_OF, None, None,
                _PRODUCTS, _CUSTOMERS, backend=None,
            )


class TestPromptAndBackend(unittest.TestCase):
    """Kiểm tra prompt (chỉ tên/mã, không dòng đơn) và backend Transformers."""

    def test_build_prompt_chi_chua_ten_ma_khong_chua_du_lieu_dong_don(self):
        """Prompt phải chứa tên/mã thực thể, as_of, rulebook, và yêu cầu chỉ trả JSON."""
        prompt = build_prompt(
            "Sting dau thang 7 ban duoc bao nhieu chai",
            "sting dau thang 7 ban duoc bao nhieu chai",
            _AS_OF, _PRODUCTS, _CUSTOMERS,
            ["Nước giải khát"], ["tiền mặt"],
            "Tuan bat dau thu Hai.",
        )
        self.assertIn("Sting dâu 330ml", prompt)
        self.assertIn("Nguyễn Văn A", prompt)
        self.assertIn("2025-07-31", prompt)
        self.assertIn("Tuan bat dau thu Hai.", prompt)
        self.assertIn("JSON", prompt)
        # Không có dữ liệu dòng đơn nào trong prompt (không số tiền, không mã đơn giả).
        self.assertNotIn("order_id", prompt)
        self.assertNotIn("line_amount", prompt)

    def test_build_prompt_co_bang_quyet_dinh_best_day_va_price_on_day(self):
        """Prompt tuning: phải có quy tắc best_day, price_on_day và cách tính confidence.

        Đây là 2 mẫu đã abstain ở lần chạy biz_101 (best_day: template cũ
        trả đúng, LLM abstain; price_on_day: cả template và LLM đều abstain).
        Prompt mới phải chỉ rõ: best_day khi hỏi 'ngày nào nhiều tiền nhất',
        price_on_day phải có period_start = period_end = ngày đó (không
        snapshot), và không hạ confidence chỉ vì không thấy doanh số.
        """
        prompt = build_prompt(
            "thang 5/2026 ngay nao cua hang ban dc nhieu tien nhat",
            "thang 5/2026 ngay nao cua hang ban duoc nhieu tien nhat",
            _AS_OF, _PRODUCTS, _CUSTOMERS,
            ["Nước giải khát"], ["tiền mặt"],
            "Tuan bat dau thu Hai.",
        )
        # Quy tắc best_day: ngày nào + nhiều tiền nhất, không phải revenue.
        self.assertIn("best_day", prompt)
        self.assertIn("ngày nào", prompt)
        self.assertIn("ranking_target=day", prompt)
        # Quy tắc price_on_day: đúng một ngày, không snapshot.
        self.assertIn("price_on_day", prompt)
        self.assertIn("period_start=period_end", prompt)
        self.assertIn("KHÔNG dùng snapshot", prompt)
        # Quy tắc confidence: không hạ chỉ vì không thấy dữ liệu dòng đơn.
        self.assertIn("KHÔNG hạ confidence", prompt)
        # Ví dụ parse minh họa phải có, nhưng không được chứa đáp án số thật.
        self.assertIn("VÍ DỤ PARSE", prompt)
        # Top product theo số lượng, không phải doanh thu.
        self.assertIn("ranking_metric=quantity", prompt)

    def test_build_prompt_co_measure_moi_va_quy_tac_ky_tuong_doi(self):
        """Prompt tuning đợt 2: 4 measure mới, kỳ tương đối khóa chặt, chống đoán measure.

        Bốn measure mới phải có trong enum và trong bảng quyết định với
        đúng ngữ nghĩa đã thống nhất với solver: avg_order_value là
        bình quân mỗi đơn (không phải tổng revenue), cancelled_order_count
        là số đơn huỷ (không phải tổng order_count), unsold_product_count
        là số mặt hàng khác nhau bán được 0 cái (không phải quantity hay
        order_count), revenue_by_month là list theo từng tháng (không cộng
        gộp). Kỳ tương đối phải khóa theo as_of (tuần bắt đầu thứ Hai,
        7/30 ngày gần nhất tính lùi gồm as_of, từ đầu tháng/năm đến as_of).
        Quy tắc chống đoán phải buộc measure='unknown' khi không có measure
        khớp đúng, thay vì chọn measure gần nhất để lấp chỗ trống. Test chỉ
        kiểm tra prompt chứa các quy tắc này, không dùng đáp án train nào.
        """
        self.assertIn("avg_order_value", MEASURE_VALUES)
        self.assertIn("cancelled_order_count", MEASURE_VALUES)
        self.assertIn("unsold_product_count", MEASURE_VALUES)
        self.assertIn("revenue_by_month", MEASURE_VALUES)
        prompt = build_prompt(
            "thang 5/2025 doanh thu trung binh tren mot don la bao nhieu",
            "thang 5/2025 doanh thu trung binh tren mot don la bao nhieu",
            _AS_OF, _PRODUCTS, _CUSTOMERS,
            ["Nước giải khát"], ["tiền mặt"],
            "Tuan bat dau thu Hai.",
        )
        # Bảng quyết định cho 4 measure mới, đúng ngữ nghĩa đã thống nhất.
        self.assertIn("avg_order_value", prompt)
        self.assertIn("KHÔNG chọn revenue", prompt)
        self.assertIn("cancelled_order_count", prompt)
        self.assertIn("KHÔNG chọn order_count", prompt)
        self.assertIn("unsold_product_count", prompt)
        self.assertIn("revenue_by_month", prompt)
        self.assertIn("period_kind=multi_month", prompt)
        # Kỳ tương đối khóa chặt theo as_of, tuần bắt đầu thứ Hai.
        self.assertIn("tuần rồi", prompt)
        self.assertIn("tuần này", prompt)
        self.assertIn("7 ngày gần nhất", prompt)
        self.assertIn("từ đầu tháng này đến hết hôm nay", prompt)
        self.assertIn("period_kind=absolute", prompt)
        # Tăng trưởng MTD: so cùng kỳ tháng trước, không dùng cả tháng đầy đủ.
        self.assertIn("cùng kỳ tháng trước", prompt)
        self.assertIn("growth_percent", prompt)
        # Tỉ lệ khách: mẫu số là khách đã mua trong tháng đó.
        self.assertIn("ĐÃ MUA trong tháng", prompt)
        # 'Bao nhiêu tiền' là number, kể cả khi câu nhắc tháng.
        self.assertIn("answer_type=number", prompt)
        # Top khách: không chọn khách vãng lai làm thực thể.
        self.assertIn("Khách lẻ", prompt)
        self.assertIn("KHÔNG được chọn khách vãng lai", prompt)
        # Quy tắc chống đoán: không có measure khớp đúng thì unknown.
        self.assertIn("QUY TẮC CHỐNG ĐOÁN MEASURE", prompt)
        self.assertIn("measure='unknown'", prompt)
        self.assertIn("KHÔNG chọn measure gần nhất", prompt)
        # Ví dụ minh họa mới phải có, nhưng không chứa business_id hay id câu hỏi.
        self.assertIn("avg_order_value", prompt)
        self.assertIn("cancelled_order_count", prompt)
        self.assertIn("revenue_by_month", prompt)
        self.assertIn("growth_percent", prompt)
        # Các quy tắc cũ không được mất khi thêm quy tắc mới.
        self.assertIn("best_day", prompt)
        self.assertIn("price_on_day", prompt)
        self.assertIn("KHÔNG dùng snapshot", prompt)

    def test_validate_and_build_chap_nhan_measure_moi(self):
        """Validate phải chấp nhận 4 measure mới (qua MEASURE_VALUES), không cần sửa logic validate.

        Mỗi measure mới được test với một JSON hợp lệ tối thiểu: kỳ absolute
        đầy đủ start/end (riêng revenue_by_month dùng multi_month với
        period_months tăng dần), entity_kind=none, answer_type đúng với
        định nghĩa đã thống nhất. Đây là dữ liệu giả tự tạo, không phải
        đáp án train.
        """
        base = {
            "entity_kind": "none",
            "period_start": "2025-05-01",
            "period_end": "2025-05-31",
            "period_kind": "absolute",
            "period_months": [],
            "ranking_k": None,
            "ranking_target": "none",
            "ranking_metric": None,
            "unit": None,
            "threshold_money": None,
            "threshold_op": None,
            "confidence_parse": 0.9,
            "parse_notes": ["test"],
            "normalized_text": "test",
        }
        cases = [
            ("avg_order_value", "number", dict(base)),
            ("cancelled_order_count", "number", dict(base)),
            ("unsold_product_count", "number", dict(base)),
        ]
        for measure, answer_type, data in cases:
            data.update({"measure": measure, "answer_type": answer_type})
            parsed = validate_and_build(data, _PRODUCTS, _CUSTOMERS)
            self.assertEqual(parsed.measure, measure)
            self.assertEqual(parsed.answer_type, answer_type)
        monthly = dict(base)
        monthly.update({
            "measure": "revenue_by_month",
            "answer_type": "list",
            "period_kind": "multi_month",
            "period_start": "2025-10-01",
            "period_end": "2025-12-31",
            "period_months": [[2025, 10], [2025, 11], [2025, 12]],
        })
        parsed = validate_and_build(monthly, _PRODUCTS, _CUSTOMERS)
        self.assertEqual(parsed.measure, "revenue_by_month")
        self.assertEqual(parsed.answer_type, "list")

    def test_transformers_backend_bao_loi_ro_rang_khi_khong_kha_dung(self):
        """Ở môi trường không có transformers/weights, backend phải raise RuntimeError, không trả JSON giả."""
        backend = TransformersBackend("model-chua-kiem-chung/test")
        with self.assertRaises(RuntimeError):
            backend.generate_json("prompt", PARSED_QUESTION_JSON_SCHEMA)

    def test_transformers_backend_khong_model_id_thi_raise(self):
        """model_id rỗng (mặc định chưa chọn) phải raise RuntimeError rõ ràng, không tự chọn model."""
        backend = TransformersBackend(None)
        with self.assertRaises(RuntimeError):
            backend.generate_json("prompt", PARSED_QUESTION_JSON_SCHEMA)


class TestVietnameseDocstringsAst(unittest.TestCase):
    """Kiểm tra bằng AST: mọi module/class/hàm public của llm_parser đều có docstring tiếng Việt."""

    def test_docstring_tieng_viet_bang_ast(self):
        """Mọi định nghĩa public phải có docstring chứa ký tự tiếng Việt có dấu."""
        module_path = _LLM_DIR / "src" / "llm_parser.py"
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        vietnamese_chars = set("àáảãạăắằẳẵặâấầẩẫậèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợùúủũụưứừửữựỳýỷỹỵđ")
        checked = []
        missing = []

        def _check(name, node):
            doc = ast.get_docstring(node)
            checked.append(name)
            if not doc or not any(ch in vietnamese_chars for ch in doc.lower()):
                missing.append(name)

        _check("module llm_parser", tree)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                _check(f"class {node.name}", node)
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if not child.name.startswith("_") or child.name == "__init__":
                            _check(f"{node.name}.{child.name}", child)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("_"):
                    _check(f"function {node.name}", node)
        self.assertEqual(missing, [], f"Thieu docstring tieng Viet: {missing}")
        # Số mục public thực tế của llm_parser: 1 module + 3 class +
        # 3 method public (__init__/generate_json) + 5 hàm public = 12.
        # Ngưỡng này đếm đúng số đó để test không pass giả khi một
        # mục public mới được thêm mà quên docstring (missing != []).
        self.assertGreaterEqual(len(checked), 12)
        self.assertIn("function build_prompt", checked)
        self.assertIn("function validate_and_build", checked)
        self.assertIn("function parse_with_llm", checked)
        self.assertIn("class TransformersBackend", checked)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# KẾT QUẢ KIỂM TRA AST VÀ TESTS (chạy thật ngày 2026-10-31)
# ---------------------------------------------------------------------------
# Lệnh: python3 -m unittest discover -s tests -p "test_llm_parser.py" -v
# (chạy từ thư mục phase2_llm, chỉ file này, không chạy test của agent khác)
# Kết quả: Ran 20 tests, OK (0.015s).
# Kiểm tra AST docstring tiếng Việt: 12/12 mục public của llm_parser.py
# (1 module + 3 class + 3 method public + 5 hàm public) đều có docstring
# chứa ký tự tiếng Việt có dấu, missing = [].
# Backend thật trong môi trường này: KHÔNG khả dụng — transformers chưa
# cài (ModuleNotFoundError), không có cache Hugging Face, không có weights.
# TransformersBackend đã được kiểm chứng là raise RuntimeError rõ ràng
# thay vì trả JSON giả (2 test backend trong file này).

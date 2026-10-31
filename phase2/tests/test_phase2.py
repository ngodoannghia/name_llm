# -*- coding: utf-8 -*-
"""Kiểm thử tích hợp Giai đoạn 2 DataMate (parser, solver, gate, cache, runner).

Mục đích của file này
---------------------
Chứng minh các module rời rạc của Giai đoạn 2 chạy chung được với nhau
theo API_CONTRACT, thay vì mỗi module chỉ đúng khi chạy riêng:

- Parser: không dấu, viết tắt, thời gian, câu "nhất", dấu ``%`` trong
  tên thuốc không bị hiểu thành percent.
- Solver: bộ lọc doanh thu, công nợ và giá bán KHÁC NHAU (doanh thu
  loại huỷ/trả, công nợ loại huỷ và COD nhưng giữ đơn trả, giá ngày
  tính cả dòng huỷ/trả); xếp hạng sản phẩm theo số lượng, xếp hạng
  khách theo doanh thu.
- Gate/format: dưới ngưỡng thì abstain, sai format thì không ghi,
  shop không có mặt hàng mà câu cần mặt hàng thì unanswerable.
- Cache: lưu/nạp được, đổi nguồn thì vô hiệu (không trả view cũ).
- Runner: điền id thật vào entry (solver trả ``id=None``).

Tất cả test đều dùng dữ liệu GIẢ tự tạo trong file (hoặc một shop nhỏ
tự tạo), KHÔNG dùng đáp án train để làm test pass giả: test chỉ kiểm
tra luật tính (ví dụ doanh thu loại đơn huỷ), không kiểm tra một con
số đáp án train cụ thể nào. Mọi test phải pass thật, không skip giả.

Chạy bằng::

    python3 -m unittest discover -s tests -v
    (chạy từ thư mục phase2/)
"""

from __future__ import annotations

import datetime
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

# ---------------------------------------------------------------------------
# Nạp module phase2 và phase1 bằng đường dẫn tương đối từ file test.
# File này nằm ở <datamate>/phase2/tests/test_phase2.py.
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent
_PHASE2_DIR = _TESTS_DIR.parent
for _candidate in (str(_PHASE2_DIR / "src"), str(_PHASE2_DIR)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from cache import cache_path, load_shop_cache, save_shop_cache  # noqa: E402
from gate import GATE_THRESHOLD, decide, validate_entry_format  # noqa: E402
from parser import (ParsedQuestion, expand_abbreviations, parse_question,  # noqa: E402
                    parse_time)
from shop_view import build_shop_view  # noqa: E402
from solver import SolveResult, solve  # noqa: E402

import run_phase2  # noqa: E402

_LINE_COLUMNS = [
    "_order_id", "_date", "_status", "_status_norm", "_payment",
    "_customer_key", "_customer_display", "_product_key",
    "_product_display", "_product_group", "_qty", "_qty_base",
    "_unit_price", "_line_amount", "_cost_price", "_line_unit",
    "_discount", "_channel",
]
_ORDER_COLUMNS = [
    "_order_id", "_date", "_status", "_status_norm", "_payment",
    "_customer_key", "_customer_display", "_order_total",
    "_amount_paid", "_amount_due", "_channel",
]


def _make_view(lines_rows, orders_rows, products=None, customers=None,
               date_min=None, date_max=None, profile=None,
               conversion_products=None):
    """Tạo view giả theo duck-typing cho solver/gate/runner.

    Đầu vào là các dòng đã ở dạng cột chuẩn của ShopView; hàm chỉ đóng
    gói chúng thành DataFrame và object có cùng thuộc tính với ShopView
    thật. Dùng view giả để test không phụ thuộc vào ingest phase1 hay
    dữ liệu train, và để từng bộ lọc được kiểm tra riêng biệt.
    """
    lines = pd.DataFrame(lines_rows, columns=_LINE_COLUMNS)
    orders = pd.DataFrame(orders_rows, columns=_ORDER_COLUMNS)
    payments = pd.DataFrame(columns=["_order_id", "_date", "_amount"])
    return SimpleNamespace(
        business_id="biz_test",
        profile=profile,
        lines=lines,
        orders=orders,
        payments=payments,
        date_min=date_min,
        date_max=date_max,
        products=list(products or []),
        customers=list(customers or []),
        base_unit_by_product={},
        conversion_factor={},
        conversion_products=set(conversion_products or set()),
    )


def _line(order_id, date, status_norm, product_key, product_display,
          qty, qty_base, unit_price, line_amount, customer_key="KH1",
          customer_display="Khach A", payment="tien mat", cost=None):
    """Tạo một dòng bán giả với các cột chuẩn cần cho test solver."""
    return {
        "_order_id": order_id, "_date": date, "_status": status_norm,
        "_status_norm": status_norm, "_payment": payment,
        "_customer_key": customer_key, "_customer_display": customer_display,
        "_product_key": product_key, "_product_display": product_display,
        "_product_group": None, "_qty": qty, "_qty_base": qty_base,
        "_unit_price": unit_price, "_line_amount": line_amount,
        "_cost_price": cost, "_line_unit": None, "_discount": None,
        "_channel": None,
    }


def _order(order_id, date, status_norm, total, paid, due,
           customer_key="KH1", customer_display="Khach A",
           payment="chuyen khoan"):
    """Tạo một đơn giả với các cột chuẩn cần cho test công nợ/tỉ lệ huỷ."""
    return {
        "_order_id": order_id, "_date": date, "_status": status_norm,
        "_status_norm": status_norm, "_payment": payment,
        "_customer_key": customer_key, "_customer_display": customer_display,
        "_order_total": total, "_amount_paid": paid, "_amount_due": due,
        "_channel": None,
    }


def _parsed(**changes):
    """Tạo ParsedQuestion với các giá trị mặc định hợp lệ cho test solver."""
    base = {
        "measure": "revenue", "entity_kind": "none", "entity_key": None,
        "entity_display": None, "period_start": datetime.date(2026, 7, 1),
        "period_end": datetime.date(2026, 7, 31), "period_kind": "absolute",
        "period_months": [], "ranking_k": None, "ranking_target": "none",
        "ranking_metric": None, "unit": None, "threshold_money": None,
        "threshold_op": None, "answer_type": "number",
        "confidence_parse": 1.0, "parse_notes": [], "normalized_text": "",
    }
    base.update(changes)
    return ParsedQuestion(**base)


class TestParser(unittest.TestCase):
    """Kiểm thử parser: chuẩn hoá, thời gian, thực thể, xếp hạng."""

    def test_expand_abbreviations(self):
        """Viết tắt và không dấu được mở rộng đúng, từ đứng độc lập."""
        result = expand_abbreviations("dthu thang nay la bnhiu")
        self.assertIn("doanh thu", result)
        self.assertIn("bao nhieu", result)

    def test_parse_time_month_absolute(self):
        """Tháng có năm cho ra đúng cả tháng, không cắt theo as_of."""
        start, end, kind, months = parse_time(
            "doanh thu thang 7/2026", datetime.date(2026, 9, 7), None, None)
        self.assertEqual((start, end), (datetime.date(2026, 7, 1),
                                        datetime.date(2026, 7, 31)))
        self.assertEqual(kind, "absolute")
        self.assertEqual(months, [])

    def test_parse_time_snapshot(self):
        """Ảnh chụp 'đến hết ngày' có period_start=None, period_end là ngày đó."""
        start, end, kind, _months = parse_time(
            "cong no den het ngay 15/08/2026",
            datetime.date(2026, 9, 7), None, None)
        self.assertIsNone(start)
        self.assertEqual(end, datetime.date(2026, 8, 15))
        self.assertEqual(kind, "snapshot")

    def test_parse_question_no_diacritics_abbreviation(self):
        """Câu không dấu + viết tắt vẫn ra measure revenue và kỳ đúng."""
        parsed = parse_question(
            "dthu thang 7/2026 la bnhiu", datetime.date(2026, 9, 7),
            None, None, [], [])
        self.assertEqual(parsed.measure, "revenue")
        self.assertEqual(parsed.period_start, datetime.date(2026, 7, 1))
        self.assertEqual(parsed.period_end, datetime.date(2026, 7, 31))

    def test_parse_question_top_product_by_quantity(self):
        """Sản phẩm bán chạy nhất có ranking_metric='quantity', không phải revenue."""
        products = [
            {"key": "SP1", "display": "Sting dau 330ml", "code": "SP1",
             "group": None},
        ]
        parsed = parse_question(
            "thang 6/2025 hang nao ban chay nhat",
            datetime.date(2026, 6, 21), None, None, products, [])
        self.assertEqual(parsed.measure, "top_product")
        self.assertEqual(parsed.ranking_metric, "quantity")

    def test_parse_question_profit_product(self):
        """Hàng lời nhất được nhận diện là top_product theo profit."""
        parsed = parse_question(
            "thang 2/2025 mat hang nao loi nhat",
            datetime.date(2026, 5, 25), None, None, [], [])
        self.assertEqual(parsed.measure, "top_product")
        self.assertEqual(parsed.ranking_metric, "profit")

    def test_parse_question_top_customer_by_revenue(self):
        """Khách mua nhiều nhất xếp theo revenue, khác với sản phẩm."""
        parsed = parse_question(
            "thang 7/2025 khach hang nao mua nhieu nhat",
            datetime.date(2026, 7, 31), None, None, [], [])
        self.assertEqual(parsed.measure, "top_customer")
        self.assertEqual(parsed.ranking_metric, "revenue")

    def test_parse_question_best_day(self):
        """Ngày doanh thu cao nhất ra best_day và answer_type date."""
        parsed = parse_question(
            "ngay nao doanh thu cao nhat thang 7/2025",
            datetime.date(2026, 7, 31), None, None, [], [])
        self.assertEqual(parsed.measure, "best_day")
        self.assertEqual(parsed.answer_type, "date")
        self.assertEqual(parsed.ranking_metric, "revenue")

    def test_parse_question_percent_in_drug_name(self):
        """Dấu % trong tên thuốc không bị suy thành đáp án percent."""
        products = [
            {"key": "SP9", "display": "Otrivin 0,1% xit mui chai 10ml",
             "code": "SP9", "group": None},
        ]
        parsed = parse_question(
            "Thang 7/2025 doanh thu cua mat hang Otrivin 0,1% xit mui "
            "chai 10ml la bao nhieu?",
            datetime.date(2026, 7, 13), None, None, products, [])
        self.assertEqual(parsed.measure, "revenue")
        self.assertEqual(parsed.answer_type, "number")
        self.assertEqual(parsed.entity_kind, "product")

    def test_parse_question_bao_nhiu_no_false_entity(self):
        """Câu 'bao nhiu' không khớp nhầm khách có tên chứa 'bao'.

        'bao nhiu' mở rộng thành 'bao bao nhieu' (từ 'bao' gốc cộng
        từ 'bao' do 'nhiu' mở rộng thành); đoạn rút gọn phải gồm các
        token khác nhau nên 'bao bao' không được tính là tên rút gọn
        2 token của khách 'Cao Anh Bảo'. Câu hỏi tổng chung phải giữ
        entity_kind='none' và measure order_count.
        """
        customers = [
            {"key": "cao anh bao", "display": "Cao Anh Bảo",
             "code": None, "group": None},
        ]
        parsed = parse_question(
            "e so don hang quy 4/2025 la bao nhiu vay",
            datetime.date(2026, 8, 24), None, None, [], customers)
        self.assertEqual(parsed.entity_kind, "none")
        self.assertEqual(parsed.measure, "order_count")

    def test_parse_question_shortened_name_still_matches(self):
        """Tên rút gọn với token khác nhau vẫn khớp (không phá luật cũ)."""
        products = [
            {"key": "SP1", "display": "Sting dau 330ml", "code": "SP1",
             "group": None},
        ]
        parsed = parse_question(
            "thang 7/2025 sting dau ban duoc bao nhieu tien",
            datetime.date(2026, 7, 31), None, None, products, [])
        self.assertEqual(parsed.entity_kind, "product")
        self.assertEqual(parsed.entity_display, "Sting dau 330ml")


class TestShopViewDateFallback(unittest.TestCase):
    """Kiểm thử shop_view: cột 'Thời gian' (order_datetime) phải cho ra ngày."""

    def test_order_datetime_used_as_order_date(self):
        """Bảng chỉ có cột 'Thời gian' thì lines/orders vẫn có ngày, không mất kỳ."""
        frame = pd.DataFrame([
            {"Mã hóa đơn": "ORD1", "Thời gian": "05/07/2026",
             "Tên hàng": "San pham A", "Số lượng": 2,
             "Thành tiền": "20000", "Trạng thái": "xong"},
            {"Mã hóa đơn": "ORD1", "Thời gian": "05/07/2026",
             "Tên hàng": "San pham B", "Số lượng": 1,
             "Thành tiền": "15000", "Trạng thái": "xong"},
        ])
        ingest_result = SimpleNamespace(
            tables={"BangBan": frame}, profile=None)
        view = build_shop_view(ingest_result, "biz_test")
        self.assertEqual(len(view.lines), 2)
        self.assertTrue(all(
            value == datetime.date(2026, 7, 5)
            for value in view.lines["_date"].tolist()))
        self.assertEqual(view.date_min, datetime.date(2026, 7, 5))
        self.assertEqual(view.date_max, datetime.date(2026, 7, 5))
        self.assertEqual(len(view.orders), 1)

    def test_order_datetime_joined_from_separate_order_table(self):
        """Bảng dòng không có ngày, bảng đơn có 'Thời gian' thì dòng vẫn join được ngày.

        Đây đúng là hình dạng của shop chỉ có cột 'Thời gian' ở bảng
        đơn (bảng dòng chỉ có mã đơn + mặt hàng + số lượng + tiền):
        trước sửa đổi, bảng đơn bị loại vì thiếu order_date nên dòng
        mất ngày và mọi câu theo kỳ trả 0.
        """
        lines_frame = pd.DataFrame([
            {"Mã hóa đơn": "ORD1", "Tên hàng": "San pham A",
             "Số lượng": 2, "Thành tiền": "20000"},
        ])
        orders_frame = pd.DataFrame([
            {"Mã hóa đơn": "ORD1", "Thời gian": "06/07/2026",
             "Trạng thái": "xong", "Tổng tiền hàng": "20000"},
        ])
        ingest_result = SimpleNamespace(
            tables={"ChiTiet": lines_frame, "HoaDon": orders_frame},
            profile=None)
        view = build_shop_view(ingest_result, "biz_test")
        self.assertEqual(len(view.lines), 1)
        self.assertEqual(view.lines["_date"].tolist()[0],
                         datetime.date(2026, 7, 6))
        self.assertEqual(view.date_min, datetime.date(2026, 7, 6))


class TestSolverFilters(unittest.TestCase):
    """Kiểm thử solver: ba bộ lọc doanh thu, công nợ, giá bán phải khác nhau."""

    def test_revenue_excludes_cancelled_and_returned(self):
        """Doanh thu chỉ tính đơn completed/delivering, loại huỷ và trả."""
        view = _make_view(
            [
                _line("O1", datetime.date(2026, 7, 10), "completed",
                      "SP1", "San pham A", 10, 10, 1000, 10000),
                _line("O2", datetime.date(2026, 7, 11), "cancelled",
                      "SP1", "San pham A", 5, 5, 1000, 5000),
                _line("O3", datetime.date(2026, 7, 12), "returned",
                      "SP1", "San pham A", 3, 3, 1000, 3000),
            ],
            [_order("O1", datetime.date(2026, 7, 10), "completed",
                    10000, 10000, 0),
             _order("O2", datetime.date(2026, 7, 11), "cancelled",
                    5000, 0, 5000),
             _order("O3", datetime.date(2026, 7, 12), "returned",
                    3000, 3000, 0)],
            products=[{"key": "SP1", "display": "San pham A",
                       "code": "SP1", "group": None}],
            date_min=datetime.date(2026, 7, 1),
            date_max=datetime.date(2026, 7, 31))
        result = solve(_parsed(measure="revenue"), view, None, None)
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.entry["answer"], 10000)

    def test_cancel_rate_denominator_includes_all_orders(self):
        """Tỉ lệ huỷ: mẫu là mọi đơn trong kỳ, tử là đơn huỷ."""
        view = _make_view(
            [],
            [_order("O1", datetime.date(2026, 7, 10), "completed",
                    10000, 10000, 0),
             _order("O2", datetime.date(2026, 7, 11), "completed",
                    20000, 20000, 0),
             _order("O3", datetime.date(2026, 7, 12), "cancelled",
                    5000, 0, 5000),
             _order("O4", datetime.date(2026, 7, 13), "returned",
                    3000, 3000, 0)],
            date_min=datetime.date(2026, 7, 1),
            date_max=datetime.date(2026, 7, 31))
        result = solve(
            _parsed(measure="cancel_rate", answer_type="percent"),
            view, None, None)
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.entry["answer"], 25.0)

    def test_debt_snapshot_excludes_cancelled_and_cod(self):
        """Công nợ tại ngày snapshot: loại đơn huỷ và COD, giữ đơn trả còn nợ.

        Kỳ snapshot theo quy ước parser là period_start=None,
        period_end=ngày snapshot; solver phải hiểu đúng quy ước này
        (không tự gán start=end thành kỳ một ngày cho doanh thu, mà
        nợ thì tính mọi đơn đặt đến hết ngày snapshot).
        """
        view = _make_view(
            [],
            [_order("O1", datetime.date(2026, 8, 10), "completed",
                    10000, 3000, 7000),
             _order("O2", datetime.date(2026, 8, 11), "cancelled",
                    9000, 0, 9000),
             _order("O3", datetime.date(2026, 8, 12), "completed",
                    8000, 0, 8000, payment="COD"),
             _order("O4", datetime.date(2026, 8, 13), "returned",
                    5000, 2000, 3000)],
            date_min=datetime.date(2026, 8, 1),
            date_max=datetime.date(2026, 8, 31))
        parsed = _parsed(
            measure="debt_total", period_start=None,
            period_end=datetime.date(2026, 8, 15),
            period_kind="snapshot")
        result = solve(parsed, view, None, None)
        self.assertEqual(result.status, "answered")
        # Chỉ O1 (7000) + O4 đơn trả còn nợ (3000); O2 huỷ và O3 COD loại.
        self.assertEqual(result.entry["answer"], 10000)

    def test_price_on_day_includes_cancelled_lines(self):
        """Giá ngày tính CẢ dòng đơn huỷ/trả, khác bộ lọc doanh thu."""
        view = _make_view(
            [_line("O9", datetime.date(2026, 7, 20), "cancelled",
                   "SP1", "San pham A", 2, 2, 7000, 14000)],
            [_order("O9", datetime.date(2026, 7, 20), "cancelled",
                    14000, 0, 14000)],
            products=[{"key": "SP1", "display": "San pham A",
                       "code": "SP1", "group": None}],
            date_min=datetime.date(2026, 7, 1),
            date_max=datetime.date(2026, 7, 31))
        parsed = _parsed(
            measure="price_on_day", entity_kind="product",
            entity_key="SP1", entity_display="San pham A",
            period_start=datetime.date(2026, 7, 20),
            period_end=datetime.date(2026, 7, 20),
            period_kind="absolute")
        result = solve(parsed, view, None, None)
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.entry["answer"], 7000)
        # Cùng dòng đó mà hỏi doanh thu thì phải bằng 0 (đơn huỷ loại).
        revenue = solve(
            _parsed(measure="revenue", entity_kind="product",
                    entity_key="SP1", entity_display="San pham A",
                    period_start=datetime.date(2026, 7, 20),
                    period_end=datetime.date(2026, 7, 20),
                    period_kind="absolute"),
            view, None, None)
        self.assertEqual(revenue.entry["answer"], 0)

    def test_top_product_by_quantity_not_revenue(self):
        """Top product theo số lượng: A bán nhiều chai hơn thắng dù doanh thu thấp hơn."""
        view = _make_view(
            [_line("O1", datetime.date(2026, 7, 10), "completed",
                   "SP_A", "San pham A", 100, 100, 100, 10000),
             _line("O2", datetime.date(2026, 7, 11), "completed",
                   "SP_B", "San pham B", 10, 10, 5000, 50000)],
            [],
            products=[{"key": "SP_A", "display": "San pham A",
                       "code": "SP_A", "group": None},
                      {"key": "SP_B", "display": "San pham B",
                       "code": "SP_B", "group": None}],
            date_min=datetime.date(2026, 7, 1),
            date_max=datetime.date(2026, 7, 31))
        parsed = _parsed(
            measure="top_product", ranking_k=1, ranking_target="product",
            ranking_metric="quantity", answer_type="string")
        result = solve(parsed, view, None, None)
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.entry["answer"], "San pham A")

    def test_top_customer_by_revenue(self):
        """Top customer theo doanh thu: khách doanh thu cao hơn thắng dù mua ít đơn vị hơn."""
        view = _make_view(
            [_line("O1", datetime.date(2026, 7, 10), "completed",
                   "SP1", "San pham A", 100, 100, 100, 10000,
                   customer_key="KH_X", customer_display="Khach X"),
             _line("O2", datetime.date(2026, 7, 11), "completed",
                   "SP1", "San pham A", 1, 1, 50000, 50000,
                   customer_key="KH_Y", customer_display="Khach Y")],
            [],
            customers=[{"key": "KH_X", "display": "Khach X",
                        "code": "KH_X", "group": None},
                       {"key": "KH_Y", "display": "Khach Y",
                        "code": "KH_Y", "group": None}],
            date_min=datetime.date(2026, 7, 1),
            date_max=datetime.date(2026, 7, 31))
        parsed = _parsed(
            measure="top_customer", ranking_k=1, ranking_target="customer",
            ranking_metric="revenue", answer_type="string")
        result = solve(parsed, view, None, None)
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.entry["answer"], "Khach Y")

    def test_top_product_profit_uses_cost(self):
        """Hàng lời nhất theo lợi nhuận: doanh thu trừ giá vốn, không phải doanh thu thuần."""
        view = _make_view(
            [_line("O1", datetime.date(2026, 7, 10), "completed",
                   "SP_A", "San pham A", 10, 10, 10000, 100000, cost=9000),
             _line("O2", datetime.date(2026, 7, 11), "completed",
                   "SP_B", "San pham B", 10, 10, 5000, 50000, cost=1000)],
            [],
            products=[{"key": "SP_A", "display": "San pham A",
                       "code": "SP_A", "group": None},
                      {"key": "SP_B", "display": "San pham B",
                       "code": "SP_B", "group": None}],
            date_min=datetime.date(2026, 7, 1),
            date_max=datetime.date(2026, 7, 31))
        parsed = _parsed(
            measure="top_product", ranking_k=1, ranking_target="product",
            ranking_metric="profit", answer_type="string")
        result = solve(parsed, view, None, None)
        self.assertEqual(result.status, "answered")
        # A lãi 10000, B lãi 40000 -> B thắng dù doanh thu thấp hơn.
        self.assertEqual(result.entry["answer"], "San pham B")


class TestSolverNewMeasures(unittest.TestCase):
    """Kiểm thử 4 measure mới và loại khách vãng lai khỏi top_customer.

    Các test này dùng dữ liệu giả tự tạo, không dùng đáp án train:
    chúng chỉ kiểm tra luật tính (bình quân = tổng doanh thu / số đơn
    phân biệt, đơn huỷ đếm theo mã đơn, mặt hàng ế đếm theo mặt hàng,
    doanh thu từng tháng trả list theo tháng, khách vãng lai bị loại
    khỏi xếp hạng). Số liệu trong test là số tự chọn để tính tay được
    ngay, không phải số đáp án của bất kỳ câu hỏi train nào.
    """

    def _july_view(self, lines_rows, orders_rows=None, products=None,
                   customers=None):
        """Tạo view giả tháng 7/2026 cho các test measure mới."""
        return _make_view(
            lines_rows, orders_rows or [],
            products=products, customers=customers,
            date_min=datetime.date(2026, 7, 1),
            date_max=datetime.date(2026, 9, 30))

    def test_avg_order_value_excludes_cancelled_orders(self):
        """Bình quân mỗi đơn: tổng doanh thu hợp lệ / số đơn hợp lệ phân biệt.

        Doanh thu tháng 7 là 10000 + 20000 = 30000 trên 2 đơn hợp lệ
        (đơn huỷ 5000 bị loại khỏi cả tử và mẫu), nên bình quân là
        15000, không phải tổng doanh thu 30000 và không phải 30000/3.
        """
        view = self._july_view([
            _line("O1", datetime.date(2026, 7, 10), "completed",
                  "SP1", "San pham A", 1, 1, 10000, 10000),
            _line("O2", datetime.date(2026, 7, 11), "completed",
                  "SP1", "San pham A", 1, 1, 20000, 20000),
            _line("O3", datetime.date(2026, 7, 12), "cancelled",
                  "SP1", "San pham A", 1, 1, 5000, 5000),
        ])
        result = solve(
            _parsed(measure="avg_order_value"), view, None, None)
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.entry["type"], "number")
        self.assertEqual(result.entry["answer"], 15000)

    def test_avg_order_value_rounds_half_up_to_vnd(self):
        """Bình quân lẻ thì làm tròn về đồng, kiểu nửa lên, như mọi đáp án tiền.

        Tổng 10001 trên 2 đơn = 5000,5 đồng, làm tròn nửa lên thành
        5001 (không phải 5000 kiểu làm tròn về số chẵn của round()).
        """
        view = self._july_view([
            _line("O1", datetime.date(2026, 7, 10), "completed",
                  "SP1", "San pham A", 1, 1, 5000, 5000),
            _line("O2", datetime.date(2026, 7, 11), "completed",
                  "SP1", "San pham A", 1, 1, 5001, 5001),
        ])
        result = solve(
            _parsed(measure="avg_order_value"), view, None, None)
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.entry["answer"], 5001)

    def test_avg_order_value_zero_orders_abstains(self):
        """Không có đơn hợp lệ nào thì bình quân không xác định, abstain, không trả 0."""
        view = self._july_view([
            _line("O3", datetime.date(2026, 7, 12), "cancelled",
                  "SP1", "San pham A", 1, 1, 5000, 5000),
        ])
        result = solve(
            _parsed(measure="avg_order_value"), view, None, None)
        self.assertEqual(result.status, "abstain")
        self.assertEqual(result.error_group, "filter")

    def test_cancelled_order_count_distinct_orders(self):
        """Đơn huỷ đếm theo mã đơn phân biệt, không đếm dòng và không đếm tổng đơn.

        Đơn O2 huỷ xuất hiện hai lần trong orders (do có hai dòng ghi
        chép) vẫn chỉ tính 1; cộng O3 huỷ nữa là 2, không phải tổng
        3 đơn trong kỳ và không phải tỉ lệ phần trăm.
        """
        view = self._july_view(
            [_line("O1", datetime.date(2026, 7, 10), "completed",
                   "SP1", "San pham A", 1, 1, 10000, 10000)],
            [_order("O1", datetime.date(2026, 7, 10), "completed",
                    10000, 10000, 0),
             _order("O2", datetime.date(2026, 7, 11), "cancelled",
                    5000, 0, 5000),
             _order("O2", datetime.date(2026, 7, 11), "cancelled",
                    5000, 0, 5000),
             _order("O3", datetime.date(2026, 7, 12), "cancelled",
                    3000, 0, 3000)])
        result = solve(
            _parsed(measure="cancelled_order_count"), view, None, None)
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.entry["answer"], 2)

    def test_cancelled_order_count_zero_when_status_recorded(self):
        """Shop có ghi trạng thái mà trong kỳ không có đơn huỷ thì trả 0 đúng nghĩa."""
        view = self._july_view(
            [],
            [_order("O1", datetime.date(2026, 7, 10), "completed",
                    10000, 10000, 0)])
        result = solve(
            _parsed(measure="cancelled_order_count"), view, None, None)
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.entry["answer"], 0)

    def test_cancelled_order_count_no_status_abstains(self):
        """Shop không có thông tin trạng thái thì không đoán đơn huỷ, abstain column."""
        view = self._july_view(
            [_line("O1", datetime.date(2026, 7, 10), "other",
                   "SP1", "San pham A", 1, 1, 10000, 10000)],
            [_order("O1", datetime.date(2026, 7, 10), "other",
                    10000, 10000, 0)])
        result = solve(
            _parsed(measure="cancelled_order_count"), view, None, None)
        self.assertEqual(result.status, "abstain")
        self.assertEqual(result.error_group, "column")

    def test_unsold_product_count_counts_products_not_lines(self):
        """Mặt hàng ế đếm theo mặt hàng, không đếm dòng hay đơn.

        Chỉ SP1 có dòng doanh thu trong kỳ, SP2 và SP3 không có dòng
        nào, nên đáp án là 2 — dù SP1 có thể có nhiều dòng, mỗi mặt
        hàng ế vẫn chỉ tính 1.
        """
        products = [
            {"key": "SP1", "display": "San pham A",
             "code": "SP1", "group": None},
            {"key": "SP2", "display": "San pham B",
             "code": "SP2", "group": None},
            {"key": "SP3", "display": "San pham C",
             "code": "SP3", "group": None},
        ]
        view = self._july_view(
            [_line("O1", datetime.date(2026, 7, 10), "completed",
                   "SP1", "San pham A", 1, 1, 10000, 10000),
             _line("O2", datetime.date(2026, 7, 11), "completed",
                   "SP1", "San pham A", 1, 1, 20000, 20000)],
            products=products)
        result = solve(
            _parsed(measure="unsold_product_count"), view, None, None)
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.entry["answer"], 2)

    def test_unsold_product_count_no_product_list_abstains(self):
        """Không có danh sách sản phẩm thì không biết mặt hàng nào tồn tại để đếm ế."""
        view = self._july_view([
            _line("O1", datetime.date(2026, 7, 10), "completed",
                  "SP1", "San pham A", 1, 1, 10000, 10000),
        ])
        result = solve(
            _parsed(measure="unsold_product_count"), view, None, None)
        self.assertEqual(result.status, "abstain")
        self.assertEqual(result.error_group, "column")

    def test_revenue_by_month_full_months_returns_list(self):
        """Doanh thu từng tháng: list theo tháng cũ đến mới, tháng không bán thì 0.

        Tháng 7 là 10000 + 20000 = 30000, tháng 8 là 50000, tháng 9
        không có dòng nào nên là 0; đáp án là list [30000, 50000, 0],
        không phải tổng gộp 80000.
        """
        view = self._july_view([
            _line("O1", datetime.date(2026, 7, 10), "completed",
                  "SP1", "San pham A", 1, 1, 10000, 10000),
            _line("O2", datetime.date(2026, 7, 11), "completed",
                  "SP1", "San pham A", 1, 1, 20000, 20000),
            _line("O3", datetime.date(2026, 8, 5), "completed",
                  "SP1", "San pham A", 1, 1, 50000, 50000),
        ])
        parsed = _parsed(
            measure="revenue_by_month",
            period_start=datetime.date(2026, 7, 1),
            period_end=datetime.date(2026, 9, 30),
            answer_type="list")
        result = solve(parsed, view, None, None)
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.entry["type"], "list")
        self.assertEqual(result.entry["answer"], [30000, 50000, 0])

    def test_revenue_by_month_uses_period_months(self):
        """Khi parser cho period_months thì dùng đúng danh sách tháng đó, theo thứ tự."""
        view = self._july_view([
            _line("O1", datetime.date(2026, 7, 10), "completed",
                  "SP1", "San pham A", 1, 1, 30000, 30000),
            _line("O2", datetime.date(2026, 8, 5), "completed",
                  "SP1", "San pham A", 1, 1, 50000, 50000),
        ])
        parsed = _parsed(
            measure="revenue_by_month",
            period_months=[(2026, 7), (2026, 8)],
            answer_type="list")
        result = solve(parsed, view, None, None)
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.entry["answer"], [30000, 50000])

    def test_revenue_by_month_partial_period_abstains(self):
        """Kỳ cắt giữa tháng thì không chia được thành các tháng đầy đủ, abstain time."""
        view = self._july_view([
            _line("O1", datetime.date(2026, 7, 10), "completed",
                  "SP1", "San pham A", 1, 1, 30000, 30000),
        ])
        parsed = _parsed(
            measure="revenue_by_month",
            period_start=datetime.date(2026, 7, 15),
            period_end=datetime.date(2026, 9, 30),
            answer_type="list")
        result = solve(parsed, view, None, None)
        self.assertEqual(result.status, "abstain")
        self.assertEqual(result.error_group, "time")

    def test_revenue_by_month_non_consecutive_months_abstains(self):
        """period_months không liên tiếp thì abstain time, không tự lấp tháng thiếu."""
        view = self._july_view([
            _line("O1", datetime.date(2026, 7, 10), "completed",
                  "SP1", "San pham A", 1, 1, 30000, 30000),
        ])
        parsed = _parsed(
            measure="revenue_by_month",
            period_months=[(2026, 7), (2026, 9)],
            answer_type="list")
        result = solve(parsed, view, None, None)
        self.assertEqual(result.status, "abstain")
        self.assertEqual(result.error_group, "time")

    def test_top_customer_excludes_walk_in(self):
        """Top khách loại khách vãng lai dù tổng gộp của chúng lớn nhất.

        Khách vãng lai 90000 (ghi là 'Khach le') phải bị loại, nên top
        2 là Khach A (50000) rồi Khach B (40000), không phải danh sách
        bắt đầu bằng khách vãng lai.
        """
        customers = [
            {"key": "KH1", "display": "Khach A",
             "code": "KH1", "group": None},
            {"key": "KL", "display": "Khach le",
             "code": "KL", "group": None},
            {"key": "KH2", "display": "Khach B",
             "code": "KH2", "group": None},
        ]
        view = self._july_view(
            [_line("O1", datetime.date(2026, 7, 10), "completed",
                   "SP1", "San pham A", 1, 1, 90000, 90000,
                   customer_key="KL", customer_display="Khach le"),
             _line("O2", datetime.date(2026, 7, 11), "completed",
                   "SP1", "San pham A", 1, 1, 50000, 50000,
                   customer_key="KH1", customer_display="Khach A"),
             _line("O3", datetime.date(2026, 7, 12), "completed",
                   "SP1", "San pham A", 1, 1, 40000, 40000,
                   customer_key="KH2", customer_display="Khach B")],
            customers=customers)
        parsed = _parsed(
            measure="top_customer", ranking_k=2,
            ranking_target="customer", ranking_metric="revenue",
            answer_type="list")
        result = solve(parsed, view, None, None)
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.entry["answer"], ["Khach A", "Khach B"])

    def test_top_customer_walk_in_variants_excluded(self):
        """Các biến thể tên khách vãng lai ('le', 'kl') cũng bị loại, không chỉ 'Khach le'."""
        view = self._july_view(
            [_line("O1", datetime.date(2026, 7, 10), "completed",
                   "SP1", "San pham A", 1, 1, 90000, 90000,
                   customer_key="le", customer_display="le"),
             _line("O2", datetime.date(2026, 7, 11), "completed",
                   "SP1", "San pham A", 1, 1, 80000, 80000,
                   customer_key="kl", customer_display="kl"),
             _line("O3", datetime.date(2026, 7, 12), "completed",
                   "SP1", "San pham A", 1, 1, 50000, 50000,
                   customer_key="KH1", customer_display="Khach A")],
            customers=[{"key": "KH1", "display": "Khach A",
                        "code": "KH1", "group": None}])
        parsed = _parsed(
            measure="top_customer", ranking_k=1,
            ranking_target="customer", ranking_metric="revenue",
            answer_type="string")
        result = solve(parsed, view, None, None)
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.entry["answer"], "Khach A")

    def test_top_customer_real_name_with_le_not_excluded(self):
        """Tên thật chứa từ 'lẻ' nhưng không khớp đúng dạng vãng lai thì không bị loại.

        'Le Van A' chuẩn hoá thành 'le van a', khác hẳn 'le', nên vẫn
        được xếp hạng bình thường.
        """
        view = self._july_view(
            [_line("O1", datetime.date(2026, 7, 10), "completed",
                   "SP1", "San pham A", 1, 1, 50000, 50000,
                   customer_key="KH3", customer_display="Le Van A")],
            customers=[{"key": "KH3", "display": "Le Van A",
                        "code": "KH3", "group": None}])
        parsed = _parsed(
            measure="top_customer", ranking_k=1,
            ranking_target="customer", ranking_metric="revenue",
            answer_type="string")
        result = solve(parsed, view, None, None)
        self.assertEqual(result.status, "answered")
        self.assertEqual(result.entry["answer"], "Le Van A")

    def test_top_customer_only_walk_in_abstains(self):
        """Chỉ còn khách vãng lai sau khi loại thì không còn nhóm nào, abstain như cũ."""
        view = self._july_view(
            [_line("O1", datetime.date(2026, 7, 10), "completed",
                   "SP1", "San pham A", 1, 1, 90000, 90000,
                   customer_key="kl", customer_display="kl")])
        parsed = _parsed(
            measure="top_customer", ranking_k=1,
            ranking_target="customer", ranking_metric="revenue",
            answer_type="string")
        result = solve(parsed, view, None, None)
        self.assertEqual(result.status, "abstain")
        self.assertEqual(result.error_group, "filter")


class TestGateAndFormat(unittest.TestCase):
    """Kiểm thử gate: ngưỡng, format, unanswerable."""

    def _gate_view(self, products=None, profile=None):
        """Tạo view cho gate: kỳ dữ liệu bao phủ kỳ hỏi để không bị unanswerable do kỳ."""
        return _make_view(
            [], [],
            products=products if products is not None else [
                {"key": "SP1", "display": "San pham A", "code": "SP1",
                 "group": None}],
            profile=profile,
            date_min=datetime.date(2025, 1, 1),
            date_max=datetime.date(2027, 1, 1))

    def test_below_threshold_becomes_abstain(self):
        """Confidence dưới ngưỡng 0,65 thì decide chuyển thành abstain."""
        self.assertEqual(GATE_THRESHOLD, 0.65)
        parsed = _parsed()
        view = self._gate_view()
        result = SolveResult(
            status="answered",
            entry={"id": "q_test", "type": "number", "answer": 123},
            confidence=0.5, error_group=None, evidence={})
        decided = decide(parsed, view, result, None)
        self.assertEqual(decided.status, "abstain")
        self.assertEqual(decided.entry["type"], "abstain")

    def test_wrong_format_becomes_abstain_format(self):
        """Đáp án number nhưng giá trị là chuỗi thì sai format, không được ghi."""
        parsed = _parsed()
        view = self._gate_view()
        result = SolveResult(
            status="answered",
            entry={"id": "q_test", "type": "number", "answer": "123"},
            confidence=1.0, error_group=None, evidence={})
        decided = decide(parsed, view, result, None)
        self.assertEqual(decided.status, "abstain")
        self.assertEqual(decided.error_group, "format")

    def test_validate_entry_format(self):
        """Validator format: số thật pass, chuỗi số fail, ngày sai tháng fail."""
        self.assertTrue(validate_entry_format(
            {"id": "q", "type": "number", "answer": 10}, "number")[0])
        self.assertFalse(validate_entry_format(
            {"id": "q", "type": "number", "answer": "10"}, "number")[0])
        self.assertFalse(validate_entry_format(
            {"id": "q", "type": "date", "answer": "2026-13"}, "date")[0])
        self.assertTrue(validate_entry_format(
            {"id": "q", "type": "table",
             "answer": {"columns": ["a", "b"], "rows": [["x", 1]]}},
            "table")[0])

    def test_unanswerable_when_shop_has_no_product(self):
        """Shop không có mặt hàng mà câu cần mặt hàng thì unanswerable, không phải abstain."""
        parsed = _parsed(
            measure="top_product", ranking_target="product",
            answer_type="string")
        profile = SimpleNamespace(
            has_product_detail=False, columns_unmapped=[],
            columns_mapped={}, parse_failures=0, rows_out=0, rows_in=0)
        view = self._gate_view(products=[], profile=profile)
        result = SolveResult(
            status="abstain", entry={"id": "q_test", "type": "abstain"},
            confidence=0.0, error_group="column", evidence={})
        decided = decide(parsed, view, result, None)
        self.assertEqual(decided.status, "unanswerable")
        self.assertEqual(decided.entry["type"], "unanswerable")


class TestCache(unittest.TestCase):
    """Kiểm thử cache: lưu/nạp, đổi nguồn thì vô hiệu, thiếu data_dir thì miss an toàn."""

    def test_save_load_roundtrip(self):
        """Lưu rồi nạp lại được đúng view (lines/orders/products)."""
        with tempfile.TemporaryDirectory() as tmp:
            shop_dir = Path(tmp) / "shop"
            shop_dir.mkdir()
            (shop_dir / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
            cache_dir = Path(tmp) / "cache"
            view = _make_view(
                [_line("O1", datetime.date(2026, 7, 10), "completed",
                       "SP1", "San pham A", 2, 2, 1000, 2000)],
                [_order("O1", datetime.date(2026, 7, 10), "completed",
                        2000, 2000, 0)],
                products=[{"key": "SP1", "display": "San pham A",
                           "code": "SP1", "group": None}],
                date_min=datetime.date(2026, 7, 1),
                date_max=datetime.date(2026, 7, 31))
            save_shop_cache(cache_dir, "biz_test", view, data_dir=shop_dir)
            self.assertTrue(cache_path(cache_dir, "biz_test").is_file())
            loaded = load_shop_cache(cache_dir, "biz_test", shop_dir)
            self.assertIsNotNone(loaded)
            self.assertEqual(len(loaded.lines), 1)
            self.assertEqual(len(loaded.orders), 1)
            self.assertEqual(loaded.products[0]["key"], "SP1")

    def test_source_change_invalidates_cache(self):
        """Đổi nội dung nguồn (đổi kích thước file) thì cache cũ vô hiệu, trả None."""
        with tempfile.TemporaryDirectory() as tmp:
            shop_dir = Path(tmp) / "shop"
            shop_dir.mkdir()
            source = shop_dir / "data.csv"
            source.write_text("a,b\n1,2\n", encoding="utf-8")
            cache_dir = Path(tmp) / "cache"
            view = _make_view([], [], date_min=None, date_max=None)
            save_shop_cache(cache_dir, "biz_test", view, data_dir=shop_dir)
            self.assertIsNotNone(
                load_shop_cache(cache_dir, "biz_test", shop_dir))
            source.write_text("a,b\n1,2\n3,4\n5,6\n", encoding="utf-8")
            self.assertIsNone(
                load_shop_cache(cache_dir, "biz_test", shop_dir))

    def test_save_without_data_dir_is_safe_miss(self):
        """Lưu mà không truyền data_dir thì lần nạp sau phải miss (không trả view cũ)."""
        with tempfile.TemporaryDirectory() as tmp:
            shop_dir = Path(tmp) / "shop"
            shop_dir.mkdir()
            (shop_dir / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
            cache_dir = Path(tmp) / "cache"
            view = _make_view([], [], date_min=None, date_max=None)
            save_shop_cache(cache_dir, "biz_test", view)
            self.assertIsNone(
                load_shop_cache(cache_dir, "biz_test", shop_dir))


class TestRunnerIntegration(unittest.TestCase):
    """Kiểm thử runner: điền id thật, import chéo parser/solver/gate không xung đột."""

    def test_solve_one_question_fills_real_id(self):
        """Runner điền id thật vào entry thay vì để id=None của solver."""
        view = _make_view(
            [_line("O1", datetime.date(2026, 7, 10), "completed",
                   "SP1", "San pham A", 10, 10, 1000, 10000)],
            [_order("O1", datetime.date(2026, 7, 10), "completed",
                    10000, 10000, 0)],
            products=[{"key": "SP1", "display": "San pham A",
                       "code": "SP1", "group": None}],
            date_min=datetime.date(2026, 7, 1),
            date_max=datetime.date(2026, 7, 31))
        question = {
            "id": "q_runner_test",
            "business_id": "biz_test",
            "question": "doanh thu thang 7/2026 la bao nhieu",
            "as_of_date": "2026-07-31",
        }
        entry, info = run_phase2.solve_one_question(
            question, view, None, None)
        self.assertEqual(entry["id"], "q_runner_test")
        self.assertEqual(entry["type"], "number")
        self.assertEqual(entry["answer"], 10000)
        self.assertEqual(info["status"], "answered")

    def test_solve_one_question_snapshot_debt(self):
        """Runner + solver xử lý đúng snapshot nợ (period_start=None)."""
        view = _make_view(
            [],
            [_order("O1", datetime.date(2026, 8, 10), "completed",
                    10000, 3000, 7000)],
            date_min=datetime.date(2026, 8, 1),
            date_max=datetime.date(2026, 8, 31))
        question = {
            "id": "q_debt_test",
            "business_id": "biz_test",
            "question": "cong no den het ngay 15/08/2026 la bao nhieu",
            "as_of_date": "2026-08-31",
        }
        entry, info = run_phase2.solve_one_question(
            question, view, None, None)
        self.assertEqual(entry["id"], "q_debt_test")
        # Nợ 7000 nếu pipeline hiểu đúng snapshot; nếu không hiểu thì
        # abstain (test này bắt buộc pass với 7000 vì luật đã rõ).
        self.assertEqual(entry["answer"], 7000)
        self.assertEqual(info["status"], "answered")


if __name__ == "__main__":
    unittest.main()

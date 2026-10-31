# -*- coding: utf-8 -*-
"""Kiểm thử ingest LLM DataMate (Giai đoạn 1 LLM, module llm_ingest).

Mục đích của file này
---------------------
Chứng minh tầng LLM của ingest giữ đúng hợp đồng trong
``phase1_llm/API_CONTRACT.md``, kể cả khi mô hình sai: JSON sai schema
phải bị loại/repair/fallback chứ không crash; semantic ngoài 37 khóa
của ``phase1/src/ingest.py: COLUMN_SYNONYMS`` phải bị loại; tên cột
không tồn tại trong DataFrame phải bị loại; map tiền/ngày/số lượng mà
mẫu không khớp (dưới 25% parse được) phải bị loại vào rejected; cột
toàn trống thì không bị loại; backend lỗi thì toàn bộ fallback
rule-based với ``sources='rule_fallback'``; README mâu thuẫn dữ liệu
thật thì dữ liệu thật thắng; header khó (dòng 3-4, cột nhiễu) thì
không crash và không map cột nhiễu thành semantic bắt buộc.

Tất cả test đều dùng dữ liệu GIẢ tự tạo trong file (DataFrame giả,
backend giả trả JSON có sẵn), KHÔNG dùng dữ liệu train thật, KHÔNG
dùng đáp án train để làm test pass giả, và KHÔNG gọi mạng hay API
thương mại nào. Backend giả đóng vai mô hình: nó trả đúng những JSON
mà test đã định sẵn, để kiểm tra logic validate/repair/fallback, chứ
không kiểm tra chất lượng của một model thật (model thật chưa được
chọn/kiểm chứng, ``DEFAULT_MODEL_ID`` cố tình là ``None``).

Import mềm dẻo (theo mẫu ``phase1/tests/test_generalization.py``):
mỗi tên trong contract được import riêng trong ``try/except``; nếu
``src/llm_ingest.py`` chưa tồn tại (Child 1 chưa xong) thì các test
phụ thuộc tên đó SKIP với lý do rõ ràng, thay vì fail giả hay crash
cả file. Khi Child 1 xong, coordinator chạy lại file này và các test
sẽ chạy thật thay vì skip.

Quy ước về định dạng đầu vào (theo contract mục 3-4):
- ``sample_rows`` là list các dict ``tên cột -> giá trị dạng chuỗi``
  (dạng records của DataFrame, tối đa 10 dòng).
- JSON phân loại bảng: ``{"table_role", "confidence", "evidence",
  "notes"}``; JSON map cột: ``{"mapping", "confidence", "evidence",
  "notes"}``; JSON README: ``{"table_roles", "status_vocabulary",
  "unit_notes", "date_notes", "conversion_table_location"}``.
- Kết quả phân loại/mapping có thể là dataclass hoặc dict; các helper
  ``_role_cua`` / ``_mapping_cua`` / ``_lay_truong`` chấp nhận cả hai
  dạng, vì contract chỉ cố định tên hàm và ý nghĩa, không cố định
  kiểu trả về là dataclass hay dict ở tầng validate.

Chạy bằng (từ thư mục ``phase1_llm/``)::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Nạp module llm_ingest bằng đường dẫn tương đối từ file test.
# File này nằm ở <datamate>/phase1_llm/tests/test_llm_ingest.py.
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent
_LLM_DIR = _TESTS_DIR.parent
for _candidate in (str(_LLM_DIR / "src"),):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

# Thư mục phase1/src để đối chiếu 37 khóa COLUMN_SYNONYMS (chỉ đọc).
_PHASE1_SRC = _LLM_DIR.parent / "phase1" / "src"
for _candidate in (str(_PHASE1_SRC),):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

try:
    import pandas as _pd
except ImportError:  # pragma: no cover - pandas là dependency của phase1
    _pd = None

# 37 semantic theo API_CONTRACT mục 3 (dùng để đối chiếu, không dùng
# để suy luận map cho shop nào; test còn đối chiếu với COLUMN_SYNONYMS
# thật của phase1 khi import được, xem TestContractSemantics).
_SEMANTICS_37 = [
    "order_id", "order_date", "order_datetime", "order_status",
    "payment_method", "customer_code", "customer_name", "customer_group",
    "product_code", "product_name", "product_group", "quantity",
    "quantity_converted", "unit", "from_unit", "base_unit",
    "conversion_factor", "unit_price", "line_amount", "cost_price",
    "order_total", "amount_paid", "amount_due", "discount",
    "supplier_code", "supplier_name", "employee_code", "employee_name",
    "branch", "lot_code", "expiry_date", "stock_quantity",
    "import_date", "delivery_date", "payment_date", "return_quantity",
    "return_amount",
]

# Các semantic kiểm chứng bằng mẫu tiền/ngày/số lượng (contract mục 5).
_MONEY_SEMANTICS = {
    "order_total", "line_amount", "unit_price", "cost_price",
    "amount_paid", "amount_due", "discount", "return_amount",
}
_DATE_SEMANTICS = {
    "order_date", "order_datetime", "expiry_date", "import_date",
    "delivery_date", "payment_date",
}
_QUANTITY_SEMANTICS = {
    "quantity", "quantity_converted", "stock_quantity", "return_quantity",
}

# Enum vai trò bảng cố định (contract mục 3, không tự thêm).
_TABLE_ROLES = [
    "order", "order_line", "product", "customer", "import",
    "inventory", "lot_expiry", "payment", "return", "expense",
    "conversion", "other",
]


def _import_ten(ten):
    """Import một tên từ ``llm_ingest``; trả ``None`` nếu module/tên chưa có.

    Đầu vào:
        ten: tên hàm/class/hằng cần import.

    Đầu ra:
        Đối tượng đó, hoặc ``None`` khi ``llm_ingest`` chưa tồn tại
        hoặc chưa định nghĩa tên đó. Test dùng tên nào thì skip khi
        tên đó là ``None`` (xem ``_yeu_cau``), không crash cả file.
    """
    try:
        import llm_ingest as _module
    except ImportError:
        return None
    return getattr(_module, ten, None)


# Import từng tên riêng, để thiếu tên nào chỉ skip test dùng tên đó.
LocalModelBackend = _import_ten("LocalModelBackend")
TransformersBackend = _import_ten("TransformersBackend")
DEFAULT_MODEL_ID = _import_ten("DEFAULT_MODEL_ID")
build_table_prompt = _import_ten("build_table_prompt")
build_mapping_prompt = _import_ten("build_mapping_prompt")
build_readme_prompt = _import_ten("build_readme_prompt")
validate_table_classification = _import_ten("validate_table_classification")
validate_column_mapping = _import_ten("validate_column_mapping")
validate_readme_notes = _import_ten("validate_readme_notes")
classify_table = _import_ten("classify_table")
suggest_mapping = _import_ten("suggest_mapping")
summarize_readme = _import_ten("summarize_readme")
run_llm_ingest_for_shop = _import_ten("run_llm_ingest_for_shop")

# 37 khóa thật của phase1 (chỉ đọc, để đối chiếu; thiếu thì None).
try:
    from ingest import COLUMN_SYNONYMS as _COLUMN_SYNONYMS
except ImportError:
    _COLUMN_SYNONYMS = None

# IngestResult/ShopProfile của phase1 để dựng shop giả cho test runner.
try:
    from ingest import IngestResult as _Phase1IngestResult
    from ingest import ShopProfile as _Phase1ShopProfile
except ImportError:
    _Phase1IngestResult = None
    _Phase1ShopProfile = None


def _yeu_cau(testcase, *ten_doi_tuong):
    """Skip test với lý do rõ ràng nếu một tên contract nào đó chưa có.

    Đầu vào:
        testcase: instance ``TestCase`` đang chạy (để gọi ``skipTest``).
        ten_doi_tuong: các đối tượng contract cần dùng trong test.

    Vì sao cần:
        Child 1 có thể chưa xong khi file test này chạy lần đầu; một
        test skip với lý do "llm_ingest chưa có X" là tín hiệu đúng
        cho coordinator, còn fail giả thì che mất tiến độ thật.
    """
    for doi_tuong in ten_doi_tuong:
        if doi_tuong is None:
            testcase.skipTest(
                "llm_ingest chua co ham/class contract tuong ung "
                "(Child 1 chua xong) - skip cho ro rang, khong fail gia"
            )
    if _pd is None:
        testcase.skipTest("pandas chua cai - khong dung duoc DataFrame gia")


def _role_cua(ket_qua):
    """Lấy ``table_role`` từ kết quả phân loại, dù là dataclass hay dict.

    Đầu vào:
        ket_qua: kết quả của ``classify_table`` /
            ``validate_table_classification``.

    Đầu ra:
        Chuỗi vai trò bảng, hoặc ``None`` nếu kết quả là ``None``.
    """
    if ket_qua is None:
        return None
    if isinstance(ket_qua, dict):
        return ket_qua.get("table_role")
    return getattr(ket_qua, "table_role", None)


def _mapping_cua(ket_qua):
    """Lấy dict ``semantic -> cột`` từ kết quả map, dù là dict hay object.

    Đầu vào:
        ket_qua: kết quả của ``validate_column_mapping`` /
            ``suggest_mapping`` (theo contract là dict).

    Đầu ra:
        Dict mapping; dict rỗng nếu kết quả là ``None``.
    """
    if ket_qua is None:
        return {}
    if isinstance(ket_qua, dict):
        # validate_column_mapping trả thẳng dict mapping; một số cài
        # đặt có thể bọc trong khóa "mapping" - chấp nhận cả hai.
        if "mapping" in ket_qua and isinstance(ket_qua["mapping"], dict):
            return ket_qua["mapping"]
        return ket_qua
    mapping = getattr(ket_qua, "mapping", None)
    return mapping if isinstance(mapping, dict) else {}


def _lay_truong(ket_qua, ten_truong, mac_dinh=None):
    """Lấy một trường từ kết quả, dù là dataclass hay dict.

    Đầu vào:
        ket_qua: kết quả cần đọc trường.
        ten_truong: tên trường cần lấy.
        mac_dinh: giá trị trả khi không có trường đó.

    Đầu ra:
        Giá trị trường, hoặc ``mac_dinh``.
    """
    if ket_qua is None:
        return mac_dinh
    if isinstance(ket_qua, dict):
        return ket_qua.get(ten_truong, mac_dinh)
    return getattr(ket_qua, ten_truong, mac_dinh)


def _json_phan_loai(role="order", **ghi_de):
    """Tạo JSON phân loại bảng hợp lệ mẫu, cho phép ghi đè từng trường.

    Đầu vào:
        role: vai trò bảng muốn test.
        ghi_de: các trường muốn thay so với mẫu hợp lệ.

    Đầu ra:
        Dict JSON đúng schema phân loại bảng của contract mục 3.
    """
    data = {
        "table_role": role,
        "confidence": 0.9,
        "evidence": ["ten bang", "header"],
        "notes": ["ghi chu mau"],
    }
    data.update(ghi_de)
    return data


def _json_mapping(mapping, **ghi_de):
    """Tạo JSON map cột hợp lệ mẫu với mapping cho trước.

    Đầu vào:
        mapping: dict ``semantic -> tên cột thật`` muốn test.
        ghi_de: các trường khác muốn thay (confidence, evidence...).

    Đầu ra:
        Dict JSON đúng schema map cột của contract mục 3.
    """
    data = {
        "mapping": dict(mapping),
        "confidence": 0.9,
        "evidence": ["header", "mau gia tri"],
        "notes": ["ghi chu mau"],
    }
    data.update(ghi_de)
    return data


def _df_don_hang_gia():
    """Tạo DataFrame đơn hàng giả (không phải dữ liệu train thật).

    Đầu ra:
        DataFrame với các cột: mã đơn, ngày bán, tên mặt hàng, số
        lượng, đơn giá, thành tiền - đủ để kiểm chứng mẫu tiền/ngày/
        số lượng đạt 100% parse được.
    """
    _yeu_cau_pandas()
    return _pd.DataFrame({
        "Mã đơn hàng": ["DH001", "DH002", "DH003", "DH004"],
        "Ngày bán": ["2025-01-02", "2025-01-03", "2025-01-04", "2025-01-05"],
        "Tên sản phẩm": ["Hàng A", "Hàng B", "Hàng C", "Hàng D"],
        "Số lượng": ["2", "3", "1", "5"],
        "Giá bán": ["10000", "20000", "15000", "30000"],
        "Thành tiền": ["20000", "60000", "15000", "150000"],
    })


def _yeu_cau_pandas():
    """Raise SkipTest nếu pandas chưa có (test cần DataFrame giả)."""
    if _pd is None:
        raise unittest.SkipTest("pandas chua cai - khong dung duoc DataFrame gia")


def _sample_rows_cua(df):
    """Chuyển DataFrame thành sample_rows dạng records toàn chuỗi.

    Đầu vào:
        df: DataFrame giả cần lấy mẫu.

    Đầu ra:
        List tối đa 10 dict ``tên cột -> giá trị dạng chuỗi`` (theo
        contract mục 4: sample_rows chỉ chuyển giá trị dạng chuỗi,
        không chuyển toàn bộ DataFrame vào prompt).
    """
    rows = []
    for _, row in df.head(10).iterrows():
        rows.append({str(col): ("" if row[col] is None else str(row[col]))
                     for col in df.columns})
    return rows


class _BackendGia:
    """Backend giả đóng vai mô hình, trả đúng JSON đã định sẵn theo lượt gọi.

    Đầu vào (__init__):
        outputs: list các dict JSON sẽ trả lần lượt, hoặc các exception
            sẽ raise lần lượt. Hết list thì lặp lại phần tử cuối, để
            test repair không bị hết output giữa chừng bất ngờ.

    Đầu ra (generate_json):
        Dict JSON của lượt gọi đó, hoặc raise exception của lượt đó.

    Vì sao cần:
        Test logic validate/repair/fallback phải kiểm soát chính xác
        mô hình trả gì ở mỗi lần gọi, điều model thật không làm được
        một cách deterministic. Lớp này kế thừa LocalModelBackend khi
        có, để cài đặt nào kiểm tra ``isinstance`` cũng chấp nhận.
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


# Kế thừa backend trừu tượng của contract khi có, để isinstance đúng.
if LocalModelBackend is not None:
    class BackendGia(_BackendGia, LocalModelBackend):
        """Backend giả kế thừa LocalModelBackend của contract (khi có)."""
else:
    BackendGia = _BackendGia


class BackendLoi(BackendGia):
    """Backend giả luôn raise RuntimeError, mô phỏng model không tải được."""

    def __init__(self):
        """Khởi tạo với một exception duy nhất, lặp lại mọi lần gọi."""
        super().__init__([RuntimeError("khong tai duoc model")])


class TestContractSemantics(unittest.TestCase):
    """Kiểm tra 37 semantic của contract khớp COLUMN_SYNONYMS của phase1."""

    def test_37_semantic_khop_column_synonyms_phase1(self):
        """Danh sách 37 khóa trong test phải đúng khóa của phase1 (chỉ đọc).

        Nếu phase1 chưa import được thì skip, vì đây là kiểm tra đối
        chiếu, không phải hành vi của llm_ingest.
        """
        if _COLUMN_SYNONYMS is None:
            self.skipTest("phase1/src/ingest.py chua import duoc")
        self.assertEqual(len(_SEMANTICS_37), 37)
        self.assertEqual(sorted(_SEMANTICS_37), sorted(_COLUMN_SYNONYMS.keys()))


class TestValidateTableClassification(unittest.TestCase):
    """Kiểm tra validate_table_classification: đúng thì qua, sai thì loại."""

    def test_json_dung_schema_thi_qua(self):
        """JSON đúng schema, role trong enum thì được chấp nhận, role giữ nguyên."""
        _yeu_cau(self, validate_table_classification)
        ket_qua = validate_table_classification(
            _json_phan_loai("order"), "ban_hang",
            ["Mã đơn hàng", "Ngày bán"],
        )
        self.assertIsNotNone(ket_qua)
        self.assertEqual(_role_cua(ket_qua), "order")

    def test_role_ngoai_enum_thi_bi_loai(self):
        """Role không có trong enum cố định thì bị loại (None hoặc ValueError).

        Contract ghi kiểu trả về là ``TableClassification | None`` nên
        ``None`` là dạng chuẩn; chấp nhận ``ValueError`` như dạng loại
        tương đương (theo mẫu phase2), nhưng không chấp nhận trả về
        một phân loại mang role ngoài enum.
        """
        _yeu_cau(self, validate_table_classification)
        try:
            ket_qua = validate_table_classification(
                _json_phan_loai("don_hang_bia"), "ban_hang",
                ["Mã đơn hàng", "Ngày bán"],
            )
        except ValueError:
            return
        self.assertIsNone(ket_qua)

    def test_json_thieu_truong_bat_buoc_thi_bi_loai(self):
        """JSON thiếu ``table_role`` thì bị loại, không được đoán role mặc định."""
        _yeu_cau(self, validate_table_classification)
        data = {"confidence": 0.9, "evidence": [], "notes": []}
        try:
            ket_qua = validate_table_classification(
                data, "ban_hang", ["Mã đơn hàng"])
        except (ValueError, KeyError, TypeError):
            return
        self.assertIsNone(ket_qua)


class TestValidateColumnMapping(unittest.TestCase):
    """Kiểm tra validate_column_mapping: schema, cột tồn tại, kiểm chứng mẫu."""

    def test_mapping_dung_va_mau_khop_thi_giu(self):
        """Map đúng semantic, cột tồn tại, mẫu tiền/ngày khớp 100% thì giữ đủ."""
        _yeu_cau(self, validate_column_mapping)
        df = _df_don_hang_gia()
        data = _json_mapping({
            "order_id": "Mã đơn hàng",
            "order_date": "Ngày bán",
            "unit_price": "Giá bán",
            "line_amount": "Thành tiền",
        })
        ket_qua = _mapping_cua(validate_column_mapping(
            data, list(df.columns), df))
        self.assertEqual(ket_qua.get("order_id"), "Mã đơn hàng")
        self.assertEqual(ket_qua.get("order_date"), "Ngày bán")
        self.assertEqual(ket_qua.get("unit_price"), "Giá bán")
        self.assertEqual(ket_qua.get("line_amount"), "Thành tiền")

    def test_semantic_ngoai_37_khoa_thi_bi_loai(self):
        """Semantic bịa ngoài 37 khóa thì bị loại, semantic đúng còn lại vẫn giữ."""
        _yeu_cau(self, validate_column_mapping)
        df = _df_don_hang_gia()
        data = _json_mapping({
            "gia_bia": "Giá bán",
            "unit_price": "Giá bán",
        })
        ket_qua = _mapping_cua(validate_column_mapping(
            data, list(df.columns), df))
        self.assertNotIn("gia_bia", ket_qua)
        self.assertEqual(ket_qua.get("unit_price"), "Giá bán")

    def test_cot_khong_ton_tai_trong_df_thi_bi_loai(self):
        """Tên cột không có trong DataFrame thì map đó bị loại ngay, không repair bừa."""
        _yeu_cau(self, validate_column_mapping)
        df = _df_don_hang_gia()
        data = _json_mapping({
            "unit_price": "Cột không tồn tại",
            "line_amount": "Thành tiền",
        })
        ket_qua = _mapping_cua(validate_column_mapping(
            data, list(df.columns), df))
        self.assertNotIn("unit_price", ket_qua)
        self.assertEqual(ket_qua.get("line_amount"), "Thành tiền")

    def test_tien_mau_khong_khop_duoi_25_phan_tram_thi_bi_loai(self):
        """Cột tiền mà chỉ 20% mẫu parse được số thì bị loại (ngưỡng contract 25%)."""
        _yeu_cau(self, validate_column_mapping)
        df = _pd.DataFrame({
            "Giá bán": ["abc", "xyz", "---", "không rõ", "10000"],
            "Thành tiền": ["20000", "60000", "15000", "150000", "90000"],
        })
        data = _json_mapping({
            "unit_price": "Giá bán",
            "line_amount": "Thành tiền",
        })
        ket_qua = _mapping_cua(validate_column_mapping(
            data, list(df.columns), df))
        self.assertNotIn("unit_price", ket_qua)
        self.assertEqual(ket_qua.get("line_amount"), "Thành tiền")

    def test_ngay_mau_khong_khop_duoi_25_phan_tram_thi_bi_loai(self):
        """Cột ngày mà chỉ 20% mẫu parse được ngày thì bị loại."""
        _yeu_cau(self, validate_column_mapping)
        df = _pd.DataFrame({
            "Ngày bán": ["không ngày", "abc", "---", "xyz", "2025-01-02"],
            "Mã đơn hàng": ["DH001", "DH002", "DH003", "DH004", "DH005"],
        })
        data = _json_mapping({
            "order_date": "Ngày bán",
            "order_id": "Mã đơn hàng",
        })
        ket_qua = _mapping_cua(validate_column_mapping(
            data, list(df.columns), df))
        self.assertNotIn("order_date", ket_qua)
        self.assertEqual(ket_qua.get("order_id"), "Mã đơn hàng")

    def test_so_luong_mau_khong_khop_duoi_25_phan_tram_thi_bi_loai(self):
        """Cột số lượng mà chỉ 20% mẫu parse được số thì bị loại."""
        _yeu_cau(self, validate_column_mapping)
        df = _pd.DataFrame({
            "Số lượng": ["abc", "xyz", "---", "không rõ", "5"],
            "Tên sản phẩm": ["Hàng A", "Hàng B", "Hàng C", "Hàng D", "Hàng E"],
        })
        data = _json_mapping({
            "quantity": "Số lượng",
            "product_name": "Tên sản phẩm",
        })
        ket_qua = _mapping_cua(validate_column_mapping(
            data, list(df.columns), df))
        self.assertNotIn("quantity", ket_qua)
        self.assertEqual(ket_qua.get("product_name"), "Tên sản phẩm")

    def test_cot_toan_trong_thi_khong_bi_loai(self):
        """Cột toàn trống có tỉ lệ mẫu là None nên không bị loại (giống phase1)."""
        _yeu_cau(self, validate_column_mapping)
        df = _pd.DataFrame({
            "Giá bán": [None, None, None],
            "Thành tiền": ["20000", "60000", "15000"],
        })
        data = _json_mapping({
            "unit_price": "Giá bán",
            "line_amount": "Thành tiền",
        })
        ket_qua = _mapping_cua(validate_column_mapping(
            data, list(df.columns), df))
        self.assertEqual(ket_qua.get("unit_price"), "Giá bán")
        self.assertEqual(ket_qua.get("line_amount"), "Thành tiền")


class TestSuggestMappingVaClassify(unittest.TestCase):
    """Kiểm tra suggest_mapping/classify_table qua backend giả và backend lỗi."""

    def test_suggest_mapping_dung_thi_tra_map_da_kiem_chung(self):
        """Backend trả map đúng thì suggest_mapping trả đúng map đó, không thêm cột lạ."""
        _yeu_cau(self, suggest_mapping)
        df = _df_don_hang_gia()
        backend = BackendGia([_json_mapping({
            "order_id": "Mã đơn hàng",
            "unit_price": "Giá bán",
        })])
        ket_qua = _mapping_cua(suggest_mapping(
            "ban_hang", df, "", ["order_id", "unit_price"], backend=backend))
        self.assertEqual(ket_qua.get("order_id"), "Mã đơn hàng")
        self.assertEqual(ket_qua.get("unit_price"), "Giá bán")

    def test_suggest_mapping_repair_sau_lan_sai_thi_thanh_cong(self):
        """Lần đầu sai semantic, lần repair đúng thì vẫn ra map đúng, tối đa 3 lần gọi.

        Contract mục 1 quy định repair tối đa 2 lần (1 lần đầu + 2 repair),
        nên backend chỉ được gọi tối đa 3 lần cho một lần suggest.
        """
        _yeu_cau(self, suggest_mapping)
        df = _df_don_hang_gia()
        backend = BackendGia([
            _json_mapping({"gia_bia": "Giá bán"}),
            _json_mapping({"unit_price": "Giá bán"}),
        ])
        ket_qua = _mapping_cua(suggest_mapping(
            "ban_hang", df, "", ["unit_price"], backend=backend))
        self.assertEqual(ket_qua.get("unit_price"), "Giá bán")
        self.assertLessEqual(backend.calls, 3)

    def test_backend_loi_thi_suggest_mapping_fallback_khong_crash(self):
        """Backend raise RuntimeError thì suggest_mapping trả dict rỗng, không crash."""
        _yeu_cau(self, suggest_mapping)
        df = _df_don_hang_gia()
        ket_qua = _mapping_cua(suggest_mapping(
            "ban_hang", df, "", ["unit_price"], backend=BackendLoi()))
        self.assertEqual(ket_qua, {})

    def test_backend_loi_thi_classify_table_fallback_khong_crash(self):
        """Backend raise RuntimeError thì classify_table vẫn trả role trong enum, không crash."""
        _yeu_cau(self, classify_table)
        df = _df_don_hang_gia()
        ket_qua = classify_table(
            "ban_hang", list(df.columns), _sample_rows_cua(df), "",
            backend=BackendLoi(),
        )
        self.assertIn(_role_cua(ket_qua), _TABLE_ROLES)

    def test_readme_mau_thuan_du_lieu_that_thi_du_lieu_thang(self):
        """README nói bảng là đơn hàng nhưng dữ liệu không có mã đơn/ngày/trạng thái.

        Dữ liệu giả chỉ có tên sản phẩm, giá, tồn kho (bảng hàng hóa/kho),
        backend vẫn trả role "order" theo README; kết quả cuối không được
        là "order" mà phải theo dữ liệu thật (other/product/inventory...).
        """
        _yeu_cau(self, classify_table)
        headers = ["Tên sản phẩm", "Giá bán", "Tồn kho"]
        sample_rows = [
            {"Tên sản phẩm": "Hàng A", "Giá bán": "10000", "Tồn kho": "50"},
            {"Tên sản phẩm": "Hàng B", "Giá bán": "20000", "Tồn kho": "30"},
        ]
        readme = "Bảng này là bảng đơn hàng của shop, ghi mọi đơn bán ra."
        backend = BackendGia([_json_phan_loai("order")])
        ket_qua = classify_table(
            "danh_muc", headers, sample_rows, readme, backend=backend)
        self.assertIn(_role_cua(ket_qua), _TABLE_ROLES)
        self.assertNotEqual(_role_cua(ket_qua), "order")

    def test_header_kho_va_cot_nhieu_thi_khong_crash_khong_map_nhieu(self):
        """DataFrame mô phỏng header dòng 3-4 + cột nhiễu thì không crash.

        Cột nhiễu "Ghi chú nhiễu" chứa chữ tự do, backend gợi ý nó làm
        cột tiền; kiểm chứng mẫu phải loại nó, trong khi cột tiền thật
        vẫn giữ. Cột "Unnamed: 0" (dư của header nhiều dòng) cũng không
        được map thành semantic bắt buộc nào.
        """
        _yeu_cau(self, suggest_mapping)
        df = _pd.DataFrame({
            "Unnamed: 0": ["", "", ""],
            "Mã đơn hàng": ["DH001", "DH002", "DH003"],
            "Ngày bán": ["2025-01-02", "2025-01-03", "2025-01-04"],
            "Ghi chú nhiễu": ["ghi chú dòng 1", "---", "xem thêm"],
            "Thành tiền": ["20000", "60000", "15000"],
        })
        backend = BackendGia([_json_mapping({
            "order_id": "Mã đơn hàng",
            "unit_price": "Ghi chú nhiễu",
            "line_amount": "Thành tiền",
        })])
        ket_qua = _mapping_cua(suggest_mapping(
            "ban_hang", df, "", ["order_id", "unit_price", "line_amount"],
            backend=backend))
        self.assertEqual(ket_qua.get("order_id"), "Mã đơn hàng")
        self.assertEqual(ket_qua.get("line_amount"), "Thành tiền")
        self.assertNotEqual(ket_qua.get("unit_price"), "Ghi chú nhiễu")
        self.assertNotIn("Unnamed: 0", set(ket_qua.values()))

    def test_dataframe_rong_thi_suggest_mapping_khong_crash(self):
        """DataFrame có cột nhưng không có dòng thì suggest_mapping không crash, trả dict."""
        _yeu_cau(self, suggest_mapping)
        df = _pd.DataFrame({
            "Mã đơn hàng": [],
            "Giá bán": [],
        })
        backend = BackendGia([_json_mapping({"order_id": "Mã đơn hàng"})])
        ket_qua = _mapping_cua(suggest_mapping(
            "ban_hang", df, "", ["order_id"], backend=backend))
        self.assertIsInstance(ket_qua, dict)


class TestRunLlmIngestForShop(unittest.TestCase):
    """Kiểm tra run_llm_ingest_for_shop: backend lỗi thì toàn bộ rule_fallback."""

    def test_backend_loi_thi_toan_bo_rule_fallback_khong_crash(self):
        """Backend lỗi ở mọi bảng thì sources đều là 'rule_fallback', không crash.

        Shop giả dựng từ IngestResult của phase1 (khi import được) với
        một bảng đơn hàng giả; fallback_count phải > 0 và không có
        added_mappings nào từ LLM lỗi. Nếu phase1 không import được thì
        dùng SimpleNamespace tối thiểu (business_id + tables).
        """
        _yeu_cau(self, run_llm_ingest_for_shop)
        df = _df_don_hang_gia()
        if _Phase1IngestResult is not None and _Phase1ShopProfile is not None:
            profile = _Phase1ShopProfile(business_id="shop_gia")
            shop = _Phase1IngestResult(
                business_id="shop_gia", tables={"ban_hang": df},
                profile=profile)
        else:
            from types import SimpleNamespace
            shop = SimpleNamespace(business_id="shop_gia",
                                    tables={"ban_hang": df})
        ket_qua = run_llm_ingest_for_shop(shop, "", backend=BackendLoi())
        sources = _lay_truong(ket_qua, "sources", {})
        self.assertIsInstance(sources, dict)
        self.assertTrue(sources, "sources khong duoc rong khi co bang can xu ly")
        for bang, nguon in sources.items():
            self.assertEqual(nguon, "rule_fallback",
                             f"bang {bang} phai fallback khi backend loi")
        fallback_count = _lay_truong(ket_qua, "fallback_count", None)
        if fallback_count is not None:
            self.assertGreater(fallback_count, 0)
        added = _lay_truong(ket_qua, "added_mappings", {})
        self.assertEqual(added, {})


class TestPromptVaBackend(unittest.TestCase):
    """Kiểm tra prompt (cắt README 4000 ký tự) và backend Transformers."""

    def test_build_table_prompt_chua_ten_bang_va_header(self):
        """Prompt phân loại bảng phải chứa tên bảng và header, không chứa toàn bộ DataFrame."""
        _yeu_cau(self, build_table_prompt)
        df = _df_don_hang_gia()
        prompt = build_table_prompt(
            "ban_hang", list(df.columns), _sample_rows_cua(df), "")
        self.assertIn("ban_hang", prompt)
        self.assertIn("Mã đơn hàng", prompt)

    def test_build_mapping_prompt_chua_semantic_chua_map(self):
        """Prompt map cột phải liệt kê semantic còn trống để LLM chỉ gợi ý chỗ đó."""
        _yeu_cau(self, build_mapping_prompt)
        df = _df_don_hang_gia()
        prompt = build_mapping_prompt(
            "ban_hang", list(df.columns), _sample_rows_cua(df), "",
            ["unit_price", "cost_price"])
        self.assertIn("unit_price", prompt)
        self.assertIn("cost_price", prompt)

    def test_build_readme_prompt_cat_4000_ky_tu_dau(self):
        """README dài hơn 4000 ký tự thì phần sau ký tự 4000 không được vào prompt.

        Đặt một marker duy nhất sau vị trí 4000; prompt đúng contract
        (cắt tối đa 4000 ký tự đầu) sẽ không chứa marker đó.
        """
        _yeu_cau(self, build_readme_prompt)
        marker = "MARKER_SAU_4000_KY_TU_DUY_NHAT"
        readme = "x" * 4000 + marker + "y" * 100
        prompt = build_readme_prompt(readme, ["ban_hang"])
        self.assertNotIn(marker, prompt)
        self.assertIn("ban_hang", prompt)

    def test_validate_readme_notes_dung_thi_qua_sai_thi_loai(self):
        """JSON README đúng schema thì qua; table_roles chứa role ngoài enum thì loại."""
        _yeu_cau(self, validate_readme_notes)
        data_dung = {
            "table_roles": {"ban_hang": "order"},
            "status_vocabulary": ["Hoàn thành"],
            "unit_notes": ["đơn vị lẻ"],
            "date_notes": ["ngày bán"],
            "conversion_table_location": None,
        }
        self.assertIsNotNone(validate_readme_notes(data_dung))
        data_sai = dict(data_dung)
        data_sai["table_roles"] = {"ban_hang": "role_bia"}
        try:
            ket_qua = validate_readme_notes(data_sai)
        except ValueError:
            return
        self.assertIsNone(ket_qua)

    def test_summarize_readme_khong_backend_thi_tra_none_khong_crash(self):
        """Không có backend thì summarize_readme trả None (không bịa ghi chú), không crash."""
        _yeu_cau(self, summarize_readme)
        ket_qua = summarize_readme("README gia cua shop.", ["ban_hang"],
                                   backend=None)
        self.assertIsNone(ket_qua)

    def test_default_model_id_la_none_va_transformers_backend_raise(self):
        """Chưa chọn model thì DEFAULT_MODEL_ID là None và backend raise rõ ràng.

        Nếu TransformersBackend chưa có thì skip; nếu có mà môi trường
        không có transformers/weights thì generate_json phải raise
        RuntimeError, không trả JSON giả.
        """
        _yeu_cau(self, TransformersBackend)
        self.assertIsNone(DEFAULT_MODEL_ID)
        backend = TransformersBackend(None)
        with self.assertRaises(RuntimeError):
            backend.generate_json("prompt", {})


class TestVietnameseDocstringsAst(unittest.TestCase):
    """Kiểm tra bằng AST: mọi mục public của llm_ingest đều có docstring tiếng Việt."""

    def test_docstring_tieng_viet_bang_ast(self):
        """Mọi định nghĩa public phải có docstring chứa ký tự tiếng Việt có dấu.

        Nếu llm_ingest.py chưa tồn tại thì skip với lý do rõ ràng.
        Ngưỡng checked >= 10 tương ứng các hàm/class chính của contract
        mục 4 (9 hàm + backend + hằng không tính); missing phải rỗng.
        """
        module_path = _LLM_DIR / "src" / "llm_ingest.py"
        if not module_path.exists():
            self.skipTest("src/llm_ingest.py chua ton tai (Child 1 chua xong)")
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        vietnamese_chars = set(
            "àáảãạăắằẳẵặâấầẩẫậèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợ"
            "ùúủũụưứừửữựỳýỷỹỵđ")
        checked = []
        missing = []

        def _check(name, node):
            doc = ast.get_docstring(node)
            checked.append(name)
            if not doc or not any(ch in vietnamese_chars for ch in doc.lower()):
                missing.append(name)

        _check("module llm_ingest", tree)
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
        self.assertGreaterEqual(len(checked), 10)
        for ten_bat_buoc in (
            "function build_table_prompt",
            "function validate_column_mapping",
            "function classify_table",
            "function suggest_mapping",
            "function run_llm_ingest_for_shop",
        ):
            self.assertIn(ten_bat_buoc, checked)


if __name__ == "__main__":
    unittest.main()

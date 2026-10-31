# -*- coding: utf-8 -*-
"""Gợi ý ingest bằng mô hình ngôn ngữ cục bộ cho DataMate (Giai đoạn 1 LLM).

Mục đích của module này
------------------------
Bổ sung cho ingest rule-based của Giai đoạn 1
(``phase1/src/ingest.py``) ở đúng những chỗ rule-based làm yếu:
phân loại bảng, map cột khó, đọc README, phát hiện header khó.
Mọi gợi ý của mô hình ở đây chỉ là gợi ý, KHÔNG phải dữ liệu:
trước khi tầng tích hợp áp dụng, gợi ý phải qua kiểm chứng bằng
dữ liệu thật của chính bảng đó (tên cột phải tồn tại, mẫu giá trị
tiền/ngày/số lượng phải parse được bằng ``normalizers`` của phase1
với tỉ lệ tối thiểu 25%). Gợi ý nào không qua kiểm chứng thì bị
loại vào ``rejected_mappings``, không áp vào pipeline, không đoán
cột khác thay thế.

Những việc mô hình KHÔNG được làm (theo hợp đồng phase1_llm):

- Không tự parse tiền/ngày/số lượng thành số, không bịa hệ số quy
  đổi, không bịa trạng thái đơn, không tính toán đáp án nào.
- Không ghi đè map rule-based đã có: :func:`suggest_mapping` chỉ
  gợi ý cho các semantic còn trống mà caller truyền vào qua tham
  số ``unmapped_semantics``.
- Không sửa ``phase1/``, không sửa dữ liệu train, không gọi mạng
  hay API thương mại. Backend thật duy nhất là mô hình chạy cục bộ,
  offline (xem :class:`TransformersBackend`).

Quy tắc "dữ liệu thật thắng README" (hợp đồng mục 5):
    Nếu README nói bảng X là đơn hàng nhưng dữ liệu thật không có
    mã đơn/ngày/trạng thái tương ứng trong header, thì vai trò bảng
    vẫn phải theo dữ liệu thật (hạ xuống ``'other'`` kèm ghi chú),
    không được ghi đè bằng README. Quy tắc này được áp trong
    :func:`classify_table` sau khi JSON của mô hình đã qua validate.

TRUNG THỰC VỀ MODEL — ĐỌC KỸ TRƯỚC KHI ĐÓNG DOCKER:
    Model mặc định CHƯA được chọn và CHƯA được kiểm chứng trong lúc
    viết module này, vì môi trường làm việc này không có sẵn gói
    ``transformers``, không có gói ``torch`` và không có cache
    weights Hugging Face nào (đã kiểm tra thật bằng
    ``import transformers``, ``import torch`` và liệt kê thư mục
    cache: cả ba đều trống). Vì vậy module này KHÔNG ghi tên một
    model cục bộ cụ thể kèm licence hay ngày phát hành, để tránh
    bịa thông tin chưa kiểm chứng. ``DEFAULT_MODEL_ID`` cố tình là
    ``None``: thà báo lỗi rõ ràng còn hơn bịa một tên model chưa
    từng chạy thử. Người tích hợp bắt buộc phải chọn một model
    public có dung lượng <= 15B, phát hành trước ngày 01/10/2026,
    có licence cho phép, rồi truyền ``model_id`` vào
    :class:`TransformersBackend` và chạy thử end-to-end trước khi
    đóng Docker. Khi chưa có backend thật, mọi câu gọi LLM đều
    fallback rule-based, không crash, không trả JSON giả.

Nguyên tắc tổng quát hoá
------------------------
Module này không biết gì về business_id, tên shop, tên ngành,
tên cột train hay rulebook train. Enum vai trò bảng và 37 semantic
được lấy đúng theo hợp đồng phase1_llm (37 khóa của
``COLUMN_SYNONYMS`` trong phase1), không hard-code theo shop nào.
Mọi kiểm chứng đều dựa trên DataFrame của chính bảng đang chạy và
``normalizers`` của phase1. Khi không kiểm chứng được, module
fallback hoặc trả rỗng/``None`` để tầng trên abstain thay vì đoán.

Tính xác định:
    Không dùng random, không dùng thời gian hiện tại làm seed.
    Thứ tự bảng/cột/semantic luôn được sắp xếp hoặc giữ theo thứ
    tự cố định của hợp đồng, để kết quả tái lập giữa các lần chạy.
    Chỉ dùng thư viện chuẩn Python ở tầng prompt/validation/
    fallback; ``transformers``/``torch`` chỉ được import lười bên
    trong :class:`TransformersBackend`.
"""

from __future__ import annotations

import json
import sys
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "TABLE_ROLES",
    "SEMANTIC_KEYS",
    "MONEY_SEMANTICS",
    "DATE_SEMANTICS",
    "QUANTITY_SEMANTICS",
    "DEFAULT_MODEL_ID",
    "MAX_REPAIR_ATTEMPTS",
    "MAX_README_CHARS",
    "MAX_SAMPLE_ROWS",
    "SAMPLE_VALIDATION_THRESHOLD",
    "TABLE_CLASSIFICATION_JSON_SCHEMA",
    "COLUMN_MAPPING_JSON_SCHEMA",
    "README_NOTES_JSON_SCHEMA",
    "TableClassification",
    "ReadmeNotes",
    "LlmIngestResult",
    "LocalModelBackend",
    "TransformersBackend",
    "build_table_prompt",
    "build_mapping_prompt",
    "build_readme_prompt",
    "validate_table_classification",
    "validate_column_mapping",
    "validate_readme_notes",
    "classify_table",
    "suggest_mapping",
    "summarize_readme",
    "run_llm_ingest_for_shop",
]

# ---------------------------------------------------------------------------
# Nạp normalizers / COLUMN_SYNONYMS / map_columns của phase1 bằng đường
# dẫn tương đối (không hard-code đường dẫn tuyệt đối của máy).
# File này nằm ở <datamate>/phase1_llm/src/llm_ingest.py, nên
# phase1/src nằm ở parents[2]/phase1/src. Đường parents[1]/phase1/src
# cũng được thử dự phòng (đúng với vị trí file runner ở phase1_llm/),
# để module vẫn nạp được khi được copy/bố trí hơi khác.
# ---------------------------------------------------------------------------

_PHASE1_SRC_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "phase1" / "src",
    Path(__file__).resolve().parents[1] / "phase1" / "src",
]
for _candidate in _PHASE1_SRC_CANDIDATES:
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

try:  # Ưu tiên dùng đúng normalizers đã kiểm chứng ở Giai đoạn 1.
    from normalizers import parse_date as _phase1_parse_date
    from normalizers import parse_money as _phase1_parse_money
    from normalizers import parse_quantity as _phase1_parse_quantity

    _PHASE1_NORMALIZERS_AVAILABLE = True
except Exception:  # pragma: no cover - dự phòng khi chạy file rời.
    _phase1_parse_date = None
    _phase1_parse_money = None
    _phase1_parse_quantity = None
    _PHASE1_NORMALIZERS_AVAILABLE = False

try:  # Từ điển semantic và map rule-based của phase1 (chỉ đọc).
    from ingest import COLUMN_SYNONYMS as _PHASE1_COLUMN_SYNONYMS
    from ingest import map_columns as _phase1_map_columns

    _PHASE1_INGEST_AVAILABLE = True
except Exception:  # pragma: no cover - dự phòng khi chạy file rời.
    _PHASE1_COLUMN_SYNONYMS = {}
    _phase1_map_columns = None
    _PHASE1_INGEST_AVAILABLE = False

# ---------------------------------------------------------------------------
# Hằng số của hợp đồng (không tự thêm giá trị mới)
# ---------------------------------------------------------------------------

# Enum vai trò bảng cố định của hợp đồng phase1_llm, mục 3.
TABLE_ROLES = [
    "order",
    "order_line",
    "product",
    "customer",
    "import",
    "inventory",
    "lot_expiry",
    "payment",
    "return",
    "expense",
    "conversion",
    "other",
]

# 37 semantic, đúng thứ tự liệt kê trong hợp đồng phase1_llm, mục 3
# (cũng là 37 khóa của COLUMN_SYNONYMS trong phase1/src/ingest.py).
# Thứ tự này được giữ cố định để mọi vòng lặp qua semantic đều
# deterministic, không phụ thuộc thứ tự dict của JSON mô hình trả về.
SEMANTIC_KEYS = [
    "order_id",
    "order_date",
    "order_datetime",
    "order_status",
    "payment_method",
    "customer_code",
    "customer_name",
    "customer_group",
    "product_code",
    "product_name",
    "product_group",
    "quantity",
    "quantity_converted",
    "unit",
    "from_unit",
    "base_unit",
    "conversion_factor",
    "unit_price",
    "line_amount",
    "cost_price",
    "order_total",
    "amount_paid",
    "amount_due",
    "discount",
    "supplier_code",
    "supplier_name",
    "employee_code",
    "employee_name",
    "branch",
    "lot_code",
    "expiry_date",
    "stock_quantity",
    "import_date",
    "delivery_date",
    "payment_date",
    "return_quantity",
    "return_amount",
]

# Nhóm semantic phải kiểm chứng bằng mẫu giá trị (hợp đồng mục 5).
# order_datetime không nằm trong nhóm ngày của hợp đồng mục 5 (chỉ
# có order_date, expiry_date, import_date, delivery_date,
# payment_date), nên nó được kiểm chứng bằng tên như các semantic
# còn lại, không ép qua kiểm chứng ngày.
MONEY_SEMANTICS = {
    "order_total",
    "line_amount",
    "unit_price",
    "cost_price",
    "amount_paid",
    "amount_due",
    "discount",
    "return_amount",
}
DATE_SEMANTICS = {
    "order_date",
    "expiry_date",
    "import_date",
    "delivery_date",
    "payment_date",
}
QUANTITY_SEMANTICS = {
    "quantity",
    "quantity_converted",
    "stock_quantity",
    "return_quantity",
}

# Model mặc định CHƯA được chọn/kiểm chứng (xem docstring module).
# Người tích hợp phải gán một model_id cụ thể, public, <= 15B, phát
# hành trước 01/10/2026, có licence cho phép, trước khi đóng Docker.
# Để None ở đây là cố tình: thà báo lỗi rõ ràng còn hơn bịa một tên
# model chưa từng chạy thử.
DEFAULT_MODEL_ID = None

# Số lần repair tối đa theo hợp đồng (không tính lần gọi đầu tiên).
MAX_REPAIR_ATTEMPTS = 2

# Giới hạn của hợp đồng: README cắt tối đa 4000 ký tự đầu, prompt
# chỉ đưa tối đa 10 dòng mẫu, mỗi giá trị mẫu chỉ ở dạng chuỗi.
MAX_README_CHARS = 4000
MAX_SAMPLE_ROWS = 10

# Ngưỡng kiểm chứng mẫu của hợp đồng mục 5: ít nhất 25% mẫu không
# trống phải parse được bằng normalizers của phase1.
SAMPLE_VALIDATION_THRESHOLD = 0.25

# Số mẫu không trống tối đa lấy từ một cột để kiểm chứng (giống
# quy ước 40 mẫu đầu của phase1, đủ phân biệt cột tiền/ngày/số
# lượng với cột tên mà không phải quét toàn bộ cột hàng trăm nghìn
# dòng).
_MAX_SAMPLE_VALUES = 40

# ---------------------------------------------------------------------------
# JSON Schema cho 3 loại gợi ý (đều là gợi ý, không phải dữ liệu)
# ---------------------------------------------------------------------------

TABLE_CLASSIFICATION_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["table_role"],
    "properties": {
        "table_role": {"type": "string", "enum": TABLE_ROLES},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
}

COLUMN_MAPPING_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mapping"],
    "properties": {
        "mapping": {
            "type": "object",
            "description": (
                "Map semantic -> tên cột thật, semantic chỉ trong "
                "37 khóa của hợp đồng, tên cột phải tồn tại đúng "
                "trong DataFrame của bảng đó."
            ),
            "additionalProperties": {"type": "string"},
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
}

README_NOTES_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "table_roles": {
            "type": "object",
            "additionalProperties": {"type": "string", "enum": TABLE_ROLES},
        },
        "status_vocabulary": {"type": "array", "items": {"type": "string"}},
        "unit_notes": {"type": "array", "items": {"type": "string"}},
        "date_notes": {"type": "array", "items": {"type": "string"}},
        "conversion_table_location": {"type": ["string", "null"]},
    },
}


# ---------------------------------------------------------------------------
# Dataclass kết quả (đúng tên/trường của hợp đồng phase1_llm, mục 4)
# ---------------------------------------------------------------------------


@dataclass
class TableClassification:
    """Kết quả phân loại một bảng (gợi ý đã qua validate).

    Ý nghĩa từng trường:

    - ``table_role``: một giá trị trong :data:`TABLE_ROLES`, không
      bao giờ là giá trị ngoài enum. Khi LLM không khả dụng hoặc
      JSON sai sau repair, tầng :func:`classify_table` trả
      ``'other'`` kèm ghi chú fallback, chứ không đoán một vai trò
      cụ thể.
    - ``confidence``: số 0..1, độ chắc của việc phân loại (0.0 khi
      fallback). Đây là độ chắc của gợi ý, không phải độ chắc của
      dữ liệu.
    - ``evidence``: list chuỗi ngắn nêu bằng chứng (tên bảng,
      header, mẫu giá trị) mà gợi ý dựa vào.
    - ``notes``: list ghi chú ngắn, trong đó có ghi chú khi vai trò
      bị hạ xuống ``'other'`` vì dữ liệu thật không khớp README
      (quy tắc "dữ liệu thật thắng README").
    """

    table_role: str = "other"
    confidence: float = 0.0
    evidence: list = field(default_factory=list)
    notes: list = field(default_factory=list)


@dataclass
class ReadmeNotes:
    """Tóm tắt README của một shop (gợi ý đã qua validate).

    Ý nghĩa từng trường:

    - ``table_roles``: dict tên bảng -> vai trò trong
      :data:`TABLE_ROLES`. Vai trò nào ngoài enum sẽ bị loại khỏi
      dict này ở bước validate, không được giữ lại để tầng trên
      tin nhầm.
    - ``status_vocabulary``: list các trạng thái đơn mà README nhắc
      tới (chỉ là từ vựng tham khảo, không phải luật trạng thái:
      trạng thái thật vẫn phải đọc từ dữ liệu).
    - ``unit_notes``: list ghi chú về đơn vị trong README.
    - ``date_notes``: list ghi chú về quy ước ngày trong README.
    - ``conversion_table_location``: tên bảng chứa bảng quy đổi
      theo README, hoặc ``None`` khi README không nêu rõ. Trường
      này chỉ là gợi ý vị trí, hệ số quy đổi thật vẫn phải đọc từ
      dữ liệu bảng đó, không được bịa từ ghi chú.

    Trường hợp biên:
        - README trống hoặc LLM không khả dụng -> tầng
          :func:`summarize_readme` trả ``None`` thay vì một
          ReadmeNotes rỗng mang ý nghĩa giả, để tầng trên biết là
          chưa có thông tin README nào được xác nhận.
    """

    table_roles: dict = field(default_factory=dict)
    status_vocabulary: list = field(default_factory=list)
    unit_notes: list = field(default_factory=list)
    date_notes: list = field(default_factory=list)
    conversion_table_location: str | None = None


@dataclass
class LlmIngestResult:
    """Kết quả chạy LLM ingest cho một shop (đúng hợp đồng mục 4).

    Ý nghĩa từng trường:

    - ``business_id``: mã shop (tên thư mục shop), chỉ để định danh
      trong báo cáo, không dùng để suy luận gì khác.
    - ``table_roles``: dict tên bảng -> vai trò bảng cuối cùng cho
      bảng đó (từ LLM nếu kiểm chứng được, hoặc ``'other'`` khi
      fallback; xem quy tắc "dữ liệu thật thắng README").
    - ``added_mappings``: dict bảng -> semantic -> tên cột thật,
      CHỈ chứa các map đã qua kiểm chứng mẫu giá trị (hợp đồng
      mục 5) và CHỈ cho các semantic mà map rule-based của phase1
      còn trống. Map rule-based đã có không bao giờ bị ghi đè ở đây.
    - ``rejected_mappings``: list các dict, mỗi dict ghi
      ``semantic`` (semantic bị loại), ``column`` (tên cột mà LLM
      gợi ý, hoặc ``''`` khi gợi ý không nêu cột hợp lệ),
      ``reason`` (lý do loại, bằng tiếng Việt, rõ ràng để truy vết)
      và ``table`` (tên bảng, trường phụ thêm để tầng runner biết
      map bị loại thuộc bảng nào, vì list này gộp chung cả shop).
      Map trong list này KHÔNG được áp vào pipeline.
    - ``readme_notes``: :class:`ReadmeNotes` từ README của shop,
      hoặc ``None`` khi không có README/LLM không khả dụng/JSON sai
      sau repair.
    - ``sources``: dict tên bảng -> ``'llm'`` (ít nhất một bước LLM
      của bảng đó thành công: phân loại hoặc gợi ý map) hoặc
      ``'rule_fallback'`` (mọi bước LLM của bảng đó đều fallback).
    - ``fallback_count``: số bảng có ``sources`` là
      ``'rule_fallback'``. Đếm theo bảng (không đếm theo số lần gọi)
      để con số này đối chiếu trực tiếp được với số bảng của shop.
    - ``warnings``: list cảnh báo trong quá trình chạy (backend thiếu,
      import phase1 thất bại, JSON sai sau repair...), theo thứ tự
      bảng đã sắp xếp, để báo cáo truy vết được.
    """

    business_id: str = ""
    table_roles: dict = field(default_factory=dict)
    added_mappings: dict = field(default_factory=dict)
    rejected_mappings: list = field(default_factory=list)
    readme_notes: ReadmeNotes | None = None
    sources: dict = field(default_factory=dict)
    fallback_count: int = 0
    warnings: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tiện ích chuẩn hoá nội bộ
# ---------------------------------------------------------------------------


def _normalize_key_local(text):
    """Chuẩn hoá một chuỗi thành dạng thường, không dấu, gọn khoảng trắng.

    Đầu vào:
        text: chuỗi bất kỳ (tên cột, tên bảng), có thể None.

    Đầu ra:
        Chuỗi đã thường hoá, bỏ dấu. Ví dụ ``'Mã Đơn Hàng'`` thành
        ``'ma don hang'``.

    Vì sao có bản nội bộ này:
        Khi phase1 nạp được thì code dùng ``normalize_key`` của
        phase1; bản này chỉ là dự phòng khi chạy file rời, để việc
        kiểm tra bằng chứng header (quy tắc dữ liệu thật thắng
        README) vẫn chạy được mà không crash. Quy ước chuẩn hoá của
        bản dự phòng giống phase1: thường hoá, bỏ dấu, gộp khoảng
        trắng, đổi ``'đ'`` thành ``'d'``.

    Trường hợp biên:
        - None hoặc chuỗi rỗng -> ``''`` (không crash, để caller coi
          là "không có giá trị").
    """
    if text is None:
        return ""
    s = unicodedata.normalize("NFC", str(text)).lower()
    decomposed = unicodedata.normalize("NFD", s)
    without = "".join(
        ch for ch in decomposed if unicodedata.category(ch) != "Mn"
    )
    without = without.translate(str.maketrans({"đ": "d", "Đ": "D"}))
    return " ".join(without.split())


def _truncate_readme(readme_text):
    """Cắt README về tối đa 4000 ký tự đầu, theo hợp đồng mục 4.

    Đầu vào:
        readme_text: nội dung README, có thể None hoặc rỗng.

    Đầu ra:
        Chuỗi README đã cắt (tối đa :data:`MAX_README_CHARS` ký tự).
        Nếu bị cắt thì thêm hậu tố ghi rõ đã cắt, để mô hình biết
        phần sau chưa được đọc và không suy luận về phần chưa thấy.

    Vì sao cắt ở 4000 ký tự đầu thay vì đưa toàn bộ:
        README của shop có thể rất dài; đưa toàn bộ sẽ làm prompt
        phình vô hạn và mô hình dễ bỏ qua phần đầu quan trọng nhất.
        4000 ký tự đầu thường chứa mô tả bảng và quy ước chính.

    Trường hợp biên:
        - None hoặc rỗng -> ``''`` (prompt sẽ ghi rõ là không có
          README, để mô hình chỉ dùng tên bảng/header/mẫu).
    """
    if not readme_text:
        return ""
    text = str(readme_text)
    if len(text) > MAX_README_CHARS:
        return text[:MAX_README_CHARS] + " ... (README da cat o 4000 ky tu)"
    return text


def _stringify_value(value):
    """Chuyển một giá trị mẫu thành chuỗi cho prompt, không mất dạng gốc.

    Đầu vào:
        value: giá trị bất kỳ trong DataFrame (số, chuỗi, ngày, None).

    Đầu ra:
        Chuỗi hiển thị: ô trống thành ``''``, các giá trị khác thành
        ``str(value)`` đã cắt khoảng trắng đầu/cuối.

    Vì sao chỉ chuyển thành chuỗi, không parse:
        Prompt chỉ cần cho mô hình thấy dạng giá trị thật (ví dụ
        ``'55.000đ'``, ``'1/1'``) để nó hiểu cột là cột gì. Việc parse
        thành số/ngày là việc của ``normalizers`` phase1 ở bước kiểm
        chứng, không phải việc của prompt hay của mô hình.
    """
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN
        return ""
    return str(value).strip()


def _format_sample_rows(sample_rows, headers):
    """Định dạng tối đa 10 dòng mẫu thành text cho prompt, dạng chuỗi.

    Đầu vào:
        sample_rows: list các dòng mẫu, mỗi dòng có thể là dict
            (tên cột -> giá trị), list/tuple (giá trị theo thứ tự
            header), hoặc DataFrame (khi đó lấy 10 dòng đầu). Cũng
            chấp nhận None (coi như không có mẫu).
        headers: list tên cột, dùng để gắn nhãn cho dòng dạng list
            và để sắp xếp khóa của dòng dạng dict cho deterministic.

    Đầu ra:
        Chuỗi nhiều dòng, mỗi dòng một mẫu, giá trị chỉ ở dạng chuỗi
        (xem :func:`_stringify_value`). Nếu không có mẫu thì trả
        dòng ghi rõ là không có dòng mẫu, để mô hình không tưởng là
        prompt bị thiếu.

    Vì sao tối đa 10 dòng:
        Hợp đồng mục 4 quy định prompt chỉ đưa tối đa 10 dòng mẫu,
        và cấm đưa toàn bộ DataFrame vào prompt. 10 dòng đủ để mô
        hình nhận dạng loại cột, mà prompt vẫn gọn và không lộ dữ
        liệu dòng đơn quá mức cần thiết.
    """
    rows = sample_rows
    if rows is None:
        return "Khong co dong mau."
    # DataFrame: lấy 10 dòng đầu, chuyển thành list dict.
    if hasattr(rows, "to_dict") and hasattr(rows, "columns"):
        try:
            rows = rows.head(MAX_SAMPLE_ROWS).to_dict("records")
        except Exception:
            return "Khong co dong mau."
    if not rows:
        return "Khong co dong mau."
    lines = []
    for row in list(rows)[:MAX_SAMPLE_ROWS]:
        if isinstance(row, dict):
            if headers:
                keys = [h for h in headers if h in row]
                keys += sorted(k for k in row if k not in headers)
            else:
                keys = sorted(row)
            parts = [f"{k}={_stringify_value(row[k])}" for k in keys]
            lines.append(", ".join(parts))
        elif isinstance(row, (list, tuple)):
            if headers:
                parts = [
                    f"{headers[i]}={_stringify_value(value)}"
                    for i, value in enumerate(row)
                    if i < len(headers)
                ]
            else:
                parts = [_stringify_value(value) for value in row]
            lines.append(", ".join(parts))
        else:
            lines.append(_stringify_value(row))
    return "\n".join(lines) if lines else "Khong co dong mau."


def _format_headers(headers):
    """Định dạng list tên cột thành chuỗi cho prompt.

    Đầu vào:
        headers: list tên cột, có thể None hoặc rỗng.

    Đầu ra:
        Chuỗi các tên cột nối bằng ``', '``, hoặc dòng ghi rõ là
        không có header khi list rỗng, để mô hình biết bảng này
        không có thông tin tên cột để dựa vào.
    """
    if not headers:
        return "Khong co header."
    return ", ".join(str(h) for h in headers)


def _is_empty_value(value):
    """Ô có coi là trống không (None, NaN, chuỗi toàn khoảng trắng).

    Đầu vào:
        value: giá trị bất kỳ.

    Đầu ra:
        True khi ô trống, False khi có nội dung. Quy ước này giống
        phase1 (``_is_empty_value`` trong ingest.py), để tỉ lệ mẫu
        kiểm chứng tính trên cùng định nghĩa "không trống".
    """
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _column_sample_values(df, column, limit=_MAX_SAMPLE_VALUES):
    """Lấy tối đa ``limit`` giá trị không trống đầu tiên của một cột.

    Đầu vào:
        df: DataFrame của bảng (hoặc None).
        column: tên cột thật cần lấy mẫu.
        limit: số mẫu tối đa (mặc định 40, theo quy ước phase1).

    Đầu ra:
        List giá trị gốc (chưa parse, giữ nguyên kiểu) theo thứ tự
        xuất hiện trong cột. List rỗng khi df là None, cột không tồn
        tại, hoặc cột toàn trống — caller phân biệt "toàn trống"
        bằng list rỗng và KHÔNG loại map trong trường hợp đó (xem
        :func:`_sample_validation_rate`).

    Vì sao chỉ lấy mẫu đầu thay vì toàn cột:
        Kiểm chứng ý nghĩa cột không cần đọc hết cột hàng trăm nghìn
        dòng; 40 mẫu đã đủ phân biệt cột tiền (parse được đa số) với
        cột tên (parse tiền gần như bằng 0), giống phase1.
    """
    if df is None:
        return []
    try:
        series = df[column]
    except Exception:
        return []
    samples = []
    try:
        values = list(series)
    except Exception:
        return []
    for value in values:
        if _is_empty_value(value):
            continue
        samples.append(value)
        if len(samples) >= limit:
            break
    return samples


def _sample_validation_rate(semantic, df, column):
    """Tính tỉ lệ mẫu parse được của một cột theo semantic (0-1 hoặc None).

    Đầu vào:
        semantic: semantic cần kiểm chứng (một trong 37 khóa).
        df: DataFrame của bảng.
        column: tên cột thật ứng với semantic đó.

    Đầu ra:
        - ``None`` khi cột toàn trống (không có mẫu nào), hoặc khi
          semantic không thuộc nhóm tiền/ngày/số lượng (các semantic
          còn lại chỉ kiểm chứng bằng tên, không có dạng parse đặc
          thù), hoặc khi df là None (không có dữ liệu để kiểm chứng
          mẫu — caller phải tự ghi warning, xem validate chi tiết).
        - Ngược lại là tỉ lệ mẫu không trống parse thành công:
          tiền dùng ``parse_money`` của phase1, ngày dùng
          ``parse_date`` của phase1, số lượng dùng
          ``parse_quantity`` của phase1 (số != None, kể cả số kèm
          đơn vị như ``'1 ly'``).

    Vì sao trả None thay vì 0 khi toàn trống:
        Hợp đồng mục 5 quy định cột toàn trống thì tỉ lệ mẫu là None
        nên không bị loại (giống phase1): không có bằng chứng chống
        lại tên cột, nên để tên quyết định. Trả 0 ở đây sẽ loại oan
        những cột thật nhưng kỳ đó chưa có dữ liệu.

    Trường hợp biên khi phase1 không nạp được:
        Hàm này trả ``None`` và caller (validate chi tiết) ghi
        warning rằng đang dùng kiểm chứng tên thay cho kiểm chứng
        mẫu, KHÔNG bịa tỉ lệ số nào. Đây là bản sao quy tắc phase1
        về ngưỡng, nhưng không thay thế được chính hàm parse của
        phase1, nên phải ghi rõ trong warning.
    """
    if semantic in MONEY_SEMANTICS:
        parse_fn = _phase1_parse_money
        kind = "money"
    elif semantic in DATE_SEMANTICS:
        parse_fn = _phase1_parse_date
        kind = "date"
    elif semantic in QUANTITY_SEMANTICS:
        parse_fn = _phase1_parse_quantity
        kind = "quantity"
    else:
        return None
    if parse_fn is None or df is None:
        return None
    samples = _column_sample_values(df, column)
    if not samples:
        return None
    ok = 0
    for value in samples:
        try:
            parsed = parse_fn(value)
        except Exception:
            parsed = None
        if kind == "quantity":
            # parse_quantity trả tuple (số, đơn vị); chỉ số mới tính.
            if isinstance(parsed, (tuple, list)) and parsed:
                parsed = parsed[0]
            elif not isinstance(parsed, (int, float)):
                parsed = None
        if parsed is not None:
            ok += 1
    return ok / len(samples)


def _name_score_local(column_normalized, synonym):
    """Chấm độ khớp tên giữa một cột và một từ khoá (0-100), bản nội bộ.

    Đầu vào:
        column_normalized: tên cột đã chuẩn hoá (thường, không dấu).
        synonym: từ khoá đồng nghĩa đã chuẩn hoá.

    Đầu ra:
        100 khi bằng nhau tuyệt đối, 95 khi bằng nhau khi bỏ khoảng
        trắng (dạng dính liền kiểu SQLite), 85 khi mọi token của từ
        khoá đều có trong tên cột, 75 khi mọi token của tên cột đều
        có trong từ khoá và tên cột có từ 2 token trở lên, ngược lại
        0. Từ khoá một từ chỉ khớp chính xác, để cột tên chung chung
        không bị gán bừa.

    Vì sao có bản nội bộ này:
        Đây là bản sao đúng thang điểm của ``_name_score`` trong
        phase1, chỉ dùng cho bước kiểm tra bằng chứng header (quy tắc
        dữ liệu thật thắng README). Nó không thay thế map_columns
        của phase1 và không dùng để tự tạo map mới.
    """
    if not column_normalized or not synonym:
        return 0
    if column_normalized == synonym:
        return 100
    col_nospace = column_normalized.replace(" ", "")
    syn_nospace = synonym.replace(" ", "")
    if col_nospace == syn_nospace:
        return 95
    col_tokens = set(column_normalized.split())
    syn_tokens = set(synonym.split())
    if len(syn_tokens) == 1:
        return 0
    if syn_tokens <= col_tokens:
        return 85
    if len(col_tokens) >= 2 and col_tokens <= syn_tokens:
        return 75
    return 0


# Semantic nào là bằng chứng header cho từng vai trò bảng, dùng cho
# quy tắc "dữ liệu thật thắng README". Chỉ những vai trò có semantic
# tương ứng trong 37 khóa mới nằm ở đây: vai trò expense không có
# semantic chi phí riêng trong 37 khóa nên không bị hạ chỉ vì thiếu
# bằng chứng (tránh hạ oan), và vai trò other/conversion cũng vậy
# (conversion có bằng chứng riêng ở dưới).
_ROLE_EVIDENCE_SEMANTICS = {
    "order": {"order_id", "order_date", "order_status"},
    "order_line": {"order_id", "product_code", "product_name", "quantity"},
    "product": {"product_code", "product_name"},
    "customer": {"customer_code", "customer_name"},
    "import": {"import_date", "supplier_code", "supplier_name",
               "product_code", "product_name"},
    "inventory": {"stock_quantity", "product_code", "product_name"},
    "lot_expiry": {"lot_code", "expiry_date"},
    "payment": {"payment_date", "amount_paid", "payment_method"},
    "return": {"return_quantity", "return_amount"},
    "conversion": {"conversion_factor", "from_unit", "base_unit"},
}


def _headers_have_role_evidence(headers, table_role):
    """Kiểm tra header có bằng chứng nào cho vai trò bảng hay không.

    Đầu vào:
        headers: list tên cột của bảng.
        table_role: vai trò mà mô hình gợi ý.

    Đầu ra:
        True khi vai trò không nằm trong bảng bằng chứng ở trên
        (không có căn cứ để hạ), hoặc khi ít nhất một header khớp
        với một semantic bằng chứng ở mức >= 75 điểm theo thang
        phase1. False chỉ khi vai trò có danh sách bằng chứng, header
        không rỗng, từ điển phase1 nạp được, mà không header nào khớp
        semantic bằng chứng nào.

    Vì sao chỉ cần một bằng chứng, không đòi đủ bộ:
        Một bảng đơn hàng thật có thể chỉ có mã đơn mà thiếu cột
        trạng thái (hoặc ngược lại) nhưng vẫn là bảng đơn hàng.
        Đòi đủ cả mã + ngày + trạng thái sẽ hạ oan nhiều bảng thật.
        Quy tắc hợp đồng chỉ hạ khi dữ liệu thật "không có mã đơn/
        ngày/trạng thái tương ứng" — tức là không có cái nào cả.

    Trường hợp biên:
        - headers rỗng -> True (không có dữ liệu để phản bác, giữ
          gợi ý của mô hình thay vì hạ dựa trên khoảng trống).
        - Từ điển phase1 không nạp được -> True (không có căn cứ
          kiểm tra, ghi warning ở tầng trên thay vì hạ oan).
    """
    wanted = _ROLE_EVIDENCE_SEMANTICS.get(table_role)
    if wanted is None:
        return True
    if not headers:
        return True
    if not _PHASE1_COLUMN_SYNONYMS:
        return True
    normalized_headers = [_normalize_key_local(h) for h in headers]
    for semantic in sorted(wanted):
        synonyms = _PHASE1_COLUMN_SYNONYMS.get(semantic, [])
        for header_norm in normalized_headers:
            for synonym in synonyms:
                if _name_score_local(header_norm, synonym) >= 75:
                    return True
    return False


def _coerce_confidence(value, errors, field_name="confidence"):
    """Ép confidence về float 0..1, ghi lỗi và trả None khi sai.

    Đầu vào:
        value: giá trị confidence mà mô hình trả (có thể vắng mặt).
        errors: list lỗi đang thu thập (hàm append vào list này).
        field_name: tên trường (để ghi lỗi rõ ràng).

    Đầu ra:
        float trong [0, 1] khi hợp lệ; 0.0 khi vắng mặt (mặc định
        "không chắc", kèm ghi chú ở tầng validate, không coi là sai
        vì schema hợp đồng không bắt buộc confidence); None khi giá
        trị sai kiểu (bool, chuỗi, số ngoài [0, 1]) — khi đó JSON
        phải đi repair thay vì bị ép lén thành một số khác.
    """
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(
            f"{field_name} phai la so 0..1, nhan duoc {value!r}."
        )
        return None
    if value < 0 or value > 1:
        errors.append(f"{field_name} {value} nam ngoai khoang 0..1.")
        return None
    return float(value)


def _coerce_string_list(value, errors, field_name):
    """Ép một trường list chuỗi về list chuỗi, ghi lỗi khi sai kiểu.

    Đầu vào:
        value: giá trị trường (có thể vắng mặt -> list rỗng).
        errors: list lỗi đang thu thập.
        field_name: tên trường (để ghi lỗi rõ ràng).

    Đầu ra:
        List chuỗi: vắng mặt -> []; list chứa phần tử không phải
        chuỗi -> ép thành chuỗi bằng str() (vì evidence/notes chỉ là
        ghi chú, ép kiểu ở đây không làm sai lệch dữ liệu tính toán);
        kiểu khác list (chuỗi đơn, số, dict) -> list rỗng kèm lỗi,
        để JSON đi repair thay vì lặng lẽ nuốt.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{field_name} phai la list chuoi, nhan duoc {type(value).__name__}.")
        return []
    return [item if isinstance(item, str) else str(item) for item in value]


def _build_repair_prompt(original_prompt, errors, previous_output):
    """Xây repair prompt liệt kê lỗi của JSON lần trước, yêu cầu trả lại JSON sửa.

    Đầu vào:
        original_prompt: prompt gốc đã gửi cho mô hình.
        errors: list chuỗi lỗi do validate trả ra (đã rõ ràng từng
            lỗi: role ngoài enum, semantic ngoài 37 khóa, tên cột
            không tồn tại, tỉ lệ mẫu dưới 25%...).
        previous_output: dict JSON lần trước (để mô hình thấy mình
            đã sai ở đâu, thay vì phải đoán lại từ đầu).

    Đầu ra:
        Chuỗi prompt mới: giữ nguyên yêu cầu gốc, thêm phần lỗi cần
        sửa và JSON cũ, và nhắc lại: chỉ trả JSON đã sửa, không giải
        thích, không bịa cột/thực thể mới để lấp chỗ trống, không
        tính số.

    Vì sao liệt kê lỗi thay vì chỉ nói "sai, làm lại":
        Mô hình không thấy được code validate, nên nói chung chung
        sẽ làm nó sửa lung tung hoặc lặp lại đúng lỗi cũ. Liệt kê
        lỗi cụ thể giúp lần repair sau trúng đích và giảm số lần gọi
        model. Tối đa 2 lần repair (xem :data:`MAX_REPAIR_ATTEMPTS`).
    """
    try:
        previous_text = json.dumps(previous_output, ensure_ascii=False)
    except (TypeError, ValueError):
        previous_text = str(previous_output)
    error_lines = "\n".join(f"- {error}" for error in errors)
    return (
        f"{original_prompt}\n\n"
        "JSON ban tra truoc do SAI va can sua lai. Cac loi cu the:\n"
        f"{error_lines}\n\n"
        f"JSON cu:\n{previous_text}\n\n"
        "Hay tra DUY NHAT object JSON da sua het cac loi tren, theo dung schema, "
        "khong giai thich, khong bia ten cot hay vai tro moi de lap cho trong, "
        "khong tinh toan so lieu. Neu mot truong khong xac dinh duoc, hay dung "
        "'other'/null/list rong va ghi vao notes."
    )


def _call_with_repair(backend, prompt, schema, validate_fn):
    """Gọi backend tối đa 1 lần chính + 2 lần repair, trả (kết quả, lỗi, nguồn).

    Đầu vào:
        backend: backend mô hình (LocalModelBackend) hoặc None.
        prompt: prompt gốc đã xây.
        schema: JSON Schema của loại gợi ý đang xin.
        validate_fn: hàm validate chi tiết nhận dict và trả
            ``(ket_qua, errors)``; ``ket_qua`` là None khi còn lỗi,
            ``errors`` là list chuỗi lỗi (rỗng khi hợp lệ).

    Đầu ra:
        Tuple ``(ket_qua, errors, source)`` trong đó source là
        ``'llm'`` (hợp lệ ngay lần đầu), ``'repair'`` (hợp lệ sau
        repair), hoặc ``'fallback'`` (không có backend, backend lỗi,
        hoặc sau 2 lần repair vẫn sai). Khi source là fallback thì
        ket_qua là None và errors ghi rõ lý do, để caller fallback
        rule-based thay vì dùng một kết quả nửa vời.

    Vì sao không retry vô hạn:
        Mỗi lần gọi model đều tốn thời gian và có thể lặp lại đúng
        lỗi cũ; hợp đồng giới hạn repair tối đa 2 lần, sau đó phải
        fallback để pipeline không bị kẹt và không bị trừ điểm vì
        một gợi ý sai.
    """
    if backend is None:
        return (None, ["Khong co backend LLM (backend=None), fallback rule-based."], "fallback")
    try:
        data = backend.generate_json(prompt, schema)
    except Exception as exc:
        return (None, [f"Backend LLM loi: {exc}"], "fallback")
    if not isinstance(data, dict):
        return (None, ["Backend LLM tra ve JSON khong phai object."], "fallback")
    result, errors = validate_fn(data)
    if result is not None:
        return (result, [], "llm")
    current_prompt = prompt
    previous = data
    for _attempt in range(MAX_REPAIR_ATTEMPTS):
        repair_prompt = _build_repair_prompt(current_prompt, errors, previous)
        try:
            data = backend.generate_json(repair_prompt, schema)
        except Exception as exc:
            return (None, [f"Backend LLM loi khi repair: {exc}"], "fallback")
        if not isinstance(data, dict):
            return (None, ["Backend LLM tra ve JSON khong phai object khi repair."], "fallback")
        result, errors = validate_fn(data)
        if result is not None:
            return (result, [], "repair")
        previous = data
    return (None, errors, "fallback")


# ---------------------------------------------------------------------------
# Backend mô hình cục bộ (tái sử dụng mẫu phase2_llm)
# ---------------------------------------------------------------------------


class LocalModelBackend(ABC):
    """Lớp trừu tượng cho một backend mô hình ngôn ngữ chạy cục bộ.

    Mục đích:
        Tách tầng prompt/validation/fallback khỏi cách gọi mô hình cụ
        thể, để tầng tích hợp có thể thay backend (Transformers, một
        backend tự viết khác, hoặc backend giả trong test) mà không
        đổi interface. Mọi backend đều phải chạy offline, trên máy
        cục bộ, không gọi API thương mại.

    Trường hợp biên:
        - Backend không khả dụng (thiếu gói, thiếu weights, lỗi tải
          model) phải raise RuntimeError rõ ràng, KHÔNG được trả một
          JSON giả vờ để pipeline tưởng là đã gợi ý xong.
    """

    @abstractmethod
    def generate_json(self, prompt: str, schema: dict) -> dict:
        """Sinh một JSON theo schema từ prompt, trả về dict đã parse.

        Đầu vào:
            prompt: prompt đầy đủ đã xây bằng các hàm build_*_prompt
                (hoặc repair prompt).
            schema: JSON Schema của loại gợi ý đang xin (một trong 3
                hằng số schema của module này).

        Đầu ra:
            dict là JSON mà mô hình trả về (chưa qua validate).

        Raise:
            RuntimeError khi backend không khả dụng hoặc không trích
            được JSON từ output của mô hình. Caller sẽ coi đây là lỗi
            backend và đi fallback, không retry vô hạn.
        """
        raise NotImplementedError


class TransformersBackend(LocalModelBackend):
    """Backend chạy mô hình cục bộ bằng thư viện transformers.

    Mục đích:
        Gọi một mô hình ngôn ngữ chạy hoàn toàn trên máy cục bộ
        (offline sau khi đã tải weights) để sinh JSON theo schema.
        Đây là backend thật duy nhất được hỗ trợ trong module này.

    Đầu vào (__init__):
        model_id: id model trên Hugging Face (hoặc đường dẫn thư mục
            weights cục bộ). Tham số này CHƯA có giá trị mặc định được
            kiểm chứng (xem DEFAULT_MODEL_ID và docstring module):
            người tích hợp bắt buộc phải chọn một model public <= 15B,
            phát hành trước 01/10/2026, có licence cho phép, rồi mới
            truyền vào đây.
        device: None (để thư viện tự chọn), 'cpu', 'cuda', hoặc một
            chuỗi device mà torch hiểu. Nếu None thì model được tải
            lên CPU khi không có GPU, để chạy được ở mọi môi trường.

    Vì sao lazy import:
        ``transformers`` và ``torch`` chỉ được import khi lần đầu gọi
        generate_json, không import ở đầu module. Nhờ vậy module vẫn
        import/test được ở môi trường chưa cài hai gói này, và lỗi
        thiếu gói được báo đúng lúc cần model, kèm hướng dẫn rõ ràng,
        thay vì crash ngay khi import.

    Trường hợp biên / trung thực:
        - model_id rỗng/None -> raise RuntimeError ngay, không tự chọn
          một model khác để lấp chỗ trống.
        - Thiếu transformers/torch, hoặc tải weights thất bại (không
          có weights, không có mạng, sai id) -> raise RuntimeError
          kèm nguyên nhân gốc, KHÔNG trả JSON giả, KHÔNG bịa model.
        - Output của model không chứa JSON hợp lệ -> raise RuntimeError
          để caller đi repair/fallback, không tự sửa JSON bằng đoán.
        - Code sinh text ở đây chưa từng được chạy thử với weights
          thật trong môi trường viết module này (vì không có weights),
          nên trước khi đóng Docker bắt buộc phải chạy thử end-to-end
          với model đã chọn và ghi kết quả vào báo cáo tích hợp.
    """

    def __init__(self, model_id=None, device=None):
        """Lưu cấu hình, chưa tải model (tải lười ở lần gọi đầu tiên).

        Đầu vào:
            model_id: id model hoặc đường dẫn weights (xem docstring class).
            device: None/'cpu'/'cuda' (xem docstring class).

        Đầu ra:
            Một backend chưa tải model; model chỉ tải ở _ensure_loaded.
        """
        self.model_id = model_id if model_id is not None else DEFAULT_MODEL_ID
        self.device = device
        self._tokenizer = None
        self._model = None
        self._loaded = False

    def _ensure_loaded(self):
        """Tải tokenizer và model khi cần, raise RuntimeError nếu không tải được.

        Đầu vào: không có (dùng self.model_id và self.device).

        Đầu ra: không có (gán self._tokenizer, self._model, self._loaded).

        Raise:
            RuntimeError khi model_id chưa chọn, khi thiếu transformers
            hoặc torch, hoặc khi tải weights thất bại vì bất kỳ lý do
            gì. Thông điệp luôn nêu rõ nguyên nhân và bước tiếp theo,
            không giấu lỗi sau một JSON giả.
        """
        if self._loaded:
            return
        if not self.model_id:
            raise RuntimeError(
                "TransformersBackend chua co model_id: model mac dinh CHUA duoc chon "
                "va CHUA duoc kiem chung. Hay chon mot model public <= 15B, phat hanh "
                "truoc 01/10/2026, co licence cho phep, roi truyen model_id vao day "
                "truoc khi dong Docker. Khong tu chon model thay nguoi tich hop."
            )
        try:
            import transformers  # noqa: F401  (chỉ kiểm tra gói có sẵn không)
        except ImportError as exc:
            raise RuntimeError(
                "TransformersBackend khong kha dung: thieu goi 'transformers'. "
                "Hay cai transformers (va torch) trong moi truong Docker roi thu lai; "
                "khong tra JSON gia khi backend khong co that."
            ) from exc
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "TransformersBackend khong kha dung: thieu 'torch' hoac lop Auto "
                "cua transformers. Hay cai day du torch + transformers roi thu lai."
            ) from exc
        try:
            self._tokenizer = AutoTokenizer.from_pretrained(self.model_id)
            load_kwargs = {}
            if self.device in ("cuda", "cpu"):
                load_kwargs["device_map"] = None
            try:
                self._model = AutoModelForCausalLM.from_pretrained(
                    self.model_id, torch_dtype="auto", **load_kwargs
                )
            except TypeError:
                # Phiên bản transformers cũ dùng tên tham số khác.
                self._model = AutoModelForCausalLM.from_pretrained(
                    self.model_id, **load_kwargs
                )
            if self.device == "cuda" and torch.cuda.is_available():
                self._model = self._model.to("cuda")
            elif self.device == "cpu":
                self._model = self._model.to("cpu")
            self._model.eval()
        except Exception as exc:
            raise RuntimeError(
                f"TransformersBackend khong tai duoc model '{self.model_id}': {exc}. "
                "Co the weights chua co san, sai model_id, hoac thieu bo nho. "
                "Khong tra JSON gia khi model khong tai duoc."
            ) from exc
        self._loaded = True

    def _extract_json_object(self, text):
        """Trích object JSON đầu tiên hợp lệ trong text do model sinh ra.

        Đầu vào:
            text: text thô mà model sinh ra (có thể kèm giải thích).

        Đầu ra:
            dict đã parse bằng json.loads.

        Raise:
            RuntimeError khi không tìm được object JSON hợp lệ nào.
            Không tự sửa JSON hỏng bằng cách đoán trường, vì đoán
            trường là đoán luôn ý nghĩa cột/bảng.

        Vì sao quét dấu ngoặc cân bằng thay vì lấy từ '{' đầu đến '}'
        cuối: output của model có thể kèm text sau JSON, và JSON lồng
        nhau có nhiều '}' — lấy đến '}' cuối sẽ nuốt cả text thừa vào
        JSON và parse hỏng, trong khi quét cân bằng dừng đúng ở '}'
        đóng object ngoài cùng.
        """
        if not text:
            raise RuntimeError("Model tra ve text rong, khong co JSON.")
        start = text.find("{")
        if start < 0:
            raise RuntimeError("Output cua model khong chua object JSON.")
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            ch = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start:index + 1])
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
                            f"Khong parse duoc JSON tu output cua model: {exc}"
                        ) from exc
                    if not isinstance(parsed, dict):
                        raise RuntimeError("JSON cua model khong phai object.")
                    return parsed
        raise RuntimeError("Output cua model co dau '{' mo nhung khong dong du.")

    def generate_json(self, prompt: str, schema: dict) -> dict:
        """Sinh JSON theo schema bằng model transformers cục bộ.

        Đầu vào:
            prompt: prompt đầy đủ (đã kèm yêu cầu chỉ trả JSON).
            schema: JSON Schema của loại gợi ý đang xin (giữ đúng chữ
                ký của LocalModelBackend; backend này chưa ép schema
                bằng constrained decoding, mà nhắc schema trong
                prompt và để các hàm validate kiểm chứng sau đó —
                vì vậy caller vẫn phải validate, không được tin
                output của model một cách mù quáng).

        Đầu ra:
            dict JSON mà model sinh ra (chưa qua validate).

        Raise:
            RuntimeError khi backend chưa tải được model hoặc không
            trích được JSON (xem _ensure_loaded và _extract_json_object).
        """
        self._ensure_loaded()
        tokenizer = self._tokenizer
        model = self._model
        try:
            if hasattr(tokenizer, "apply_chat_template") and tokenizer.chat_template:
                messages = [{"role": "user", "content": prompt}]
                text_in = tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True
                )
            else:
                text_in = prompt
            inputs = tokenizer(text_in, return_tensors="pt")
            if self.device == "cuda":
                inputs = {key: value.to("cuda") for key, value in inputs.items()}
            outputs = model.generate(
                **inputs, max_new_tokens=512, do_sample=False, num_beams=1
            )
            generated = outputs[0][inputs["input_ids"].shape[1]:]
            text_out = tokenizer.decode(generated, skip_special_tokens=True)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"TransformersBackend sinh text that bai: {exc}"
            ) from exc
        return self._extract_json_object(text_out)


# ---------------------------------------------------------------------------
# Xây prompt tiếng Việt
# ---------------------------------------------------------------------------


def build_table_prompt(table_name, headers, sample_rows, readme_text) -> str:
    """Xây prompt tiếng Việt yêu cầu mô hình phân loại một bảng.

    Đầu vào:
        table_name: tên bảng/sheet (chuỗi, có thể rỗng).
        headers: list tên cột của bảng.
        sample_rows: tối đa 10 dòng mẫu (xem :func:`_format_sample_rows`;
            giá trị chỉ ở dạng chuỗi, không đưa toàn bộ DataFrame).
        readme_text: nội dung README của shop (sẽ bị cắt tối đa 4000
            ký tự đầu, xem :func:`_truncate_readme`).

    Đầu ra:
        Chuỗi prompt hoàn chỉnh. Prompt yêu cầu mô hình: chỉ trả DUY
        NHẤT một object JSON theo schema TableClassification (vai trò
        trong enum TABLE_ROLES, confidence 0..1, evidence, notes),
        không giải thích, không tính số, không bịa vai trò ngoài enum.
        Prompt nhắc rõ quy tắc "dữ liệu thật thắng README": nếu README
        nói một đằng mà header/mẫu nói một nẻo thì theo header/mẫu.

    Vì sao nhấn mạnh "không tính số":
        Nhiệm vụ của mô hình chỉ là hiểu bảng thành vai trò; mọi con
        số, tiền, ngày, số lượng đều do code phase1 tính/parse sau đó.
        Mô hình mà tự tính số trong prompt sẽ vừa sai (không thấy toàn
        bộ dữ liệu) vừa làm lệch JSON.
    """
    readme_part = _truncate_readme(readme_text)
    if not readme_part:
        readme_part = "(Khong co README.)"
    schema_text = json.dumps(TABLE_CLASSIFICATION_JSON_SCHEMA, ensure_ascii=False)
    roles_text = ", ".join(TABLE_ROLES)
    return (
        "Ban la bo phan phan loai bang cua he thong DataMate. "
        "Nhiem vu DUY NHAT cua ban la doc ten bang, header va cac dong mau "
        "duoi day, roi tra ve MOT object JSON theo dung schema, khong giai thich, "
        "khong tinh toan so lieu, khong them text ngoai JSON.\n\n"
        f"Ten bang: {table_name}\n"
        f"Header: {_format_headers(headers)}\n"
        f"Cac dong mau (toi da 10 dong, gia tri dang chuoi):\n{_format_sample_rows(sample_rows, headers)}\n\n"
        f"README cua shop (cat toi da 4000 ky tu dau):\n{readme_part}\n\n"
        f"Schema JSON (tra dung cac truong nay):\n{schema_text}\n\n"
        "Quy tac bat buoc:\n"
        f"- table_role chi duoc lay trong enum: {roles_text}. "
        "Khong duoc tu them vai tro moi.\n"
        "- confidence la so 0..1, chi do chac cua viec phan loai.\n"
        "- evidence la list chuoi ngan neu bang chung: ten bang, header, mau gia tri.\n"
        "- DU LIEU THAT THANG README: neu README noi bang nay la mot loai bang "
        "nhung header/mau gia tri khong co cot tuong ung (vi du noi la don hang "
        "nhung khong co ma don/ngay/trang thai), hay chon vai tro theo du lieu that "
        "hoac 'other', va ghi ro vao notes, khong duoc ghi de bang README.\n"
        "- Chi tra DUY NHAT object JSON, khong giai thich, khong tinh so.\n"
    )


def build_mapping_prompt(table_name, headers, sample_rows, readme_text, unmapped_semantics) -> str:
    """Xây prompt tiếng Việt yêu cầu mô hình gợi ý map cột khó.

    Đầu vào:
        table_name: tên bảng/sheet.
        headers: list tên cột thật của bảng (mô hình CHỈ được chọn
            cột trong list này, không được bịa tên cột mới).
        sample_rows: tối đa 10 dòng mẫu, giá trị dạng chuỗi.
        readme_text: README của shop (cắt tối đa 4000 ký tự đầu).
        unmapped_semantics: list semantic còn trống sau bước map
            rule-based của phase1. Mô hình CHỈ gợi ý cho các semantic
            này, không gợi ý lại semantic đã có map rule-based.

    Đầu ra:
        Chuỗi prompt hoàn chỉnh. Prompt yêu cầu mô hình: chỉ trả DUY
        NHẤT một object JSON theo schema ColumnMapping (mapping là
        dict semantic -> tên cột thật), không giải thích, không tính
        số, không bịa hệ số quy đổi hay trạng thái đơn. Prompt liệt
        kê đúng các semantic cần map và đúng các tên cột được phép
        chọn, để tầng validate có thể loại ngay map nào vi phạm.

    Vì sao chỉ map semantic còn trống:
        Map rule-based của phase1 đã được kiểm chứng bằng tên + mẫu
        và là nguồn tin cậy hơn gợi ý LLM. Cho LLM gợi ý lại semantic
        đã có map sẽ tạo xung đột và có nguy cơ ghi đè một map đúng
        bằng một map sai. Hợp đồng quy định LLM chỉ lấp chỗ trống.
    """
    readme_part = _truncate_readme(readme_text)
    if not readme_part:
        readme_part = "(Khong co README.)"
    if unmapped_semantics:
        semantics_text = ", ".join(str(s) for s in unmapped_semantics)
    else:
        semantics_text = "(Khong co semantic can map.)"
    schema_text = json.dumps(COLUMN_MAPPING_JSON_SCHEMA, ensure_ascii=False)
    return (
        "Ban la bo phan map cot cua he thong DataMate. "
        "Nhiem vu DUY NHAT cua ban la doc ten bang, header va cac dong mau "
        "duoi day, roi tra ve MOT object JSON theo dung schema, khong giai thich, "
        "khong tinh toan so lieu, khong them text ngoai JSON.\n\n"
        f"Ten bang: {table_name}\n"
        f"Header (CHI duoc chon cot trong danh sach nay, khong duoc bia ten cot moi): {_format_headers(headers)}\n"
        f"Cac dong mau (toi da 10 dong, gia tri dang chuoi):\n{_format_sample_rows(sample_rows, headers)}\n\n"
        f"README cua shop (cat toi da 4000 ky tu dau):\n{readme_part}\n\n"
        f"Cac semantic CAN MAP (chi map cac semantic nay, khong map semantic khac): {semantics_text}\n\n"
        f"Schema JSON (tra dung cac truong nay):\n{schema_text}\n\n"
        "Quy tac bat buoc:\n"
        "- mapping la dict semantic -> ten cot THAT, ten cot phai ton tai dung trong header tren. "
        "Cot khong co trong header thi khong duoc dua vao mapping.\n"
        "- semantic chi duoc lay trong danh sach CAN MAP tren (mot tap con cua 37 semantic cua hop dong).\n"
        "- Khong duoc bia he so quy doi, khong duoc bia trang thai don, khong duoc tu parse tien/ngay/so luong thanh so. "
        "Ban chi chon ten cot; viec parse so lieu do code dam nhan sau do.\n"
        "- confidence la so 0..1, chi do chac cua viec map cot.\n"
        "- evidence la list chuoi ngan neu bang chung: ten cot, mau gia tri.\n"
        "- Neu mot semantic khong xac dinh duoc cot phu hop: bo qua semantic do, khong duoc doan cot de lap cho trong.\n"
        "- Chi tra DUY NHAT object JSON, khong giai thich, khong tinh so.\n"
    )


def build_readme_prompt(readme_text, table_names) -> str:
    """Xây prompt tiếng Việt yêu cầu mô hình tóm tắt README của shop.

    Đầu vào:
        readme_text: nội dung README của shop (cắt tối đa 4000 ký tự
            đầu, xem :func:`_truncate_readme`).
        table_names: list tên các bảng của shop (để mô hình gắn vai
            trò cho đúng bảng đã có, không bịa tên bảng mới).

    Đầu ra:
        Chuỗi prompt hoàn chỉnh. Prompt yêu cầu mô hình: chỉ trả DUY
        NHẤT một object JSON theo schema ReadmeNotes (table_roles,
        status_vocabulary, unit_notes, date_notes,
        conversion_table_location), không giải thích, không tính số,
        không bịa hệ số quy đổi hay trạng thái đơn. Vai trò bảng chỉ
        trong enum TABLE_ROLES; bảng quy đổi chỉ nêu tên bảng (hoặc
        null), không nêu hệ số.

    Vì sao tóm tắt README vẫn chỉ là gợi ý:
        README có thể mô tả quy ước chung nhưng dữ liệu thật mới là
        chuẩn cuối cùng. Tóm tắt này chỉ dùng để gợi ý cho tầng
        ingest (ví dụ tìm bảng quy đổi ở đâu), còn hệ số, trạng thái,
        đơn vị thật đều phải đọc từ dữ liệu và kiểm chứng lại.
    """
    readme_part = _truncate_readme(readme_text)
    if not readme_part:
        readme_part = "(Khong co README.)"
    if table_names:
        tables_text = ", ".join(str(t) for t in table_names)
    else:
        tables_text = "(Khong co bang.)"
    schema_text = json.dumps(README_NOTES_JSON_SCHEMA, ensure_ascii=False)
    roles_text = ", ".join(TABLE_ROLES)
    return (
        "Ban la bo phan doc README cua he thong DataMate. "
        "Nhiem vu DUY NHAT cua ban la doc README duoi day, roi tra ve MOT object JSON "
        "theo dung schema, khong giai thich, khong tinh toan so lieu, khong them text ngoai JSON.\n\n"
        f"README cua shop (cat toi da 4000 ky tu dau):\n{readme_part}\n\n"
        f"Cac bang cua shop (chi gan vai tro cho cac bang nay, khong bia ten bang moi): {tables_text}\n\n"
        f"Schema JSON (tra dung cac truong nay):\n{schema_text}\n\n"
        "Quy tac bat buoc:\n"
        f"- table_roles la dict ten bang -> vai tro, vai tro chi trong enum: {roles_text}.\n"
        "- status_vocabulary la list cac trang thai don ma README nhac toi (chi la tu vung tham khao).\n"
        "- unit_notes la list ghi chu ngan ve don vi trong README.\n"
        "- date_notes la list ghi chu ngan ve quy uoc ngay trong README.\n"
        "- conversion_table_location la ten bang chua bang quy doi theo README, hoac null neu README khong neu ro. "
        "Chi neu TEN BANG, khong neu he so quy doi cu the (he so that phai doc tu du lieu bang do).\n"
        "- Khong duoc bia he so quy doi, khong duoc bia trang thai don, khong duoc tu parse tien/ngay/so luong thanh so.\n"
        "- Chi tra DUY NHAT object JSON, khong giai thich, khong tinh so.\n"
    )


# ---------------------------------------------------------------------------
# Validation chi tiết (trả cả lỗi, để repair prompt liệt kê được)
# ---------------------------------------------------------------------------


def _validate_table_classification_detailed(data, table_name, headers):
    """Validate chi tiết JSON phân loại bảng, trả (TableClassification, lỗi).

    Đầu vào:
        data: dict JSON mà mô hình trả về.
        table_name: tên bảng (để ghi lỗi rõ ràng, không dùng để suy
            luận thêm ngoài việc ghi chú).
        headers: list tên cột của bảng (hiện chỉ dùng để ghi lỗi rõ
            ràng; kiểm tra bằng chứng header theo quy tắc dữ liệu
            thật thắng README nằm ở :func:`classify_table`, vì ở đó
            mới có đủ ngữ cảnh để hạ vai trò kèm ghi chú).

    Đầu ra:
        Tuple ``(ket_qua, errors)``: ket_qua là TableClassification
        khi hợp lệ, None khi có lỗi; errors là list chuỗi lỗi (rỗng
        khi hợp lệ). Hàm public :func:`validate_table_classification`
        chỉ lấy phần ket_qua của hàm này.

    Luật validate (sai ở đâu thì lỗi ở đó, không tự sửa):
        - data không phải dict -> lỗi, không ép kiểu.
        - table_role vắng mặt hoặc ngoài enum TABLE_ROLES -> lỗi
          (không tự đổi thành 'other' ở đây, vì repair phải có cơ
          hội sửa đúng vai trò; tầng classify mới fallback 'other'
          sau khi hết repair).
        - confidence sai kiểu hoặc ngoài [0, 1] -> lỗi; vắng mặt thì
          mặc định 0.0 (không coi là sai, vì schema hợp đồng không
          bắt buộc confidence).
        - evidence/notes không phải list -> lỗi; phần tử không phải
          chuỗi thì ép thành chuỗi (chỉ là ghi chú, không ảnh hưởng
          dữ liệu tính toán).

    Trường hợp biên:
        - Trường lạ ngoài schema (ví dụ thêm trường tính toán) không
          bị coi là lỗi ở đây, mà bị bỏ qua khi xây TableClassification
          — vì dataclass chỉ giữ 4 trường của hợp đồng, trường lạ
          không lọt vào kết quả. (Khác với phase2_llm, nơi trường lạ
          bị coi là sai schema vì ParsedQuestion có trường tính toán
          nhạy cảm; ở đây trường lạ không mang ý nghĩa tính toán.)
    """
    errors = []
    if not isinstance(data, dict):
        return (None, [f"JSON phan loai bang '{table_name}' khong phai object."])
    role = data.get("table_role")
    if role not in TABLE_ROLES:
        errors.append(
            f"table_role {role!r} khong nam trong enum TABLE_ROLES "
            f"({', '.join(TABLE_ROLES)})."
        )
    confidence = _coerce_confidence(data.get("confidence"), errors)
    evidence = _coerce_string_list(data.get("evidence"), errors, "evidence")
    notes = _coerce_string_list(data.get("notes"), errors, "notes")
    if errors or confidence is None:
        return (None, errors)
    if data.get("confidence") is None:
        notes = notes + ["confidence vang mat, mac dinh 0.0 (khong phai do mo hinh bao)."]
    return (
        TableClassification(
            table_role=role,
            confidence=confidence,
            evidence=evidence,
            notes=notes,
        ),
        [],
    )


def _validate_column_mapping_detailed(data, df_columns, df):
    """Validate chi tiết JSON map cột, trả (map đã kiểm chứng, map bị loại, cảnh báo).

    Đầu vào:
        data: dict JSON mà mô hình trả về (phải có trường ``mapping``
            là dict semantic -> tên cột thật).
        df_columns: list tên cột thật của bảng (dùng để kiểm tra tồn
            tại; tên cột phải khớp đúng, kể cả hoa/thường và khoảng
            trắng, không chuẩn hoá lén).
        df: DataFrame của bảng (dùng để kiểm chứng mẫu giá trị, xem
            mục 5 hợp đồng). Nếu df là None thì chỉ kiểm tra tên,
            không kiểm chứng mẫu được — hàm ghi warning về việc này
            (xem đầu ra), và caller chạy thật (run_llm_ingest) luôn
            truyền df nên trường hợp này chỉ xảy ra khi gọi hàm rời.

    Đầu ra:
        Tuple ``(verified, rejected, warnings)`` trong đó:

        - ``verified``: dict semantic -> tên cột thật, CHỈ chứa map
          đã qua mọi kiểm tra (semantic trong 37 khóa, cột tồn tại,
          kiểm chứng mẫu đạt ngưỡng nếu thuộc nhóm tiền/ngày/số
          lượng). Đây cũng là giá trị mà hàm public
          :func:`validate_column_mapping` trả về.
        - ``rejected``: list dict, mỗi dict ghi ``semantic``,
          ``column`` (tên cột gợi ý, hoặc ``''`` khi gợi ý không nêu
          cột hợp lệ), ``reason`` (lý do loại bằng tiếng Việt).
        - ``warnings``: list cảnh báo (ví dụ phase1 normalizers không
          nạp được nên chỉ kiểm chứng bằng tên, hoặc df là None nên
          chưa kiểm chứng mẫu). Warning này KHÔNG bịa tỉ lệ số nào.

    Luật loại (không đoán, không repair bằng tên khác không tồn tại):
        - mapping vắng mặt hoặc không phải dict -> toàn bộ bị loại,
          rejected ghi một mục với semantic ``''`` và lý do rõ ràng.
        - semantic ngoài 37 khóa -> loại map đó, không đổi tên
          semantic thành một khóa gần giống.
        - tên cột không phải chuỗi, hoặc không tồn tại trong
          df_columns (và trong df nếu có) -> loại map đó ngay, không
          thử cột khác.
        - semantic tiền/ngày/số lượng: lấy tối đa 40 mẫu không trống
          đầu tiên của cột, parse bằng normalizers phase1; tỉ lệ parse
          được < 25% -> loại map đó. Cột toàn trống (không có mẫu)
          thì KHÔNG loại (tỉ lệ là None, giống phase1).
        - Các semantic còn lại: kiểm chứng bằng tên (đã qua việc cột
          tồn tại và semantic hợp lệ) hoặc để trống nếu không có mẫu;
          không dùng mẫu để bịa map.

    Trường hợp biên khi phase1 không nạp được:
        Tiền/ngày/số lượng không parse kiểm chứng được bằng hàm phase1
        thật, nên hàm dùng kiểm chứng tên thay thế và ghi warning rõ
        ràng: "normalizers phase1 khong nap duoc, chi kiem chung bang
        ten". Đây là bản sao quy tắc về ngưỡng, không phải bản sao
        hàm parse, và warning này phải được tầng trên đưa vào báo cáo
        để người đọc biết map đó chưa được kiểm chứng mẫu thật.
    """
    verified = {}
    rejected = []
    warnings = []
    if not isinstance(data, dict):
        return (
            {},
            [{"semantic": "", "column": "",
              "reason": "JSON map cot khong phai object."}],
            warnings,
        )
    mapping = data.get("mapping")
    if not isinstance(mapping, dict):
        return (
            {},
            [{"semantic": "", "column": "",
              "reason": "Truong 'mapping' vang mat hoac khong phai dict."}],
            warnings,
        )
    columns_set = set(df_columns) if df_columns else set()
    if df is None:
        warnings.append(
            "Khong co DataFrame de kiem chung mau gia tri, chi kiem tra ten cot."
        )
    if not _PHASE1_NORMALIZERS_AVAILABLE:
        warnings.append(
            "normalizers phase1 khong nap duoc, chi kiem chung bang ten "
            "(ban sao quy tac phase1 ve nguong 25%, khong phai ham parse that); "
            "map tien/ngay/so luong chua duoc kiem chung mau that."
        )
    # Sắp xếp semantic để thứ tự verified/rejected deterministic,
    # không phụ thuộc thứ tự key trong JSON của mô hình.
    for semantic in sorted(mapping):
        column = mapping[semantic]
        if semantic not in SEMANTIC_KEYS:
            rejected.append({
                "semantic": str(semantic),
                "column": column if isinstance(column, str) else "",
                "reason": (
                    f"Semantic '{semantic}' ngoai 37 khoa cua hop dong, "
                    "khong duoc map."
                ),
            })
            continue
        if not isinstance(column, str) or not column:
            rejected.append({
                "semantic": semantic,
                "column": "",
                "reason": f"Ten cot goi y cho semantic '{semantic}' khong phai chuoi hop le.",
            })
            continue
        if columns_set and column not in columns_set:
            rejected.append({
                "semantic": semantic,
                "column": column,
                "reason": (
                    f"Cot '{column}' khong ton tai trong DataFrame cua bang, "
                    "loai ngay, khong doan cot khac thay the."
                ),
            })
            continue
        if df is not None:
            try:
                df_has = column in list(df.columns)
            except Exception:
                df_has = False
            if not df_has:
                rejected.append({
                    "semantic": semantic,
                    "column": column,
                    "reason": (
                        f"Cot '{column}' khong ton tai trong DataFrame cua bang, "
                        "loai ngay, khong doan cot khac thay the."
                    ),
                })
                continue
        rate = _sample_validation_rate(semantic, df, column)
        if rate is not None and rate < SAMPLE_VALIDATION_THRESHOLD:
            rejected.append({
                "semantic": semantic,
                "column": column,
                "reason": (
                    f"Cot '{column}' cho semantic '{semantic}' chi co "
                    f"{rate:.0%} mau khong trong parse duoc bang normalizers "
                    "phase1, duoi nguong 25% cua hop dong."
                ),
            })
            continue
        verified[semantic] = column
    return (verified, rejected, warnings)


def _validate_readme_notes_detailed(data):
    """Validate chi tiết JSON tóm tắt README, trả (ReadmeNotes, lỗi).

    Đầu vào:
        data: dict JSON mà mô hình trả về.

    Đầu ra:
        Tuple ``(ket_qua, errors)``: ket_qua là ReadmeNotes khi hợp
        lệ, None khi có lỗi nghiêm trọng; errors là list chuỗi lỗi.
        Hàm public :func:`validate_readme_notes` chỉ lấy phần ket_qua.

    Luật validate (loại phần sai, giữ phần đúng, không bịa):
        - data không phải dict -> lỗi toàn bộ, trả None.
        - table_roles không phải dict -> lỗi toàn bộ, trả None; từng
          vai trò ngoài enum TABLE_ROLES thì bị loại khỏi dict (ghi
          vào errors để repair có cơ hội sửa), các vai trò hợp lệ
          vẫn giữ. Nếu sau khi loại mà errors còn lại thì kết quả
          vẫn là None ở lần validate này, để repair sửa rồi mới nhận
          — tránh nhận một tóm tắt nửa đúng nửa sai mà không báo.
        - status_vocabulary/unit_notes/date_notes không phải list ->
          lỗi; phần tử không phải chuỗi thì ép thành chuỗi.
        - conversion_table_location: phải là chuỗi hoặc null; kiểu
          khác thì đặt thành None kèm lỗi (để repair sửa), vì vị trí
          bảng quy đổi sai kiểu không thể tin được.

    Trường hợp biên:
        - Các trường vắng mặt đều có mặc định rỗng/None hợp lệ, vì
          schema hợp đồng không bắt buộc trường nào của ReadmeNotes;
          một README không nhắc tới trạng thái hay đơn vị thì tóm tắt
          vẫn hợp lệ với list rỗng, không phải lỗi.
    """
    errors = []
    if not isinstance(data, dict):
        return (None, ["JSON tom tat README khong phai object."])
    table_roles_raw = data.get("table_roles")
    table_roles = {}
    if table_roles_raw is None:
        table_roles = {}
    elif not isinstance(table_roles_raw, dict):
        errors.append("table_roles phai la dict ten bang -> vai tro.")
    else:
        for table_name in sorted(table_roles_raw):
            role = table_roles_raw[table_name]
            if role in TABLE_ROLES:
                table_roles[str(table_name)] = role
            else:
                errors.append(
                    f"table_roles['{table_name}'] = {role!r} ngoai enum TABLE_ROLES, loai muc nay."
                )
    status_vocabulary = _coerce_string_list(
        data.get("status_vocabulary"), errors, "status_vocabulary"
    )
    unit_notes = _coerce_string_list(data.get("unit_notes"), errors, "unit_notes")
    date_notes = _coerce_string_list(data.get("date_notes"), errors, "date_notes")
    location = data.get("conversion_table_location")
    if location is not None and not isinstance(location, str):
        errors.append(
            "conversion_table_location phai la chuoi ten bang hoac null, "
            f"nhan duoc {type(location).__name__}."
        )
        location = None
    if errors:
        return (None, errors)
    return (
        ReadmeNotes(
            table_roles=table_roles,
            status_vocabulary=status_vocabulary,
            unit_notes=unit_notes,
            date_notes=date_notes,
            conversion_table_location=location,
        ),
        [],
    )


# ---------------------------------------------------------------------------
# Hàm validate public (đúng tên/chữ ký của hợp đồng mục 4)
# ---------------------------------------------------------------------------


def validate_table_classification(data: dict, table_name, headers) -> TableClassification | None:
    """Kiểm chứng JSON phân loại bảng và trả TableClassification, hoặc None khi sai.

    Đầu vào:
        data: dict JSON mà mô hình trả về.
        table_name: tên bảng (để ghi lỗi rõ ràng).
        headers: list tên cột của bảng.

    Đầu ra:
        TableClassification khi JSON hợp lệ (vai trò trong enum,
        confidence 0..1, evidence/notes là list chuỗi); None khi JSON
        sai schema, vai trò ngoài enum, hoặc confidence sai — khi đó
        caller phải đi repair (tối đa 2 lần) hoặc fallback, không được
        tự đổi vai trò sai thành một vai trò đoán.

    Vì sao hàm này không áp quy tắc "dữ liệu thật thắng README":
        Quy tắc đó cần ngữ cảnh bảng đầy đủ và phải kèm ghi chú hạ
        vai trò; nó nằm ở :func:`classify_table` (tầng gọi validate
        rồi kiểm tra bằng chứng header). Hàm validate thuần túy này
        chỉ kiểm tra JSON có đúng schema hay không, để tầng repair
        và tầng test đều dùng được một chuẩn duy nhất.
    """
    result, _errors = _validate_table_classification_detailed(data, table_name, headers)
    return result


def validate_column_mapping(data: dict, df_columns, df) -> dict:
    """Kiểm chứng JSON map cột và chỉ trả map đã qua kiểm chứng mẫu.

    Đầu vào:
        data: dict JSON mà mô hình trả về (trường ``mapping``).
        df_columns: list tên cột thật của bảng.
        df: DataFrame của bảng (dùng để kiểm chứng mẫu giá trị).

    Đầu ra:
        Dict semantic -> tên cột thật, CHỈ chứa map đã qua kiểm chứng
        (xem :func:`_validate_column_mapping_detailed` để rõ từng luật
        loại). Map nào không qua kiểm chứng sẽ KHÔNG có trong dict
        này — caller nào cần lý do loại thì dùng hàm chi tiết nội bộ
        hoặc xem ``rejected_mappings`` của :func:`run_llm_ingest_for_shop`.

    Trường hợp biên:
        - data không phải dict hoặc mapping không phải dict -> dict
          rỗng (không crash, không đoán map nào).
        - df là None hoặc normalizers phase1 không nạp được -> hàm
          chỉ kiểm tra tên cột; warning về việc chưa kiểm chứng mẫu
          thật nằm ở hàm chi tiết và ở :func:`run_llm_ingest_for_shop`
          (hàm public này không có kênh trả warning theo hợp đồng).
    """
    verified, _rejected, _warnings = _validate_column_mapping_detailed(
        data, df_columns, df
    )
    return verified


def validate_readme_notes(data: dict) -> ReadmeNotes | None:
    """Kiểm chứng JSON tóm tắt README và trả ReadmeNotes, hoặc None khi sai.

    Đầu vào:
        data: dict JSON mà mô hình trả về.

    Đầu ra:
        ReadmeNotes khi JSON hợp lệ (mọi vai trò trong table_roles
        đều thuộc enum TABLE_ROLES, các list ghi chú là list chuỗi,
        conversion_table_location là chuỗi hoặc null); None khi JSON
        sai — khi đó caller phải đi repair (tối đa 2 lần) hoặc trả
        None để tầng trên fallback, không được tự bịa tóm tắt.

    Trường hợp biên:
        - Trường vắng mặt được điền mặc định rỗng/None hợp lệ, vì
          schema hợp đồng không bắt buộc trường nào của ReadmeNotes.
        - Vai trò ngoài enum trong table_roles làm cả JSON bị trả
          None ở lần validate này (để repair sửa), thay vì lặng lẽ
          nhận một tóm tắt đã bị cắt xén mà caller không biết.
    """
    result, _errors = _validate_readme_notes_detailed(data)
    return result


# ---------------------------------------------------------------------------
# Hàm cấp cao: phân loại, gợi ý map, tóm tắt README
# ---------------------------------------------------------------------------


def _sample_rows_from_df(df, headers):
    """Lấy tối đa 10 dòng mẫu từ DataFrame, dưới dạng list dict chuỗi.

    Đầu vào:
        df: DataFrame của bảng (hoặc None).
        headers: list tên cột (để gắn nhãn dòng mẫu).

    Đầu ra:
        List tối đa 10 dict, mỗi dict là một dòng với giá trị đã
        chuyển thành chuỗi (xem :func:`_stringify_value`). List rỗng
        khi df là None hoặc bảng không có dòng. Hàm này chỉ phục vụ
        việc xây prompt, không parse số liệu gì.

    Trường hợp biên:
        - df không phải DataFrame (không có head/to_dict) -> list
          rỗng, không crash, prompt sẽ ghi là không có dòng mẫu.
    """
    if df is None:
        return []
    try:
        records = df.head(MAX_SAMPLE_ROWS).to_dict("records")
    except Exception:
        return []
    rows = []
    for record in records:
        if isinstance(record, dict):
            rows.append({k: _stringify_value(v) for k, v in record.items()})
    return rows


def _classify_table_detailed(table_name, headers, sample_rows, readme_text, backend=None):
    """Phân loại bảng kèm nguồn, trả (TableClassification, nguồn, lỗi).

    Đầu vào:
        table_name, headers, sample_rows, readme_text: như
            :func:`classify_table`.
        backend: backend mô hình hoặc None.

    Đầu ra:
        Tuple ``(classification, source, errors)`` trong đó source là
        ``'llm'`` (hợp lệ ngay lần đầu), ``'repair'`` (hợp lệ sau
        repair), hoặc ``'fallback'`` (không có backend, backend lỗi,
        hoặc sau 2 lần repair vẫn sai — khi đó classification là
        TableClassification với role ``'other'``, confidence 0.0 và
        notes ghi rõ lý do fallback). Hàm public
        :func:`classify_table` chỉ lấy phần classification.

    Quy tắc "dữ liệu thật thắng README" áp ở đây, sau validate:
        Nếu classification từ LLM có vai trò cụ thể nhưng header
        không có bằng chứng nào cho vai trò đó (xem
        :func:`_headers_have_role_evidence`), vai trò bị hạ xuống
        ``'other'``, confidence giữ nguyên không tăng, notes thêm
        ghi chú giải thích. Nguồn vẫn tính là nguồn LLM (vì JSON đã
        hợp lệ), nhưng vai trò cuối cùng theo dữ liệu thật.
    """
    prompt = build_table_prompt(table_name, headers, sample_rows, readme_text)

    def _validate(data):
        return _validate_table_classification_detailed(data, table_name, headers)

    result, errors, source = _call_with_repair(
        backend, prompt, TABLE_CLASSIFICATION_JSON_SCHEMA, _validate
    )
    if result is None:
        notes = ["Fallback rule-based: khong co phan loai LLM hop le."] + errors
        return (
            TableClassification(table_role="other", confidence=0.0,
                                evidence=[], notes=notes),
            "fallback",
            errors,
        )
    if not _headers_have_role_evidence(headers, result.table_role):
        notes = list(result.notes) + [
            "Du lieu that thang README: header khong co cot tuong ung "
            f"(ma don/ngay/trang thai hoac cot dac trung) cho vai tro "
            f"'{result.table_role}', ha xuong 'other'."
        ]
        result = TableClassification(
            table_role="other",
            confidence=result.confidence,
            evidence=result.evidence,
            notes=notes,
        )
    return (result, source, [])


def classify_table(table_name, headers, sample_rows, readme_text, backend=None) -> TableClassification:
    """Phân loại một bảng bằng LLM, fallback 'other' khi không có gợi ý hợp lệ.

    Đầu vào:
        table_name: tên bảng/sheet.
        headers: list tên cột của bảng.
        sample_rows: tối đa 10 dòng mẫu (giá trị dạng chuỗi; xem
            :func:`_format_sample_rows`).
        readme_text: README của shop (cắt tối đa 4000 ký tự đầu).
        backend: backend mô hình (LocalModelBackend) hoặc None. Khi
            None, hàm không gọi model mà fallback ngay, không crash.

    Đầu ra:
        TableClassification (không bao giờ None, theo hợp đồng):
        vai trò trong enum TABLE_ROLES, confidence 0..1, evidence và
        notes. Khi LLM không khả dụng, backend lỗi, hoặc JSON sai sau
        tối đa 2 lần repair, hàm trả role ``'other'``, confidence 0.0
        và notes ghi rõ lý do fallback — để tầng trên biết đây là
        fallback, không phải một phân loại đã kiểm chứng.

    Vì sao áp quy tắc "dữ liệu thật thắng README" ở đây:
        Xem :func:`_classify_table_detailed`: vai trò LLM gợi ý nhưng
        header không có bằng chứng nào tương ứng sẽ bị hạ xuống
        ``'other'`` kèm ghi chú, thay vì để README ghi đè dữ liệu thật.

    Trường hợp biên:
        - headers/sample_rows rỗng -> vẫn gọi LLM nếu có backend
          (mô hình chỉ có tên bảng/README để đoán), nhưng kiểm tra
          bằng chứng header sẽ bỏ qua khi headers rỗng (không có dữ
          liệu để phản bác).
        - backend raise RuntimeError (thiếu gói/weights/model_id) ->
          bắt lỗi và fallback, không để exception lan ra giết pipeline.
    """
    result, _source, _errors = _classify_table_detailed(
        table_name, headers, sample_rows, readme_text, backend
    )
    return result


def _suggest_mapping_detailed(table_name, df, readme_text, unmapped_semantics, backend=None):
    """Gợi ý map cột kèm map bị loại, trả (map đã kiểm chứng, map bị loại, cảnh báo, nguồn).

    Đầu vào:
        table_name: tên bảng.
        df: DataFrame của bảng.
        readme_text: README của shop (cắt tối đa 4000 ký tự đầu).
        unmapped_semantics: list semantic còn trống sau map rule-based;
            hàm CHỈ xin gợi ý cho các semantic này, không gợi ý lại
            semantic đã có map (xem :func:`build_mapping_prompt`).
        backend: backend mô hình hoặc None.

    Đầu ra:
        Tuple ``(verified, rejected, warnings, source)`` trong đó
        verified là dict semantic -> cột thật đã qua kiểm chứng mẫu,
        rejected là list dict (semantic, column, reason) như ở hàm
        validate chi tiết, warnings là list cảnh báo, source là
        ``'llm'``/``'repair'``/``'fallback'`` (xem
        :func:`_call_with_repair`). Khi backend là None hoặc lỗi thì
        verified/rejected đều rỗng và source là fallback — không có
        gợi ý nào thì không có gì để loại, và cũng không có gì để áp.

    Vì sao validate ở đây khác validate thuần túy:
        Validate thuần túy (:func:`validate_column_mapping`) chỉ trả
        map đã qua kiểm chứng, làm mất lý do loại. Hàm chi tiết này
        giữ lại rejected để :func:`run_llm_ingest_for_shop` đưa vào
        ``rejected_mappings`` cho báo cáo truy vết được.
    """
    if df is None:
        return ({}, [], ["Khong co DataFrame cho bang, khong goi y map."], "fallback")
    try:
        headers = [str(c) for c in df.columns]
    except Exception:
        headers = []
    sample_rows = _sample_rows_from_df(df, headers)
    wanted = [s for s in (unmapped_semantics or []) if s in SEMANTIC_KEYS]
    if not wanted:
        return ({}, [], [], "fallback")
    prompt = build_mapping_prompt(table_name, headers, sample_rows, readme_text, wanted)

    def _validate(data):
        verified, rejected, warnings = _validate_column_mapping_detailed(
            data, headers, df
        )
        # _call_with_repair cần phân biệt "hợp lệ" với "có lỗi cần
        # repair": một JSON có mapping hợp lệ về schema nhưng một vài
        # map bị loại vì mẫu dưới 25% vẫn là JSON hợp lệ, không cần
        # repair (repair không cứu được map sai mẫu, vì mẫu là dữ
        # liệu thật). Chỉ khi mapping vắng/không phải dict, hoặc có
        # semantic ngoài 37 khóa, hoặc cột không tồn tại thì mới cần
        # repair — vì đó là lỗi schema mà mô hình có thể sửa.
        # Vì vậy ở đây: nếu data có mapping là dict thì coi là hợp lệ
        # (trả verified, kể cả khi verified rỗng vì mẫu yếu), và
        # rejected được giữ lại; ngược lại thì trả lỗi để repair.
        if not isinstance(data, dict) or not isinstance(data.get("mapping"), dict):
            return (None, ["Truong 'mapping' vang mat hoac khong phai dict."])
        schema_errors = []
        for semantic in sorted(data["mapping"]):
            column = data["mapping"][semantic]
            if semantic not in SEMANTIC_KEYS:
                schema_errors.append(
                    f"Semantic '{semantic}' ngoai 37 khoa cua hop dong."
                )
            elif not isinstance(column, str) or column not in headers:
                schema_errors.append(
                    f"Cot goi y '{column}' cho semantic '{semantic}' "
                    "khong ton tai trong header cua bang."
                )
        if schema_errors:
            return (None, schema_errors)
        # Gói verified/rejected/warnings vào một dict trung gian để
        # _call_with_repair trả về được cả ba; caller sẽ bóc ra.
        return ({"verified": verified, "rejected": rejected,
                 "warnings": warnings}, [])

    result, errors, source = _call_with_repair(
        backend, prompt, COLUMN_MAPPING_JSON_SCHEMA, _validate
    )
    if result is None:
        return ({}, [], errors, "fallback")
    return (result["verified"], result["rejected"], result["warnings"], source)


def suggest_mapping(table_name, df, readme_text, unmapped_semantics, backend=None) -> dict:
    """Gợi ý map cột cho các semantic còn trống, chỉ trả map đã qua kiểm chứng mẫu.

    Đầu vào:
        table_name: tên bảng.
        df: DataFrame của bảng.
        readme_text: README của shop (cắt tối đa 4000 ký tự đầu).
        unmapped_semantics: list semantic còn trống sau map rule-based
            của phase1. Hàm CHỈ gợi ý cho các semantic này, KHÔNG gợi
            ý lại và KHÔNG ghi đè map rule-based đã có.
        backend: backend mô hình hoặc None. Khi None, hàm trả dict
            rỗng (fallback), không crash.

    Đầu ra:
        Dict semantic -> tên cột thật, CHỈ chứa map đã qua kiểm chứng
        (semantic trong 37 khóa, cột tồn tại trong DataFrame, mẫu
        tiền/ngày/số lượng đạt tối thiểu 25% parse được bằng
        normalizers phase1, hoặc cột toàn trống). Map không qua kiểm
        chứng sẽ KHÔNG có trong dict này — caller cần lý do loại thì
        xem ``rejected_mappings`` của :func:`run_llm_ingest_for_shop`.

    Trường hợp biên:
        - unmapped_semantics rỗng hoặc toàn semantic ngoài 37 khóa ->
          dict rỗng, không gọi backend (không có gì để hỏi).
        - df là None -> dict rỗng, không crash.
        - backend lỗi hoặc JSON sai sau tối đa 2 lần repair -> dict
          rỗng (fallback), không đoán map nào.
    """
    verified, _rejected, _warnings, _source = _suggest_mapping_detailed(
        table_name, df, readme_text, unmapped_semantics, backend
    )
    return verified


def summarize_readme(readme_text, table_names, backend=None) -> ReadmeNotes | None:
    """Tóm tắt README của shop bằng LLM, trả None khi không có tóm tắt hợp lệ.

    Đầu vào:
        readme_text: nội dung README của shop (cắt tối đa 4000 ký tự
            đầu). Khi rỗng/None thì hàm trả None ngay, không gọi
            backend (không có gì để tóm tắt).
        table_names: list tên các bảng của shop.
        backend: backend mô hình hoặc None. Khi None, hàm trả None
            (fallback), không crash.

    Đầu ra:
        ReadmeNotes khi JSON hợp lệ (mọi vai trò trong enum, các list
        ghi chú là list chuỗi, conversion_table_location là chuỗi
        hoặc null); None khi không có README, không có backend,
        backend lỗi, hoặc JSON sai sau tối đa 2 lần repair.

    Vì sao trả None thay vì ReadmeNotes rỗng khi fallback:
        ReadmeNotes rỗng mang ý nghĩa "README đã đọc và không có gì",
        trong khi fallback mang ý nghĩa "chưa đọc được README". Tầng
        trên phải phân biệt hai trường hợp này để không tưởng là đã
        xác nhận README trống.

    Trường hợp biên:
        - README chỉ có khoảng trắng -> coi như rỗng, trả None.
        - table_names rỗng -> vẫn gọi LLM nếu có README (mô hình tóm
          tắt chung, table_roles sẽ rỗng), vì README vẫn có thể chứa
          từ vựng trạng thái/đơn vị hữu ích.
    """
    if not readme_text or not str(readme_text).strip():
        return None
    prompt = build_readme_prompt(readme_text, table_names)

    def _validate(data):
        return _validate_readme_notes_detailed(data)

    result, _errors, _source = _call_with_repair(
        backend, prompt, README_NOTES_JSON_SCHEMA, _validate
    )
    return result


def run_llm_ingest_for_shop(shop_result, readme_text, backend=None) -> LlmIngestResult:
    """Chạy LLM ingest cho một shop, trả LlmIngestResult đúng hợp đồng mục 4.

    Đầu vào:
        shop_result: kết quả ingest phase1 của shop — có thể là
            đối tượng :class:`IngestResult` của phase1 (có thuộc tính
            ``business_id`` và ``tables`` là dict tên bảng ->
            DataFrame), hoặc dict có khóa ``'business_id'`` và
            ``'tables'`` tương ứng. Hàm chỉ đọc shop_result, không
            sửa DataFrame hay hồ sơ phase1 nào.
        readme_text: README của shop (có thể None/rỗng; khi đó
            readme_notes là None và prompt các bảng ghi là không có
            README).
        backend: backend mô hình hoặc None. Khi None, mọi bảng đều
            fallback (sources ``'rule_fallback'``, added_mappings
            rỗng), không crash.

    Đầu ra:
        LlmIngestResult với đầy đủ 8 trường của hợp đồng (xem
        docstring của dataclass). Luồng cho mỗi bảng, theo thứ tự
        tên bảng đã sắp xếp (deterministic):

        1. Phân loại bảng bằng :func:`classify_table` (kèm quy tắc
           dữ liệu thật thắng README).
        2. Tính map rule-based bằng ``map_columns`` của phase1 (nếu
           nạp được); chỉ xét LLM cho các semantic còn trống trong
           map đó, không ghi đè map rule-based.
        3. Gợi ý map bằng LLM cho semantic còn trống, chỉ giữ map đã
           qua kiểm chứng mẫu; map bị loại đưa vào rejected_mappings
           kèm tên bảng, semantic, cột và lý do.
        4. Ghi sources cho bảng: ``'llm'`` nếu ít nhất một bước LLM
           của bảng thành công (phân loại hoặc gợi ý map), ngược lại
           ``'rule_fallback'``; fallback_count đếm số bảng fallback.

    Vì sao không chạy lại forward-fill/dedup/hợp nhất/parse ở đây:
        Module này chỉ gợi ý và kiểm chứng; mọi bước chuẩn hoá vẫn do
        phase1 làm. Hàm này không trả rows_in/rows_out và không đổi
        dữ liệu bảng, nên không thể làm lệch số dòng của baseline —
        việc đối chiếu rows thuộc về runner (Child 2).

    Trường hợp biên:
        - shop_result không có tables hoặc tables rỗng -> trả kết quả
          với business_id (nếu có), các dict rỗng, readme_notes từ
          summarize_readme (hoặc None), fallback_count 0.
        - Bảng không phải DataFrame (không có columns) -> bỏ qua bảng
          đó kèm warning, không crash cả shop.
        - phase1 map_columns không nạp được -> coi mọi semantic là
          còn trống, ghi warning một lần cho cả shop; LLM gợi ý vẫn
          phải qua kiểm chứng mẫu trước khi vào added_mappings.
        - normalizers phase1 không nạp được -> map tiền/ngày/số lượng
          chỉ kiểm chứng bằng tên, warning ghi rõ ở từng bảng có map
          loại đó (xem validate chi tiết), không bịa tỉ lệ mẫu.
    """
    if isinstance(shop_result, dict):
        business_id = str(shop_result.get("business_id", "") or "")
        tables = shop_result.get("tables", {}) or {}
    else:
        business_id = str(getattr(shop_result, "business_id", "") or "")
        tables = getattr(shop_result, "tables", {}) or {}
    if not isinstance(tables, dict):
        tables = {}

    result = LlmIngestResult(business_id=business_id)
    table_names = sorted(str(name) for name in tables)
    if not _PHASE1_INGEST_AVAILABLE:
        result.warnings.append(
            "map_columns phase1 khong nap duoc, coi moi semantic la chua map "
            "va chi goi y LLM cho cac semantic con trong theo dinh nghia nay."
        )
    if readme_text:
        readme_notes = summarize_readme(readme_text, table_names, backend)
        if readme_notes is None and backend is not None:
            result.warnings.append(
                "Tom tat README bang LLM that bai hoac JSON sai sau repair, "
                "readme_notes la None (fallback)."
            )
        result.readme_notes = readme_notes
    else:
        result.readme_notes = None

    for table_name in table_names:
        df = tables[table_name]
        try:
            headers = [str(c) for c in df.columns]
        except Exception:
            result.warnings.append(
                f"Bang '{table_name}' khong co columns hop le, bo qua bang nay."
            )
            continue
        sample_rows = _sample_rows_from_df(df, headers)

        classification, class_source, _class_errors = _classify_table_detailed(
            table_name, headers, sample_rows, readme_text, backend
        )
        result.table_roles[table_name] = classification.table_role

        if _phase1_map_columns is not None:
            try:
                rule_map = _phase1_map_columns(df) or {}
            except Exception as exc:
                rule_map = {}
                result.warnings.append(
                    f"map_columns phase1 loi o bang '{table_name}': {exc}, "
                    "coi moi semantic la chua map."
                )
        else:
            rule_map = {}
        unmapped = [s for s in SEMANTIC_KEYS if s not in rule_map]

        verified, rejected, map_warnings, map_source = _suggest_mapping_detailed(
            table_name, df, readme_text, unmapped, backend
        )
        for warning in map_warnings:
            # Chỉ ghi warning kiểm chứng tên một lần cho mỗi nội dung,
            # tránh lặp lại cùng một cảnh báo cho mọi bảng.
            tagged = f"Bang '{table_name}': {warning}"
            if tagged not in result.warnings:
                result.warnings.append(tagged)
        if verified:
            result.added_mappings[table_name] = dict(sorted(verified.items()))
        for item in rejected:
            result.rejected_mappings.append({
                "semantic": item.get("semantic", ""),
                "column": item.get("column", ""),
                "reason": item.get("reason", ""),
                "table": table_name,
            })

        if class_source in ("llm", "repair") or map_source in ("llm", "repair"):
            result.sources[table_name] = "llm"
        else:
            result.sources[table_name] = "rule_fallback"
    result.fallback_count = sum(
        1 for source in result.sources.values() if source == "rule_fallback"
    )
    return result

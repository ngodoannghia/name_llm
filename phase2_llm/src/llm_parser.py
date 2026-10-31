# -*- coding: utf-8 -*-
"""Parser câu hỏi DataMate bằng mô hình ngôn ngữ cục bộ (Giai đoạn 2 LLM).

Mục đích của module này
------------------------
Bổ sung cho parser template của Giai đoạn 2 (``phase2/src/parser.py``):
khi template không nhận diện được câu hỏi, một mô hình ngôn ngữ chạy
cục bộ, offline sẽ đọc câu hỏi và trả về một JSON theo đúng hợp đồng
:class:`ParsedQuestion` của Giai đoạn 2, để tầng solver vẫn chỉ tính
toán bằng code trên dữ liệu đã chuẩn hoá, không tự nhẩm số.

Luồng làm việc, theo đúng thứ tự ưu tiên an toàn cho điểm PA
(sai bị trừ, abstain được 0):

1. Xây prompt tiếng Việt từ câu hỏi, câu đã chuẩn hoá không dấu,
   ``as_of_date``, danh sách tên/mã thực thể của chính shop đang chạy
   (chỉ tên/mã, KHÔNG đưa dữ liệu dòng đơn) và tóm tắt rulebook.
2. Gọi backend mô hình cục bộ để lấy JSON.
3. Kiểm chứng JSON bằng :func:`validate_and_build`: measure phải nằm
   trong enum của hợp đồng, kỳ phải hợp lệ, thực thể phải tồn tại
   trong shop. Sai ở đâu thì sửa ở đó.
4. Nếu JSON sai, gọi lại backend tối đa 2 lần với repair prompt liệt
   kê lỗi. Sau 2 lần vẫn sai thì fallback về parser template.
5. Nếu backend không khả dụng, hoặc thực thể/kỳ không kiểm chứng
   được, thì fallback về parser template và KHÔNG BAO GIỜ đoán thực
   thể hay kỳ để lấp chỗ trống.

TRUNG THỰC VỀ MODEL — ĐỌC KỸ TRƯỚC KHI ĐÓNG DOCKER:
    Model mặc định CHƯA được chọn và CHƯA được kiểm chứng trong lúc
    viết module này, vì môi trường làm việc này không có sẵn gói
    ``transformers`` và không có sẵn weights mô hình cục bộ nào
    (đã kiểm tra thật bằng ``import transformers`` và bằng cách liệt
    kê cache Hugging Face: cả hai đều trống). Vì vậy module này KHÔNG
    ghi tên một model cục bộ cụ thể kèm licence hay ngày phát hành,
    để tránh bịa thông tin chưa kiểm chứng. ``model_id`` là một tham
    số cấu hình do người tích hợp truyền vào. Trước khi đóng Docker,
    bắt buộc phải chọn một model public có dung lượng <= 15B, phát
    hành trước ngày 01/10/2026, có licence cho phép dùng cho mục đích
    này, và kiểm chứng bằng cách chạy thật
    :class:`TransformersBackend` với model đó. Tuyệt đối không gọi
    ChatGPT hay bất kỳ API thương mại nào: backend duy nhất được hỗ
    trợ ở đây là mô hình chạy cục bộ, offline.

Nguyên tắc tổng quát hoá
------------------------
Module này không biết gì về business_id, id câu hỏi, tên cột train,
ngành train, rulebook train hay đáp án train. Mọi kiểm chứng thực thể
đều dựa trên danh sách products/customers/groups/payments của chính
shop đang chạy (tham số truyền vào), và mọi kiểm chứng kỳ đều dựa
trên ngày tháng hợp lệ theo lịch dương, không hard-code lịch Tết hay
ngày lễ nào. Khi không kiểm chứng được, module fallback hoặc trả
``measure='unknown'`` để tầng gate abstain thay vì đoán.

Chỉ dùng thư viện chuẩn Python ở tầng prompt/validation/fallback;
``transformers`` chỉ được import lười (lazy) bên trong
:class:`TransformersBackend`, để module vẫn import và test được ở môi
trường chưa có mô hình, và để lỗi thiếu weights được báo rõ ràng thay
vì giả vờ trả JSON.

Ghi chú về tổ chức file (theo yêu cầu giao việc): backend KHÔNG tách
thành ``src/backends.py`` riêng mà gộp vào chính file này (các class
:class:`LocalModelBackend` và :class:`TransformersBackend`), vì backend
chỉ có hai class và gắn chặt với schema/validation ở đây; tách file
chỉ làm tăng điểm import mà không thêm giá trị tổng quát hoá nào.
"""

from __future__ import annotations

import calendar as _calendar_mod
import datetime
import json
import re
import sys
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ParsedQuestion",
    "LocalModelBackend",
    "TransformersBackend",
    "ParseResult",
    "PARSED_QUESTION_JSON_SCHEMA",
    "MEASURE_VALUES",
    "PERIOD_KIND_VALUES",
    "ENTITY_KIND_VALUES",
    "RANKING_TARGET_VALUES",
    "RANKING_METRIC_VALUES",
    "ANSWER_TYPE_VALUES",
    "THRESHOLD_OP_VALUES",
    "DEFAULT_MODEL_ID",
    "build_prompt",
    "build_repair_prompt",
    "summarize_rulebook",
    "validate_and_build",
    "parse_with_llm",
]

# ---------------------------------------------------------------------------
# Nạp ParsedQuestion và expand_abbreviations của phase2 bằng đường dẫn
# tương đối từ file này (không hard-code đường dẫn tuyệt đối của máy).
# File này nằm ở <datamate>/phase2_llm/src/llm_parser.py, nên phase2/src
# nằm ở parents[2]/phase2/src.
# ---------------------------------------------------------------------------
_PHASE2_SRC = Path(__file__).resolve().parents[2] / "phase2" / "src"
if _PHASE2_SRC.is_dir() and str(_PHASE2_SRC) not in sys.path:
    sys.path.insert(0, str(_PHASE2_SRC))

try:  # Ưu tiên dùng đúng ParsedQuestion đã kiểm chứng ở Giai đoạn 2.
    from parser import ParsedQuestion as _Phase2ParsedQuestion
    from parser import expand_abbreviations as _phase2_expand

    ParsedQuestion = _Phase2ParsedQuestion
    _PHASE2_AVAILABLE = True
except Exception:  # pragma: no cover - dự phòng khi chạy file rời.
    # Nếu phase2 không nạp được (ví dụ file bị copy riêng ra ngoài),
    # dùng bản dự phòng có đúng các trường của hợp đồng, để module
    # vẫn chạy và test được thay vì crash cả pipeline. Bản dự phòng
    # này không thêm trường bắt buộc mới nào so với hợp đồng.
    _PHASE2_AVAILABLE = False

    @dataclass
    class ParsedQuestion:  # type: ignore[no-redef]
        """Bản dự phòng của ParsedQuestion khi phase2 chưa nạp được."""

        measure: str = "unknown"
        entity_kind: str = "none"
        entity_key: str | None = None
        entity_display: str | None = None
        period_start: datetime.date | None = None
        period_end: datetime.date | None = None
        period_kind: str = "none"
        period_months: list = field(default_factory=list)
        ranking_k: int | None = None
        ranking_target: str = "none"
        ranking_metric: str | None = None
        unit: str | None = None
        threshold_money: float | None = None
        threshold_op: str | None = None
        answer_type: str = "unknown"
        confidence_parse: float = 0.0
        parse_notes: list = field(default_factory=list)
        normalized_text: str = ""

    def _phase2_expand(text):
        """Bản dự phòng: thường hoá, bỏ dấu, giữ nguyên từ ngữ."""
        if text is None:
            return ""
        s = str(text).lower()
        decomposed = unicodedata.normalize("NFD", s)
        without = "".join(
            ch for ch in decomposed if unicodedata.category(ch) != "Mn"
        )
        return " ".join(
            unicodedata.normalize(
                "NFC", without.translate(str.maketrans({"đ": "d", "Đ": "D"}))
            ).split()
        )


# ---------------------------------------------------------------------------
# Enum của hợp đồng (API_CONTRACT mục 2) — không tự thêm giá trị mới.
# ---------------------------------------------------------------------------

MEASURE_VALUES = [
    "revenue",
    "order_count",
    "quantity",
    "cancel_rate",
    "revenue_share",
    "growth_percent",
    "growth_amount",
    "top_product",
    "top_customer",
    "top_employee",
    "best_day",
    "best_month",
    "debt_total",
    "debt_top",
    "import_total",
    "avg_price",
    "price_on_day",
    "continuous_growth",
    "revenue_by_payment",
    "revenue_by_group",
    "revenue_by_channel",
    "quantity_by_unit",
    "product_revenue",
    "unknown",
]
PERIOD_KIND_VALUES = ["absolute", "relative", "snapshot", "multi_month", "none"]
ENTITY_KIND_VALUES = [
    "product",
    "customer",
    "group",
    "payment",
    "employee",
    "channel",
    "none",
]
RANKING_TARGET_VALUES = ["product", "customer", "day", "month", "employee", "none"]
RANKING_METRIC_VALUES = ["quantity", "revenue", "profit", "debt"]
ANSWER_TYPE_VALUES = [
    "number",
    "percent",
    "string",
    "date",
    "boolean",
    "list",
    "table",
    "unknown",
]
THRESHOLD_OP_VALUES = ["gte", "gt", "lte", "lt"]

# Hình thức thanh toán chung của ngôn ngữ (không phải dữ liệu train):
# chỉ dùng để kiểm chứng một thực thể payment mà mô hình nêu ra, vì
# validate_and_build không nhận danh sách payments của shop. Nếu shop
# có hình thức thanh toán ngoài danh sách này, tầng tích hợp nên mở
# rộng kiểm chứng ở đó thay vì sửa enum ở đây một cách tùy tiện.
_KNOWN_PAYMENTS = {
    "tien mat": "tiền mặt",
    "chuyen khoan": "chuyển khoản",
    "cod": "COD",
    "the": "thẻ",
    "vi dien tu": "ví điện tử",
    "tra sau": "trả sau",
    "ghi no": "ghi nợ",
}

# Kênh bán chung (chỉ dùng để nhận diện từ ngữ, không dùng để kiểm
# chứng thực thể channel, vì validate không có danh sách kênh của shop
# nên thực thể channel luôn được coi là "không kiểm chứng được").
_KNOWN_CHANNELS = [
    "shopee",
    "lazada",
    "tiktok",
    "facebook",
    "website",
    "grab",
    "shopeefood",
]

# Model mặc định CHƯA được chọn/kiểm chứng (xem docstring module).
# Người tích hợp phải gán một model_id cụ thể, public, <= 15B, phát
# hành trước 01/10/2026, có licence cho phép, trước khi đóng Docker.
# Để None ở đây là cố tình: thà báo lỗi rõ ràng còn hơn bịa một tên
# model chưa từng chạy thử.
DEFAULT_MODEL_ID = None

# Các trường JSON được phép trong output của mô hình. Trường lạ sẽ bị
# validate coi là sai schema (xem validate_and_build), để repair
# prompt yêu cầu mô hình bỏ trường đó đi, thay vì lặng lẽ nuốt một
# trường có thể mang ý nghĩa tính toán mà solver không hiểu.
_KNOWN_JSON_KEYS = {
    "measure",
    "entity_kind",
    "entity_key",
    "entity_display",
    "period_start",
    "period_end",
    "period_kind",
    "period_months",
    "ranking_k",
    "ranking_target",
    "ranking_metric",
    "unit",
    "threshold_money",
    "threshold_op",
    "answer_type",
    "confidence_parse",
    "parse_notes",
    "normalized_text",
}

# Giới hạn số thực thể đưa vào prompt: đủ lớn cho shop thực tế, nhưng
# có trần để prompt không phình vô hạn khi shop có danh mục rất lớn.
# Khi bị cắt, prompt ghi rõ tổng số, để mô hình biết danh sách chưa
# đầy đủ và không tự bịa thực thể ngoài danh sách.
_MAX_ENTITIES_IN_PROMPT = 500

# Số lần repair tối đa theo yêu cầu (không tính lần gọi đầu tiên).
MAX_REPAIR_ATTEMPTS = 2


# ---------------------------------------------------------------------------
# JSON Schema cho ParsedQuestion (dùng cho Structured Output của model).
# ---------------------------------------------------------------------------

PARSED_QUESTION_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["measure", "entity_kind", "period_kind", "answer_type"],
    "properties": {
        "measure": {"type": "string", "enum": MEASURE_VALUES},
        "entity_kind": {"type": "string", "enum": ENTITY_KIND_VALUES},
        "entity_key": {"type": ["string", "null"]},
        "entity_display": {"type": ["string", "null"]},
        "period_start": {
            "type": ["string", "null"],
            "description": "Ngày YYYY-MM-DD, hoặc null (đặc biệt: snapshot thì null).",
        },
        "period_end": {
            "type": ["string", "null"],
            "description": "Ngày YYYY-MM-DD, hoặc null khi không có kỳ.",
        },
        "period_kind": {"type": "string", "enum": PERIOD_KIND_VALUES},
        "period_months": {
            "type": "array",
            "description": "Chỉ dùng khi period_kind là multi_month: list các [năm, tháng].",
            "items": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
            },
        },
        "ranking_k": {
            "type": ["integer", "null"],
            "description": "null nếu không phải câu xếp hạng; 1 nếu nói 'nhất'.",
        },
        "ranking_target": {"type": "string", "enum": RANKING_TARGET_VALUES},
        "ranking_metric": {
            "type": ["string", "null"],
            "enum": RANKING_METRIC_VALUES + [None],
        },
        "unit": {"type": ["string", "null"]},
        "threshold_money": {"type": ["number", "null"]},
        "threshold_op": {
            "type": ["string", "null"],
            "enum": THRESHOLD_OP_VALUES + [None],
        },
        "answer_type": {"type": "string", "enum": ANSWER_TYPE_VALUES},
        "confidence_parse": {"type": "number", "minimum": 0, "maximum": 1},
        "parse_notes": {"type": "array", "items": {"type": "string"}},
        "normalized_text": {"type": "string"},
    },
}


# ---------------------------------------------------------------------------
# Tiện ích chuẩn hoá nội bộ (không phụ thuộc phase1 để module đứng độc lập
# khi phase2 parser không nạp được; khi phase2 có mặt thì vẫn dùng cùng
# quy ước: thường hoá, bỏ dấu, chỉ giữ chữ/số, gộp khoảng trắng).
# ---------------------------------------------------------------------------

def _normalize_key_local(text):
    """Chuẩn hoá một chuỗi thành dạng không dấu, thường hoá, gọn khoảng trắng.

    Đầu vào:
        text: chuỗi bất kỳ (tên sản phẩm, mã, tên nhóm...), có thể None.

    Đầu ra:
        Chuỗi đã thường hoá, bỏ dấu, chỉ giữ chữ/số và khoảng trắng đơn.
        Ví dụ: ``'Sting dâu 330ml'`` -> ``'sting dau 330ml'``.

    Vì sao cần:
        So sánh thực thể của mô hình với thực thể của shop phải chịu
        được khác biệt dấu/cách viết hoa, nhưng không được chịu được
        khác biệt về chữ cái hay con số — nếu không sẽ khớp nhầm sang
        một sản phẩm khác tên gần giống.

    Trường hợp biên:
        - None hoặc chuỗi rỗng -> ``''`` (không crash, để caller coi
          là "không có giá trị" thay vì một thực thể rỗng).
    """
    if text is None:
        return ""
    s = unicodedata.normalize("NFC", str(text)).lower()
    decomposed = unicodedata.normalize("NFD", s)
    without = "".join(
        ch for ch in decomposed if unicodedata.category(ch) != "Mn"
    )
    without = without.translate(str.maketrans({"đ": "d", "Đ": "D"}))
    return " ".join("".join(ch if ch.isalnum() else " " for ch in without).split())


def _parse_iso_date(value, field_name, errors):
    """Parse một chuỗi ngày YYYY-MM-DD thành datetime.date, ghi lỗi nếu sai.

    Đầu vào:
        value: chuỗi ngày, datetime.date, hoặc None.
        field_name: tên trường (để ghi lỗi rõ ràng).
        errors: list lỗi đang thu thập (hàm append vào list này).

    Đầu ra:
        datetime.date nếu hợp lệ, None nếu value là None hoặc không
        hợp lệ (khi đó errors đã có thêm một mục).

    Vì sao chặt chẽ:
        Ngày 31/2, tháng 13 hay chuỗi không đúng định dạng đều là kỳ
        không hợp lệ, phải đi repair/fallback chứ không được tự sửa
        thành một ngày gần giống, vì sửa ngày là sửa luôn đáp án.

    Trường hợp biên:
        - value là datetime.date sẵn (mô hình trả qua backend đã parse)
          thì giữ nguyên, không ép qua chuỗi.
        - value là bool (kiểu JSON lạ) thì coi là sai, không coi True
          là 1.
    """
    if value is None:
        return None
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.datetime):
        return value.date()
    if not isinstance(value, str):
        errors.append(f"{field_name} phai la chuoi YYYY-MM-DD hoac null, nhan duoc {type(value).__name__}.")
        return None
    text = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        errors.append(f"{field_name} '{value}' khong dung dinh dang YYYY-MM-DD.")
        return None
    try:
        year, month, day = (int(part) for part in text.split("-"))
        return datetime.date(year, month, day)
    except ValueError:
        errors.append(f"{field_name} '{value}' khong phai ngay hop le tren lich.")
        return None


def _entity_candidates(entities):
    """Chuẩn hoá danh sách thực thể của shop thành bảng tra cứu.

    Đầu vào:
        entities: list dict với các trường ``'key'``, ``'display'``,
            ``'code'``, ``'group'`` (theo ShopView), có thể None.

    Đầu ra:
        List các tuple ``(entity_goc, tap_chuan_hoa)`` trong đó tập
        chuẩn hoá gồm key, display và code (nếu có) đã normalize.
        Thứ tự được giữ nguyên theo list đầu vào, để việc chọn ứng
        viên đầu tiên là deterministic khi có nhiều ứng viên trùng.

    Vì sao cần:
        Mô hình có thể trả entity_key là mã, còn entity_display là
        tên, hoặc ngược lại (key là tên chuẩn hoá). Bảng này cho phép
        cả hai cách đều khớp cùng một thực thể của shop, rồi tầng
        validate sẽ chuẩn hoá lại về key/display gốc của shop.
    """
    candidates = []
    for entity in entities or []:
        if not isinstance(entity, dict):
            continue
        norms = set()
        for field_name in ("key", "display", "code"):
            value = entity.get(field_name)
            norm = _normalize_key_local(value)
            if norm:
                norms.add(norm)
        candidates.append((entity, norms))
    return candidates


def _canonical_entity(kind, key, display, products, customers):
    """Kiểm chứng và chuẩn hoá một thực thể mà mô hình nêu ra.

    Đầu vào:
        kind: entity_kind mà mô hình nêu (product/customer/group/
            payment/employee/channel/none).
        key/display: entity_key/entity_display mà mô hình nêu.
        products/customers: danh sách thực thể của shop đang chạy.

    Đầu ra:
        Tuple ``(key_chuan, display_chuan)`` lấy từ dữ liệu shop (với
        product/customer/group) hoặc từ bảng thanh toán chung (với
        payment). Với kind 'none' thì trả (None, None).

    Raise:
        ValueError với lý do rõ ràng khi thực thể không kiểm chứng
        được: không có key/display mà vẫn nêu kind cụ thể; key/display
        không khớp thực thể nào của shop; group không có trong shop;
        payment ngoài bảng thanh toán chung; employee/channel không
        có danh sách kiểm chứng trong tham số (theo yêu cầu, trường
        hợp này phải fallback, không được đoán).

    Vì sao chặt chẽ:
        Đây là chốt chặn chống hallucination quan trọng nhất của
        module: mô hình bịa một tên sản phẩm gần giống tên thật sẽ
        làm solver tính sai hoàn toàn, tệ hơn abstain rất nhiều. Thà
        fallback về template (rồi gate abstain nếu cần) còn hơn dùng
        một thực thể chưa từng tồn tại trong shop.
    """
    norm_key = _normalize_key_local(key)
    norm_display = _normalize_key_local(display)
    if kind == "none":
        if norm_key or norm_display:
            raise ValueError(
                "entity_kind la 'none' nhung entity_key/entity_display khong rong: "
                "khong nhat quan, can sua kind hoac bo key/display."
            )
        return (None, None)
    if kind in ("product", "customer"):
        if not norm_key and not norm_display:
            raise ValueError(
                f"entity_kind la '{kind}' nhung khong co entity_key lan entity_display: "
                "khong the kiem chung thuc the."
            )
        entities = products if kind == "product" else customers
        for entity, norms in _entity_candidates(entities):
            if (norm_key and norm_key in norms) or (
                norm_display and norm_display in norms
            ):
                return (entity.get("key"), entity.get("display"))
        raise ValueError(
            f"Thuc the {kind} voi key={key!r} display={display!r} khong ton tai "
            "trong danh sach cua shop: khong duoc doan thuc the khac thay the."
        )
    if kind == "group":
        if not norm_key and not norm_display:
            raise ValueError(
                "entity_kind la 'group' nhung khong co ten nhom: khong the kiem chung."
            )
        wanted = norm_key or norm_display
        seen = {}
        for source in (products or [], customers or []):
            if not isinstance(source, dict):
                continue
            group = source.get("group")
            if group:
                seen.setdefault(_normalize_key_local(group), group)
        if wanted in seen:
            return (wanted, seen[wanted])
        raise ValueError(
            f"Nhom {key!r}/{display!r} khong ton tai trong shop: khong duoc doan nhom khac."
        )
    if kind == "payment":
        wanted = norm_key or norm_display
        if wanted in _KNOWN_PAYMENTS:
            return (wanted, _KNOWN_PAYMENTS[wanted])
        raise ValueError(
            f"Hinh thuc thanh toan {key!r}/{display!r} khong nam trong bang thanh toan "
            "chung: khong the kiem chung, can fallback."
        )
    # employee/channel: không có danh sách kiểm chứng trong chữ ký
    # validate_and_build (chỉ có products/customers), nên không thể
    # khẳng định thực thể đó có trong shop hay không.
    raise ValueError(
        f"entity_kind '{kind}' khong kiem chung duoc tu products/customers cua shop: "
        "can fallback ve parser template thay vi tin mo hinh."
    )


# ---------------------------------------------------------------------------
# Backend mô hình cục bộ
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
          JSON giả vờ để pipeline tưởng là đã parse xong.
    """

    @abstractmethod
    def generate_json(self, prompt: str, schema: dict) -> dict:
        """Sinh một JSON theo schema từ prompt, trả về dict đã parse.

        Đầu vào:
            prompt: prompt đầy đủ đã xây bằng build_prompt (hoặc
                repair prompt).
            schema: JSON Schema của ParsedQuestion (hằng số
                PARSED_QUESTION_JSON_SCHEMA).

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

    def __init__(self, model_id, device=None):
        """Lưu cấu hình, chưa tải model (tải lười ở lần gọi đầu tiên).

        Đầu vào:
            model_id: id model hoặc đường dẫn weights (xem docstring class).
            device: None/'cpu'/'cuda' (xem docstring class).

        Đầu ra:
            Một backend chưa tải model; model chỉ tải ở _ensure_loaded.
        """
        self.model_id = model_id
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
            trường là đoán luôn đáp án của câu hỏi.

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
            schema: JSON Schema của ParsedQuestion (giữ đúng chữ ký
                của LocalModelBackend; backend này chưa ép schema
                bằng constrained decoding, mà nhắc schema trong
                prompt và để validate_and_build kiểm chứng sau đó
                — vì vậy caller vẫn phải validate, không được tin
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
# Kết quả parse (hợp đồng cố định cho tầng tích hợp)
# ---------------------------------------------------------------------------

@dataclass
class ParseResult:
    """Kết quả của một lần parse bằng mô hình (kèm fallback).

    Ý nghĩa từng trường:

    - ``parsed``: ParsedQuestion cuối cùng sẽ đưa cho solver. Khi
      fallback, đây là kết quả của parser template, không phải của
      mô hình — caller phải đọc ``source`` để biết điều này.
    - ``source``: ``'llm'`` (mô hình trả JSON hợp lệ ngay lần đầu),
      ``'repair'`` (mô hình trả JSON hợp lệ sau repair), hoặc
      ``'template_fallback'`` (đã fallback về parser template, hoặc
      không có backend/template nào và trả unknown để abstain).
    - ``confidence``: độ chắc của kết quả cuối. Với nguồn llm/repair
      thì bằng ``parsed.confidence_parse``; với fallback thì bằng
      confidence của template (hoặc 0.0 khi không có template).
    - ``notes``: giải thích ngắn gọn vì sao chọn nguồn này, lỗi nào
      đã gặp, và vì sao fallback (nếu có) — để báo cáo lỗi truy vết
      được, thay vì chỉ thấy một con số confidence.
    """

    parsed: ParsedQuestion
    source: str
    confidence: float
    notes: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tóm tắt rulebook và xây prompt
# ---------------------------------------------------------------------------

def summarize_rulebook(rulebook, calendar=None):
    """Tóm tắt rulebook thành vài dòng cho prompt, không hard-code luật train.

    Đầu vào:
        rulebook: đối tượng rulebook của split đang chạy (có thể là
            chuỗi, dict, object có thuộc tính text/rules, hoặc None).
        calendar: lịch của split (hiện chỉ giữ để đúng chữ ký mở rộng
            cho tương lai; tóm tắt thời gian Tết phải tra calendar ở
            tầng parse_time, không tra bằng chuỗi ở đây).

    Đầu ra:
        Chuỗi tóm tắt ngắn gọn. Nếu rulebook có nội dung trích được
        thì dùng nội dung đó (cắt ngắn để prompt không phình); nếu
        không có rulebook thì trả câu nói rõ là không có rulebook, để
        mô hình chỉ dùng kỳ ghi rõ trong câu và không suy luật ngoài
        câu hỏi.

    Vì sao không hard-code luật train vào đây:
        Test set dùng rulebook mới, có thể khác luật train. Ghi cứng
        luật train vào prompt sẽ làm mô hình áp luật cũ cho shop mới
        và tính sai. Tóm tắt phải lấy từ rulebook của chính split đang
        chạy, truyền vào qua tham số.
    """
    if rulebook is None:
        return (
            "Khong co rulebook cua split dang chay: chi dung ky ghi ro trong cau hoi, "
            "khong suy them luat thoi gian hay xep hang ngoai cau hoi."
        )
    text = None
    if isinstance(rulebook, str):
        text = rulebook
    elif isinstance(rulebook, dict):
        parts = []
        for key in sorted(rulebook):
            parts.append(f"{key}: {rulebook[key]}")
        text = "\n".join(parts)
    else:
        for attr in ("text", "content", "rules", "summary"):
            value = getattr(rulebook, attr, None)
            if isinstance(value, str) and value.strip():
                text = value
                break
        if text is None:
            text = str(rulebook)
    text = " ".join(str(text).split())
    if len(text) > 2000:
        text = text[:2000] + " ... (da cat ngan)"
    return text


def _format_entity_list(entities, label):
    """Định dạng một danh sách thực thể thành dòng cho prompt, chỉ tên/mã.

    Đầu vào:
        entities: list dict (products/customers) hoặc list chuỗi
            (groups/payments), hoặc None.
        label: nhãn hiển thị (ví dụ 'San pham', 'Khach hang').

    Đầu ra:
        Chuỗi nhiều dòng ``'- <display> (key: <key>)'`` hoặc một dòng
        ghi là không có danh sách, kèm ghi chú khi danh sách bị cắt ở
        _MAX_ENTITIES_IN_PROMPT. KHÔNG đưa dữ liệu dòng đơn, doanh số
        hay bất kỳ con số nào của shop vào prompt: mô hình chỉ cần
        biết tên/mã để chọn thực thể, số liệu phải do solver tính.

    Trường hợp biên:
        - entities là None hoặc rỗng -> trả dòng 'Khong co'.
        - Phần tử không phải dict (groups/payments dạng chuỗi) -> dùng
          chính chuỗi đó làm tên hiển thị.
    """
    if not entities:
        return f"{label}: Khong co."
    items = []
    for entity in entities:
        if isinstance(entity, dict):
            display = entity.get("display") or entity.get("key") or ""
            key = entity.get("key") or ""
            code = entity.get("code") or ""
            if code and code != key:
                items.append(f"- {display} (key: {key}; code: {code})")
            else:
                items.append(f"- {display} (key: {key})")
        else:
            items.append(f"- {entity}")
    # Sắp xếp để prompt deterministic, không phụ thuộc thứ tự list đầu vào.
    items = sorted(set(items))
    total = len(items)
    shown = items[:_MAX_ENTITIES_IN_PROMPT]
    header = f"{label} (tong {total}):"
    if total > len(shown):
        shown.append(
            f"... (da cat, chi hien {len(shown)} / {total} muc; "
            "neu thuc the trong cau khong co trong danh sach hien thi, "
            "khong duoc tu bia key/display)."
        )
    return header + "\n" + "\n".join(shown)


def build_prompt(question_text, normalized_text, as_of, products, customers, groups, payments, rulebook_summary) -> str:
    """Xây prompt tiếng Việt yêu cầu mô hình trả DUY NHẤT JSON theo schema.

    Đầu vào:
        question_text: câu hỏi gốc của người dùng (giữ dấu, giữ nguyên).
        normalized_text: câu hỏi sau chuẩn hoá không dấu + mở rộng
            viết tắt (từ parser template), để mô hình đọc được cả hai
            dạng khi câu gốc viết tắt nặng.
        as_of: ngày "hôm nay" của câu hỏi (datetime.date hoặc chuỗi
            YYYY-MM-DD); mọi cách nói tương đối (tháng này, tuần rồi,
            hôm nay...) đều tính theo ngày này.
        products/customers: danh sách thực thể của shop (chỉ tên/mã,
            xem _format_entity_list).
        groups/payments: danh sách nhóm / hình thức thanh toán của
            shop (list chuỗi hoặc list dict), có thể None.
        rulebook_summary: tóm tắt rulebook của split (xem
            summarize_rulebook).

    Đầu ra:
        Chuỗi prompt hoàn chỉnh. Prompt yêu cầu mô hình: chỉ trả JSON
        theo đúng schema (không giải thích, không tính số, không thêm
        text ngoài JSON); chỉ dùng thực thể có trong danh sách; nếu
        thực thể không có trong danh sách thì không bịa key/display
        mà để entity_kind='none' và ghi chú; kỳ snapshot thì
        period_start=null; period_months chỉ điền khi multi_month.

    Vì sao nhấn mạnh "không tự tính số":
        Nhiệm vụ của mô hình chỉ là hiểu câu hỏi thành ParsedQuestion;
        mọi con số đáp án phải do solver tính bằng code từ dữ liệu.
        Mô hình mà tự tính số trong prompt sẽ vừa sai (không thấy dữ
        liệu dòng đơn) vừa làm lệch JSON.
    """
    if isinstance(as_of, datetime.datetime):
        as_of_text = as_of.date().isoformat()
    elif isinstance(as_of, datetime.date):
        as_of_text = as_of.isoformat()
    else:
        as_of_text = str(as_of)
    prompt = (
        "Ban la bo phan phan tich cau hoi cua he thong DataMate. "
        "Nhiem vu DUY NHAT cua ban la doc cau hoi tieng Viet duoi day va tra ve "
        "MOT object JSON theo dung schema ParsedQuestion, khong giai thich, "
        "khong tinh toan dap an so, khong them text ngoai JSON.\n\n"
        f"Cau hoi goc: {question_text}\n"
        f"Cau hoi chuan hoa khong dau: {normalized_text}\n"
        f"Ngay hom nay (as_of_date): {as_of_text}. Moi cach noi tuong doi "
        "(thang nay, tuan roi, hom nay, hom qua, N ngay gan nhat...) deu tinh theo ngay nay.\n\n"
        "Danh sach thuc the cua shop (CHI duoc dung nhung thuc the co trong danh sach nay, "
        "dung dung key/display goc; KHONG dua du lieu dong don vao day nen ban KHONG thay doanh so):\n"
        f"{_format_entity_list(products, 'San pham')}\n"
        f"{_format_entity_list(customers, 'Khach hang')}\n"
        f"{_format_entity_list(groups, 'Nhom')}\n"
        f"{_format_entity_list(payments, 'Hinh thuc thanh toan')}\n\n"
        f"Tom tat rulebook ve thoi gian va xep hang: {rulebook_summary}\n\n"
        "Quy tac bat buoc:\n"
        "- measure chi duoc lay trong enum: revenue, order_count, quantity, cancel_rate, "
        "revenue_share, growth_percent, growth_amount, top_product, top_customer, top_employee, "
        "best_day, best_month, debt_total, debt_top, import_total, avg_price, price_on_day, "
        "continuous_growth, revenue_by_payment, revenue_by_group, revenue_by_channel, "
        "quantity_by_unit, product_revenue, unknown.\n"
        "- period_kind chi duoc lay trong: absolute, relative, snapshot, multi_month, none. "
        "period_start/period_end la chuoi YYYY-MM-DD hoac null. Ky snapshot (den het ngay X) thi "
        "period_start=null, period_end=X. period_months chi dien khi period_kind=multi_month, "
        "dang list cac [nam, thang] (so nguyen); cac ky khac de [].\n"
        "- ranking_k la null neu khong phai cau xep hang, la 1 neu noi 'nhat', la N neu noi 'top N'.\n"
        "- threshold_op chi duoc lay trong: gte, gt, lte, lt, hoac null.\n"
        "- answer_type chi duoc lay trong: number, percent, string, date, boolean, list, table, unknown.\n"
        "- Neu thuc the trong cau KHONG co trong danh sach tren: KHONG duoc bia key/display, "
        "hay de entity_kind='none', entity_key=null, entity_display=null, va ghi ro vao parse_notes.\n"
        "- Neu ky hoac measure khong xac dinh duoc: dung 'none'/'unknown', ghi ro vao parse_notes, "
        "KHONG duoc doan ky hay measure de lap cho trong.\n"
        "- confidence_parse la so 0..1, chi do chac cua viec phan tich cau hoi.\n"
        "- Chi tra DUY NHAT object JSON, khong giai thich, khong tinh so dap an.\n"
    )
    return prompt


def build_repair_prompt(original_prompt, errors, previous_output) -> str:
    """Xây repair prompt liệt kê lỗi của JSON lần trước, yêu cầu trả lại JSON sửa.

    Đầu vào:
        original_prompt: prompt gốc đã gửi cho mô hình.
        errors: list chuỗi lỗi do validate_and_build trả ra (đã rõ ràng
            từng lỗi: measure ngoài enum, kỳ không hợp lệ, thực thể
            không tồn tại...).
        previous_output: dict JSON lần trước (để mô hình thấy mình đã
            sai ở đâu, thay vì phải đoán lại từ đầu).

    Đầu ra:
        Chuỗi prompt mới: giữ nguyên yêu cầu gốc, thêm phần lỗi cần
        sửa và JSON cũ, và nhắc lại: chỉ trả JSON đã sửa, không giải
        thích, không bịa thực thể/kỳ mới để lấp chỗ trống.

    Vì sao liệt kê lỗi thay vì chỉ nói "sai, làm lại":
        Mô hình không thấy được code validate, nên nói chung chung sẽ
        làm nó sửa lung tung hoặc lặp lại đúng lỗi cũ. Liệt kê lỗi cụ
        thể giúp lần repair sau trúng đích và giảm số lần gọi model.
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
        "khong giai thich, khong bia thuc the hay ky moi de lap cho trong. "
        "Neu mot truong khong xac dinh duoc, hay dung 'unknown'/'none'/null va ghi vao parse_notes."
    )


# ---------------------------------------------------------------------------
# Validation: JSON -> ParsedQuestion (chốt chặn chống hallucination)
# ---------------------------------------------------------------------------

def validate_and_build(data: dict, products, customers) -> ParsedQuestion:
    """Kiểm chứng JSON của mô hình và xây ParsedQuestion, raise nếu sai.

    Đầu vào:
        data: dict JSON mà mô hình trả về (backend đã parse JSON thô).
        products/customers: danh sách thực thể của shop đang chạy, dùng
            để kiểm chứng thực thể (xem _canonical_entity).

    Đầu ra:
        ParsedQuestion với các trường đã chuẩn hoá: period_start/end
        là datetime.date (hoặc None), period_months là list các tuple
        (năm, tháng), entity_key/display đã chuẩn hoá về dữ liệu shop,
        normalized_text giữ nguyên như mô hình trả (tầng parse_with_llm
        sẽ ghi đè bằng câu chuẩn hoá thật để truy vết chính xác).

    Raise:
        ValueError kèm lý do cụ thể khi: data không phải dict; có
        trường lạ ngoài hợp đồng; measure/period_kind/entity_kind/
        ranking_target/answer_type ngoài enum; ranking_k không phải
        int hợp lệ; period_start/end sai định dạng, không phải ngày
        thật, start > end, hoặc không nhất quán với period_kind
        (snapshot phải có start null, none phải không có kỳ,
        multi_month phải có period_months hợp lệ, các kỳ khác phải
        có đủ start/end và period_months rỗng); period_months có tháng
        ngoài 1-12, trùng lặp hoặc không tăng dần; threshold không hợp
        lệ; confidence_parse ngoài [0, 1]; parse_notes không phải list
        chuỗi; hoặc thực thể không tồn tại/không kiểm chứng được trong
        shop. Caller bắt ValueError này để đi repair hoặc fallback —
        không bao giờ tự sửa một trường sai thành một giá trị đoán.

    Trường hợp biên cần nhớ:
        - Các trường tùy chọn vắng mặt được điền mặc định theo hợp
          đồng (entity none, kỳ none, ranking null...), không coi là
          sai, vì schema chỉ bắt buộc 4 trường measure, entity_kind,
          period_kind, answer_type. Không thêm trường bắt buộc mới.
        - confidence_parse vắng mặt được mặc định 0.5 kèm ghi chú, vì
          0.5 là mức "không chắc cũng không phủ định" — đủ để tầng
          gate không tin mù quáng, và ghi chú nói rõ đây là mặc định,
          không phải độ chắc do mô hình tự báo.
        - ranking_metric vắng mặt là None (hợp lệ khi không xếp hạng).
        - threshold_money có mà threshold_op vắng là hợp lệ (giữ None,
          không tự gán 'gte', vì gán mặc định là một suy luận mà tầng
          solver/gate phải tự quyết theo câu hỏi, không phải parser).
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        raise ValueError(
            f"Output cua model phai la object JSON (dict), nhan duoc {type(data).__name__}."
        )
    unknown_keys = sorted(set(data) - _KNOWN_JSON_KEYS)
    if unknown_keys:
        errors.append(f"Co truong la ngoai hop dong: {', '.join(unknown_keys)}.")
    measure = data.get("measure")
    if measure not in MEASURE_VALUES:
        errors.append(f"measure {measure!r} ngoai enum hop dong.")
    entity_kind = data.get("entity_kind")
    if entity_kind not in ENTITY_KIND_VALUES:
        errors.append(f"entity_kind {entity_kind!r} ngoai enum hop dong.")
    period_kind = data.get("period_kind")
    if period_kind not in PERIOD_KIND_VALUES:
        errors.append(f"period_kind {period_kind!r} ngoai enum hop dong.")
    answer_type = data.get("answer_type")
    if answer_type not in ANSWER_TYPE_VALUES:
        errors.append(f"answer_type {answer_type!r} ngoai enum hop dong.")
    ranking_target = data.get("ranking_target", "none")
    if ranking_target not in RANKING_TARGET_VALUES:
        errors.append(f"ranking_target {ranking_target!r} ngoai enum hop dong.")
    ranking_metric = data.get("ranking_metric")
    if ranking_metric is not None and ranking_metric not in RANKING_METRIC_VALUES:
        errors.append(f"ranking_metric {ranking_metric!r} ngoai enum hop dong.")
    ranking_k = data.get("ranking_k")
    if ranking_k is not None:
        if isinstance(ranking_k, bool) or not isinstance(ranking_k, int):
            errors.append(f"ranking_k phai la int hoac null, nhan duoc {ranking_k!r}.")
        elif ranking_k < 1 or ranking_k > 100:
            errors.append(f"ranking_k {ranking_k} ngoai khoang hop le 1..100.")
    unit = data.get("unit")
    if unit is not None and (not isinstance(unit, str) or not unit.strip()):
        errors.append(f"unit phai la chuoi khong rong hoac null, nhan duoc {unit!r}.")
    threshold_money = data.get("threshold_money")
    if threshold_money is not None:
        if isinstance(threshold_money, bool) or not isinstance(threshold_money, (int, float)):
            errors.append(f"threshold_money phai la so hoac null, nhan duoc {threshold_money!r}.")
        elif threshold_money != threshold_money or threshold_money in (float("inf"), float("-inf")):
            errors.append("threshold_money khong duoc la NaN hoac vo cuc.")
        elif threshold_money < 0:
            errors.append(f"threshold_money {threshold_money} am la khong hop le.")
    threshold_op = data.get("threshold_op")
    if threshold_op is not None and threshold_op not in THRESHOLD_OP_VALUES:
        errors.append(f"threshold_op {threshold_op!r} ngoai enum gte|gt|lte|lt.")
    if threshold_money is None and threshold_op is not None:
        errors.append("threshold_op co gia tri nhung threshold_money la null: khong nhat quan.")
    confidence_parse = data.get("confidence_parse", None)
    confidence_note = None
    if confidence_parse is None:
        confidence_parse = 0.5
        confidence_note = "Model khong tra confidence_parse, mac dinh 0.5 (khong chac)."
    elif isinstance(confidence_parse, bool) or not isinstance(confidence_parse, (int, float)):
        errors.append(f"confidence_parse phai la so 0..1, nhan duoc {confidence_parse!r}.")
        confidence_parse = 0.5
    elif confidence_parse < 0 or confidence_parse > 1:
        errors.append(f"confidence_parse {confidence_parse} ngoai khoang [0, 1].")
    parse_notes = data.get("parse_notes", [])
    if not isinstance(parse_notes, list) or any(not isinstance(item, str) for item in parse_notes):
        errors.append("parse_notes phai la list cac chuoi.")
        parse_notes = [str(item) for item in parse_notes] if isinstance(parse_notes, list) else []
    normalized_text = data.get("normalized_text", "")
    if normalized_text is not None and not isinstance(normalized_text, str):
        errors.append("normalized_text phai la chuoi.")
        normalized_text = str(normalized_text)
    if normalized_text is None:
        normalized_text = ""

    # --- Kiểm chứng kỳ (chặt chẽ, vì kỳ sai là đáp án sai toàn bộ). ---
    period_start = _parse_iso_date(data.get("period_start"), "period_start", errors)
    period_end = _parse_iso_date(data.get("period_end"), "period_end", errors)
    raw_months = data.get("period_months", [])
    period_months: list[tuple[int, int]] = []
    if raw_months is None:
        raw_months = []
    if not isinstance(raw_months, list):
        errors.append(f"period_months phai la list cac [nam, thang], nhan duoc {raw_months!r}.")
    else:
        for item in raw_months:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                errors.append(f"period_months co phan tu khong phai [nam, thang]: {item!r}.")
                continue
            year, month = item
            if isinstance(year, bool) or isinstance(month, bool):
                errors.append(f"period_months co gia tri bool khong hop le: {item!r}.")
                continue
            if not isinstance(year, int) or not isinstance(month, int):
                errors.append(f"period_months phai la so nguyen: {item!r}.")
                continue
            if month < 1 or month > 12:
                errors.append(f"period_months co thang ngoai 1..12: {item!r}.")
                continue
            if year < 1900 or year > 2200:
                errors.append(f"period_months co nam ngoai khoang hop le: {item!r}.")
                continue
            period_months.append((year, month))
    if period_months != sorted(set(period_months)):
        # Trùng lặp hoặc không tăng dần đều là dấu hiệu mô hình liệt kê
        # tháng lộn xộn; không tự sắp xếp lại vì thứ tự tháng là một
        # phần của ý nghĩa câu hỏi (ví dụ tăng liên tục theo thời gian).
        errors.append("period_months phai tang dan theo thoi gian va khong trung lap.")
    if period_kind in PERIOD_KIND_VALUES:
        if period_kind == "none":
            if period_start is not None or period_end is not None or period_months:
                errors.append("period_kind 'none' nhung lai co period_start/period_end/period_months.")
        elif period_kind == "snapshot":
            if period_start is not None:
                errors.append("period_kind 'snapshot' thi period_start phai la null (anh chup den het ngay).")
            if period_end is None:
                errors.append("period_kind 'snapshot' thi period_end khong duoc null.")
        elif period_kind == "multi_month":
            if not period_months:
                errors.append("period_kind 'multi_month' thi period_months khong duoc rong.")
            if period_start is None or period_end is None:
                errors.append("period_kind 'multi_month' thi period_start/period_end khong duoc null.")
            elif period_start > period_end:
                errors.append("period_start lon hon period_end.")
            elif period_months:
                if (period_start.year, period_start.month) > period_months[0]:
                    errors.append("period_start nam sau thang dau tien cua period_months.")
                if (period_end.year, period_end.month) < period_months[-1]:
                    errors.append("period_end nam truoc thang cuoi cung cua period_months.")
        else:  # absolute / relative
            if period_start is None or period_end is None:
                errors.append(f"period_kind '{period_kind}' thi period_start/period_end khong duoc null.")
            elif period_start > period_end:
                errors.append("period_start lon hon period_end.")
            if period_months:
                errors.append(f"period_kind '{period_kind}' thi period_months phai rong.")
    if errors:
        raise ValueError("JSON sai schema / khong hop le: " + " ".join(errors))

    # --- Kiểm chứng thực thể (chỉ chạy khi các lỗi trên đã sạch, để
    # thông báo lỗi tập trung và repair không bị dội quá nhiều lỗi). ---
    entity_key, entity_display = _canonical_entity(
        entity_kind,
        data.get("entity_key"),
        data.get("entity_display"),
        products,
        customers,
    )
    notes = list(parse_notes)
    if confidence_note is not None:
        notes.append(confidence_note)
    return ParsedQuestion(
        measure=measure,
        entity_kind=entity_kind,
        entity_key=entity_key,
        entity_display=entity_display,
        period_start=period_start,
        period_end=period_end,
        period_kind=period_kind,
        period_months=list(period_months),
        ranking_k=ranking_k,
        ranking_target=ranking_target,
        ranking_metric=ranking_metric,
        unit=unit.strip() if isinstance(unit, str) else None,
        threshold_money=float(threshold_money) if threshold_money is not None else None,
        threshold_op=threshold_op,
        answer_type=answer_type,
        confidence_parse=float(confidence_parse),
        parse_notes=notes,
        normalized_text=normalized_text,
    )


# ---------------------------------------------------------------------------
# Hàm chính: parse bằng LLM, repair tối đa 2 lần, rồi fallback an toàn.
# ---------------------------------------------------------------------------

def _unknown_parsed(normalized_text):
    """Tạo ParsedQuestion unknown để abstain khi không còn đường nào khác.

    Đầu vào:
        normalized_text: câu đã chuẩn hoá (để truy vết).

    Đầu ra:
        ParsedQuestion với measure/answer_type 'unknown', confidence 0.0
        và ghi chú rõ là không parse được. Đây là kết quả an toàn nhất
        khi cả mô hình lẫn template đều không dùng được: gate sẽ
        abstain thay vì đoán số.

    Vì sao không raise ở đây:
        parse_with_llm là tầng cuối trước solver/gate; raise sẽ crash
        cả shop, trong khi một kết quả unknown chỉ làm mất một câu.
    """
    return ParsedQuestion(
        measure="unknown",
        entity_kind="none",
        entity_key=None,
        entity_display=None,
        period_start=None,
        period_end=None,
        period_kind="none",
        period_months=[],
        ranking_k=None,
        ranking_target="none",
        ranking_metric=None,
        unit=None,
        threshold_money=None,
        threshold_op=None,
        answer_type="unknown",
        confidence_parse=0.0,
        parse_notes=["Khong parse duoc bang LLM lan template: tra unknown de abstain."],
        normalized_text=normalized_text,
    )


def _fallback_result(question_text, as_of, calendar, rulebook, products, customers, normalized_text, reason_notes):
    """Chạy parser template làm fallback, hoặc trả unknown nếu không có template.

    Đầu vào:
        question_text/as_of/calendar/rulebook/products/customers:
            đúng chữ ký của parse_question template.
        normalized_text: câu đã chuẩn hoá (để ghi vào kết quả unknown
            khi template cũng không dùng được).
        reason_notes: list lý do vì sao phải fallback (lỗi backend,
            lỗi validate sau repair...), để ghi vào notes cuối.

    Đầu ra:
        ParseResult với source='template_fallback'. Nếu template chạy
        được thì confidence là confidence_parse của template và notes
        gồm lý do fallback + notes của template; nếu template không
        có hoặc template crash thì trả unknown với confidence 0.0.

    Vì sao template được gọi đúng chữ ký gốc của nó:
        parse_question của phase2 nhận (question_text, as_of, calendar,
        rulebook, products, customers) — không nhận groups/payments.
        Gọi sai chữ ký sẽ crash fallback, đúng lúc cần fallback nhất,
        nên hàm này chỉ truyền đúng 6 tham số đó.
    """
    notes = list(reason_notes)
    if _PHASE2_AVAILABLE:
        # Nếu caller không truyền template_parse_fn, vẫn thử dùng
        # parse_question của phase2 đã nạp được ở đầu module, vì đó
        # chính là template fallback theo hợp đồng.
        try:
            from parser import parse_question as _template_parse
        except Exception:
            _template_parse = None
        if _template_parse is not None:
            try:
                parsed = _template_parse(
                    question_text, as_of, calendar, rulebook, products, customers
                )
                template_notes = list(getattr(parsed, "parse_notes", []) or [])
                return ParseResult(
                    parsed=parsed,
                    source="template_fallback",
                    confidence=float(getattr(parsed, "confidence_parse", 0.0) or 0.0),
                    notes=notes + ["Fallback ve parser template."] + template_notes,
                )
            except Exception as exc:
                notes.append(f"Parser template cung loi: {exc}.")
    return ParseResult(
        parsed=_unknown_parsed(normalized_text),
        source="template_fallback",
        confidence=0.0,
        notes=notes + ["Khong co parser template kha dung: tra unknown de abstain."],
    )


def parse_with_llm(question_text, as_of, calendar, rulebook, products, customers, groups=None, payments=None, backend=None, template_parse_fn=None) -> ParseResult:
    """Parse một câu hỏi bằng mô hình cục bộ, repair tối đa 2 lần, rồi fallback.

    Đầu vào:
        question_text: câu hỏi gốc.
        as_of: ngày "hôm nay" của câu hỏi (datetime.date).
        calendar: lịch của split (để tra Tết ở tầng template fallback)
            hoặc None.
        rulebook: rulebook của split đang chạy (để tóm tắt vào prompt
            và để template fallback dùng) hoặc None.
        products/customers: danh sách thực thể của shop (để đưa vào
            prompt và để kiểm chứng thực thể).
        groups/payments: danh sách nhóm / hình thức thanh toán của
            shop, có thể None (khi None, prompt ghi "Khong co").
        backend: một LocalModelBackend (thường là TransformersBackend),
            hoặc None (khi đó đi thẳng fallback, không giả vờ có model).
        template_parse_fn: hàm parser template (thường là
            phase2.parse_question) để fallback, hoặc None (khi đó hàm
            tự thử nạp parse_question của phase2, nếu vẫn không có thì
            trả unknown).

    Đầu ra:
        ParseResult theo hợp đồng: source 'llm' khi JSON đầu hợp lệ,
        'repair' khi JSON hợp lệ sau repair, 'template_fallback' khi
        đã fallback. confidence là confidence_parse của kết quả cuối
        (với fallback là confidence của template). normalized_text của
        kết quả LLM/repair luôn được ghi đè bằng câu chuẩn hoá thật từ
        expand_abbreviations, để truy vết không phụ thuộc vào những gì
        mô hình tự ghi.

    Các bước và trường hợp biên:
        1. Không có backend -> fallback ngay, không gọi model ảo.
        2. Backend raise (thiếu gói/weights/lỗi sinh) -> ghi lỗi, fallback.
        3. JSON sai schema/kỳ/thực thể -> repair tối đa 2 lần, mỗi lần
           gửi repair prompt liệt kê lỗi; lần repair thành công thì
           source='repair'.
        4. Sau 2 lần repair vẫn sai -> fallback, KHÔNG đoán thêm.
        5. Template fallback crash hoặc không có -> trả unknown để
           abstain, KHÔNG crash cả pipeline vì một câu hỏi.
        6. Mọi nhánh đều không bao giờ tự điền thực thể hay kỳ: trường
           nào không kiểm chứng được thì hoặc repair (để mô hình sửa),
           hoặc fallback, hoặc unknown.
    """
    normalized_text = _phase2_expand(question_text)
    rulebook_summary = summarize_rulebook(rulebook, calendar)
    prompt = build_prompt(
        question_text,
        normalized_text,
        as_of,
        products,
        customers,
        groups,
        payments,
        rulebook_summary,
    )
    if backend is None:
        fallback_notes = ["Khong co backend LLM: di thang fallback ve parser template."]
        if template_parse_fn is not None:
            try:
                parsed = template_parse_fn(
                    question_text, as_of, calendar, rulebook, products, customers
                )
                return ParseResult(
                    parsed=parsed,
                    source="template_fallback",
                    confidence=float(getattr(parsed, "confidence_parse", 0.0) or 0.0),
                    notes=fallback_notes + ["Fallback ve parser template."]
                    + list(getattr(parsed, "parse_notes", []) or []),
                )
            except Exception as exc:
                return ParseResult(
                    parsed=_unknown_parsed(normalized_text),
                    source="template_fallback",
                    confidence=0.0,
                    notes=fallback_notes + [f"Parser template cung loi: {exc}."],
                )
        return _fallback_result(
            question_text, as_of, calendar, rulebook, products, customers,
            normalized_text, fallback_notes,
        )

    errors: list[str] = []
    current_prompt = prompt
    raw_output = None
    for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
        try:
            raw_output = backend.generate_json(current_prompt, PARSED_QUESTION_JSON_SCHEMA)
        except Exception as exc:
            errors.append(f"Loi backend o lan thu {attempt + 1}: {exc}.")
            break
        try:
            parsed = validate_and_build(raw_output, products, customers)
        except ValueError as exc:
            errors.append(str(exc))
            if attempt < MAX_REPAIR_ATTEMPTS:
                current_prompt = build_repair_prompt(prompt, errors, raw_output)
                continue
            break
        except Exception as exc:
            errors.append(f"Loi khong ngo khi validate: {exc}.")
            break
        # JSON hợp lệ: ghi đè normalized_text bằng câu chuẩn hoá thật.
        parsed.normalized_text = normalized_text
        source = "llm" if attempt == 0 else "repair"
        notes = []
        if attempt > 0:
            notes.append(f"JSON hop le sau {attempt} lan repair.")
        notes.extend(list(parsed.parse_notes or []))
        return ParseResult(
            parsed=parsed,
            source=source,
            confidence=float(parsed.confidence_parse),
            notes=notes,
        )

    # Hết repair hoặc backend lỗi: fallback, không đoán thêm.
    fallback_notes = ["LLM khong tra duoc JSON hop le sau toi da 2 lan repair: " + " ".join(errors)]
    if template_parse_fn is not None:
        try:
            parsed = template_parse_fn(
                question_text, as_of, calendar, rulebook, products, customers
            )
            return ParseResult(
                parsed=parsed,
                source="template_fallback",
                confidence=float(getattr(parsed, "confidence_parse", 0.0) or 0.0),
                notes=fallback_notes + ["Fallback ve parser template."]
                + list(getattr(parsed, "parse_notes", []) or []),
            )
        except Exception as exc:
            return ParseResult(
                parsed=_unknown_parsed(normalized_text),
                source="template_fallback",
                confidence=0.0,
                notes=fallback_notes + [f"Parser template cung loi: {exc}."],
            )
    return _fallback_result(
        question_text, as_of, calendar, rulebook, products, customers,
        normalized_text, fallback_notes,
    )

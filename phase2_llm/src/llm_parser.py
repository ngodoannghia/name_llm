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
   kê lỗi. Sau 2 lần vẫn sai thì raise để caller chuyển thành
   abstain — KHÔNG fallback về parser template (theo yêu cầu của
   người dùng ngày 2026-10-31, vì fallback template dựa trên cụm từ
   của train nên rất hên xui trên test).
5. Nếu backend không khả dụng, hoặc thực thể/kỳ không kiểm chứng
   được, thì raise để abstain và KHÔNG BAO GIỜ đoán thực thể hay
   kỳ để lấp chỗ trống.

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
# Enum của hợp đồng (API_CONTRACT mục 2) — chỉ thêm giá trị khi solver
# đã cam kết bao phủ đúng định nghĩa đó (xem COVERED_MEASURES của
# solver). Bốn measure avg_order_value, cancelled_order_count,
# unsold_product_count, revenue_by_month được thêm vì solver đã triển
# khai đúng các định nghĩa này; không tự thêm measure ngoài danh sách đó.
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
    "avg_order_value",
    "cancelled_order_count",
    "unsold_product_count",
    "revenue_by_month",
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
        errors.append(f"{field_name} phải là chuỗi YYYY-MM-DD hoặc null, nhận được {type(value).__name__}.")
        return None
    text = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        errors.append(f"{field_name} '{value}' không đúng định dạng YYYY-MM-DD.")
        return None
    try:
        year, month, day = (int(part) for part in text.split("-"))
        return datetime.date(year, month, day)
    except ValueError:
        errors.append(f"{field_name} '{value}' không phải ngày hợp lệ trên lịch.")
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
        hợp này phải raise để abstain, không được đoán).

    Vì sao chặt chẽ:
        Đây là chốt chặn chống hallucination quan trọng nhất của
        module: mô hình bịa một tên sản phẩm gần giống tên thật sẽ
        làm solver tính sai hoàn toàn, tệ hơn abstain rất nhiều. Thà
        raise để abstain còn hơn dùng
        một thực thể chưa từng tồn tại trong shop.
    """
    norm_key = _normalize_key_local(key)
    norm_display = _normalize_key_local(display)
    if kind == "none":
        if norm_key or norm_display:
            raise ValueError(
                "entity_kind là 'none' nhưng entity_key/entity_display không rỗng: "
                "không nhất quán, cần sửa kind hoặc bỏ key/display."
            )
        return (None, None)
    if kind in ("product", "customer"):
        if not norm_key and not norm_display:
            raise ValueError(
                f"entity_kind là '{kind}' nhưng không có entity_key lẫn entity_display: "
                "không thể kiểm chứng thực thể."
            )
        entities = products if kind == "product" else customers
        for entity, norms in _entity_candidates(entities):
            if (norm_key and norm_key in norms) or (
                norm_display and norm_display in norms
            ):
                return (entity.get("key"), entity.get("display"))
        raise ValueError(
            f"Thực thể {kind} với key={key!r} display={display!r} không tồn tại "
            "trong danh sách của shop: không được đoán thực thể khác thay thế."
        )
    if kind == "group":
        if not norm_key and not norm_display:
            raise ValueError(
                "entity_kind là 'group' nhưng không có tên nhóm: không thể kiểm chứng."
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
            f"Nhóm {key!r}/{display!r} không tồn tại trong shop: không được đoán nhóm khác."
        )
    if kind == "payment":
        wanted = norm_key or norm_display
        if wanted in _KNOWN_PAYMENTS:
            return (wanted, _KNOWN_PAYMENTS[wanted])
        raise ValueError(
            f"Hình thức thanh toán {key!r}/{display!r} không nằm trong bảng thanh toán "
            "chung: không thể kiểm chứng, cần raise để abstain."
        )
    # employee/channel: không có danh sách kiểm chứng trong chữ ký
    # validate_and_build (chỉ có products/customers), nên không thể
    # khẳng định thực thể đó có trong shop hay không.
    raise ValueError(
        f"entity_kind '{kind}' không kiểm chứng được từ products/customers của shop: "
        "cần raise để abstain thay vì tin mô hình."
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
                "TransformersBackend chưa có model_id: model mặc định CHƯA được chọn "
                "và CHƯA được kiểm chứng. Hãy chọn một model public <= 15B, phát hành "
                "trước 01/10/2026, có licence cho phép, rồi truyền model_id vào đây "
                "trước khi đóng Docker. Không tự chọn model thay người tích hợp."
            )
        try:
            import transformers  # noqa: F401  (chỉ kiểm tra gói có sẵn không)
        except ImportError as exc:
            raise RuntimeError(
                "TransformersBackend không khả dụng: thiếu gói 'transformers'. "
                "Hãy cài transformers (và torch) trong môi trường Docker rồi thử lại; "
                "không trả JSON giả khi backend không có thật."
            ) from exc
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "TransformersBackend không khả dụng: thiếu 'torch' hoặc lớp Auto "
                "của transformers. Hãy cài đầy đủ torch + transformers rồi thử lại."
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
                f"TransformersBackend không tải được model '{self.model_id}': {exc}. "
                "Có thể weights chưa có sẵn, sai model_id, hoặc thiếu bộ nhớ. "
                "Không trả JSON giả khi model không tải được."
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
            raise RuntimeError("Model trả về text rỗng, không có JSON.")
        start = text.find("{")
        if start < 0:
            raise RuntimeError("Output của model không chứa object JSON.")
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
                            f"Không parse được JSON từ output của model: {exc}"
                        ) from exc
                    if not isinstance(parsed, dict):
                        raise RuntimeError("JSON của model không phải object.")
                    return parsed
        raise RuntimeError("Output của model có dấu '{' mở nhưng không đóng đủ.")

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
                f"TransformersBackend sinh text thất bại: {exc}"
            ) from exc
        return self._extract_json_object(text_out)


# ---------------------------------------------------------------------------
# Kết quả parse (hợp đồng cố định cho tầng tích hợp)
# ---------------------------------------------------------------------------

@dataclass
class ParseResult:
    """Kết quả của một lần parse bằng mô hình (KHÔNG fallback template).

    Ý nghĩa từng trường:

    - ``parsed``: ParsedQuestion cuối cùng sẽ đưa cho solver, luôn
      là kết quả của mô hình (đã qua validate_and_build).
    - ``source``: ``'llm'`` (mô hình trả JSON hợp lệ ngay lần đầu)
      hoặc ``'repair'`` (mô hình trả JSON hợp lệ sau repair).
      Không còn nguồn ``'template_fallback'``: khi mô hình lỗi, hàm
      parse raise để caller chuyển câu đó thành abstain, thay vì
      trả kết quả template.
    - ``confidence``: độ chắc của kết quả cuối, bằng
      ``parsed.confidence_parse``.
    - ``notes``: giải thích ngắn gọn vì sao chọn nguồn này và lỗi
      nào đã gặp trong các lần repair — để báo cáo lỗi truy vết
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
            "Không có rulebook của split đang chạy: chỉ dùng kỳ ghi rõ trong câu hỏi, "
            "không suy thêm luật thời gian hay xếp hạng ngoài câu hỏi."
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
        text = text[:2000] + " ... (đã cắt ngắn)"
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
        - entities là None hoặc rỗng -> trả dòng 'Không có'.
        - Phần tử không phải dict (groups/payments dạng chuỗi) -> dùng
          chính chuỗi đó làm tên hiển thị.
    """
    if not entities:
        return f"{label}: Không có."
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
            f"... (đã cắt, chỉ hiện {len(shown)} / {total} mục; "
            "nếu thực thể trong câu không có trong danh sách hiển thị, "
            "không được tự bịa key/display)."
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
        "Bạn là bộ phận phân tích câu hỏi của hệ thống DataMate. "
        "Nhiệm vụ DUY NHẤT của bạn là đọc câu hỏi tiếng Việt dưới đây và trả về "
        "MỘT object JSON theo đúng schema ParsedQuestion, không giải thích, "
        "không tính toán đáp án số, không thêm text ngoài JSON.\n\n"
        f"Câu hỏi gốc: {question_text}\n"
        f"Câu hỏi chuẩn hoá không dấu: {normalized_text}\n"
        f"Ngày hôm nay (as_of_date): {as_of_text}. Mọi cách nói tương đối "
        "(tháng này, tuần rồi, hôm nay, hôm qua, N ngày gần nhất...) đều tính theo ngày này.\n\n"
        "Danh sách thực thể của shop (CHỈ được dùng những thực thể có trong danh sách này, "
        "dùng đúng key/display gốc; KHÔNG đưa dữ liệu dòng đơn vào đây nên bạn KHÔNG thấy doanh số):\n"
        f"{_format_entity_list(products, 'San pham')}\n"
        f"{_format_entity_list(customers, 'Khach hang')}\n"
        f"{_format_entity_list(groups, 'Nhom')}\n"
        f"{_format_entity_list(payments, 'Hinh thuc thanh toan')}\n\n"
        f"Tóm tắt rulebook về thời gian và xếp hạng: {rulebook_summary}\n\n"
        "Quy tắc bắt buộc:\n"
        "- measure chỉ được lấy trong enum: revenue, order_count, quantity, cancel_rate, "
        "revenue_share, growth_percent, growth_amount, top_product, top_customer, top_employee, "
        "best_day, best_month, debt_total, debt_top, import_total, avg_price, price_on_day, "
        "continuous_growth, revenue_by_payment, revenue_by_group, revenue_by_channel, "
        "quantity_by_unit, product_revenue, avg_order_value, cancelled_order_count, "
        "unsold_product_count, revenue_by_month, unknown.\n"
        "- period_kind chỉ được lấy trong: absolute, relative, snapshot, multi_month, none. "
        "period_start/period_end là chuỗi YYYY-MM-DD hoặc null. Kỳ snapshot (đến hết ngày X) thì "
        "period_start=null, period_end=X. period_months chỉ điền khi period_kind=multi_month, "
        "dạng list các [năm, tháng] (số nguyên); các kỳ khác để [].\n"
        "- ranking_k là null nếu không phải câu xếp hạng, là 1 nếu nói 'nhất', là N nếu nói 'top N'.\n"
        "- threshold_op chỉ được lấy trong: gte, gt, lte, lt, hoặc null.\n"
        "- answer_type chỉ được lấy trong: number, percent, string, date, boolean, list, table, unknown.\n"
        "- Nếu thực thể trong câu KHÔNG có trong danh sách trên: KHÔNG được bịa key/display, "
        "hãy để entity_kind='none', entity_key=null, entity_display=null, và ghi rõ vào parse_notes.\n"
        "- Nếu kỳ hoặc measure không xác định được: dùng 'none'/'unknown', ghi rõ vào parse_notes, "
        "KHÔNG được đoán kỳ hay measure để lấp chỗ trống.\n"
        "- confidence_parse là số 0..1, chỉ độ chắc của việc phân tích câu hỏi.\n"
        "- Chỉ trả DUY NHẤT object JSON, không giải thích, không tính số đáp án.\n"
        "\n"
        "QUY TRÌNH QUYẾT ĐỊNH (làm theo đúng thứ tự này, đừng nhảy thẳng tới measure):\n"
        "1. Đáp án hỏi cái gì? Một con số (number), một tỉ lệ % (percent), một cái tên (string), "
        "một ngày (date), đúng/sai (boolean), hay một danh sách/bảng (list/table)? "
        "Đáp án là ngày thì measure KHÔNG phải revenue.\n"
        "2. Có xếp hạng không? 'nhất', 'cao nhất', 'nhiều nhất', 'top N' thì ranking_k=1 (hoặc N). "
        "Xác định ranking_target TRƯỚC khi chọn measure: 'ngày nào' -> day, 'tháng nào' -> month, "
        "'mặt hàng/sản phẩm nào' -> product, 'khách nào' -> customer, 'nhân viên nào' -> employee. "
        "Cụm 'N ngày gần nhất' KHÔNG phải xếp hạng (từ 'nhất' ở đó chỉ là một phần của cụm thời gian).\n"
        "3. Chọn measure theo bảng dưới đây, không chọn theo tên gọi chung chung.\n"
        "4. Chọn thực thể và kỳ, rồi kiểm tra lại tính nhất quán: measure + answer_type + ranking + kỳ "
        "phải khớp nhau (ví dụ best_day thì answer_type phải là date, price_on_day thì kỳ phải là đúng một ngày).\n"
        "5. Chỉ hạ confidence khi cách hiểu câu hỏi thật sự mơ hồ (nhiều measure đều hợp lý). "
        "KHÔNG hạ confidence chỉ vì bạn không thấy doanh số/giá trong prompt: bạn CỐ TÌNH không được "
        "thấy dữ liệu dòng đơn, solver sẽ tính đáp án bằng code sau đó. Mẫu câu rõ ràng thì confidence cao, "
        "dù bạn không biết con số đáp án là bao nhiêu.\n"
        "\n"
        "BẢNG QUYẾT ĐỊNH MEASURE (các mẫu hay sai nhất, ưu tiên áp dụng trước các quy tắc chung):\n"
        "- 'ngày nào' + 'nhiều tiền nhất' / 'doanh thu cao nhất' / 'bán được nhiều tiền nhất' trong một kỳ "
        "-> measure=best_day, answer_type=date, ranking_k=1, ranking_target=day, ranking_metric=revenue. "
        "Kỳ là kỳ được hỏi (ví dụ tháng 5/2026 -> period_start=2026-05-01, period_end=2026-05-31, "
        "period_kind=absolute). KHÔNG chọn revenue, KHÔNG chọn top_product, dù câu có chữ 'doanh thu'.\n"
        "- 'tháng nào' + 'nhiều tiền nhất' / 'doanh thu cao nhất' -> measure=best_month, answer_type=date, "
        "ranking_k=1, ranking_target=month, ranking_metric=revenue.\n"
        "- 'giá' / 'đơn giá' của MỘT mặt hàng cụ thể VÀO MỘT ngày cụ thể (ví dụ 'ngày 25/09/2025 mặt hàng X "
        "giá bao nhiêu một gói') -> measure=price_on_day, answer_type=number, entity_kind=product. "
        "Kỳ BẮT BUỘC là đúng một ngày đó: period_start=period_end=ngày đó, period_kind=absolute. "
        "KHÔNG dùng snapshot (snapshot có period_start=null sẽ làm solver không biết ngày D và phải abstain). "
        "KHÔNG chọn avg_price (giá trung bình cả kỳ) hay product_revenue (doanh thu của mặt hàng).\n"
        "- 'mặt hàng bán chạy nhất' / 'sản phẩm bán chạy nhất' -> measure=top_product, answer_type=string "
        "(k=1), ranking_k=1, ranking_target=product, ranking_metric=quantity (bán chạy tính theo số lượng "
        "quy đổi, không phải theo doanh thu), trừ khi câu nói rõ 'lời/lãi nhất' thì ranking_metric=profit.\n"
        "- 'trung bình mỗi đơn' / 'bình quân mỗi đơn' / 'giá trị trung bình một đơn' / 'trung bình một đơn "
        "mang về bao nhiêu doanh thu' -> measure=avg_order_value, answer_type=number, entity_kind=none "
        "(trừ khi câu có lọc mặt hàng/nhóm thì giữ đúng thực thể đó). Đây là doanh thu bình quân trên một đơn, "
        "KHÔNG phải tổng doanh thu nên KHÔNG chọn revenue, KHÔNG chọn avg_price.\n"
        "- 'số đơn bị huỷ' / 'mấy đơn huỷ' / 'đếm số đơn đã huỷ trong kỳ' -> measure=cancelled_order_count, "
        "answer_type=number, entity_kind=none. Đây là số đơn có trạng thái huỷ trong kỳ, KHÔNG phải tổng số đơn "
        "nên KHÔNG chọn order_count.\n"
        "- 'mấy mặt hàng không bán được' / 'mấy món ế' / 'bao nhiêu mặt hàng bán không được cái nào trong kỳ' "
        "-> measure=unsold_product_count, answer_type=number, entity_kind=none. Đây là số mặt hàng khác nhau "
        "có doanh số bằng 0 trong kỳ, KHÔNG phải số dòng đơn hay số lượng bán, nên KHÔNG chọn quantity "
        "hay order_count.\n"
        "- 'doanh thu N tháng liền nhau kết thúc ở tháng X là bao nhiêu mỗi tháng, từ tháng cũ đến tháng mới' "
        "-> measure=revenue_by_month, answer_type=list, period_kind=multi_month, period_start=ngày 1 của tháng "
        "đầu tiên, period_end=ngày cuối của tháng cuối cùng, period_months=list các tháng đó theo thứ tự cũ "
        "đến mới. Đáp án là một danh sách, mỗi phần tử là doanh thu của một tháng, KHÔNG cộng gộp thành một "
        "số revenue duy nhất.\n"
        "- 'bán được bao nhiêu tiền' / 'doanh thu' (không xếp hạng, không hỏi ngày/tháng nhất) -> measure=revenue, "
        "answer_type=number. 'số đơn' / 'bao nhiêu đơn' -> order_count. 'bán được bao nhiêu kg/chai/gói' của một "
        "mặt hàng cụ thể -> quantity, unit=đơn vị được hỏi (kg/chai/gói).\n"
        "- 'tỉ lệ' / '%' trong đáp án -> answer_type=percent, measure tương ứng là cancel_rate, revenue_share "
        "hoặc growth_percent, KHÔNG phải revenue. Dấu % trong TÊN sản phẩm (ví dụ tên thuốc có '0,1%') "
        "không làm câu hỏi thành câu percent.\n"
        "- KỲ TƯƠNG ĐỐI (khóa chặt, tính đúng theo as_of_date, tuần bắt đầu thứ Hai): 'tuần rồi' = thứ Hai đến "
        "Chủ nhật của tuần trước tuần chứa as_of; 'tuần này' = thứ Hai của tuần chứa as_of đến as_of; "
        "'7 ngày gần nhất' = 7 ngày tính lùi từ as_of, gồm cả as_of (as_of trừ 6 ngày đến as_of); "
        "'30 ngày gần nhất' tương tự (as_of trừ 29 ngày đến as_of); 'từ đầu năm đến hết hôm nay' = ngày 1 "
        "tháng 1 của năm as_of đến as_of; 'từ đầu tháng này đến hết hôm nay' = mùng 1 tháng của as_of đến "
        "as_of. Khi câu đã xác định được kỳ theo as_of như trên thì dùng period_kind=absolute với "
        "period_start/period_end cụ thể đó, KHÔNG để period_kind mơ hồ và KHÔNG tự suy ra một kỳ khác.\n"
        "- TĂNG TRƯỞNG MTD: câu 'từ đầu tháng này đến hết hôm nay so với cùng kỳ tháng trước' / 'hơn kém cùng "
        "kỳ tháng trước bao nhiêu %' -> measure=growth_percent, answer_type=percent, period_start=mùng 1 "
        "tháng của as_of, period_end=as_of, period_kind=absolute. KHÔNG dùng cả tháng đầy đủ để so sánh: "
        "solver sẽ tự so với cùng số ngày đó của tháng trước. Nếu câu hỏi số tiền tăng/giảm thay vì % thì "
        "dùng growth_amount với cùng kỳ đó.\n"
        "- TỈ LỆ KHÁCH: câu 'bao nhiêu % khách trong tháng X mua từ N đơn trở lên' thì mẫu số là số khách ĐÃ "
        "MUA trong tháng X đó, tử số là số khách trong nhóm đó mua từ N đơn trở lên trong cùng tháng X. "
        "KHÔNG chia cho tổng số khách mọi thời gian, KHÔNG chia cho tổng số đơn. Chỉ chọn một measure khi "
        "định nghĩa của nó khớp đúng dạng tỉ lệ khách này; KHÔNG được lấy một measure gần giống để lấp chỗ "
        "trống (ví dụ revenue_share là tỉ lệ doanh thu, không phải tỉ lệ khách). Nếu không có measure nào "
        "khớp đúng thì dùng measure='unknown' theo quy tắc chống đoán bên dưới.\n"
        "- 'bao nhiêu tiền' -> answer_type=number, kể cả khi câu nhắc tháng hay ngày (ví dụ 'doanh thu tháng "
        "5/2026' là number, KHÔNG phải date). Chỉ khi câu hỏi 'ngày nào' / 'tháng nào' thì answer_type mới là date.\n"
        "- TOP KHÁCH HÀNG: solver sẽ loại khách vãng lai ('Khách lẻ', 'lẻ', 'kl') khỏi xếp hạng, nên LLM "
        "KHÔNG được chọn khách vãng lai làm thực thể cho câu top khách. Nếu câu top khách không nêu tên khách "
        "cụ thể thì entity_kind=none, ranking_target=customer.\n"
        "\n"
        "QUY TẮC CHỐNG ĐOÁN MEASURE (để câu chưa bao phủ thành abstain thay vì thành sai):\n"
        "- Nếu không có measure nào trong enum khớp đúng định nghĩa của câu hỏi (ví dụ dạng câu chưa được "
        "solver bao phủ), thì đặt measure='unknown', ghi rõ vào parse_notes là chưa có measure phù hợp, và "
        "KHÔNG chọn measure gần nhất để lấp chỗ trống. Quy tắc này KHÔNG làm yếu các measure đã có "
        "(best_day, price_on_day, revenue, top_product, top_customer, avg_order_value, cancelled_order_count, "
        "unsold_product_count, revenue_by_month...): khi một measure đã khớp đúng thì vẫn chọn nó với "
        "confidence bình thường.\n"
        "\n"
        "VÍ DỤ PARSE (chỉ minh họa dạng câu, không phải đáp án, không dùng dữ liệu shop cụ thể):\n"
        "- Câu dạng 'tháng 5/2026 ngày nào cửa hàng bán được nhiều tiền nhất' -> "
        "{measure: best_day, answer_type: date, ranking_k: 1, ranking_target: day, ranking_metric: revenue, "
        "period_start: 2026-05-01, period_end: 2026-05-31, period_kind: absolute}.\n"
        "- Câu dạng 'ngày 25/09/2025 mặt hàng X giá bao nhiêu một gói' -> "
        "{measure: price_on_day, answer_type: number, entity_kind: product, "
        "period_start: 2025-09-25, period_end: 2025-09-25, period_kind: absolute}.\n"
        "- Câu dạng 'mặt hàng bán chạy nhất tháng 6/2026 là gì' -> "
        "{measure: top_product, answer_type: string, ranking_k: 1, ranking_target: product, "
        "ranking_metric: quantity, period_start: 2026-06-01, period_end: 2026-06-30, period_kind: absolute}.\n"
        "- Câu dạng 'doanh thu trung bình trên một đơn tháng 5/2025 là bao nhiêu' -> "
        "{measure: avg_order_value, answer_type: number, entity_kind: none, "
        "period_start: 2025-05-01, period_end: 2025-05-31, period_kind: absolute}.\n"
        "- Câu dạng 'tháng 2/2025 cửa hàng có mấy đơn ở trạng thái huỷ' -> "
        "{measure: cancelled_order_count, answer_type: number, entity_kind: none, "
        "period_start: 2025-02-01, period_end: 2025-02-28, period_kind: absolute}.\n"
        "- Câu dạng 'doanh thu 3 tháng liền nhau kết thúc ở tháng 12/2025 là bao nhiêu mỗi tháng, từ tháng cũ "
        "đến tháng mới' -> "
        "{measure: revenue_by_month, answer_type: list, period_kind: multi_month, "
        "period_start: 2025-10-01, period_end: 2025-12-31, period_months: [[2025, 10], [2025, 11], [2025, 12]]}.\n"
        "- Câu dạng 'từ đầu tháng này đến hết hôm nay, doanh thu tăng hay giảm bao nhiêu % so với cùng kỳ "
        "tháng trước' -> "
        "{measure: growth_percent, answer_type: percent, period_kind: absolute, "
        "period_start: mùng 1 tháng của as_of_date, period_end: as_of_date}.\n"
        "\n"
        "QUY TẮC THỰC THỂ (áp dụng cho price_on_day và mọi câu có mặt hàng cụ thể):\n"
        "- Chọn đúng một thực thể trong danh sách shop, giữ nguyên key/display gốc, KỂ CẢ phần biến thể "
        "trong tên (ví dụ 100g, 330ml, bao 50kg). KHÔNG được rút gọn tên, KHÔNG được chọn một mặt hàng "
        "tên gần giống khác để thay thế, vì solver khớp đúng tên đó trong dữ liệu.\n"
        "- Nếu tên trong câu không khớp chính xác bất kỳ thực thể nào trong danh sách, để entity_kind='none' "
        "và ghi rõ vào parse_notes, thay vì đoán một thực thể gần giống.\n"
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
        "JSON bạn trả trước đó SAI và cần sửa lại. Các lỗi cụ thể:\n"
        f"{error_lines}\n\n"
        f"JSON cu:\n{previous_text}\n\n"
        "Hãy trả DUY NHẤT object JSON đã sửa hết các lỗi trên, theo đúng schema, "
        "không giải thích, không bịa thực thể hay kỳ mới để lấp chỗ trống. "
        "Nếu một trường không xác định được, hãy dùng 'unknown'/'none'/null và ghi vào parse_notes."
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
            f"Output của model phải là object JSON (dict), nhận được {type(data).__name__}."
        )
    unknown_keys = sorted(set(data) - _KNOWN_JSON_KEYS)
    if unknown_keys:
        errors.append(f"Có trường lạ ngoài hợp đồng: {', '.join(unknown_keys)}.")
    measure = data.get("measure")
    if measure not in MEASURE_VALUES:
        errors.append(f"measure {measure!r} ngoài enum hợp đồng.")
    entity_kind = data.get("entity_kind")
    if entity_kind not in ENTITY_KIND_VALUES:
        errors.append(f"entity_kind {entity_kind!r} ngoài enum hợp đồng.")
    period_kind = data.get("period_kind")
    if period_kind not in PERIOD_KIND_VALUES:
        errors.append(f"period_kind {period_kind!r} ngoài enum hợp đồng.")
    answer_type = data.get("answer_type")
    if answer_type not in ANSWER_TYPE_VALUES:
        errors.append(f"answer_type {answer_type!r} ngoài enum hợp đồng.")
    ranking_target = data.get("ranking_target", "none")
    if ranking_target not in RANKING_TARGET_VALUES:
        errors.append(f"ranking_target {ranking_target!r} ngoài enum hợp đồng.")
    ranking_metric = data.get("ranking_metric")
    if ranking_metric is not None and ranking_metric not in RANKING_METRIC_VALUES:
        errors.append(f"ranking_metric {ranking_metric!r} ngoài enum hợp đồng.")
    ranking_k = data.get("ranking_k")
    if ranking_k is not None:
        if isinstance(ranking_k, bool) or not isinstance(ranking_k, int):
            errors.append(f"ranking_k phải là int hoặc null, nhận được {ranking_k!r}.")
        elif ranking_k < 1 or ranking_k > 100:
            errors.append(f"ranking_k {ranking_k} ngoài khoảng hợp lệ 1..100.")
    unit = data.get("unit")
    if unit is not None and (not isinstance(unit, str) or not unit.strip()):
        errors.append(f"unit phải là chuỗi không rỗng hoặc null, nhận được {unit!r}.")
    threshold_money = data.get("threshold_money")
    if threshold_money is not None:
        if isinstance(threshold_money, bool) or not isinstance(threshold_money, (int, float)):
            errors.append(f"threshold_money phải là số hoặc null, nhận được {threshold_money!r}.")
        elif threshold_money != threshold_money or threshold_money in (float("inf"), float("-inf")):
            errors.append("threshold_money không được là NaN hoặc vô cực.")
        elif threshold_money < 0:
            errors.append(f"threshold_money {threshold_money} âm là không hợp lệ.")
    threshold_op = data.get("threshold_op")
    if threshold_op is not None and threshold_op not in THRESHOLD_OP_VALUES:
        errors.append(f"threshold_op {threshold_op!r} ngoài enum gte|gt|lte|lt.")
    if threshold_money is None and threshold_op is not None:
        errors.append("threshold_op có giá trị nhưng threshold_money là null: không nhất quán.")
    confidence_parse = data.get("confidence_parse", None)
    confidence_note = None
    if confidence_parse is None:
        confidence_parse = 0.5
        confidence_note = "Model không trả confidence_parse, mặc định 0.5 (không chắc)."
    elif isinstance(confidence_parse, bool) or not isinstance(confidence_parse, (int, float)):
        errors.append(f"confidence_parse phải là số 0..1, nhận được {confidence_parse!r}.")
        confidence_parse = 0.5
    elif confidence_parse < 0 or confidence_parse > 1:
        errors.append(f"confidence_parse {confidence_parse} ngoài khoảng [0, 1].")
    parse_notes = data.get("parse_notes", [])
    if not isinstance(parse_notes, list) or any(not isinstance(item, str) for item in parse_notes):
        errors.append("parse_notes phải là list các chuỗi.")
        parse_notes = [str(item) for item in parse_notes] if isinstance(parse_notes, list) else []
    normalized_text = data.get("normalized_text", "")
    if normalized_text is not None and not isinstance(normalized_text, str):
        errors.append("normalized_text phải là chuỗi.")
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
        errors.append(f"period_months phải là list các [năm, tháng], nhận được {raw_months!r}.")
    else:
        for item in raw_months:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                errors.append(f"period_months có phần tử không phải [năm, tháng]: {item!r}.")
                continue
            year, month = item
            if isinstance(year, bool) or isinstance(month, bool):
                errors.append(f"period_months có giá trị bool không hợp lệ: {item!r}.")
                continue
            if not isinstance(year, int) or not isinstance(month, int):
                errors.append(f"period_months phải là số nguyên: {item!r}.")
                continue
            if month < 1 or month > 12:
                errors.append(f"period_months có tháng ngoài 1..12: {item!r}.")
                continue
            if year < 1900 or year > 2200:
                errors.append(f"period_months có năm ngoài khoảng hợp lệ: {item!r}.")
                continue
            period_months.append((year, month))
    if period_months != sorted(set(period_months)):
        # Trùng lặp hoặc không tăng dần đều là dấu hiệu mô hình liệt kê
        # tháng lộn xộn; không tự sắp xếp lại vì thứ tự tháng là một
        # phần của ý nghĩa câu hỏi (ví dụ tăng liên tục theo thời gian).
        errors.append("period_months phải tăng dần theo thời gian và không trùng lặp.")
    if period_kind in PERIOD_KIND_VALUES:
        if period_kind == "none":
            if period_start is not None or period_end is not None or period_months:
                errors.append("period_kind 'none' nhưng lại có period_start/period_end/period_months.")
        elif period_kind == "snapshot":
            if period_start is not None:
                errors.append("period_kind 'snapshot' thì period_start phải là null (ảnh chụp đến hết ngày).")
            if period_end is None:
                errors.append("period_kind 'snapshot' thì period_end không được null.")
        elif period_kind == "multi_month":
            if not period_months:
                errors.append("period_kind 'multi_month' thì period_months không được rỗng.")
            if period_start is None or period_end is None:
                errors.append("period_kind 'multi_month' thì period_start/period_end không được null.")
            elif period_start > period_end:
                errors.append("period_start lớn hơn period_end.")
            elif period_months:
                if (period_start.year, period_start.month) > period_months[0]:
                    errors.append("period_start nằm sau tháng đầu tiên của period_months.")
                if (period_end.year, period_end.month) < period_months[-1]:
                    errors.append("period_end nằm trước tháng cuối cùng của period_months.")
        else:  # absolute / relative
            if period_start is None or period_end is None:
                errors.append(f"period_kind '{period_kind}' thì period_start/period_end không được null.")
            elif period_start > period_end:
                errors.append("period_start lớn hơn period_end.")
            if period_months:
                errors.append(f"period_kind '{period_kind}' thì period_months phải rỗng.")
    if errors:
        raise ValueError("JSON sai schema / không hợp lệ: " + " ".join(errors))

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
# Hàm chính: parse bằng LLM, repair tối đa 2 lần, KHÔNG fallback.
# ---------------------------------------------------------------------------

def _unknown_parsed(normalized_text):
    """Tạo ParsedQuestion unknown để abstain khi không còn đường nào khác.

    Đầu vào:
        normalized_text: câu đã chuẩn hoá (để truy vết).

    Đầu ra:
        ParsedQuestion với measure/answer_type 'unknown', confidence 0.0
        và ghi chú rõ là không parse được. Hàm này hiện chỉ còn dùng
        trong kiểm thử và làm tài liệu cho trường hợp unknown; đường
        suy luận chính không còn dùng nó để thay cho fallback template
        — khi LLM lỗi, parse_with_llm raise và runner chuyển câu đó
        thành abstain trực tiếp.
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
        parse_notes=["Không parse được bằng LLM: trả unknown để abstain."],
        normalized_text=normalized_text,
    )


def parse_with_llm(question_text, as_of, calendar, rulebook, products, customers, groups=None, payments=None, backend=None) -> ParseResult:
    """Parse một câu hỏi bằng mô hình, repair tối đa 2 lần, KHÔNG fallback template.

    Đầu vào:
        question_text: câu hỏi gốc.
        as_of: ngày "hôm nay" của câu hỏi (datetime.date).
        calendar: lịch của split (để tra Tết ở tóm tắt rulebook)
            hoặc None.
        rulebook: rulebook của split đang chạy (để tóm tắt vào prompt)
            hoặc None.
        products/customers: danh sách thực thể của shop (để đưa vào
            prompt và để kiểm chứng thực thể).
        groups/payments: danh sách nhóm / hình thức thanh toán của
            shop, có thể None (khi None, prompt ghi "Không có").
        backend: một backend mô hình (thường là TransformersBackend
            hoặc backend OpenAI ở tầng phát triển).

    Đầu ra:
        ParseResult theo hợp đồng: source 'llm' khi JSON đầu hợp lệ,
        'repair' khi JSON hợp lệ sau repair. confidence là
        confidence_parse của kết quả cuối. normalized_text của kết
        quả LLM/repair luôn được ghi đè bằng câu chuẩn hoá thật từ
        expand_abbreviations, để truy vết không phụ thuộc vào những gì
        mô hình tự ghi.

    Các bước và trường hợp biên (KHÔNG có fallback template, theo
    yêu cầu của người dùng ngày 2026-10-31: fallback template dựa
    trên cụm từ của train nên rất hên xui trên test có cách hỏi và
    định dạng khác, vì vậy không được dùng nó để đoán parse):
        1. Không có backend -> raise RuntimeError ngay, không giả vờ
           có mô hình, không fallback.
        2. Backend raise (thiếu gói/weights/lỗi sinh) -> raise ngay,
           không retry vô hạn.
        3. JSON sai schema/kỳ/thực thể -> repair tối đa 2 lần, mỗi lần
           gửi repair prompt liệt kê lỗi; lần repair thành công thì
           source='repair'.
        4. Sau 2 lần repair vẫn sai -> raise ValueError với toàn bộ
           lỗi đã thu thập, KHÔNG đoán thêm, KHÔNG fallback.
        5. Caller (runner) bắt lỗi ở trên và chuyển câu đó thành
           abstain, không crash cả shop, không bịa đáp án.
        6. Mọi nhánh đều không bao giờ tự điền thực thể hay kỳ: trường
           nào không kiểm chứng được thì hoặc repair (để mô hình sửa),
           hoặc raise để abstain.
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
        # Không có backend LLM: raise để caller chuyển thành abstain.
        # KHÔNG fallback về parser template (xem docstring ở trên).
        raise RuntimeError(
            "Không có backend LLM: không fallback về parser template, "
            "câu này sẽ abstain.")
    errors: list[str] = []
    current_prompt = prompt
    raw_output = None
    for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
        try:
            raw_output = backend.generate_json(current_prompt, PARSED_QUESTION_JSON_SCHEMA)
        except Exception as exc:
            # Backend lỗi: raise ngay để caller abstain, không fallback.
            raise RuntimeError(
                f"Lỗi backend ở lần thứ {attempt + 1}: {exc}.") from exc
        try:
            parsed = validate_and_build(raw_output, products, customers)
        except ValueError as exc:
            errors.append(str(exc))
            if attempt < MAX_REPAIR_ATTEMPTS:
                current_prompt = build_repair_prompt(prompt, errors, raw_output)
                continue
            break
        except Exception as exc:
            errors.append(f"Lỗi không ngờ khi validate: {exc}.")
            break
        # JSON hợp lệ: ghi đè normalized_text bằng câu chuẩn hoá thật.
        parsed.normalized_text = normalized_text
        source = "llm" if attempt == 0 else "repair"
        notes = []
        if attempt > 0:
            notes.append(f"JSON hợp lệ sau {attempt} lần repair.")
        notes.extend(list(parsed.parse_notes or []))
        return ParseResult(
            parsed=parsed,
            source=source,
            confidence=float(parsed.confidence_parse),
            notes=notes,
        )

    # Hết repair vẫn sai: raise để caller abstain, KHÔNG fallback
    # template, KHÔNG đoán thêm.
    raise ValueError(
        "LLM không trả được JSON hợp lệ sau tối đa 2 lần repair: "
        + " ".join(errors))

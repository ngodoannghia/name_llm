#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tạo dữ liệu trace đã kiểm chứng cho LoRA parser DataMate.

Mục đích của script này
------------------------
Biến câu hỏi train thành dữ liệu huấn luyện cho parser LoRA, nhưng
CHỈ giữ trace nào đã được kiểm chứng bằng code, đúng cổng quyết định
ở MASTER_PLAN mục 8 và API_CONTRACT mục 2, mục 3:

1. Chạy template parser của phase2 (import qua ``sys.path``, không
   copy-sửa code phase2) để có candidate ``ParsedQuestion``.
2. Chạy solver của phase2 (``solve``) trên ``ShopView`` của shop đó.
3. Đối chiếu đáp án solver trả ra với đáp án train bằng hàm chấm
   ``answers_match`` của phase1. Chỉ khi kết quả là ``"correct"``
   thì trace mới được ghi với ``verified=True``.

Quy tắc trung thực bắt buộc (không được vi phạm khi sửa script này):

- Đáp án train KHÔNG BAO GIỜ được dùng làm đầu vào của
  ``ParsedQuestion``. Đáp án chỉ xuất hiện sau khi solver đã chạy,
  ở bước đối chiếu. Một trace mà solver trả abstain, trả sai, hoặc
  chưa chạy solver thì không bao giờ được ghi ``verified=True``.
- Mặc định script CHỈ ghi: trace đã kiểm chứng, biến thể tổng quát
  hoá suy ra từ trace đã kiểm chứng, và ví dụ tiêu cực cho
  abstain/unanswerable. Candidate chưa kiểm chứng bị loại và chỉ
  xuất hiện trong thống kê, trừ khi caller bật cờ
  ``--keep-unverified-candidates`` (khi đó chúng vẫn ghi
  ``verified=False`` và ``verification_method`` nói rõ chưa kiểm
  chứng, để phục vụ debug, không phải để train mù).
- Không hard-code theo id câu hỏi hay mã shop để tạo trace. Mọi lựa
  chọn đều theo thứ tự deterministic (business_id rồi id câu hỏi)
  và theo kết quả chạy solver, không theo danh sách câu/shop viết
  sẵn trong code.

Dữ liệu tổng quát hoá (MASTER_PLAN mục 8 bắt buộc phải trộn thêm):

- Biến thể không dấu: bỏ dấu câu hỏi gốc, giữ nguyên
  ``ParsedQuestion`` đã kiểm chứng.
- Biến thể viết tắt: thay cụm từ đầy đủ bằng viết tắt quen thuộc
  (``doanh thu`` -> ``dthu``, ``bao nhieu`` -> ``nhiu``,
  ``duoc`` -> ``dc``...) trên bản không dấu, giữ nguyên parse.
  Hai biến thể này KHÔNG chạy lại solver trên câu biến thể, nên
  chúng ghi ``verified=False`` với phương pháp
  ``inherited_from_solver_verified_source_not_rerun`` — trung thực
  về việc parse được kế thừa, không bịa là đã chạy solver.
- Ví dụ tiêu cực: câu hỏi về thực thể đã kiểm tra là không có trong
  danh mục của shop, và câu hỏi về kỳ nằm ngoài khoảng dữ liệu của
  shop (dựa vào ``date_min``/``date_max`` của view lúc tạo, không
  hard-code năm). Các ví dụ này không gán đáp án nào cả (định dạng
  JSONL của pipeline này vốn không có trường đáp án), mà chỉ ghi
  ``parse_notes`` rõ ràng và độ tin cậy parse thấp, để model học
  khi nào nên abstain/unanswerable thay vì đoán.

Định dạng một dòng JSONL đầu ra (đúng yêu cầu nhiệm vụ)::

    {
      "question": str,                 # câu hỏi (gốc hoặc biến thể)
      "normalized_question": str,      # câu hỏi sau chuẩn hoá của parser
      "as_of_date": "YYYY-MM-DD",
      "business_id": str,
      "parsed_question_json": {...},   # đúng các trường ParsedQuestion
      "verified": bool,
      "verification_method": str,
      "source": str,
      "generalization_tag": str,       # none | no_diacritics |
                                       # abbreviated |
                                       # negative_entity_absent |
                                       # negative_period_out_of_range
    }

Chạy mẫu (tạo file mẫu trong ``data/``)::

    python3 generate_verified_traces.py --shops biz_101 --limit 40

Chạy toàn bộ train (chậm hơn nhiều vì phải nạp view từng shop)::

    python3 generate_verified_traces.py --limit 0

Script không gọi mạng/API, chạy offline, deterministic. Đọc dữ liệu
train ở chế độ chỉ đọc, không sửa bất kỳ file nào ngoài file đầu ra
do caller chỉ định.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import unicodedata
from dataclasses import asdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Nạp phase1/phase2 bằng đường dẫn tương đối từ file này, không hard-code
# đường dẫn tuyệt đối của máy. File này nằm ở
# <datamate>/phase2_llm/training/generate_verified_traces.py nên thư mục
# datamate là parents[2].
# ---------------------------------------------------------------------------
_DATAMATE_DIR = Path(__file__).resolve().parents[2]
_PHASE1_SRC = _DATAMATE_DIR / "phase1" / "src"
_PHASE2_SRC = _DATAMATE_DIR / "phase2" / "src"
for _candidate in (str(_PHASE2_SRC), str(_PHASE1_SRC)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

try:
    from parser import expand_abbreviations as _phase2_expand
    from parser import parse_question as _phase2_parse_question
    _PARSER_AVAILABLE = True
except Exception:  # pragma: no cover - parser là bắt buộc, báo rõ ở main.
    _phase2_expand = None
    _phase2_parse_question = None
    _PARSER_AVAILABLE = False

try:
    from solver import solve as _phase2_solve
    _SOLVER_AVAILABLE = True
except Exception:  # pragma: no cover
    _phase2_solve = None
    _SOLVER_AVAILABLE = False

try:
    from evaluate import answers_match as _phase1_answers_match
    _EVALUATE_AVAILABLE = True
except Exception:  # pragma: no cover
    _phase1_answers_match = None
    _EVALUATE_AVAILABLE = False

try:
    from rulebook import load_calendar as _load_calendar
    from rulebook import load_rulebook as _load_rulebook
    _RULEBOOK_AVAILABLE = True
except Exception:  # pragma: no cover
    _load_calendar = None
    _load_rulebook = None
    _RULEBOOK_AVAILABLE = False

try:
    from cache import load_shop_cache as _load_shop_cache
    _CACHE_AVAILABLE = True
except Exception:  # pragma: no cover
    _load_shop_cache = None
    _CACHE_AVAILABLE = False

try:
    from ingest import load_shop as _phase1_load_shop
    from shop_view import build_shop_view as _build_shop_view
    _INGEST_AVAILABLE = True
except Exception:  # pragma: no cover
    _phase1_load_shop = None
    _build_shop_view = None
    _INGEST_AVAILABLE = False

try:
    from text_utils import strip_diacritics as _phase1_strip
    _STRIP_AVAILABLE = True
except Exception:  # pragma: no cover - dùng bản dự phòng bên dưới.
    _phase1_strip = None
    _STRIP_AVAILABLE = False

__all__ = [
    "ALLOWED_MEASURES",
    "ALLOWED_ENTITY_KINDS",
    "ALLOWED_PERIOD_KINDS",
    "ALLOWED_ANSWER_TYPES",
    "PARSED_FIELDS",
    "strip_diacritics",
    "abbreviate_text",
    "parsed_to_json",
    "validate_parsed_json",
    "make_trace_row",
    "make_generalization_variants",
    "make_negative_examples",
    "load_jsonl",
    "load_shop_view",
    "generate_traces",
    "main",
]

# Các giá trị hợp lệ theo API_CONTRACT mục 2 (kèm ranking_metric mà
# dataclass ParsedQuestion của phase2 bổ sung thêm ngoài contract).
ALLOWED_MEASURES = {
    "revenue", "order_count", "quantity", "cancel_rate",
    "revenue_share", "growth_percent", "growth_amount", "top_product",
    "top_customer", "top_employee", "best_day", "best_month",
    "debt_total", "debt_top", "import_total", "avg_price",
    "price_on_day", "continuous_growth", "revenue_by_payment",
    "revenue_by_group", "revenue_by_channel", "quantity_by_unit",
    "product_revenue", "unknown",
}
ALLOWED_ENTITY_KINDS = {
    "product", "customer", "group", "payment", "employee", "channel",
    "none",
}
ALLOWED_PERIOD_KINDS = {
    "absolute", "relative", "snapshot", "multi_month", "none",
}
ALLOWED_ANSWER_TYPES = {
    "number", "percent", "string", "date", "boolean", "list", "table",
    "unknown",
}
PARSED_FIELDS = [
    "measure", "entity_kind", "entity_key", "entity_display",
    "period_start", "period_end", "period_kind", "period_months",
    "ranking_k", "ranking_target", "ranking_metric", "unit",
    "threshold_money", "threshold_op", "answer_type",
    "confidence_parse", "parse_notes", "normalized_text",
]

# Bảng viết tắt ngược (dạng đầy đủ -> viết tắt) dùng cho biến thể tổng
# quát hoá. Chỉ thay cụm từ đứng độc lập, không thay trong tên riêng
# dài, và chỉ áp dụng trên bản không dấu để khớp đúng dạng người dùng
# hay gõ. Đây là từ vựng ngôn ngữ chung, không theo shop hay câu nào.
_ABBREV_REVERSE = [
    ("doanh thu", "dthu"),
    ("bao nhieu", "nhiu"),
    ("san pham", "sp"),
    ("khach hang", "kh"),
    ("so luong", "sl"),
    ("nhan vien", "nv"),
    ("hom nay", "hnay"),
    ("hom qua", "hqua"),
    ("duoc", "dc"),
]

# Tên thực thể giả lập dùng cho ví dụ tiêu cực. Lúc tạo, script kiểm
# tra tên này thực sự vắng mặt trong danh mục của shop đang chạy; nếu
# trùng thì đổi hậu tố cho đến khi vắng mặt, không bao giờ ghi ví dụ
# tiêu cực bằng một thực thể có thật.
_NEGATIVE_ENTITY_BASE = "Mat hang gia lap khong ton tai"


def strip_diacritics(text: str) -> str:
    """Bỏ dấu tiếng Việt, giữ nguyên chữ cái và khoảng trắng.

    Đầu vào:
        text: chuỗi bất kỳ, có thể rỗng hoặc None.

    Đầu ra:
        Chuỗi không dấu. Ưu tiên dùng hàm đã kiểm chứng của phase1;
        nếu phase1 không nạp được thì dùng bản dự phòng bằng
        unicodedata (tách dấu tổ hợp rồi bỏ, đổi đ/Đ thành d/D).
        Hàm này chỉ phục vụ biến thể tổng quát hoá, không thay đổi
        ý nghĩa câu hỏi hay ParsedQuestion đi kèm.
    """
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    if _STRIP_AVAILABLE and _phase1_strip is not None:
        return _phase1_strip(s)
    decomposed = unicodedata.normalize("NFD", s)
    without = "".join(
        ch for ch in decomposed if unicodedata.category(ch) != "Mn"
    )
    return unicodedata.normalize(
        "NFC", without.translate(str.maketrans({"đ": "d", "Đ": "D"}))
    )


def abbreviate_text(text: str) -> str:
    """Viết tắt hoá một câu hỏi đã bỏ dấu, theo bảng chung.

    Đầu vào:
        text: câu hỏi, thường đã qua :func:`strip_diacritics`.

    Đầu ra:
        Câu hỏi sau khi thay các cụm đầy đủ bằng viết tắt quen thuộc
        (chỉ thay từ/cụm đứng độc lập, dùng ranh giới từ, thử cụm dài
        trước để "doanh thu" không bị thay từng từ lẻ). Nếu không thay
        được cụm nào thì trả nguyên chuỗi đầu vào, để caller biết mà
        bỏ qua biến thể trùng lặp thay vì ghi dòng trùng.
    """
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    for full, short in _ABBREV_REVERSE:
        s = re.sub(r"\b" + re.escape(full) + r"\b", short, s)
    return " ".join(s.split())


def parsed_to_json(parsed) -> dict:
    """Chuyển một ParsedQuestion thành dict JSON đúng các trường contract.

    Đầu vào:
        parsed: object ParsedQuestion của phase2 (dataclass).

    Đầu ra:
        Dict chỉ gồm các trường trong :data:`PARSED_FIELDS`, trong đó
        ngày chuyển thành chuỗi ``YYYY-MM-DD`` (hoặc None) và
        ``period_months`` chuyển thành danh sách ``[năm, tháng]`` để
        ghi JSON được. Hàm không thêm, không bớt trường, và không đưa
        đáp án train hay id câu hỏi vào dict này.
    """
    raw = asdict(parsed)
    out = {}
    for field in PARSED_FIELDS:
        value = raw.get(field)
        if isinstance(value, datetime.date):
            out[field] = value.isoformat()
        elif field == "period_months":
            out[field] = [[int(y), int(m)] for (y, m) in (value or [])]
        else:
            out[field] = value
    return out


def validate_parsed_json(parsed_json: dict) -> tuple[bool, str]:
    """Kiểm tra một parsed_question_json có đúng schema ParsedQuestion không.

    Đầu vào:
        parsed_json: dict cần kiểm tra.

    Đầu ra:
        Tuple ``(hợp_lệ, lý_do)``. Hợp lệ khi đủ trường, measure /
        entity_kind / period_kind / answer_type nằm trong tập cho phép,
        ``confidence_parse`` là số trong [0, 1], ``parse_notes`` là
        list chuỗi, và ngày (nếu có) đúng định dạng ``YYYY-MM-DD``.
        Hàm này dùng chung cho generator và cho bước kiểm tra dataset
        của train_lora, nên logic kiểm tra phải giống nhau ở hai nơi.
    """
    if not isinstance(parsed_json, dict):
        return False, "parsed_question_json không phải dict"
    for field in PARSED_FIELDS:
        if field not in parsed_json:
            return False, f"thiếu trường ParsedQuestion: {field}"
    if parsed_json["measure"] not in ALLOWED_MEASURES:
        return False, f"measure không hợp lệ: {parsed_json['measure']}"
    if parsed_json["entity_kind"] not in ALLOWED_ENTITY_KINDS:
        return False, f"entity_kind không hợp lệ: {parsed_json['entity_kind']}"
    if parsed_json["period_kind"] not in ALLOWED_PERIOD_KINDS:
        return False, f"period_kind không hợp lệ: {parsed_json['period_kind']}"
    if parsed_json["answer_type"] not in ALLOWED_ANSWER_TYPES:
        return False, f"answer_type không hợp lệ: {parsed_json['answer_type']}"
    conf = parsed_json["confidence_parse"]
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        return False, "confidence_parse không phải số"
    if not 0.0 <= float(conf) <= 1.0:
        return False, "confidence_parse ngoài [0, 1]"
    if not isinstance(parsed_json["parse_notes"], list) or not all(
        isinstance(x, str) for x in parsed_json["parse_notes"]
    ):
        return False, "parse_notes phải là list chuỗi"
    for key in ("period_start", "period_end"):
        value = parsed_json[key]
        if value is not None:
            try:
                datetime.date.fromisoformat(str(value))
            except ValueError:
                return False, f"{key} không đúng YYYY-MM-DD: {value}"
    return True, ""


def make_trace_row(question: str, normalized_question: str,
                   as_of_date: str, business_id: str,
                   parsed_json: dict, verified: bool,
                   verification_method: str, source: str,
                   generalization_tag: str) -> dict:
    """Tạo một dòng JSONL đúng schema đầu ra của pipeline.

    Đầu vào:
        question, normalized_question, as_of_date, business_id: thông
            tin câu hỏi (không gồm id câu hỏi, để không ai dùng id để
            tra đáp án hay hard-code theo câu).
        parsed_json: dict đã qua :func:`validate_parsed_json`.
        verified, verification_method, source, generalization_tag:
            nhãn kiểm chứng và nguồn gốc, ghi đúng sự thật (xem
            docstring module về quy tắc trung thực).

    Đầu ra:
        Dict một dòng trace. Hàm không tự suy verified từ dữ liệu:
        caller phải truyền đúng giá trị đã chứng minh được.
    """
    return {
        "question": question,
        "normalized_question": normalized_question,
        "as_of_date": as_of_date,
        "business_id": business_id,
        "parsed_question_json": parsed_json,
        "verified": bool(verified),
        "verification_method": verification_method,
        "source": source,
        "generalization_tag": generalization_tag,
    }


def make_generalization_variants(base_row: dict) -> list[dict]:
    """Tạo biến thể tổng quát hoá từ một trace đã kiểm chứng.

    Đầu vào:
        base_row: dòng trace gốc có ``verified=True`` (caller phải đảm
            bảo điều này; hàm không tự kiểm chứng lại).

    Đầu ra:
        Danh sách biến thể (có thể rỗng nếu câu gốc đã không dấu và
        không chứa cụm viết tắt được). Mỗi biến thể giữ nguyên
        ``parsed_question_json`` và ``as_of_date``/``business_id`` của
        gốc, nhưng ghi ``verified=False`` vì solver chưa chạy trên câu
        biến thể — parse chỉ được kế thừa, không được bịa là đã kiểm
        chứng. Câu biến thể trùng với câu gốc hoặc trùng nhau thì bị
        loại, để dataset không có dòng trùng lặp vô ích.
    """
    original = base_row["question"]
    stripped = strip_diacritics(original)
    variants = []
    seen = {original}
    for variant_text, tag in (
        (stripped, "no_diacritics"),
        (abbreviate_text(stripped), "abbreviated"),
    ):
        if not variant_text or variant_text in seen:
            continue
        seen.add(variant_text)
        variants.append(make_trace_row(
            question=variant_text,
            normalized_question=base_row["normalized_question"],
            as_of_date=base_row["as_of_date"],
            business_id=base_row["business_id"],
            parsed_json=base_row["parsed_question_json"],
            verified=False,
            verification_method=(
                "inherited_from_solver_verified_source_not_rerun"
            ),
            source="generalization_transform_of_verified_trace",
            generalization_tag=tag,
        ))
    return variants


def make_negative_examples(source_row: dict, view) -> list[dict]:
    """Tạo ví dụ tiêu cực cho abstain/unanswerable từ một shop đã có view.

    Đầu vào:
        source_row: một dòng trace đã kiểm chứng của shop đó, dùng để
            lấy kỳ/as_of tham chiếu và measure mẫu (không lấy đáp án,
            vì dòng trace không chứa đáp án).
        view: ShopView của shop đó, dùng để kiểm tra thực thể vắng mặt
            và khoảng dữ liệu (``date_min``/``date_max``).

    Đầu ra:
        Tối đa 2 ví dụ, mỗi ví dụ đều ``verified=False``,
        ``source="negative_example_synthetic"``, không gán đáp án:

        - ``negative_entity_absent``: câu hỏi về một mặt hàng đã kiểm
          tra là không có trong danh mục của shop; parse giữ measure
          và kỳ của nguồn nhưng ``confidence_parse`` hạ xuống 0.2 và
          ``parse_notes`` ghi rõ thực thể không tồn tại, để model học
          không đoán thực thể khác thay thế.
        - ``negative_period_out_of_range``: câu hỏi về một tháng nằm
          trước khoảng dữ liệu của shop (tháng 1 của năm
          ``date_min.year - 5``); ``confidence_parse`` 0.3 và
          ``parse_notes`` ghi rõ kỳ ngoài dữ liệu. Nếu view không có
          ``date_min`` thì bỏ qua ví dụ này thay vì đoán năm.

    Trường hợp biên: nếu tên thực thể giả lập vô tình trùng với một
    sản phẩm thật của shop thì đổi hậu tố số cho đến khi vắng mặt.
    """
    negatives = []
    base_parsed = dict(source_row["parsed_question_json"])
    products = getattr(view, "products", None) or []

    def _norm(text):
        """Chuẩn hoá nhẹ để so sánh tên: thường hoá, bỏ dấu, gộp trắng."""
        return " ".join(strip_diacritics(str(text)).lower().split())

    product_norms = {_norm(p.get("display") or "") for p in products}
    product_norms |= {_norm(p.get("key") or "") for p in products}
    absent_name = _NEGATIVE_ENTITY_BASE
    suffix = 0
    while _norm(absent_name) in product_norms:
        suffix += 1
        absent_name = f"{_NEGATIVE_ENTITY_BASE} {suffix}"
    entity_parsed = dict(base_parsed)
    entity_parsed["entity_kind"] = "product"
    entity_parsed["entity_key"] = absent_name
    entity_parsed["entity_display"] = absent_name
    entity_parsed["confidence_parse"] = 0.2
    entity_parsed["parse_notes"] = [
        "Ví dụ tiêu cực: thực thể này đã kiểm tra là không có trong "
        "danh mục sản phẩm của shop lúc tạo dữ liệu, nên câu hỏi "
        "không được trả bằng cách đoán một sản phẩm khác; cần "
        "abstain hoặc unanswerable.",
    ]
    entity_question = (
        f"Doanh thu {absent_name} trong ky cua cau hoi goc la bao nhieu"
    )
    negatives.append(make_trace_row(
        question=entity_question,
        normalized_question=(
            _phase2_expand(entity_question)
            if _phase2_expand is not None else entity_question.lower()
        ),
        as_of_date=source_row["as_of_date"],
        business_id=source_row["business_id"],
        parsed_json=entity_parsed,
        verified=False,
        verification_method="negative_example_no_solver_run_no_answer",
        source="negative_example_synthetic",
        generalization_tag="negative_entity_absent",
    ))

    date_min = getattr(view, "date_min", None)
    if date_min is not None:
        out_year = date_min.year - 5
        period_parsed = dict(base_parsed)
        period_parsed["period_start"] = f"{out_year:04d}-01-01"
        period_parsed["period_end"] = f"{out_year:04d}-01-31"
        period_parsed["period_kind"] = "absolute"
        period_parsed["period_months"] = []
        period_parsed["confidence_parse"] = 0.3
        period_parsed["parse_notes"] = [
            "Ví dụ tiêu cực: kỳ hỏi nằm hoàn toàn trước khoảng dữ "
            "liệu của shop (dựa vào date_min của view lúc tạo dữ "
            "liệu), nên không được trả 0 hay đoán số; cần "
            "unanswerable hoặc abstain.",
        ]
        period_question = (
            f"Doanh thu thang 1 nam {out_year} cua cua hang la bao nhieu"
        )
        negatives.append(make_trace_row(
            question=period_question,
            normalized_question=(
                _phase2_expand(period_question)
                if _phase2_expand is not None
                else period_question.lower()
            ),
            as_of_date=source_row["as_of_date"],
            business_id=source_row["business_id"],
            parsed_json=period_parsed,
            verified=False,
            verification_method="negative_example_no_solver_run_no_answer",
            source="negative_example_synthetic",
            generalization_tag="negative_period_out_of_range",
        ))
    return negatives


def load_jsonl(path: Path) -> list[dict]:
    """Đọc một file JSONL thành list dict, bỏ dòng trống.

    Đầu vào:
        path: đường dẫn file JSONL.

    Đầu ra:
        List các dict theo thứ tự file. Dòng không phải JSON hợp lệ sẽ
        làm script dừng bằng lỗi rõ ràng, thay vì lặng lẽ bỏ qua và
        tạo dataset thiếu mà không ai biết.
    """
    rows = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_shop_view(business_id: str, data_dir: Path,
                   cache_dir: Path | None):
    """Nạp ShopView của một shop, ưu tiên cache phase2 rồi mới ingest lại.

    Đầu vào:
        business_id: mã shop cần nạp.
        data_dir: thư mục ``data`` của split train (chứa các thư mục
            shop), dùng để tính hash nguồn cho cache và để ingest.
        cache_dir: thư mục cache phase2, hoặc None để bỏ qua cache.

    Đầu ra:
        ShopView, hoặc None nếu cả cache lẫn ingest đều thất bại (khi
        đó caller bỏ qua shop này và ghi vào thống kê, không crash cả
        đợt chạy). Cache chỉ được dùng khi hash nguồn khớp, theo đúng
        thiết kế của ``cache.py`` — view cũ khi nguồn đã đổi sẽ không
        bao giờ được trả về.
    """
    if cache_dir is not None and _CACHE_AVAILABLE:
        try:
            view = _load_shop_cache(cache_dir, business_id, data_dir)
        except Exception:
            view = None
        if view is not None:
            return view
    if not _INGEST_AVAILABLE:
        return None
    try:
        ingest_result = _phase1_load_shop(business_id, data_dir)
        return _build_shop_view(ingest_result, business_id)
    except Exception:
        return None


def generate_traces(train_dir: Path, output_path: Path,
                    shops: list[str] | None, limit: int,
                    cache_dir: Path | None, use_solver: bool,
                    keep_unverified: bool,
                    with_generalization: bool,
                    with_negatives: bool) -> dict:
    """Chạy toàn bộ pipeline tạo trace và ghi file JSONL đầu ra.

    Đầu vào:
        train_dir: thư mục split train (chứa questions.jsonl,
            answers.jsonl, rulebook.md, calendar.csv, data/).
        output_path: file JSONL sẽ ghi (thư mục cha được tạo nếu chưa
            có; file cũ bị ghi đè toàn bộ để không trộn trace cũ/mới).
        shops: danh sách mã shop cần chạy, hoặc None để chạy mọi shop
            có trong questions.jsonl. Đây là bộ lọc chạy do caller
            chọn, không phải hard-code tạo trace theo shop.
        limit: số câu hỏi tối đa được xét (0 = không giới hạn). Câu
            hỏi được xét theo thứ tự business_id rồi id, deterministic.
        cache_dir: thư mục cache phase2, hoặc None để luôn ingest lại.
        use_solver: True thì chạy solver + đối chiếu đáp án để xác
            minh; False thì mọi candidate đều ghi verified=False (chế
            độ dự phòng khi solver không chạy được, và README phải ghi
            rõ giới hạn này — không được đổi False thành True thủ công).
        keep_unverified: True thì ghi thêm candidate chưa kiểm chứng
            với verified=False để debug; False (mặc định) thì loại bỏ.
        with_generalization: có tạo biến thể không dấu/viết tắt không.
        with_negatives: có tạo ví dụ tiêu cực cho mỗi shop có trace
            đã kiểm chứng không (tối đa 2 ví dụ/shop).

    Đầu ra:
        Dict thống kê: số câu đã xét, candidate, solver trả lời, đúng
        (giữ lại), sai (loại), abstain (loại), số dòng đã ghi theo
        từng loại, và số shop bị bỏ qua do nạp view thất bại. Thống kê
        này là bằng chứng kiểm chứng, phải được trích nguyên vẹn vào
        README/báo cáo, không được làm tròn hay sửa tay.
    """
    questions = load_jsonl(train_dir / "questions.jsonl")
    answers = {
        row["id"]: row for row in load_jsonl(train_dir / "answers.jsonl")
    }
    if _RULEBOOK_AVAILABLE:
        rulebook = _load_rulebook(
            train_dir / "rulebook.md", train_dir / "calendar.csv")
        calendar = _load_calendar(train_dir / "calendar.csv")
    else:
        rulebook, calendar = None, None
    data_dir = train_dir / "data"

    ordered = sorted(
        questions, key=lambda q: (str(q.get("business_id", "")),
                                  str(q.get("id", ""))))
    if shops:
        wanted = set(shops)
        ordered = [q for q in ordered if q.get("business_id") in wanted]
    if limit and limit > 0:
        ordered = ordered[:limit]

    stats = {
        "questions_examined": 0,
        "candidates_parsed": 0,
        "candidates_invalid_schema": 0,
        "solver_answered": 0,
        "solver_correct_kept": 0,
        "solver_wrong_dropped": 0,
        "solver_abstain_dropped": 0,
        "unverified_candidates_written": 0,
        "generalization_variants_written": 0,
        "negative_examples_written": 0,
        "shops_skipped_view_failed": 0,
        "rows_written": 0,
    }
    rows: list[dict] = []
    views: dict[str, object] = {}
    failed_shops: set[str] = set()
    verified_by_shop: dict[str, dict] = {}

    for question in ordered:
        business_id = str(question.get("business_id", ""))
        question_id = str(question.get("id", ""))
        if business_id in failed_shops:
            continue
        if business_id not in views:
            view = load_shop_view(business_id, data_dir, cache_dir)
            if view is None:
                failed_shops.add(business_id)
                stats["shops_skipped_view_failed"] += 1
                continue
            views[business_id] = view
        view = views[business_id]
        try:
            as_of = datetime.date.fromisoformat(
                str(question.get("as_of_date", "")))
        except ValueError:
            continue
        stats["questions_examined"] += 1
        products = getattr(view, "products", None) or []
        customers = getattr(view, "customers", None) or []
        parsed = _phase2_parse_question(
            str(question.get("question", "")), as_of, calendar, rulebook,
            products, customers)
        stats["candidates_parsed"] += 1
        parsed_json = parsed_to_json(parsed)
        ok, _reason = validate_parsed_json(parsed_json)
        if not ok:
            stats["candidates_invalid_schema"] += 1
            continue
        base_row = make_trace_row(
            question=str(question.get("question", "")),
            normalized_question=parsed.normalized_text,
            as_of_date=as_of.isoformat(),
            business_id=business_id,
            parsed_json=parsed_json,
            verified=False,
            verification_method="candidate_not_verified",
            source="template_parser_candidate",
            generalization_tag="none",
        )
        verified = False
        if use_solver and _SOLVER_AVAILABLE and _EVALUATE_AVAILABLE:
            reference = answers.get(question_id)
            if reference is not None:
                result = _phase2_solve(parsed, view, rulebook, calendar)
                entry = result.entry or {}
                if entry.get("type") not in (None, "abstain"):
                    stats["solver_answered"] += 1
                    submission = {
                        "id": question_id,
                        "type": entry.get("type"),
                        "answer": entry.get("answer"),
                    }
                    if _phase1_answers_match(reference, submission) == "correct":
                        verified = True
                    else:
                        stats["solver_wrong_dropped"] += 1
                else:
                    stats["solver_abstain_dropped"] += 1
            else:
                stats["solver_abstain_dropped"] += 1
        if verified:
            stats["solver_correct_kept"] += 1
            verified_row = make_trace_row(
                question=base_row["question"],
                normalized_question=base_row["normalized_question"],
                as_of_date=base_row["as_of_date"],
                business_id=base_row["business_id"],
                parsed_json=parsed_json,
                verified=True,
                verification_method="solver_output_matches_train_answer",
                source="template_parser_plus_solver",
                generalization_tag="none",
            )
            rows.append(verified_row)
            verified_by_shop.setdefault(business_id, verified_row)
            if with_generalization:
                variants = make_generalization_variants(verified_row)
                rows.extend(variants)
                stats["generalization_variants_written"] += len(variants)
        elif keep_unverified:
            rows.append(base_row)
            stats["unverified_candidates_written"] += 1

    if with_negatives:
        for business_id in sorted(verified_by_shop):
            view = views.get(business_id)
            if view is None:
                continue
            negatives = make_negative_examples(
                verified_by_shop[business_id], view)
            rows.extend(negatives)
            stats["negative_examples_written"] += len(negatives)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    stats["rows_written"] = len(rows)
    return stats


def main(argv: list[str] | None = None) -> int:
    """Điểm vào dòng lệnh của script tạo trace.

    Đầu vào:
        argv: tham số dòng lệnh (None thì lấy từ sys.argv).

    Đầu ra:
        Mã thoát 0 khi ghi file thành công (kể cả khi một vài shop bị
        bỏ qua do nạp view thất bại, vì đó đã ghi trong thống kê);
        1 khi thiếu parser/rulebook (không thể tạo trace đáng tin) hoặc
        khi đọc file train thất bại. Thống kê kiểm chứng được in ra
        stdout để caller trích vào README/báo cáo.
    """
    parser = argparse.ArgumentParser(
        description="Tạo trace ParsedQuestion đã kiểm chứng bằng solver.")
    parser.add_argument(
        "--train-dir", type=Path, default=_DATAMATE_DIR / "train",
        help="Thư mục split train (mặc định: <datamate>/train).")
    parser.add_argument(
        "--output", type=Path,
        default=Path(__file__).resolve().parent / "data"
        / "verified_traces_sample.jsonl",
        help="File JSONL đầu ra.")
    parser.add_argument(
        "--shops", type=str, default="",
        help="Danh sách mã shop cách nhau bằng dấu phẩy; rỗng = mọi shop.")
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Số câu hỏi tối đa được xét (0 = không giới hạn).")
    parser.add_argument(
        "--cache-dir", type=Path,
        default=_DATAMATE_DIR / "phase2" / "cache",
        help="Thư mục cache phase2; dùng --no-cache để luôn ingest lại.")
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Bỏ qua cache phase2, luôn ingest lại từ dữ liệu gốc.")
    parser.add_argument(
        "--no-solver", action="store_true",
        help="Không chạy solver: mọi candidate đều verified=False. "
        "Chỉ dùng khi solver không chạy được, và phải ghi rõ giới hạn "
        "này trong báo cáo.")
    parser.add_argument(
        "--keep-unverified-candidates", action="store_true",
        help="Ghi thêm candidate chưa kiểm chứng (verified=False) để "
        "debug. Mặc định tắt: candidate chưa kiểm chứng bị loại.")
    parser.add_argument(
        "--no-generalization", action="store_true",
        help="Không tạo biến thể không dấu/viết tắt.")
    parser.add_argument(
        "--no-negatives", action="store_true",
        help="Không tạo ví dụ tiêu cực abstain/unanswerable.")
    args = parser.parse_args(argv)

    if not _PARSER_AVAILABLE or not _RULEBOOK_AVAILABLE:
        print("LỖI: không nạp được parser phase2 hoặc rulebook phase1; "
              "không thể tạo trace đáng tin.", file=sys.stderr)
        return 1
    if args.no_solver or not _SOLVER_AVAILABLE or not _EVALUATE_AVAILABLE:
        print("CẢNH BÁO: chạy ở chế độ không kiểm chứng bằng solver "
              "(--no-solver hoặc thiếu solver/evaluate). Mọi dòng ghi "
              "ra sẽ có verified=False; không được đổi thành True thủ "
              "công.", file=sys.stderr)
    shops = [s.strip() for s in args.shops.split(",") if s.strip()] \
        or None
    stats = generate_traces(
        train_dir=args.train_dir,
        output_path=args.output,
        shops=shops,
        limit=args.limit,
        cache_dir=None if args.no_cache else args.cache_dir,
        use_solver=not args.no_solver,
        keep_unverified=args.keep_unverified_candidates,
        with_generalization=not args.no_generalization,
        with_negatives=not args.no_negatives,
    )
    print(f"Đã ghi {stats['rows_written']} dòng vào {args.output}")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Điểm vào mới của Giai đoạn 2 DataMate với parser LLM (xem phase2/API_CONTRACT mục 6).

Mục đích của script này
-----------------------
Nối parser LLM (khi có) với solver, gate, cache và shop_view đã có của
Giai đoạn 2, theo đúng luồng của ``phase2/run_phase2.py`` mà KHÔNG sửa
bất kỳ file nào trong ``phase2/``:

1. Đọc ``questions.jsonl`` theo thứ tự ``business_id`` rồi ``id`` câu
   hỏi (deterministic, không dùng ngẫu nhiên).
2. Với mỗi shop, theo thứ tự: thử nạp cache (dùng lại cache của phase2
   theo mặc định, để không ingest lại vô ích), nếu miss thì ingest bằng
   phase1 (``load_shop``) + dựng view (``shop_view``) + lưu cache; giải
   mọi câu của shop đó rồi GIẢI PHÓNG view trước khi sang shop khác.
3. Mỗi câu hỏi đi qua: parse bằng LLM (``parse_with_llm``) có fallback
   về parser template, đánh dấu nguồn parse (``source``) -> solver
   (``solve``, không sửa) -> gate (``compute_confidence`` làm trần rồi
   ``decide`` áp ngưỡng 0,65 + kiểm tra unanswerable + format, không
   sửa logic gate; chỉ truyền confidence đã hiệu chỉnh, lấy
   ``min(confidence_solver, confidence_parse)`` làm trần như
   ``run_phase2.py``, và nếu nguồn là ``template_fallback`` thì không
   tăng confidence vượt quá confidence của template).
4. Ghi submission TĂNG DẦN (mỗi câu ghi ngay, flush ngay) để crash giữa
   chừng vẫn còn đáp án đã giải.
5. Chấm bằng phase1 ``evaluate.score_submission`` và ghi
   ``reports/PHASE2_LLM_SUMMARY.json`` cùng
   ``reports/error_analysis_llm.json`` (trong thư mục ``phase2_llm``).

Trạng thái backend và cách runner phát hiện module thật
------------------------------------------------------
Việc phát hiện ``llm_parser`` diễn ra ở thời điểm import, không cần
sửa runner khi module đó xuất hiện:

- Nếu ``phase2_llm/src/llm_parser.py`` tồn tại và import được, runner
  dùng ``parse_with_llm`` thật của module đó (và truyền ``backend``
  qua cho module đó nếu caller có truyền backend, vì backend thật dùng
  ``generate_json`` chứ không dùng ``generate`` của stub).
- Nếu module đó chưa có hoặc import lỗi, runner KHÔNG crash mà dùng
  bản fallback tạm thời ngay trong file này: ``parse_with_llm`` gọi
  parser template của phase2 và đóng gói kết quả thành ``ParseResult``
  với ``source='template_fallback'``.
- QUAN TRỌNG: chỉ riêng việc module ``llm_parser`` tồn tại KHÔNG có
  nghĩa là câu hỏi đã do LLM parse. Module thật, khi không được truyền
  backend mô hình (không có weights/transformers), sẽ tự rơi về
  template với ``source='template_fallback'`` theo thiết kế của nó.
  Vì vậy báo cáo phải căn cứ vào ``parse_sources`` (số câu từng nguồn)
  chứ không chỉ căn cứ vào trường ``backend``. KHÔNG gọi mạng/API
  thương mại nào trong runner này; mọi đường parse đều offline.

Cách chạy (mặc định tính tương đối từ thư mục ``phase2_llm``)::

    python3 run_phase2_llm.py [--train-dir ../train]
      [--cache-dir ../phase2/cache]
      [--output ./output/submission_llm.jsonl]
      [--shops biz_101,biz_102] [--limit N]

Quy ước thoát: trả 0 khi chạy hết mọi shop đã chọn (shop lỗi riêng lẻ
đã được bắt và ghi vào báo cáo thì không giết cả đợt chạy); trả khác 0
khi lỗi ở tầng chạy chung không bắt được (ví dụ không đọc được file câu
hỏi, không nạp được rulebook/calendar).

Tổng quát hoá: script không hard-code business_id, id câu hỏi, tên cột
train hay đáp án train. Đáp án train (nếu có file ``answers.jsonl``)
chỉ dùng để CHẤM và phân tích lỗi sau khi đã ghi submission, không dùng
để quyết định đáp án.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import gc
import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Nạp phase1, phase2 và phase2_llm/src bằng đường dẫn tương đối từ file
# này, không hard-code đường dẫn tuyệt đối của máy. File này nằm ở
# <datamate>/phase2_llm/run_phase2_llm.py nên phase1/src và phase2/src
# nằm ngay dưới thư mục cha của nó, còn llm_parser (khi có) nằm ở
# <datamate>/phase2_llm/src/.
# ---------------------------------------------------------------------------
_PHASE2_LLM_DIR = Path(__file__).resolve().parent
_PHASE2_DIR = _PHASE2_LLM_DIR.parent / "phase2"
_PHASE1_SRC = _PHASE2_LLM_DIR.parent / "phase1" / "src"
_PHASE2_SRC = _PHASE2_DIR / "src"
_LLM_SRC = _PHASE2_LLM_DIR / "src"
for _candidate in (str(_LLM_SRC), str(_PHASE2_SRC), str(_PHASE1_SRC)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from cache import load_shop_cache, save_shop_cache  # noqa: E402
from gate import GATE_THRESHOLD, compute_confidence, decide  # noqa: E402
from ingest import load_shop  # noqa: E402
from parser import parse_question as parse_question_template  # noqa: E402
from rulebook import load_calendar, load_rulebook  # noqa: E402
from shop_view import build_shop_view  # noqa: E402
from solver import solve  # noqa: E402

try:
    from evaluate import answers_match, load_answers, load_submission, score_submission  # noqa: E402
except ImportError:  # pragma: no cover - dự phòng khi evaluate chưa có
    answers_match = None
    load_answers = None
    load_submission = None
    score_submission = None

#: Số câu lỗi nhóm parser của baseline phase2, lấy từ
#: ``phase2/reports/error_analysis.json`` (791 câu). Chỉ dùng để SO
#: SÁNH trong báo cáo, không dùng để quyết định đáp án hay parse.
BASELINE_PARSER_ERROR_COUNT = 791

#: Các measure hợp lệ theo API_CONTRACT mục 2, dùng để kiểm chứng đầu
#: ra JSON của LLM trước khi dựng ``ParsedQuestion`` (xem
#: :func:`validate_and_build`). Liệt kê theo contract, không theo một
#: shop hay một ngành train nào.
_ALLOWED_MEASURES = frozenset({
    "revenue", "order_count", "quantity", "cancel_rate", "revenue_share",
    "growth_percent", "growth_amount", "top_product", "top_customer",
    "top_employee", "best_day", "best_month", "debt_total", "debt_top",
    "import_total", "avg_price", "price_on_day", "continuous_growth",
    "revenue_by_payment", "revenue_by_group", "revenue_by_channel",
    "quantity_by_unit", "product_revenue", "unknown",
})

#: Các answer_type hợp lệ theo API_CONTRACT mục 2.
_ALLOWED_ANSWER_TYPES = frozenset({
    "number", "percent", "string", "date", "boolean", "list", "table",
    "unknown",
})

__all__ = [
    "ParseResult",
    "LocalModelBackend",
    "BACKEND_KIND",
    "BASELINE_PARSER_ERROR_COUNT",
    "build_prompt",
    "validate_and_build",
    "parse_with_llm",
    "parse_args",
    "load_questions",
    "parse_as_of",
    "sanitize_entry",
    "solve_one_question",
    "run_phase2_llm",
    "main",
]


# ---------------------------------------------------------------------------
# Interface đã thỏa thuận với agent schema/backend (brief chung)
# ---------------------------------------------------------------------------


@dataclass
class ParseResult:
    """Kết quả parse của một câu hỏi, kèm nguồn và độ chắc.

    Ý nghĩa từng trường:

    - ``parsed``: ``ParsedQuestion`` theo API_CONTRACT mục 2 (của
      phase2, không định nghĩa lại ở đây để tránh xung đột kiểu).
    - ``source``: nguồn parse, một trong ``'llm'`` (parser LLM thật),
      ``'template_fallback'`` (rơi về parser template vì LLM lỗi hoặc
      chưa có), ``'llm_validated'`` (JSON của LLM đã qua
      ``validate_and_build``). Runner ghi nguồn này vào báo cáo để
      biết chính xác bao nhiêu câu do LLM parse, bao nhiêu câu do
      fallback — không được ghi ``'llm'`` cho câu fallback.
    - ``confidence``: độ chắc của lần parse này, 0..1. Với fallback thì
      bằng ``parsed.confidence_parse`` của template (không tăng thêm).
    - ``notes``: ghi chú truy vết (vì sao fallback, vì sao không chắc).

    Tổng quát hoá: class này chỉ là vỏ bọc duck-typing; ``llm_parser``
    thật có thể định nghĩa ``ParseResult`` riêng với cùng các trường,
    runner chỉ đọc thuộc tính chứ không ép kiểu (xem
    :func:`_adapt_parse_result`).
    """

    parsed: object = None
    source: str = "template_fallback"
    confidence: float = 0.0
    notes: list = field(default_factory=list)


class LocalModelBackend:
    """Backend mô hình cục bộ, bản stub tạm thời khi chưa có LLM thật.

    Vai trò theo brief chung: backend này là nơi duy nhất gọi mô hình
    cục bộ (<= 15B, chạy offline) để sinh JSON structured output từ
    prompt. Vì mô hình thật chưa được bàn giao cho runner này, bản stub
    KHÔNG giả vờ sinh đáp án: :meth:`generate` luôn raise
    ``RuntimeError`` rõ ràng, và :func:`parse_with_llm` bắt lỗi đó để
    rơi về parser template. Khi agent backend bàn giao backend thật
    (qua ``llm_parser``), class này không còn được dùng nữa.

    Đầu vào của constructor: ``model_path`` (đường dẫn mô hình, tùy
    chọn, chỉ để ghi chú — stub không nạp mô hình nào) và ``**kwargs``
    (các tham số khác của backend thật, stub bỏ qua để chữ ký tương
    thích khi thay thế).
    """

    def __init__(self, model_path=None, **kwargs):
        """Khởi tạo stub backend, chỉ ghi lại đường dẫn mô hình.

        Không nạp mô hình, không gọi mạng, không tốn tài nguyên; mọi
        lời gọi sinh văn bản đều sẽ raise ở :meth:`generate` để caller
        biết rõ backend thật chưa có, thay vì trả văn bản giả.
        """
        self.model_path = model_path
        self.extra_kwargs = dict(kwargs)

    def generate(self, prompt):
        """Sinh văn bản từ prompt — bản stub luôn raise lỗi.

        Đầu vào: ``prompt`` là chuỗi prompt đã dựng bằng
        :func:`build_prompt` (tham số chỉ để chữ ký tương thích, stub
        không dùng tới).

        Đầu ra: không bao giờ trả về (luôn raise ``RuntimeError``),
        để :func:`parse_with_llm` rơi về fallback template một cách
        tường minh thay vì dùng văn bản giả mạo làm đáp án LLM.
        """
        raise RuntimeError(
            "Backend LLM chưa được bàn giao (llm_parser chưa tồn tại); "
            "runner sẽ fallback về parser template thay vì sinh văn bản giả."
        )


def _try_import_real_llm_parser():
    """Thử nạp ``parse_with_llm`` thật từ ``phase2_llm/src/llm_parser.py``.

    Trả về hàm thật nếu module tồn tại và import được, ngược lại trả
    ``None``. Mọi lỗi import đều được bắt, vì module đang được agent
    khác làm song song và có thể ở trạng thái dở dang; runner không
    được crash chỉ vì module đó chưa về hoặc import lỗi.
    """
    try:
        from llm_parser import parse_with_llm as real_parse  # noqa: E402

        return real_parse
    except Exception:
        return None


_REAL_PARSE_WITH_LLM = _try_import_real_llm_parser()

#: Loại backend thực tế mà runner đang dùng trong lần chạy này:
#: ``'llm_parser'`` khi module thật đã về và import được, ngược lại
#: ``'template_fallback_tam_thoi'``. Giá trị này được ghi vào summary
#: và báo cáo để coordinator không nhầm fallback với LLM thật.
BACKEND_KIND = "llm_parser" if _REAL_PARSE_WITH_LLM is not None \
    else "template_fallback_tam_thoi"


def _adapt_parse_result(result, fallback_notes=None):
    """Chuẩn hoá kết quả parse của LLM về dạng ``ParseResult`` của runner.

    Đầu vào: ``result`` có thể là ``ParseResult`` của module LLM (đọc
    thuộc tính ``parsed``/``source``/``confidence``/``notes``), là
    ``ParsedQuestion`` trực tiếp (có thuộc tính ``measure``), hoặc là
    dict với các khóa tương ứng. ``fallback_notes`` là ghi chú thêm
    khi cần.

    Đầu ra: ``ParseResult`` đã chuẩn hoá, hoặc ``None`` khi ``result``
    không chứa được ``ParsedQuestion`` nào dùng được (khi đó caller
    phải fallback về template, không được đoán parse từ kết quả lạ).

    Vì sao cần hàm này: runner không được ép kiểu kết quả của module
    khác (module đó có thể định nghĩa ``ParseResult`` riêng), nên chỉ
    đọc thuộc tính bằng duck-typing, giống cách ``gate.py`` làm.
    """
    notes = list(fallback_notes or [])
    if result is None:
        return None
    if isinstance(result, dict):
        parsed = result.get("parsed")
        if parsed is None:
            return None
        extra_notes = result.get("notes") or []
        if isinstance(extra_notes, (list, tuple)):
            notes.extend(str(item) for item in extra_notes)
        confidence = result.get("confidence")
        if not isinstance(confidence, (int, float)) \
                or isinstance(confidence, bool):
            confidence = getattr(parsed, "confidence_parse", 0.0) or 0.0
        return ParseResult(
            parsed=parsed,
            source=str(result.get("source") or "llm"),
            confidence=float(confidence),
            notes=notes,
        )
    parsed = getattr(result, "parsed", None)
    if parsed is not None:
        source = getattr(result, "source", None) or "llm"
        confidence = getattr(result, "confidence", None)
        if not isinstance(confidence, (int, float)) \
                or isinstance(confidence, bool):
            confidence = getattr(parsed, "confidence_parse", 0.0) or 0.0
        raw_notes = getattr(result, "notes", None) or []
        if isinstance(raw_notes, (list, tuple)):
            notes.extend(str(item) for item in raw_notes)
        return ParseResult(
            parsed=parsed,
            source=str(source),
            confidence=float(confidence),
            notes=notes,
        )
    # Kết quả chính là ParsedQuestion (có measure, không có vỏ bọc).
    if hasattr(result, "measure"):
        confidence = getattr(result, "confidence_parse", 0.0) or 0.0
        return ParseResult(
            parsed=result, source="llm",
            confidence=float(confidence), notes=notes,
        )
    return None


def _parse_with_template_fallback(question_text, as_of, calendar, rulebook,
                                  products, customers, reason):
    """Parse bằng parser template của phase2, đóng gói thành ``ParseResult``.

    Đầu vào: giống :func:`parse_question` của phase2, cộng ``reason``
    là lý do fallback (để ghi vào ``notes`` cho truy vết).

    Đầu ra: ``ParseResult`` với ``source='template_fallback'`` và
    ``confidence`` bằng đúng ``confidence_parse`` của template — không
    tăng, không giảm thêm, vì fallback không được tự tin hơn chính
    parser mà nó rơi về.
    """
    parsed = parse_question_template(
        question_text, as_of, calendar, rulebook,
        products or [], customers or [],
    )
    notes = [reason]
    for note in getattr(parsed, "parse_notes", None) or []:
        notes.append(str(note))
    return ParseResult(
        parsed=parsed,
        source="template_fallback",
        confidence=float(getattr(parsed, "confidence_parse", 0.0) or 0.0),
        notes=notes,
    )


def build_prompt(question_text, products, customers):
    """Dựng prompt structured-output cho LLM từ câu hỏi và danh sách shop.

    Đầu vào:
        question_text: câu hỏi gốc của người dùng.
        products/customers: list dict của shop (``'key'``, ``'display'``,
            ``'code'``, ``'group'``) theo ShopView.

    Đầu ra:
        Chuỗi prompt yêu cầu LLM chỉ trả JSON theo schema của
        ``ParsedQuestion`` (measure, entity, kỳ, ranking, unit,
        threshold, answer_type, confidence), kèm danh sách thực thể của
        shop để LLM khớp tên mà không bịa thực thể ngoài shop. Danh
        sách thực thể được cắt ở 100 mục mỗi loại để prompt không phình
        vô hạn với shop lớn; việc cắt này chỉ ảnh hưởng prompt, không
        ảnh hưởng fallback template (template vẫn thấy toàn bộ list).

    Trường hợp biên: danh sách rỗng thì prompt ghi rõ shop không có
    thực thể loại đó, để LLM không bịa tên sản phẩm/khách.
    """
    def _names(items):
        """Lấy tối đa 100 tên hiển thị từ list thực thể, bỏ mục rỗng."""
        names = []
        for item in items or []:
            display = item.get("display") if isinstance(item, dict) else None
            if display:
                names.append(str(display))
            if len(names) >= 100:
                break
        return names

    product_names = _names(products)
    customer_names = _names(customers)
    return (
        "Bạn là bộ phận parse câu hỏi của hệ thống DataMate. "
        "Chỉ trả JSON duy nhất, không giải thích thêm, theo schema: "
        '{"measure": str, "entity_kind": str, "entity_key": str hoặc null, '
        '"entity_display": str hoặc null, "period_start": "YYYY-MM-DD" hoặc null, '
        '"period_end": "YYYY-MM-DD" hoặc null, "period_kind": str, '
        '"period_months": [[năm, tháng], ...], "ranking_k": int hoặc null, '
        '"ranking_target": str, "unit": str hoặc null, '
        '"threshold_money": number hoặc null, "threshold_op": str hoặc null, '
        '"answer_type": str, "confidence_parse": number 0..1}. '
        "Measure phải thuộc tập hợp của API_CONTRACT mục 2. "
        "Chỉ chọn entity_key trong danh sách của shop dưới đây, "
        "không bịa thực thể ngoài shop; không chắc thì giảm "
        "confidence_parse thay vì đoán. "
        f"Câu hỏi: {question_text}\n"
        f"Sản phẩm của shop: {product_names}\n"
        f"Khách hàng của shop: {customer_names}\n"
    )


def validate_and_build(raw_output, question_text, as_of, calendar, rulebook,
                       products, customers):
    """Kiểm chứng JSON của LLM rồi dựng ``ParsedQuestion``, sai thì ``None``.

    Đầu vào:
        raw_output: dict JSON mà LLM trả (đã parse JSON xong, chưa tin
            cậy); các tham số còn lại giống :func:`parse_question` của
            phase2, dùng để đối chiếu thực thể với shop và để dựng
            ``ParsedQuestion`` bằng đúng class của phase2.
        question_text: câu hỏi gốc, chỉ dùng để truy vết khi cần.

    Đầu ra:
        ``ParsedQuestion`` đã kiểm chứng, hoặc ``None`` khi JSON sai
        schema (measure/answer_type ngoài tập hợp cho phép, ngày sai
        định dạng, thực thể không có trong shop, confidence ngoài
        [0, 1]). Trả ``None`` thay vì raise, để caller rơi về template
        một cách an toàn — một JSON LLM lỗi không được giết cả shop.

    Cách kiểm chứng (không đoán, không sửa lặng lẽ):
    - measure và answer_type phải nằm trong tập hợp của contract; nếu
      không thì loại, vì solver không bao phủ measure lạ.
    - period_start/period_end (nếu có) phải parse được thành ngày thật
      theo ISO; period_months (nếu có) phải là list các cặp
      [năm, tháng] với tháng 1..12.
    - Nếu entity_kind là product/customer mà entity_key không nằm trong
      list của shop thì loại (LLM bịa thực thể); entity_key rỗng thì
      giữ kind nhưng key là ``None`` để gate/solver xử lý như không
      match (không tự bịa key).
    - confidence_parse được kẹp về [0, 1]; thiếu thì mặc định 0,5 (không
      chắc) thay vì 1,0, vì JSON không kèm độ chắc thì không được coi
      là chắc chắn.

    Lưu ý: đây là bản kiểm chứng độc lập của runner; khi ``llm_parser``
    thật về, nó có ``validate_and_build`` riêng và runner sẽ dùng đường
    parse của module đó (xem :func:`parse_with_llm`), bản này chỉ còn
    là dự phòng cho trường hợp caller gọi trực tiếp.
    """
    if not isinstance(raw_output, dict):
        return None
    measure = raw_output.get("measure")
    if measure not in _ALLOWED_MEASURES:
        return None
    answer_type = raw_output.get("answer_type", "unknown")
    if answer_type not in _ALLOWED_ANSWER_TYPES:
        return None
    entity_kind = raw_output.get("entity_kind") or "none"
    entity_key = raw_output.get("entity_key")
    if entity_kind in ("product", "customer"):
        pool = products if entity_kind == "product" else customers
        keys = {item.get("key") for item in pool or []
                if isinstance(item, dict) and item.get("key")}
        if entity_key is not None and entity_key not in keys:
            return None
    elif entity_key is None:
        entity_key = None

    def _parse_date(value):
        """Chuyển chuỗi ISO thành date, sai thì ``None``."""
        if value is None:
            return None
        if isinstance(value, datetime.datetime):
            return value.date()
        if isinstance(value, datetime.date):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return datetime.date.fromisoformat(value.strip())
            except ValueError:
                return None
        return None

    period_start = _parse_date(raw_output.get("period_start"))
    period_end = _parse_date(raw_output.get("period_end"))
    if raw_output.get("period_start") is not None and period_start is None:
        return None
    if raw_output.get("period_end") is not None and period_end is None:
        return None
    period_months = []
    raw_months = raw_output.get("period_months") or []
    if not isinstance(raw_months, (list, tuple)):
        return None
    for item in raw_months:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return None
        try:
            year, month = int(item[0]), int(item[1])
        except (TypeError, ValueError):
            return None
        if not 1 <= month <= 12:
            return None
        period_months.append((year, month))
    confidence = raw_output.get("confidence_parse", 0.5)
    if not isinstance(confidence, (int, float)) \
            or isinstance(confidence, bool):
        confidence = 0.5
    confidence = max(0.0, min(1.0, float(confidence)))
    ranking_k = raw_output.get("ranking_k")
    if ranking_k is not None:
        try:
            ranking_k = int(ranking_k)
        except (TypeError, ValueError):
            return None
    threshold_money = raw_output.get("threshold_money")
    if threshold_money is not None:
        try:
            threshold_money = float(threshold_money)
        except (TypeError, ValueError):
            return None
    threshold_op = raw_output.get("threshold_op")
    if threshold_op is not None and threshold_op not in ("gte", "gt", "lte", "lt"):
        return None
    # Dựng ParsedQuestion bằng đúng class của phase2 (import từ parser),
    # không định nghĩa lại dataclass trùng tên để tránh xung đột kiểu.
    from parser import ParsedQuestion  # noqa: E402

    return ParsedQuestion(
        measure=str(measure),
        entity_kind=str(entity_kind),
        entity_key=entity_key,
        entity_display=raw_output.get("entity_display"),
        period_start=period_start,
        period_end=period_end,
        period_kind=str(raw_output.get("period_kind") or "none"),
        period_months=period_months,
        ranking_k=ranking_k,
        ranking_target=str(raw_output.get("ranking_target") or "none"),
        unit=raw_output.get("unit"),
        threshold_money=threshold_money,
        threshold_op=threshold_op,
        answer_type=str(answer_type),
        confidence_parse=confidence,
        parse_notes=["llm_validated"],
        normalized_text=str(question_text or ""),
    )


def parse_with_llm(question_text, as_of, calendar, rulebook,
                   products, customers, backend=None):
    """Parse một câu hỏi bằng LLM, lỗi thì fallback về parser template.

    Đầu vào: giống :func:`parse_question` của phase2, cộng ``backend``
    tùy chọn (backend mô hình cục bộ; khi module LLM thật về thì tham
    số này được module đó dùng theo cách của nó, runner chỉ truyền qua
    nếu hàm thật chấp nhận).

    Đầu ra:
        ``ParseResult`` với ``source='llm'`` (hoặc nguồn mà module thật
        trả) khi LLM parse thành công, và ``source='template_fallback'``
        khi: chưa có module LLM thật, module thật raise lỗi, module
        thật trả kết quả không dùng được, hoặc backend được truyền vào
        mà sinh lỗi. Không bao giờ raise chỉ vì LLM lỗi — một câu hỏi
        lỗi parse vẫn phải đi qua solver/gate và được ghi submission
        (thường là abstain), không được giết cả shop.

    Thứ tự và lý do:
    1. Nếu có module thật thì gọi nó trước (đúng interface đã thỏa
       thuận: trả ``ParseResult`` với parsed/source/confidence/notes),
       và truyền ``backend`` qua cho module đó (backend thật dùng
       ``generate_json``; runner không được tự gọi ``generate`` của
       backend thật rồi bỏ qua module, vì như vậy sẽ phá đường repair
       và validate của module đó).
    2. Nếu không có module thật, hoặc module thật lỗi, thì gọi parser
       template và đóng gói thành fallback — đồng thời ghi rõ lý do
       vào ``notes`` để báo cáo phân biệt được LLM thật với fallback.
    3. Nếu không có module thật mà caller truyền ``backend`` (ví dụ
       stub :class:`LocalModelBackend` raise khi sinh), cũng rơi về
       template theo cùng một đường, vì backend lỗi là lỗi của tầng
       parse, không phải lỗi của đợt chạy.
    """
    if _REAL_PARSE_WITH_LLM is not None:
        try:
            if backend is not None:
                raw = _REAL_PARSE_WITH_LLM(
                    question_text, as_of, calendar, rulebook,
                    products or [], customers or [], backend=backend)
            else:
                raw = _REAL_PARSE_WITH_LLM(
                    question_text, as_of, calendar, rulebook,
                    products or [], customers or [],
                )
            adapted = _adapt_parse_result(raw)
        except Exception as exc:  # noqa: BLE001 - LLM lỗi thì fallback
            return _parse_with_template_fallback(
                question_text, as_of, calendar, rulebook, products,
                customers,
                reason=f"llm_parser lỗi, fallback về template: {exc!r}",
            )
        if adapted is not None and adapted.parsed is not None:
            return adapted
        return _parse_with_template_fallback(
            question_text, as_of, calendar, rulebook, products,
            customers,
            reason="llm_parser trả kết quả không dùng được, "
            "fallback về template",
        )
    if backend is not None:
        # Chỉ còn stub của runner: thử sinh qua backend, lỗi thì fallback.
        try:
            backend.generate(build_prompt(question_text, products, customers))
        except Exception as exc:  # noqa: BLE001 - backend lỗi thì fallback
            return _parse_with_template_fallback(
                question_text, as_of, calendar, rulebook, products,
                customers,
                reason=f"backend lỗi, fallback về template: {exc!r}",
            )
    return _parse_with_template_fallback(
        question_text, as_of, calendar, rulebook, products,
        customers,
        reason="llm_parser chưa tồn tại (fallback tạm thời của runner), "
        "dùng parser template của phase2",
    )


# ---------------------------------------------------------------------------
# Tiện ích đọc dữ liệu và chuẩn hoá entry (giống run_phase2.py)
# ---------------------------------------------------------------------------


def parse_args(argv=None):
    """Phân tích tham số dòng lệnh theo API_CONTRACT mục 6.

    Đầu vào:
        argv: danh sách tham số (``None`` thì lấy từ ``sys.argv``).

    Đầu ra:
        Đối tượng argparse.Namespace với các trường ``train_dir``,
        ``cache_dir``, ``output``, ``shops`` (chuỗi gốc hoặc ``None``)
        và ``limit`` (int hoặc ``None``).

    Khác với ``run_phase2.py`` ở mặc định: ``--cache-dir`` mặc định là
    cache của phase2 (``../phase2/cache`` tính từ ``phase2_llm``), để
    không ingest lại vô ích khi cache phase2 còn hiệu lực; ``--output``
    mặc định là ``./output/submission_llm.jsonl`` trong ``phase2_llm``
    để không ghi đè submission của phase2. ``--limit 0`` được coi là
    không giới hạn, giống ``run_phase2.py``.
    """
    parser = argparse.ArgumentParser(
        description="Chạy end-to-end Giai đoạn 2 DataMate với parser LLM.")
    parser.add_argument(
        "--train-dir", default="../train",
        help="Thư mục split train (mặc định ../train, tính từ phase2_llm/).")
    parser.add_argument(
        "--cache-dir", default="../phase2/cache",
        help="Thư mục cache theo shop (mặc định ../phase2/cache, dùng lại "
        "cache của phase2 nếu có).")
    parser.add_argument(
        "--output", default="./output/submission_llm.jsonl",
        help="File submission ghi tăng dần (mặc định "
        "./output/submission_llm.jsonl).")
    parser.add_argument(
        "--shops", default=None,
        help="Chỉ chạy các shop này, cách nhau bằng dấu phẩy (mặc định: mọi shop).")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Chỉ chạy N câu hỏi đầu tiên theo thứ tự đã sắp (mặc định: mọi câu).")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        args.limit = None
    return args


def load_questions(questions_path):
    """Đọc questions.jsonl và sắp xếp theo business_id rồi id câu hỏi.

    Đầu vào:
        questions_path: đường dẫn tới file JSONL câu hỏi.

    Đầu ra:
        Danh sách dict câu hỏi đã sắp xếp. Dòng trống được bỏ qua;
        bản ghi thiếu ``id`` hoặc ``business_id`` sẽ gây ``ValueError``
        (không bỏ lặng lẽ, vì submission thiếu id thì không chấm được).

    Vì sao sắp xếp ở đây thay vì tin thứ tự file: thứ tự file có thể
    đổi giữa các lần xuất dữ liệu, trong khi pipeline cam kết
    deterministic theo (business_id, id) ở mọi tầng.
    """
    questions = []
    with open(questions_path, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict) or not record.get("id") \
                    or not record.get("business_id"):
                raise ValueError(
                    f"Dòng {line_number} trong {questions_path} thiếu "
                    "id hoặc business_id, không thể chạy pipeline.")
            questions.append(record)
    questions.sort(key=lambda q: (str(q["business_id"]), str(q["id"])))
    return questions


def parse_as_of(question, view):
    """Giải ngày ``as_of`` của một câu hỏi, có dự phòng xác định.

    Đầu vào:
        question: dict câu hỏi, kỳ vọng có ``as_of_date`` dạng
            ``YYYY-MM-DD``.
        view: ShopView của shop (dùng ``date_max`` làm dự phòng).

    Đầu ra:
        Tuple ``(as_of, da_dung_du_phong)``. Ngày hợp lệ thì dùng đúng
        ngày đó; ngày thiếu/sai định dạng thì dùng ``date_max`` của
        shop, nếu shop cũng không có ngày nào thì dùng 2026-01-01, và
        cờ dự phòng là ``True`` để caller ghi chú vào log phân tích.
    """
    raw = question.get("as_of_date")
    if isinstance(raw, str) and raw.strip():
        try:
            return datetime.date.fromisoformat(raw.strip()), False
        except ValueError:
            pass
    date_max = getattr(view, "date_max", None)
    if isinstance(date_max, datetime.datetime):
        return date_max.date(), True
    if isinstance(date_max, datetime.date):
        return date_max, True
    return datetime.date(2026, 1, 1), True


def sanitize_entry(entry):
    """Chuyển entry đáp án về kiểu JSON thuần, không đổi giá trị.

    Đầu vào:
        entry: dict đáp án của solver/gate (có thể chứa số numpy do
            pandas tính ra, hoặc tuple trong table rows).

    Đầu ra:
        Dict mới với cùng khóa/giá trị, trong đó số numpy thành
        int/float Python, tuple thành list, bool giữ nguyên bool (phải
        kiểm tra bool TRƯỚC int vì bool là lớp con của int trong
        Python). Giá trị không chuyển được thì giữ nguyên để tầng ghi
        JSON báo lỗi rõ ràng thay vì lặng lẽ đổi đáp án.
    """
    def _clean(value):
        """Chuyển đệ quy một giá trị về kiểu JSON thuần."""
        if isinstance(value, bool) or value is None \
                or isinstance(value, (str, int, float)):
            return value
        if isinstance(value, dict):
            return {key: _clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_clean(item) for item in value]
        try:
            import numbers

            if isinstance(value, numbers.Integral):
                return int(value)
            if isinstance(value, numbers.Real):
                return float(value)
        except (TypeError, ValueError, OverflowError):
            pass
        return value

    if not isinstance(entry, dict):
        return {"id": None, "type": "abstain"}
    return {key: _clean(item) for key, item in entry.items()}


# ---------------------------------------------------------------------------
# Giải một câu hỏi: parse LLM (fallback template) -> solver -> gate
# ---------------------------------------------------------------------------


def solve_one_question(question, view, rulebook, calendar, parse_fn=None):
    """Giải một câu hỏi thành (entry, thông tin phân tích lỗi).

    Đầu vào:
        question: dict câu hỏi (có ``id``, ``question``, ``as_of_date``).
        view: ShopView của shop chứa câu hỏi.
        rulebook: rulebook của split đang chạy.
        calendar: calendar của split đang chạy.
        parse_fn: hàm parse tùy chọn, chữ ký như :func:`parse_with_llm`
            (``None`` thì dùng :func:`parse_with_llm` mặc định). Tham số
            này phục vụ kiểm thử (tiêm backend lỗi để kiểm tra fallback)
            và để coordinator tiêm parser LLM thật mà không sửa runner.

    Đầu ra:
        Tuple ``(entry, info)`` trong đó entry là dict submission đã
        điền id thật, còn info là dict gồm ``measure``, ``status``,
        ``error_group``, ``confidence``, ``as_of_fallback``,
        ``parse_source`` (``'llm'`` hay ``'template_fallback'``) và
        ``parse_confidence`` (độ chắc parse sau hiệu chỉnh).

    Thứ tự và lý do từng bước (không được đảo, giống run_phase2.py):

    1. Parser: LLM parse trước, lỗi thì fallback template; nguồn parse
       được ghi lại. Confidence parse được HIỆU CHỈNH trước khi đưa vào
       solver/gate: lấy ``min(confidence_parse của ParsedQuestion,
       confidence của ParseResult)`` — với fallback thì hai giá trị này
       bằng nhau nên không tăng vượt quá confidence của template; với
       LLM thì trần này chặn trường hợp ParsedQuestion tự tin 1,0
       nhưng ParseResult chỉ chắc 0,7.
    2. Solver: tính đáp án bằng code, entry lúc này có ``id=None``.
    3. Gate: ``compute_confidence`` tính confidence theo bằng chứng và
       dùng làm TRẦN cho confidence của solver (lấy min), vì ``decide``
       chỉ dùng confidence của solver khi solver đã gán. Sau đó
       ``decide`` kiểm tra unanswerable trước, áp ngưỡng 0,65, kiểm
       tra format sau. Logic gate KHÔNG bị sửa ở đây, chỉ truyền
       confidence đã hiệu chỉnh qua ``parsed``.
    4. Điền id thật và sanitize: bước cuối cùng, sau mọi quyết định
       của gate.

    Trường hợp biên: mọi ngoại lệ trong một câu hỏi đều được bắt thành
    abstain với ``error_group='filter'`` và ghi rõ lỗi, để một câu lỗi
    không giết cả shop. Riêng ngoại lệ ở tầng parse thì được bắt sớm
    hơn (rơi về template) trước khi tới lớp bắt tổng này.
    """
    question_id = str(question["id"])
    parse_callable = parse_fn if parse_fn is not None else parse_with_llm
    try:
        as_of, as_of_fallback = parse_as_of(question, view)
        try:
            raw_result = parse_callable(
                question.get("question", ""), as_of, calendar, rulebook,
                getattr(view, "products", []) or [],
                getattr(view, "customers", []) or [],
            )
            adapted = _adapt_parse_result(raw_result)
        except Exception as exc:  # noqa: BLE001 - parse lỗi thì fallback
            adapted = _parse_with_template_fallback(
                question.get("question", ""), as_of, calendar, rulebook,
                getattr(view, "products", []) or [],
                getattr(view, "customers", []) or [],
                reason=f"hàm parse lỗi, fallback về template: {exc!r}",
            )
        if adapted is None or adapted.parsed is None:
            adapted = _parse_with_template_fallback(
                question.get("question", ""), as_of, calendar, rulebook,
                getattr(view, "products", []) or [],
                getattr(view, "customers", []) or [],
                reason="hàm parse trả kết quả không dùng được, "
                "fallback về template",
            )
        parsed = adapted.parsed
        parse_source = adapted.source or "template_fallback"
        # Hiệu chỉnh confidence parse (xem docstring bước 1): lấy min
        # của confidence_parse gốc và confidence của ParseResult, rồi
        # ghi lại vào một bản sao của ParsedQuestion để gate nhìn thấy
        # đúng trần này. Không sửa ParsedQuestion gốc của module khác.
        original_confidence = getattr(parsed, "confidence_parse", 0.0) or 0.0
        if isinstance(adapted.confidence, (int, float)) \
                and not isinstance(adapted.confidence, bool):
            calibrated = min(float(original_confidence),
                             float(adapted.confidence))
        else:
            calibrated = float(original_confidence)
        if parse_source == "template_fallback":
            # Ràng buộc bổ sung theo nhiệm vụ: fallback không được tăng
            # confidence vượt quá confidence của template. Vì
            # _parse_with_template_fallback đã đặt confidence bằng đúng
            # confidence_parse của template nên min ở trên đã bảo đảm
            # điều này; dòng gán dưới đây chỉ làm ràng buộc tường minh
            # và chống trường hợp ParseResult lạ mang confidence cao hơn.
            calibrated = min(calibrated, float(original_confidence))
        try:
            parsed = dataclasses.replace(
                parsed, confidence_parse=float(calibrated))
        except Exception:
            # ParsedQuestion không phải dataclass thay được (ví dụ
            # object giả trong kiểm thử): giữ parsed gốc, gate vẫn
            # dùng confidence_parse gốc, không crash vì hiệu chỉnh.
            pass
        result = solve(parsed, view, rulebook, calendar)
        # Gate làm trần confidence (xem docstring bước 3).
        if getattr(result, "status", None) == "answered":
            try:
                gate_confidence = compute_confidence(
                    parsed, view, result.evidence)
                solver_confidence = getattr(result, "confidence", None)
                if isinstance(solver_confidence, (int, float)) \
                        and not isinstance(solver_confidence, bool):
                    result.confidence = min(
                        float(solver_confidence), float(gate_confidence))
                else:
                    result.confidence = float(gate_confidence)
            except Exception:
                # Tính trần confidence lỗi thì giữ confidence của
                # solver, decide vẫn áp ngưỡng và format như thường.
                pass
        result = decide(parsed, view, result, rulebook)
        entry = dict(getattr(result, "entry", None) or {})
        entry["id"] = question_id
        if getattr(result, "status", None) == "abstain":
            entry = {"id": question_id, "type": "abstain"}
        elif getattr(result, "status", None) == "unanswerable":
            entry = {"id": question_id, "type": "unanswerable"}
            raw_entry = getattr(result, "entry", None) or {}
            if isinstance(raw_entry, dict) and raw_entry.get("reason"):
                entry["reason"] = raw_entry["reason"]
        entry = sanitize_entry(entry)
        error_group = getattr(result, "error_group", None)
        status = getattr(result, "status", None) or "abstain"
        if status == "abstain" and error_group is None:
            error_group = "filter"
        if status == "unanswerable":
            error_group = "unanswerable"
        info = {
            "measure": getattr(parsed, "measure", "unknown"),
            "status": status,
            "error_group": error_group,
            "confidence": getattr(result, "confidence", None),
            "as_of_fallback": as_of_fallback,
            "parse_source": parse_source,
            "parse_confidence": float(calibrated),
        }
        return entry, info
    except Exception as exc:  # noqa: BLE001 - bắt tổng theo từng câu
        return (
            {"id": question_id, "type": "abstain"},
            {
                "measure": "unknown",
                "status": "abstain",
                "error_group": "filter",
                "confidence": 0.0,
                "as_of_fallback": False,
                "parse_source": "template_fallback",
                "parse_confidence": 0.0,
                "runner_error": repr(exc),
            },
        )


# ---------------------------------------------------------------------------
# Chạy toàn bộ pipeline và ghi báo cáo
# ---------------------------------------------------------------------------


def run_phase2_llm(train_dir, cache_dir, output_path, shops=None,
                   limit=None, reports_dir=None, parse_fn=None,
                   on_question=None, on_shop=None):
    """Chạy pipeline Giai đoạn 2 với parser LLM và trả về dict tóm tắt.

    Đầu vào:
        train_dir: thư mục split (chứa questions.jsonl, answers.jsonl,
            rulebook.md, calendar.csv, data/).
        cache_dir: thư mục cache theo shop (mặc định nên là cache của
            phase2 để không ingest lại vô ích).
        output_path: file submission ghi tăng dần.
        shops: list business_id cần chạy, hoặc ``None`` để chạy mọi shop
            có trong questions.
        limit: số câu hỏi tối đa (theo thứ tự đã sắp), hoặc ``None``.
        reports_dir: thư mục ghi ``PHASE2_LLM_SUMMARY.json`` và
            ``error_analysis_llm.json`` (mặc định là
            ``phase2_llm/reports``; kiểm thử truyền thư mục tạm để
            không ghi đè báo cáo của đợt chạy full).
        parse_fn: hàm parse tùy chọn truyền xuống
            :func:`solve_one_question` (``None`` thì dùng mặc định).
        on_question: callback log tùy chọn, gọi sau mỗi câu hỏi đã giải
            xong với ``(question, entry, info, elapsed_seconds)`` trong
            đó ``question`` là dict câu hỏi gốc, ``entry`` là dict
            submission, ``info`` là dict thông tin phân tích của
            :func:`solve_one_question`. ``None`` thì không log gì thêm
            (hành vi cũ, để test cũ không bị ảnh hưởng bởi log).
        on_shop: callback log tùy chọn, gọi sau mỗi shop với
            ``(business_id, n_questions, cache_hit, seconds, errors)``
            trong đó ``errors`` là list lỗi của shop đó (rỗng nếu shop
            chạy tốt). ``None`` thì không log gì thêm.

    Lưu ý cho callback log: callback KHÔNG BAO GIỜ được làm chết
    pipeline — mọi ngoại lệ trong callback đều được bắt và bỏ qua ở
    nơi gọi, vì log chỉ là quan sát, không phải một bước tính toán.

    Đầu ra:
        Dict summary đã ghi ra ``reports/PHASE2_LLM_SUMMARY.json`` (cùng
        với ``reports/error_analysis_llm.json``), gồm backend thực tế
        đã dùng, PA tổng, per-type, per-business, holdout,
        số correct/wrong/abstain, tỉ lệ bao phủ, số câu theo từng nguồn
        parse, thời gian từng shop, cache hit/miss và shop lỗi.

    Hành vi khi shop lỗi: giống ``run_phase2.py`` — load/build view lỗi
    thì shop đó được ghi vào ``shop_errors``, mọi câu của shop đó được
    ghi thành abstain (để submission vẫn đủ câu), và đợt chạy TIẾP TỤC
    sang shop khác. Chỉ lỗi ở tầng chạy chung (không đọc được
    questions, rulebook, calendar) mới lan ra ngoài và làm main trả
    exit khác 0.
    """
    train_dir = Path(train_dir)
    cache_dir = Path(cache_dir)
    output_path = Path(output_path)
    reports_dir = Path(reports_dir) if reports_dir is not None \
        else _PHASE2_LLM_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    questions = load_questions(train_dir / "questions.jsonl")
    if shops:
        wanted = {str(shop).strip() for shop in shops if str(shop).strip()}
        questions = [q for q in questions if str(q["business_id"]) in wanted]
    if limit is not None:
        questions = questions[:limit]
    questions_by_id = {str(q["id"]): q for q in questions}

    # Nạp rulebook/calendar của split đang chạy (không hard-code luật).
    calendar = load_calendar(train_dir / "calendar.csv")
    rulebook = load_rulebook(train_dir / "rulebook.md", train_dir / "calendar.csv")

    by_shop = defaultdict(list)
    for question in questions:
        by_shop[str(question["business_id"])].append(question)

    answers = None
    answers_path = train_dir / "answers.jsonl"
    if answers_path.is_file() and load_answers is not None:
        answers = load_answers(answers_path)

    shop_timings = {}
    shop_errors = {}
    cache_stats = {"hit": 0, "miss": 0}
    per_question = []  # (qid, business_id, entry, info)
    written = 0
    run_started = time.perf_counter()

    # Ghi tăng dần: mở file ở chế độ ghi mới (đợt chạy mới) rồi append
    # từng câu ngay sau khi giải xong, flush sau mỗi câu.
    with open(output_path, "w", encoding="utf-8") as out_handle:
        for business_id in sorted(by_shop):
            shop_started = time.perf_counter()
            shop_questions = by_shop[business_id]
            view = None
            cache_hit = False
            try:
                view = load_shop_cache(cache_dir, business_id, train_dir)
                if view is not None:
                    cache_hit = True
                    cache_stats["hit"] += 1
                else:
                    cache_stats["miss"] += 1
                    ingest_result = load_shop(
                        business_id, train_dir / "data")
                    view = build_shop_view(ingest_result, business_id)
                    try:
                        # BẮT BUỘC truyền data_dir=train_dir để cache
                        # tính được hash nguồn; không truyền thì cache
                        # lưu với source_hash=None và lần sau luôn miss.
                        save_shop_cache(
                            cache_dir, business_id, view,
                            data_dir=train_dir)
                    except Exception as exc:  # noqa: BLE001
                        # Lỗi ghi cache không giết đợt chạy: lần sau
                        # ingest lại, đáp án vẫn đúng.
                        shop_errors.setdefault(business_id, []).append(
                            f"cache_save_failed: {exc!r}")
                for question in shop_questions:
                    # Đo thời gian từng câu để callback log dùng được;
                    # log không được làm chết pipeline nên callback được
                    # bọc try/except ở dưới (xem ghi chú ở docstring).
                    question_started = time.perf_counter()
                    entry, info = solve_one_question(
                        question, view, rulebook, calendar,
                        parse_fn=parse_fn)
                    question_seconds = time.perf_counter() - question_started
                    out_handle.write(
                        json.dumps(entry, ensure_ascii=False) + "\n")
                    out_handle.flush()
                    per_question.append(
                        (str(question["id"]), business_id, entry, info))
                    written += 1
                    if on_question is not None:
                        try:
                            on_question(
                                question, entry, info, question_seconds)
                        except Exception:  # noqa: BLE001 - log lỗi thì bỏ qua
                            pass
            except Exception as exc:  # noqa: BLE001 - bắt tổng theo shop
                shop_errors.setdefault(business_id, []).append(repr(exc))
                # Shop lỗi vẫn ghi abstain cho mọi câu của shop đó để
                # submission đủ câu, rồi tiếp tục shop khác.
                for question in shop_questions:
                    entry = {"id": str(question["id"]), "type": "abstain"}
                    out_handle.write(
                        json.dumps(entry, ensure_ascii=False) + "\n")
                    out_handle.flush()
                    shop_error_info = {
                        "measure": "unknown",
                        "status": "abstain",
                        "error_group": "filter",
                        "confidence": 0.0,
                        "as_of_fallback": False,
                        "parse_source": "template_fallback",
                        "parse_confidence": 0.0,
                        "runner_error": repr(exc),
                    }
                    per_question.append((
                        str(question["id"]), business_id, entry,
                        shop_error_info))
                    written += 1
                    if on_question is not None:
                        try:
                            on_question(question, entry, shop_error_info, 0.0)
                        except Exception:  # noqa: BLE001 - log lỗi thì bỏ qua
                            pass
            finally:
                # Giải phóng view trước khi sang shop khác (contract).
                view = None
                gc.collect()
                shop_timings[business_id] = {
                    "seconds": round(time.perf_counter() - shop_started, 3),
                    "questions": len(shop_questions),
                    "cache_hit": cache_hit,
                }
                if on_shop is not None:
                    try:
                        on_shop(
                            business_id, len(shop_questions), cache_hit,
                            shop_timings[business_id]["seconds"],
                            list(shop_errors.get(business_id, [])))
                    except Exception:  # noqa: BLE001 - log lỗi thì bỏ qua
                        pass

    # Chấm bằng phase1 evaluate (chỉ khi có đáp án tham chiếu).
    summary = {
        "backend": BACKEND_KIND,
        "backend_note": (
            "Module llm_parser đã được nạp; nguồn parse thực tế xem "
            "parse_sources (nếu không có backend model được cấu hình, "
            "llm_parser tự fallback về template theo thiết kế của nó)."
            if BACKEND_KIND == "llm_parser"
            else "FALLBACK TẠM THỜI CỦA RUNNER: llm_parser chưa tồn tại "
            "khi chạy, runner dùng parser template của phase2; không "
            "phải LLM thật, không gọi API thương mại."
        ),
        "total_questions": len(questions),
        "written": written,
        "cache": cache_stats,
        "shop_timings": shop_timings,
        "shop_errors": shop_errors,
        "run_seconds": round(time.perf_counter() - run_started, 3),
        "status_counts": dict(Counter(info["status"] for _qid, _biz, _e, info in per_question)),
        "parse_sources": dict(Counter(
            info.get("parse_source", "template_fallback")
            for _qid, _biz, _e, info in per_question)),
        "coverage": {},
        "score": None,
        "holdout": None,
        "non_holdout": None,
    }
    status_counts = Counter(info["status"] for _qid, _biz, _e, info in per_question)
    total = len(per_question) if per_question else 1
    summary["coverage"] = {
        "answered": status_counts.get("answered", 0),
        "abstain": status_counts.get("abstain", 0),
        "unanswerable_predicted": status_counts.get("unanswerable", 0),
        "answered_rate": status_counts.get("answered", 0) / total,
        "abstain_rate": status_counts.get("abstain", 0) / total,
        "unanswerable_rate": status_counts.get("unanswerable", 0) / total,
    }

    verdict_by_id = {}
    if answers is not None and score_submission is not None \
            and load_submission is not None and answers_match is not None:
        submission = load_submission(output_path)
        report = score_submission(submission, answers, questions_by_id)
        summary["score"] = {
            "total": report.total,
            "correct": report.correct,
            "wrong": report.wrong,
            "abstain": report.abstain,
            "pa": report.pa,
            "per_type": report.per_type,
            "per_business": report.per_business,
            "holdout_pa": report.holdout_pa,
        }
        # PA riêng holdout và các shop còn lại (tính từ per_business,
        # dùng đúng tập HOLDOUT của phase1 evaluate, không hard-code
        # lại danh sách shop ở đây ngoài việc đọc per_business).
        try:
            from evaluate import HOLDOUT_BUSINESSES  # noqa: E402
        except ImportError:
            HOLDOUT_BUSINESSES = frozenset()
        holdout_tot = holdout_cor = holdout_wro = 0
        other_tot = other_cor = other_wro = 0
        for biz, group in report.per_business.items():
            if biz in HOLDOUT_BUSINESSES:
                holdout_tot += group["total"]
                holdout_cor += group["correct"]
                holdout_wro += group["wrong"]
            else:
                other_tot += group["total"]
                other_cor += group["correct"]
                other_wro += group["wrong"]
        summary["holdout"] = {
            "total": holdout_tot, "correct": holdout_cor,
            "wrong": holdout_wro,
            "pa": (holdout_cor - holdout_wro) / holdout_tot if holdout_tot else None,
        }
        summary["non_holdout"] = {
            "total": other_tot, "correct": other_cor,
            "wrong": other_wro,
            "pa": (other_cor - other_wro) / other_tot if other_tot else None,
        }
        for qid, reference in answers.items():
            if str(qid) in questions_by_id:
                verdict_by_id[str(qid)] = answers_match(
                    reference, submission.get(str(qid)))

    # Phân tích lỗi: nhóm abstain/unanswerable theo error_group, và
    # nhóm câu sai theo measure đã parse (để biết loại câu nào yếu).
    # Nhóm parser ở đây là số câu abstain/unanswerable do parser (theo
    # error_group của solver/gate), so được trực tiếp với baseline 791.
    error_groups = {}
    for qid, biz, _entry, info in per_question:
        if info["status"] == "answered":
            continue
        group = info["error_group"] or "filter"
        bucket = error_groups.setdefault(group, {"count": 0, "examples": []})
        bucket["count"] += 1
        if len(bucket["examples"]) < 3:
            bucket["examples"].append(
                {"id": qid, "business_id": biz,
                 "measure": info["measure"],
                 "parse_source": info.get("parse_source")})
    wrong_by_measure = {}
    for qid, biz, _entry, info in per_question:
        if verdict_by_id.get(qid) != "wrong":
            continue
        bucket = wrong_by_measure.setdefault(
            info["measure"], {"count": 0, "examples": []})
        bucket["count"] += 1
        if len(bucket["examples"]) < 3:
            bucket["examples"].append({"id": qid, "business_id": biz})
    wrong_by_business = {}
    for qid, biz, _entry, _info in per_question:
        if verdict_by_id.get(qid) != "wrong":
            continue
        bucket = wrong_by_business.setdefault(
            biz, {"count": 0, "examples": []})
        bucket["count"] += 1
        if len(bucket["examples"]) < 3:
            bucket["examples"].append({"id": qid})
    parser_remaining = error_groups.get("parser", {}).get("count", 0)
    error_analysis = {
        "backend": BACKEND_KIND,
        "error_groups": error_groups,
        "parser_remaining": parser_remaining,
        "parser_baseline": BASELINE_PARSER_ERROR_COUNT,
        "parser_change_vs_baseline": parser_remaining - BASELINE_PARSER_ERROR_COUNT,
        "parse_sources": summary["parse_sources"],
        "wrong_by_measure": wrong_by_measure,
        "wrong_by_business": wrong_by_business,
        "verdict_counts": dict(Counter(verdict_by_id.values())),
        "shop_errors": shop_errors,
        "shop_timings": shop_timings,
    }

    with open(reports_dir / "PHASE2_LLM_SUMMARY.json", "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    with open(reports_dir / "error_analysis_llm.json", "w", encoding="utf-8") as handle:
        json.dump(error_analysis, handle, ensure_ascii=False, indent=2)
    return summary


def main(argv=None):
    """Điểm vào dòng lệnh: parse args, chạy pipeline, in tóm tắt.

    Trả exit code 0 khi chạy hết (xem docstring module về quy ước
    thoát); khác 0 khi lỗi tầng chạy chung không bắt được.
    """
    args = parse_args(argv)
    # Đường dẫn mặc định trong contract là tương đối từ thư mục chứa
    # script (nơi đặt script), nên giải tương đối theo vị trí script,
    # không theo thư mục làm việc hiện tại, để chạy từ đâu cũng giống nhau.
    train_dir = Path(args.train_dir)
    if not train_dir.is_absolute():
        train_dir = (_PHASE2_LLM_DIR / train_dir).resolve()
    cache_dir = Path(args.cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = (_PHASE2_LLM_DIR / cache_dir).resolve()
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = (_PHASE2_LLM_DIR / output_path).resolve()
    shops = None
    if args.shops:
        shops = [part.strip() for part in args.shops.split(",") if part.strip()]
    summary = run_phase2_llm(
        train_dir, cache_dir, output_path, shops=shops, limit=args.limit)
    score = summary.get("score") or {}
    print(
        f"Done: {summary['written']}/{summary['total_questions']} câu, "
        f"backend={summary['backend']}, "
        f"parse_sources={summary['parse_sources']}, "
        f"PA={score.get('pa')}, correct={score.get('correct')}, "
        f"wrong={score.get('wrong')}, abstain={score.get('abstain')}, "
        f"holdout_pa={score.get('holdout_pa')}, "
        f"cache={summary['cache']}, "
        f"thời_gian={summary['run_seconds']}s")
    if summary["shop_errors"]:
        print(f"Shop lỗi (đã bắt, không giết đợt chạy): {summary['shop_errors']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

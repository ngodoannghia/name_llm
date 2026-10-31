# -*- coding: utf-8 -*-
"""Solver tính toán đáp án từ ShopView cho Giai đoạn 2 DataMate.

Mục đích của module này
------------------------
Parser (``parser.py``) chỉ hiểu câu hỏi thành ``ParsedQuestion`` với 4
trục (đo cái gì, của ai, kỳ nào, dạng đáp án nào); nó không tính số.
Module này là nơi duy nhất biến ``ParsedQuestion`` + ``ShopView`` thành
con số, bằng code tính toán trên dữ liệu đã chuẩn hoá, không bao giờ để
mô hình tự nhẩm số. Nhờ vậy mọi đáp án đều truy vết được qua
``SolveResult.evidence`` (measure nào, kỳ nào, bao nhiêu dòng đã dùng,
bộ lọc nào), và cùng một câu hỏi luôn cho cùng một đáp án (không dùng
ngẫu nhiên, thứ tự xếp hạng có tie-break xác định).

Nguyên tắc bao phủ (bắt buộc, theo nhiệm vụ Solver):
    - CHỈ trả lời các measure đã bao phủ (xem ``COVERED_MEASURES``).
      Measure ngoài danh sách thì abstain với
      ``error_group='uncovered'``, không đoán định nghĩa của nó.
    - Measure ``unknown`` (parser không hiểu được) thì abstain với
      ``error_group='parser'``, vì đó là lỗi hiểu câu hỏi, không phải
      lỗi chưa bao phủ.
    - Measure đã bao phủ nhưng dữ liệu thiếu (thiếu cột, thiếu thực
      thể, thiếu hệ số quy đổi, kỳ không có dòng nào...) thì abstain
      với error_group tương ứng (``column``/``entity``/``unit``/
      ``filter``/``time``), kèm ghi chú trong evidence, để Giai đoạn 3
      phân tích lỗi đúng nhóm thay vì tưởng là chưa bao phủ.

Bộ lọc RIÊNG theo measure (theo ``API_CONTRACT`` mục 3, không dùng một
bộ lọc chung cho mọi loại câu):
    - revenue/order_count/quantity/top_*/revenue_by_*/quantity_by_unit/
      best_*/growth/continuous_growth: chỉ đơn revenue (xem
      ``is_revenue_order``), ngày đặt trong kỳ, bỏ đơn huỷ/trả kể cả
      trả một phần (trạng thái đơn là trạng thái của cả đơn).
    - cancel_rate: mẫu số là MỌI đơn đặt trong kỳ (mọi trạng thái), tử
      số là đơn huỷ.
    - debt_*: đơn không huỷ (kể cả đơn trả nếu còn nợ), loại COD/sàn
      thương mại điện tử, nợ tại ngày X = tổng đơn trừ tiền trả đến hết
      ngày X, chỉ tính đơn còn nợ > 0.
    - price_on_day: tính CẢ dòng của đơn huỷ/trả, ngày đặt đúng bằng
      ngày D, đơn giá cho một đơn vị cơ bản trước chiết khấu dòng.

Quy ước trình bày đáp án:
    - Tiền trả về là số nguyên VND (làm tròn về đồng, kiểu nửa lên).
    - Percent làm tròn 2 chữ số thập phân.
    - Xếp hạng: tiêu chí giảm dần, hoà thì theo key (mã/tên) tăng dần;
      riêng best_day/best_month hoà thì lấy ngày/tháng sớm nhất và ghi
      rõ trong evidence (ngày/tháng không có mã nên không thể hoà theo
      mã; test set đã loại câu hoà ở biên nên ảnh hưởng nhỏ, và điểm
      này cần Giai đoạn 3 rà lại nếu rulebook mới nói khác).
    - Tiền tính bằng Decimal từ chuỗi của từng khoản để tránh sai số
      float tích luỹ (ví dụ tổng nhiều khoản 2 chữ số thập phân).

Tổng quát hoá:
    Không hard-code business_id, id câu hỏi, tên cột train, schema theo
    ngành train, rulebook train hay đáp án train. Mọi quyết định đều
    dựa vào cột chuẩn của ShopView, rulebook của split đang chạy (đọc
    qua ``_rulebook_flag``) và ParsedQuestion. Shop mới, ngành mới vẫn
    đi qua đúng các hàm này.

Lưu ý về ``ParsedQuestion`` và ``ShopView`` trong import:
    ``ParsedQuestion`` bắt buộc import từ ``parser`` (module của Agent
    Parser, cùng thư mục src) theo contract; khi chạy kiểm thử mà
    ``parser.py`` chưa tồn tại thì test dùng stub tạm trong ``/tmp``
    (không ghi stub vào src). ``ShopView`` chỉ dùng cho gợi ý kiểu nên
    import trong try/except: solver làm việc theo duck-typing trên các
    thuộc tính ``lines``/``orders``/``payments``/``products``/
    ``customers`` của view, nên view thiếu module vẫn tính được.
"""

import sys as _sys
from collections import Counter as _Counter
from dataclasses import dataclass
from datetime import date as _date, timedelta as _timedelta
from decimal import ROUND_HALF_UP as _HALF_UP, Decimal as _Decimal
from pathlib import Path as _Path

# --- Nạp phase1 (chỉ cần text_utils) bằng đường dẫn tương đối, giống
# shop_view.py: phase2/src/solver.py -> datamate/ là parents[2].
_PHASE1_SRC = _Path(__file__).resolve().parents[2] / "phase1" / "src"
if str(_PHASE1_SRC) not in _sys.path:
    _sys.path.insert(0, str(_PHASE1_SRC))

from text_utils import normalize_key  # noqa: E402

try:
    from shop_view import ShopView  # noqa: E402
except ImportError:  # pragma: no cover - chỉ dùng cho gợi ý kiểu
    ShopView = object  # type: ignore[assignment]

from parser import ParsedQuestion  # noqa: E402

__all__ = [
    "SolveResult", "COVERED_MEASURES", "UNCOVERED_MEASURES",
    "is_revenue_order", "solve",
]


# ---------------------------------------------------------------------------
# Dataclass kết quả và tập measure
# ---------------------------------------------------------------------------


@dataclass
class SolveResult:
    """Kết quả giải một câu hỏi, theo đúng ``API_CONTRACT`` mục 3.

    Các trường:
        status: ``answered`` khi có đáp án, ``abstain`` khi chủ động
            không trả lời (kể cả chưa bao phủ), ``unanswerable`` dành
            cho gate quyết định sau (solver không tự kết luận
            unanswerable, vì bằng chứng mạnh về thiếu dữ liệu thuộc về
            gate kiểm tra trước ngưỡng).
        entry: dict đáp án ``{'id': None, 'type': ..., 'answer': ...}``
            khi answered; ``{'id': None, 'type': 'abstain'}`` khi
            abstain. ``id`` để None vì ``ParsedQuestion`` không mang
            id câu hỏi: tầng chạy (run_phase2) sẽ điền id thật khi ghi
            submission, solver không được bịa id.
        confidence: độ chắc của solver cho đáp án này (0..1). Đáp án
            đúng chuẩn là 1.0, trừ 0.1 khi phép tính phải bao gồm đơn
            trạng thái ``other`` (không có cột trạng thái nên không có
            căn cứ loại đơn) hoặc khi công nợ phải dùng ảnh chụp tiền
            trả cuối kỳ thay cho tiền trả theo ngày (xem evidence
            ``confidence_notes``). Abstain thì 0.0. Gate sẽ áp ngưỡng
            0.65 và điều chỉnh tiếp, nên số này là đầu vào của gate,
            không phải quyết định cuối.
        error_group: None khi answered; khi abstain thì một trong
            ``parser``/``entity``/``column``/``filter``/``time``/
            ``unit``/``format``/``uncovered``/``unanswerable``.
        evidence: dict truy vết, luôn có ``measure``, ``period`` (cặp
            chuỗi ngày hoặc None), ``rows_used`` (số dòng/bản ghi đã
            dùng), ``filter`` (mô tả bộ lọc bằng chữ), ``group_key``
            (key nhóm khi câu hỏi xếp hạng/nhóm, hoặc None), cộng các
            ghi chú riêng của từng measure (``warnings``,
            ``confidence_notes``, ``other_status_included``...).
    """

    status: str
    entry: dict | None
    confidence: float
    error_group: str | None
    evidence: dict


#: Các measure solver này cam kết trả lời khi dữ liệu đủ (xem nhiệm vụ).
#: ``product_revenue`` là doanh thu của một sản phẩm cụ thể, tính bằng
#: đúng máy móc của ``revenue`` có lọc thực thể, nên bao phủ cùng nhóm.
COVERED_MEASURES = frozenset({
    "revenue", "order_count", "quantity", "cancel_rate",
    "revenue_share", "growth_percent", "growth_amount",
    "top_product", "top_customer", "best_day", "best_month",
    "debt_total", "debt_top", "price_on_day", "continuous_growth",
    "revenue_by_payment", "revenue_by_group", "revenue_by_channel",
    "quantity_by_unit", "product_revenue",
})

#: Các measure đã biết tên nhưng solver này cố tình chưa bao phủ, để
#: báo cáo phân biệt được "biết mà chưa làm" với "không hiểu câu hỏi".
#: ``top_employee`` cần định nghĩa xếp hạng nhân viên (theo doanh thu
#: hay số đơn) chưa có trong contract; ``import_total`` là nhập hàng
#: chi tiết; ``avg_price`` mơ hồ giữa giá trung bình theo dòng, theo
#: đơn và theo đơn vị cơ bản nên không đoán định nghĩa.
UNCOVERED_MEASURES = frozenset({
    "top_employee", "import_total", "avg_price",
})


# ---------------------------------------------------------------------------
# Tiện ích đọc rulebook, tiền, phần trăm, ngày
# ---------------------------------------------------------------------------


def _rulebook_flag(rulebook, name):
    """Đọc một cờ luật từ rulebook, bất kể rulebook là object hay dict.

    Rulebook của Giai đoạn 1 là object có thuộc tính (ví dụ
    ``revenue_excludes_cancelled``), nhưng code gọi solver có thể đưa
    dict. Hàm này thử cả hai dạng, trả None khi rulebook là None hoặc
    không có cờ đó. None nghĩa là "rulebook không nói gì về luật này",
    và solver sẽ dùng mặc định của contract (loại huỷ/trả khỏi doanh
    thu), chứ không bịa luật mới.
    """
    if rulebook is None:
        return None
    if isinstance(rulebook, dict):
        return rulebook.get(name)
    return getattr(rulebook, name, None)


def is_revenue_order(status_norm: str, rulebook) -> bool:
    """Kiểm tra một đơn có tính vào doanh thu không, theo trạng thái.

    Đầu vào:
        status_norm: nhãn trạng thái chuẩn của ShopView (``completed``/
            ``cancelled``/``returned``/``delivering``/``other``).
        rulebook: rulebook của split đang chạy (object hoặc dict),
            hoặc None khi không có rulebook.

    Đầu ra:
        True khi đơn tính vào doanh thu, False khi không.

    Luật, theo ``API_CONTRACT`` mục 3:
        - ``completed`` và ``delivering`` luôn là doanh thu (đơn đã bán
          xong hoặc đang giao thì tiền bán đã phát sinh).
        - ``cancelled`` và ``returned`` mặc định KHÔNG là doanh thu,
          trừ khi rulebook nói ngược lại một cách tường minh: cờ
          ``revenue_excludes_cancelled`` bằng False (tức rulebook nói
          doanh thu KHÔNG loại đơn huỷ) thì huỷ vẫn tính, tương tự cho
          ``revenue_excludes_returned``. Cờ bằng None (rulebook không
          nói) thì giữ mặc định loại, không suy diễn.
        - ``other`` (shop không có cột trạng thái) thì True: shop không
          ghi trạng thái thì không có căn cứ loại đơn nào, loại hết sẽ
          cho doanh thu 0 sai hoàn toàn, trong khi giữ lại chỉ sai ở
          những đơn huỷ/trả lẫn trong đó (nếu có). Trường hợp này
          solver ghi ``other_status_included`` vào evidence và trừ
          confidence, để gate biết đáp án này kém chắc hơn.
        - Trạng thái lạ (không thuộc 5 nhãn) thì False cho an toàn:
          không biết trạng thái là gì thì không dám tính vào doanh thu.
    """
    if status_norm in ("completed", "delivering"):
        return True
    if status_norm == "cancelled":
        # Chỉ tính khi rulebook nói tường minh là KHÔNG loại đơn huỷ.
        return _rulebook_flag(rulebook, "revenue_excludes_cancelled") is False
    if status_norm == "returned":
        return _rulebook_flag(rulebook, "revenue_excludes_returned") is False
    if status_norm == "other":
        return True
    return False


def _is_blank(value):
    """Kiểm tra một ô có trống không (None, NaN, chuỗi rỗng).

    Dùng cùng định nghĩa với shop_view: số 0 là giá trị thật, không
    phải trống. Hàm này cần ở solver vì DataFrame có thể chứa NaN của
    pandas ở các ô thiếu, và phép tính phải phân biệt "thiếu" với "0".

    Sửa tích hợp Giai đoạn 2 (chỉ tối ưu thứ tự, KHÔNG đổi ngữ nghĩa):
    trước đây mọi ô đều đi qua ``pandas.isna`` (đắt khi trích hàng
    trăm nghìn dòng cho mỗi câu hỏi). Nay chuỗi và số được kết luận
    ngay (chuỗi trống -> trống, số hữu hạn -> không trống, NaN đã bắt
    ở trên; ``pandas.isna`` của chuỗi luôn False và của số hữu hạn
    luôn False nên kết quả không đổi), chỉ các kiểu còn lại (ngày,
    Timestamp, đối tượng lạ) mới đi qua ``pandas.isna`` như cũ.
    """
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (int, float)):
        return False
    try:
        import pandas as _pd

        if _pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return False


def _as_date(value):
    """Đổi một ô ngày thành datetime.date, hoặc None nếu không phải ngày.

    Cột ``_date`` của ShopView vốn đã là datetime.date, nhưng khi view
    đi qua cache (pickle/parquet) thì ngày có thể thành Timestamp hoặc
    chuỗi. Hàm này chấp nhận cả ba dạng: datetime -> lấy phần ngày,
    date -> giữ nguyên, chuỗi ``YYYY-MM-DD`` -> parse, còn lại None.
    Không đoán ngày từ số hay chuỗi lạ.
    """
    if _is_blank(value):
        return None
    if isinstance(value, _date):
        # datetime là lớp con của date nên phải kiểm tra datetime trước
        # khi trả: datetime.date() cho ra đúng phần ngày, không giờ.
        try:
            return value.date()  # type: ignore[attr-defined]
        except AttributeError:
            return value
    if hasattr(value, "to_pydatetime"):
        try:
            return value.to_pydatetime().date()
        except Exception:
            return None
    if isinstance(value, str):
        text = value.strip()
        try:
            parts = text.split("-")
            if len(parts) == 3:
                return _date(int(parts[0]), int(parts[1]), int(parts[2]))
        except (ValueError, TypeError):
            return None
    return None


def _to_vnd_int(amount) -> int:
    """Làm tròn một số tiền về số nguyên VND, kiểu nửa lên.

    Dùng Decimal từ chuỗi của số tiền (không dùng round() của Python,
    vì round() làm tròn kiểu ngân hàng: 0.5 về số chẵn, khác với luật
    chấm percent/tiền kiểu nửa lên). Tiền VND thực tế không có phần lẻ
    nhỏ hơn đồng, nên làm tròn này chỉ dọn sai số float, không đổi giá
    trị kinh tế.
    """
    if amount is None:
        return 0
    quantized = _Decimal(str(amount)).quantize(
        _Decimal("1"), rounding=_HALF_UP)
    return int(quantized)


def _sum_money(amounts) -> int:
    """Cộng các khoản tiền thành số nguyên VND, chính xác theo Decimal.

    Cộng bằng Decimal từ chuỗi của từng khoản (không cộng float rồi
    mới làm tròn), để tổng hàng nghìn dòng không lệch vài đồng do sai
    số biểu diễn float. Khoản None được bỏ qua (dòng không có tiền
    thì không đóng góp, không tính là 0 một cách ồn ào ở đây; caller
    chịu trách nhiệm đếm dòng thiếu nếu cần).
    """
    total = _Decimal("0")
    for amount in amounts:
        if amount is None or _is_blank(amount):
            continue
        try:
            total += _Decimal(str(amount))
        except Exception:
            # Khoản không phải số thì bỏ qua, không crash cả phép tính.
            continue
    return _to_vnd_int(total)


def _round_percent(numerator, denominator) -> float:
    """Tính phần trăm (tử/mẫu*100), làm tròn 2 chữ số, kiểu nửa lên.

    Trả float (ví dụ 12.34). Caller phải kiểm tra mẫu khác 0 trước khi
    gọi, vì chia cho 0 là không xác định: hàm này không tự trả 0 cho
    trường hợp đó, để caller quyết định abstain thay vì bịa 0%.
    """
    value = (_Decimal(str(numerator)) / _Decimal(str(denominator))
             * _Decimal("100"))
    return float(value.quantize(_Decimal("0.01"), rounding=_HALF_UP))


def _num_quantity(value) -> int | float:
    """Chuẩn hoá một tổng số lượng: nguyên thì trả int, lẻ thì float.

    Số lượng quy đổi có thể lẻ (nửa đơn vị, 0.5 hộp...), nhưng đa số
    tổng là nguyên. Trả int khi giá trị gần như nguyên (sai số float
    dưới 1e-9) để đáp án gọn và khớp luật number của khung chấm; còn
    lại làm tròn 4 chữ số thập phân (đủ cho phân số đơn vị thường gặp,
    không tạo ra số lẻ vô nghĩa do float).
    """
    if value is None:
        return 0
    rounded = round(float(value), 4)
    if abs(rounded - round(rounded)) < 1e-9:
        return int(round(rounded))
    return rounded


def _period_str(value) -> str | None:
    """Đổi một ngày thành chuỗi ``YYYY-MM-DD`` cho evidence, hoặc None."""
    if value is None:
        return None
    if isinstance(value, _date):
        return value.isoformat()
    return str(value)


def _norm(value) -> str:
    """Chuẩn hoá một chuỗi thành khoá so sánh (không dấu, thường).

    Dùng cho mọi so sánh thực thể trong solver: mã, tên, nhóm, phương
    thức thanh toán, kênh đều so bằng khoá này để ``Cà phê`` khớp
    ``ca phe`` và ``KH0114`` khớp chính nó. Chuỗi trống/None thành ``''``.
    """
    if value is None:
        return ""
    return normalize_key(value)


def _days_in_month(year: int, month: int) -> int:
    """Trả số ngày của một tháng, tính đúng năm nhuận.

    Dùng mẹo lấy ngày đầu tháng sau trừ đi một ngày, thay vì tra bảng
    số ngày (bảng tra dễ sai tháng 2 năm nhuận). Tháng 12 thì tháng
    sau là tháng 1 năm sau.
    """
    if month == 12:
        first_next = _date(year + 1, 1, 1)
    else:
        first_next = _date(year, month + 1, 1)
    return (first_next - _date(year, month, 1)).days


def _month_range(year: int, month: int):
    """Trả (ngày đầu, ngày cuối) của một tháng dương lịch."""
    start = _date(year, month, 1)
    end = _date(year, month, _days_in_month(year, month))
    return start, end


def _previous_period(start: _date, end: _date):
    """Tính kỳ so sánh cho growth, theo đúng luật contract mục 3.

    Đầu vào: kỳ hỏi (start, end) đã đóng (cả hai đều khác None).
    Đầu ra: (start_truoc, end_truoc) của kỳ so sánh.

    Ba trường hợp, theo thứ tự ưu tiên:
        1. Kỳ hỏi là một tháng đầy đủ (từ ngày 1 đến cuối tháng) thì kỳ
           so là tháng trước đầy đủ. Đây là trường hợp "tháng tuyệt
           đối" (ví dụ hỏi tháng 8 thì so tháng 7 đầy đủ).
        2. Kỳ hỏi bắt đầu ngày 1 nhưng chưa hết tháng (ví dụ tháng này
           tính đến ngày 30) thì kỳ so là tháng trước CÙNG SỐ NGÀY
           (từ ngày 1 tháng trước đến ngày tương ứng, kẹp theo số ngày
           của tháng trước). Đây là trường hợp "tháng này" (month to
           date): so 30 ngày đầu tháng này với 30 ngày đầu tháng trước,
           không so với cả tháng trước (vì tháng trước dài hơn).
        3. Các kỳ còn lại (quý, năm, khoảng tự do...) thì kỳ so là
           khoảng liền trước có cùng độ dài: kết thúc ngay trước ngày
           bắt đầu kỳ hỏi, bắt đầu lùi lại đúng số ngày của kỳ hỏi.
    Không dùng calendar ở đây vì kỳ hỏi đã được parser giải thành ngày
    cụ thể; calendar chỉ cần cho Tết/tuần ở tầng parser, không phải
    tầng solver.
    """
    if start.day == 1:
        if end.day == _days_in_month(end.year, end.month):
            # Trường hợp 1: tháng đầy đủ -> tháng trước đầy đủ.
            if end.month == 1:
                prev_year, prev_month = end.year - 1, 12
            else:
                prev_year, prev_month = end.year, end.month - 1
            return _month_range(prev_year, prev_month)
        # Trường hợp 2: từ đầu tháng đến giữa tháng -> tháng trước
        # cùng số ngày (kẹp ngày cuối theo tháng trước).
        if start.month == 1:
            prev_year, prev_month = start.year - 1, 12
        else:
            prev_year, prev_month = start.year, start.month - 1
        prev_end_day = min(end.day, _days_in_month(prev_year, prev_month))
        return (_date(prev_year, prev_month, 1),
                _date(prev_year, prev_month, prev_end_day))
    # Trường hợp 3: khoảng liền trước cùng độ dài.
    length = (end - start).days + 1
    prev_end = start - _timedelta(days=1)
    prev_start = prev_end - _timedelta(days=length - 1)
    return prev_start, prev_end


# ---------------------------------------------------------------------------
# Trích bản ghi từ ShopView và lọc thực thể / kỳ
# ---------------------------------------------------------------------------


def _column_list(df, column, length):
    """Lấy danh sách giá trị của một cột, hoặc toàn None khi thiếu cột.

    ShopView luôn tạo đủ cột chuẩn, nhưng view đi qua cache cũ hoặc
    view tự tạo trong test có thể thiếu cột phụ (``_channel``,
    ``_discount``...). Hàm này che chắn cho mọi bước trích: thiếu cột
    thì coi như mọi dòng đều trống ở cột đó, không crash vì KeyError.
    """
    if df is None or column not in df.columns:
        return [None] * length
    return df[column].tolist()


def _line_records(view):
    """Trích lines của view thành danh sách dict gọn, chỉ các cột cần.

    Mỗi dict có các khoá: ``order_id``, ``date``, ``status_norm``,
    ``payment``, ``customer_key``, ``customer_display``,
    ``product_key``, ``product_display``, ``product_group``, ``qty``,
    ``qty_base``, ``unit_price``, ``line_amount``, ``cost_price``,
    ``line_unit``, ``discount``, ``channel``. Trích bằng ``tolist()``
    theo cột rồi zip (nhanh hơn ``iterrows``/``to_dict`` rất nhiều
    trên bảng hàng trăm nghìn dòng), và chuẩn hoá ngày ngay tại đây
    để các bước sau chỉ làm việc với datetime.date.
    """
    df = getattr(view, "lines", None)
    if df is None or len(df) == 0:
        return []
    n = len(df)
    order_ids = _column_list(df, "_order_id", n)
    dates = _column_list(df, "_date", n)
    statuses = _column_list(df, "_status_norm", n)
    payments = _column_list(df, "_payment", n)
    cust_keys = _column_list(df, "_customer_key", n)
    cust_displays = _column_list(df, "_customer_display", n)
    prod_keys = _column_list(df, "_product_key", n)
    prod_displays = _column_list(df, "_product_display", n)
    prod_groups = _column_list(df, "_product_group", n)
    qtys = _column_list(df, "_qty", n)
    qty_bases = _column_list(df, "_qty_base", n)
    unit_prices = _column_list(df, "_unit_price", n)
    amounts = _column_list(df, "_line_amount", n)
    costs = _column_list(df, "_cost_price", n)
    units = _column_list(df, "_line_unit", n)
    discounts = _column_list(df, "_discount", n)
    channels = _column_list(df, "_channel", n)
    records = []
    for values in zip(order_ids, dates, statuses, payments, cust_keys,
                      cust_displays, prod_keys, prod_displays, prod_groups,
                      qtys, qty_bases, unit_prices, amounts, costs,
                      units, discounts, channels):
        (oid, raw_date, status, payment, ckey, cdisplay, pkey,
         pdisplay, pgroup, qty, qty_base, price, amount, cost, unit,
         discount, channel) = values
        records.append({
            "order_id": None if _is_blank(oid) else str(oid).strip(),
            "date": _as_date(raw_date),
            "status_norm": None if _is_blank(status) else str(status),
            "payment": None if _is_blank(payment) else str(payment),
            "customer_key": None if _is_blank(ckey) else str(ckey),
            "customer_display": None if _is_blank(cdisplay) else str(cdisplay),
            "product_key": None if _is_blank(pkey) else str(pkey),
            "product_display": None if _is_blank(pdisplay) else str(pdisplay),
            "product_group": None if _is_blank(pgroup) else str(pgroup),
            "qty": None if _is_blank(qty) else qty,
            "qty_base": None if _is_blank(qty_base) else qty_base,
            "unit_price": None if _is_blank(price) else price,
            "line_amount": None if _is_blank(amount) else amount,
            "cost_price": None if _is_blank(cost) else cost,
            "line_unit": None if _is_blank(unit) else str(unit),
            "discount": None if _is_blank(discount) else discount,
            "channel": None if _is_blank(channel) else str(channel),
        })
    return records


def _order_records(view):
    """Trích orders của view thành danh sách dict gọn, chỉ cột cần.

    Mỗi dict có các khoá: ``order_id``, ``date``, ``status_norm``,
    ``payment``, ``customer_key``, ``customer_display``,
    ``order_total``, ``amount_paid``, ``amount_due``, ``channel``.
    Cách trích giống ``_line_records`` (tolist + zip) vì cùng lý do
    hiệu năng. Đơn không có mã thì ``order_id`` None và sẽ bị loại ở
    các bước đếm đơn (không thể xác định đơn nào).
    """
    df = getattr(view, "orders", None)
    if df is None or len(df) == 0:
        return []
    n = len(df)
    order_ids = _column_list(df, "_order_id", n)
    dates = _column_list(df, "_date", n)
    statuses = _column_list(df, "_status_norm", n)
    payments = _column_list(df, "_payment", n)
    cust_keys = _column_list(df, "_customer_key", n)
    cust_displays = _column_list(df, "_customer_display", n)
    totals = _column_list(df, "_order_total", n)
    paids = _column_list(df, "_amount_paid", n)
    dues = _column_list(df, "_amount_due", n)
    channels = _column_list(df, "_channel", n)
    records = []
    for values in zip(order_ids, dates, statuses, payments, cust_keys,
                      cust_displays, totals, paids, dues, channels):
        (oid, raw_date, status, payment, ckey, cdisplay, total, paid,
         due, channel) = values
        records.append({
            "order_id": None if _is_blank(oid) else str(oid).strip(),
            "date": _as_date(raw_date),
            "status_norm": None if _is_blank(status) else str(status),
            "payment": None if _is_blank(payment) else str(payment),
            "customer_key": None if _is_blank(ckey) else str(ckey),
            "customer_display": None if _is_blank(cdisplay) else str(cdisplay),
            "order_total": None if _is_blank(total) else total,
            "amount_paid": None if _is_blank(paid) else paid,
            "amount_due": None if _is_blank(due) else due,
            "channel": None if _is_blank(channel) else str(channel),
        })
    return records


def _in_period(record_date, start, end):
    """Kiểm tra một ngày có nằm trong kỳ [start, end] không.

    Kỳ mở cả hai đầu None nghĩa là "toàn bộ dữ liệu": khi đó mọi dòng
    đều qua, kể cả dòng không có ngày (vì không có kỳ để loại nó).
    Kỳ có ngày cụ thể thì dòng không có ngày bị loại, vì không xác
    định được nó thuộc kỳ hay không, và tính nó vào sẽ làm sai kỳ.
    """
    if start is None and end is None:
        return True
    if record_date is None:
        return False
    if start is not None and record_date < start:
        return False
    if end is not None and record_date > end:
        return False
    return True


def _entity_norms(parsed):
    """Trả tập khoá chuẩn hoá của thực thể hỏi, hoặc tập rỗng.

    Thực thể có thể được parser ghi bằng key, bằng display, hoặc cả
    hai. So sánh bằng khoá chuẩn hoá của cả hai chuỗi để câu hỏi ghi
    tên có dấu vẫn khớp key không dấu và ngược lại. Tập rỗng nghĩa là
    parser không cho thực thể nào (khi đó caller phải tự kiểm tra
    entity_kind trước khi gọi hàm match).
    """
    norms = set()
    for text in (getattr(parsed, "entity_key", None),
                 getattr(parsed, "entity_display", None)):
        norm = _norm(text)
        if norm:
            norms.add(norm)
    return norms


def _line_matches_entity(line, parsed):
    """Kiểm tra một dòng bán có thuộc thực thể hỏi không.

    Theo từng entity_kind:
        - ``product``: khớp mã hoặc tên mặt hàng của dòng.
        - ``customer``: khớp mã hoặc tên khách của dòng.
        - ``group``: khớp nhóm mặt hàng của dòng (nhóm ghi trên dòng,
          hoặc nhóm tra từ master đã nằm trong ``_product_group`` của
          ShopView khi dựng lines từ master).
        - ``payment``: khớp phương thức thanh toán của dòng (đã join
          từ bảng đơn khi dòng thiếu).
        - ``channel``: khớp kênh bán của dòng (đã join từ bảng đơn).
        - ``none``: mọi dòng đều khớp (câu hỏi không lọc thực thể).
        - ``employee``: luôn False ở solver này, vì xếp hạng/lọc theo
          nhân viên thuộc nhóm chưa bao phủ (xem UNCOVERED_MEASURES).
    So sánh bằng khoá chuẩn hoá (không dấu) để cách viết có/không dấu
    không làm lệch kết quả.
    """
    kind = getattr(parsed, "entity_kind", "none") or "none"
    if kind == "none":
        return True
    norms = _entity_norms(parsed)
    if not norms:
        return False
    if kind == "product":
        return _norm(line.get("product_key")) in norms \
            or _norm(line.get("product_display")) in norms
    if kind == "customer":
        return _norm(line.get("customer_key")) in norms \
            or _norm(line.get("customer_display")) in norms
    if kind == "group":
        return _norm(line.get("product_group")) in norms
    if kind == "payment":
        return _norm(line.get("payment")) in norms
    if kind == "channel":
        return _norm(line.get("channel")) in norms
    return False


def _order_matches_entity(order, parsed):
    """Kiểm tra một đơn có thuộc thực thể hỏi không.

    Đơn không có mặt hàng nên chỉ lọc được theo khách, phương thức
    thanh toán và kênh; thực thể ``product``/``group`` trên đơn thì
    luôn False (muốn lọc theo mặt hàng phải dùng lines, caller phải
    chọn nguồn lines trong trường hợp đó). Các kind còn lại xử lý
    giống ``_line_matches_entity`` để hai nguồn nhất quán.
    """
    kind = getattr(parsed, "entity_kind", "none") or "none"
    if kind == "none":
        return True
    norms = _entity_norms(parsed)
    if not norms:
        return False
    if kind == "customer":
        return _norm(order.get("customer_key")) in norms \
            or _norm(order.get("customer_display")) in norms
    if kind == "payment":
        return _norm(order.get("payment")) in norms
    if kind == "channel":
        return _norm(order.get("channel")) in norms
    return False


def _entity_exists(view, lines, orders, parsed):
    """Kiểm tra thực thể hỏi có tồn tại trong shop không.

    Phân biệt hai trường hợp mà solver phải xử lý khác nhau:
        - Thực thể KHÔNG tồn tại trong shop (sai tên, parser match
          nhầm, hoặc shop không có loại dữ liệu đó): solver abstain
          với ``entity``, vì không có gì để tính.
        - Thực thể CÓ tồn tại nhưng không phát sinh trong kỳ hỏi:
          doanh thu/số lượng/đếm đơn của nó trong kỳ là 0, đó là đáp
          án đúng, không phải abstain.
    Cách kiểm tra: đối chiếu với danh sách products/customers của view
    (đã gồm master) và với chính lines/orders. Nhóm thì đối chiếu với
    nhóm của products và nhóm trên lines. Thanh toán/kênh thì đối chiếu
    với giá trị thực tế trên lines/orders.
    """
    kind = getattr(parsed, "entity_kind", "none") or "none"
    if kind == "none":
        return True
    norms = _entity_norms(parsed)
    if not norms:
        return False
    if kind == "product":
        for item in getattr(view, "products", []) or []:
            if _norm(item.get("key")) in norms \
                    or _norm(item.get("display")) in norms:
                return True
        return any(_line_matches_entity(line, parsed) for line in lines)
    if kind == "customer":
        for item in getattr(view, "customers", []) or []:
            if _norm(item.get("key")) in norms \
                    or _norm(item.get("display")) in norms:
                return True
        return any(_line_matches_entity(line, parsed) for line in lines) \
            or any(_order_matches_entity(order, parsed) for order in orders)
    if kind == "group":
        for item in getattr(view, "products", []) or []:
            if _norm(item.get("group")) in norms:
                return True
        return any(_norm(line.get("product_group")) in norms
                   for line in lines)
    if kind == "payment":
        values = {_norm(line.get("payment")) for line in lines}
        values |= {_norm(order.get("payment")) for order in orders}
        return bool(norms & values)
    if kind == "channel":
        values = {_norm(line.get("channel")) for line in lines}
        values |= {_norm(order.get("channel")) for order in orders}
        return bool(norms & values)
    return False


def _revenue_lines(lines, parsed, rulebook, start, end):
    """Lọc lines theo bộ lọc doanh thu: trạng thái + kỳ + thực thể.

    Trả (danh sách dòng đã lọc, cờ có dòng trạng thái ``other``).
    Dòng trạng thái ``other`` được tính vào doanh thu theo
    ``is_revenue_order`` (shop không ghi trạng thái thì không có căn
    cứ loại), nhưng cờ này được trả về để caller ghi vào evidence và
    trừ confidence. Dòng không có trạng thái (None) cũng được coi là
    ``other`` ở đây, vì ShopView đã chuẩn hoá trạng thái trống thành
    ``other`` khi không có cột trạng thái; nếu view cũ nào đó còn để
    None thì xử lý tương tự cho an toàn.
    """
    filtered = []
    other_included = False
    for line in lines:
        status = line.get("status_norm") or "other"
        if not is_revenue_order(status, rulebook):
            continue
        if status == "other":
            other_included = True
        if not _in_period(line.get("date"), start, end):
            continue
        if not _line_matches_entity(line, parsed):
            continue
        filtered.append(line)
    return filtered, other_included


def _revenue_orders(orders, parsed, rulebook, start, end):
    """Lọc orders theo bộ lọc doanh thu, tương tự ``_revenue_lines``.

    Dùng cho shop không có dòng bán (chỉ có hoá đơn tổng): khi đó
    doanh thu cấp đơn lấy từ ``_order_total`` của đơn revenue, và
    thực thể chỉ lọc được theo khách/thanh toán/kênh (lọc theo mặt
    hàng trên orders luôn cho rỗng, caller phải xử lý riêng).
    """
    filtered = []
    other_included = False
    for order in orders:
        status = order.get("status_norm") or "other"
        if not is_revenue_order(status, rulebook):
            continue
        if status == "other":
            other_included = True
        if not _in_period(order.get("date"), start, end):
            continue
        if not _order_matches_entity(order, parsed):
            continue
        filtered.append(order)
    return filtered, other_included


def _effective_period(parsed, view):
    """Trả (start, end) thực tế dùng để lọc, khi parser không cho kỳ.

    Nếu parser cho kỳ (ít nhất một đầu khác None) thì dùng đúng kỳ đó,
    kể cả kỳ mở một đầu (ví dụ "đến hết ngày X" chỉ có end). Nếu parser
    không cho kỳ nào (cả hai None) thì dùng toàn bộ khoảng dữ liệu của
    shop (date_min..date_max) để evidence ghi rõ kỳ thực tế, thay vì
    để kỳ trống gây hiểu nhầm là không lọc ngày.
    """
    start = getattr(parsed, "period_start", None)
    end = getattr(parsed, "period_end", None)
    if start is None and end is None:
        start = getattr(view, "date_min", None)
        end = getattr(view, "date_max", None)
    return start, end


def _base_evidence(parsed, start, end):
    """Tạo evidence gốc cho một câu hỏi, các measure sẽ bổ sung thêm.

    Luôn có đủ 5 khoá contract yêu cầu: ``measure``, ``period`` (cặp
    chuỗi ngày), ``rows_used`` (ban đầu 0, measure điền sau),
    ``filter`` (ban đầu mô tả kỳ + thực thể, measure bổ sung bộ lọc
    riêng), ``group_key`` (None, measure xếp hạng/nhóm điền sau).
    """
    kind = getattr(parsed, "entity_kind", "none") or "none"
    entity = getattr(parsed, "entity_display", None) \
        or getattr(parsed, "entity_key", None)
    filter_text = f"ky={_period_str(start)}..{_period_str(end)}"
    if kind != "none" and entity:
        filter_text += f"; thuc_the={kind}:{entity}"
    return {
        "measure": getattr(parsed, "measure", None),
        "period": (_period_str(start), _period_str(end)),
        "rows_used": 0,
        "filter": filter_text,
        "group_key": None,
        "warnings": [],
        "confidence_notes": [],
    }


def _abstain(parsed, start, end, error_group, note):
    """Tạo SolveResult abstain với evidence đầy đủ.

    Mọi đường abstain trong solver đều đi qua đây để entry, confidence
    và evidence nhất quán: entry là ``{'id': None, 'type': 'abstain'}``
    (id thật do tầng chạy điền sau), confidence 0.0, error_group theo
    nhóm lỗi, evidence ghi rõ lý do trong ``warnings`` để báo cáo lỗi
    phân nhóm được mà không cần đoán từ code.
    """
    evidence = _base_evidence(parsed, start, end)
    evidence["warnings"].append(note)
    return SolveResult(
        status="abstain",
        entry={"id": None, "type": "abstain"},
        confidence=0.0,
        error_group=error_group,
        evidence=evidence,
    )


def _answered(parsed, start, end, entry_type, answer, rows_used,
              filter_text, group_key, confidence, warnings,
              confidence_notes):
    """Tạo SolveResult answered với evidence đầy đủ.

    ``confidence`` đã bao gồm các khoản trừ của solver (đơn ``other``,
    ảnh chụp công nợ...); gate sẽ áp ngưỡng và trừ thêm theo công thức
    của nó. ``warnings`` và ``confidence_notes`` được ghi nguyên vẹn
    vào evidence để người đọc báo cáo biết vì sao confidence không
    phải 1.0 mà không cần đọc code.
    """
    evidence = _base_evidence(parsed, start, end)
    evidence["rows_used"] = int(rows_used)
    evidence["filter"] = filter_text
    evidence["group_key"] = group_key
    evidence["warnings"] = list(warnings)
    evidence["confidence_notes"] = list(confidence_notes)
    return SolveResult(
        status="answered",
        entry={"id": None, "type": entry_type, "answer": answer},
        confidence=float(confidence),
        error_group=None,
        evidence=evidence,
    )


def _confidence_for(other_included, extra_notes):
    """Tính confidence của solver và ghi chú các khoản trừ.

    Bắt đầu 1.0. Đơn trạng thái ``other`` trong phép tính trừ 0.1 (vì
    không biết trong đó có bao nhiêu đơn huỷ/trả lẫn vào). Các ghi chú
    thêm (ví dụ dùng ảnh chụp tiền trả cho công nợ) do caller truyền
    vào dưới dạng (số điểm trừ, ghi chú) cũng được áp dụng và ghi rõ.
    Kết quả kẹp trong [0, 1].
    """
    confidence = 1.0
    notes = []
    if other_included:
        confidence -= 0.1
        notes.append("tinh ca don trang thai 'other' (shop khong co "
                     "cot trang thai) vao doanh thu, tru 0.1")
    for deduction, note in extra_notes:
        confidence -= deduction
        notes.append(note)
    return max(0.0, min(1.0, confidence)), notes


def _threshold_op_ok(order_revenue, threshold, op):
    """Kiểm tra doanh thu một đơn có qua ngưỡng tiền không.

    Ngưỡng mặc định khi câu nói "từ X trở lên" là ``gte`` (>=). Các
    toán tử còn lại: ``gt`` (>), ``lte`` (<=), ``lt`` (<). Toán tử lạ
    thì coi như ``gte`` cho an toàn, vì đa số câu ngưỡng trong dữ liệu
    đều là "từ X trở lên".
    """
    if order_revenue is None:
        return False
    if op in (None, "gte"):
        return order_revenue >= threshold
    if op == "gt":
        return order_revenue > threshold
    if op == "lte":
        return order_revenue <= threshold
    if op == "lt":
        return order_revenue < threshold
    return order_revenue >= threshold


def _threshold_order_ids(parsed, lines_rev, orders_rev, use_lines):
    """Tính tập mã đơn qua ngưỡng tiền, hoặc None khi không có ngưỡng.

    Dùng cho câu "đơn có doanh thu từ X trở lên": doanh thu cấp đơn
    được tính từ nguồn phù hợp:
        - Khi lọc theo mặt hàng/nhóm (cần lines) thì doanh thu đơn là
          tổng tiền dòng đã lọc theo thực thể đó (phần doanh thu của
          đơn thuộc về thực thể hỏi).
        - Khi không lọc theo mặt hàng/nhóm mà có orders thì doanh thu
          đơn là ``_order_total`` của đơn (tổng bill, gồm cả phần phí
          cộng thêm nếu có), vì đó mới là "doanh thu của đơn".
        - Khi chỉ có lines thì doanh thu đơn là tổng tiền dòng của đơn.
    Trả (tập mã đơn qua ngưỡng, ghi chú nguồn) hoặc (None, ghi chú)
    khi câu hỏi không có ngưỡng. Trả ("unavailable", ghi chú) khi có
    ngưỡng nhưng không tính được doanh thu cấp đơn nào (toàn None),
    để caller abstain với ``column`` thay vì đếm 0 sai.
    """
    threshold = getattr(parsed, "threshold_money", None)
    if threshold is None:
        return None, "khong co nguong tien"
    op = getattr(parsed, "threshold_op", None) or "gte"
    per_order = {}
    if use_lines:
        for line in lines_rev:
            oid = line.get("order_id")
            if oid is None or line.get("line_amount") is None:
                continue
            per_order[oid] = per_order.get(oid, 0.0) \
                + float(line["line_amount"])
    else:
        for order in orders_rev:
            oid = order.get("order_id")
            if oid is None or order.get("order_total") is None:
                continue
            per_order[oid] = float(order["order_total"])
    if not per_order:
        return "unavailable", ("co nguong tien nhung khong tinh duoc "
                                "doanh thu cap don (toan None)")
    passing = {oid for oid, total in per_order.items()
               if _threshold_op_ok(total, threshold, op)}
    return passing, (f"nguong tien {op} {threshold}, "
                     f"{len(passing)}/{len(per_order)} don qua nguong")


# ---------------------------------------------------------------------------
# Đối chiếu thực thể: khớp chính xác, hoặc khớp theo token khi tên rút gọn
# ---------------------------------------------------------------------------


def _tokens_subset(entity_norm, target_norm):
    """Kiểm tra mọi token của tên rút gọn có nằm trong tên đầy đủ không.

    Parser chấp nhận tên rút gọn (ví dụ ``Sting dau`` cho ``Sting dau
    330ml``) khi mọi token của tên rút gọn nằm trong tên đầy đủ và tên
    rút gọn có từ 2 token trở lên. Hàm này cài đặt đúng điều kiện đó
    ở phía solver, để khi parser đưa key là tên rút gọn (thay vì key
    đầy đủ của view) thì solver vẫn đối chiếu được, thay vì abstain
    oan. Tên rút gọn chỉ 1 token thì KHÔNG chấp nhận kiểu này, vì một
    token chung (``ca phe``) sẽ khớp hàng chục sản phẩm khác nhau.
    """
    entity_tokens = entity_norm.split()
    if len(entity_tokens) < 2:
        return False
    target_tokens = set(target_norm.split())
    return set(entity_tokens) <= target_tokens


def _item_matches_norms(item_key, item_display, norms, subset):
    """Kiểm tra một mục view (key/display) có khớp tập khoá hỏi không.

    Khớp chính xác theo khoá chuẩn hoá luôn được thử trước. Nếu
    ``subset`` True thì thử thêm khớp theo token (xem
    ``_tokens_subset``) cho cả key và display, để tên rút gọn vẫn khớp
    tên đầy đủ. Trả False khi không khớp kiểu nào.
    """
    if _norm(item_key) in norms or _norm(item_display) in norms:
        return True
    if not subset:
        return False
    for norm in norms:
        if _tokens_subset(norm, _norm(item_key)) \
                or _tokens_subset(norm, _norm(item_display)):
            return True
    return False


def _resolve_entity_mode(view, lines, orders, parsed):
    """Xác định cách đối chiếu thực thể: 'exact', 'subset' hay None.

    - ``exact``: thực thể khớp chính xác (theo khoá chuẩn hoá) với ít
      nhất một mục trong view hoặc một dòng/đơn. Đây là trường hợp
      thường gặp khi parser đã giải tên về key đầy đủ của view.
    - ``subset``: không khớp chính xác, nhưng tên hỏi là tên rút gọn
      (>= 2 token) nằm trong tên đầy đủ của một sản phẩm/khách trong
      view. Chỉ áp dụng cho product/customer, vì chỉ hai loại này mới
      có tên rút gọn trong câu hỏi.
    - None: không khớp kiểu nào -> thực thể không tồn tại trong shop,
      solver phải abstain với ``entity``.
    Caller dùng mode này cho mọi bước lọc của câu hỏi đó, để cách khớp
    nhất quán từ đầu đến cuối (không chỗ khớp chính xác, chỗ khớp
    rút gọn gây lệch số).
    """
    kind = getattr(parsed, "entity_kind", "none") or "none"
    if kind == "none":
        return "exact"
    norms = _entity_norms(parsed)
    if not norms:
        return None
    # Khớp chính xác trước, dùng đúng logic của _entity_exists.
    if _entity_exists(view, lines, orders, parsed):
        return "exact"
    if kind in ("product", "customer"):
        items = getattr(view, "products", []) if kind == "product" \
            else getattr(view, "customers", [])
        for item in items or []:
            if _item_matches_norms(item.get("key"), item.get("display"),
                                   norms, subset=True):
                return "subset"
    return None


def _line_matches_entity_mode(line, parsed, mode):
    """Kiểm tra dòng có thuộc thực thể hỏi không, theo mode đã chọn.

    Mode ``exact`` dùng đúng ``_line_matches_entity`` (khớp chính xác).
    Mode ``subset`` (chỉ cho product/customer) dùng khớp theo token:
    mọi token của tên hỏi phải nằm trong token của mã/tên dòng. Các
    kind khác (group/payment/channel) không có khái niệm tên rút gọn
    nên luôn dùng khớp chính xác, bất kể mode.
    """
    kind = getattr(parsed, "entity_kind", "none") or "none"
    if kind == "none":
        return True
    if mode == "subset" and kind in ("product", "customer"):
        norms = _entity_norms(parsed)
        if kind == "product":
            targets = [_norm(line.get("product_key")),
                       _norm(line.get("product_display"))]
        else:
            targets = [_norm(line.get("customer_key")),
                       _norm(line.get("customer_display"))]
        for norm in norms:
            if any(_tokens_subset(norm, target) for target in targets):
                return True
        return False
    return _line_matches_entity(line, parsed)


def _order_matches_entity_mode(order, parsed, mode):
    """Kiểm tra đơn có thuộc thực thể hỏi không, theo mode đã chọn.

    Tương tự ``_line_matches_entity_mode`` nhưng cho đơn: mode subset
    chỉ áp dụng cho customer (đơn không có mặt hàng để khớp rút gọn).
    """
    kind = getattr(parsed, "entity_kind", "none") or "none"
    if kind == "none":
        return True
    if mode == "subset" and kind == "customer":
        norms = _entity_norms(parsed)
        targets = [_norm(order.get("customer_key")),
                   _norm(order.get("customer_display"))]
        for norm in norms:
            if any(_tokens_subset(norm, target) for target in targets):
                return True
        return False
    return _order_matches_entity(order, parsed)


def _revenue_lines_mode(lines, parsed, rulebook, start, end, mode):
    """Lọc lines theo bộ lọc doanh thu, dùng mode đối chiếu đã chọn.

    Giống ``_revenue_lines`` nhưng truyền mode vào bước lọc thực thể,
    để câu hỏi tên rút gọn được lọc nhất quán theo token thay vì khớp
    chính xác (khớp chính xác sẽ cho rỗng và trả 0 sai). Trả (dòng đã
    lọc, cờ có dòng trạng thái ``other``).
    """
    filtered = []
    other_included = False
    for line in lines:
        status = line.get("status_norm") or "other"
        if not is_revenue_order(status, rulebook):
            continue
        if status == "other":
            other_included = True
        if not _in_period(line.get("date"), start, end):
            continue
        if not _line_matches_entity_mode(line, parsed, mode):
            continue
        filtered.append(line)
    return filtered, other_included


def _revenue_orders_mode(orders, parsed, rulebook, start, end, mode):
    """Lọc orders theo bộ lọc doanh thu, dùng mode đối chiếu đã chọn.

    Tương tự ``_revenue_orders`` nhưng có mode, dùng cho shop không có
    dòng bán hoặc khi cần doanh thu cấp đơn (ngưỡng tiền, công nợ).
    """
    filtered = []
    other_included = False
    for order in orders:
        status = order.get("status_norm") or "other"
        if not is_revenue_order(status, rulebook):
            continue
        if status == "other":
            other_included = True
        if not _in_period(order.get("date"), start, end):
            continue
        if not _order_matches_entity_mode(order, parsed, mode):
            continue
        filtered.append(order)
    return filtered, other_included


def _revenue_value(parsed, view, lines, orders, rulebook, start, end, mode):
    """Tính tổng doanh thu trong kỳ, chọn nguồn lines hoặc orders.

    Nguồn lines được ưu tiên khi shop có dòng bán, vì doanh thu dòng
    là chi tiết nhất và dùng được cho mọi lọc thực thể (kể cả mặt
    hàng/nhóm). Chỉ khi shop không có dòng nào mới dùng orders với
    ``_order_total`` (shop hoá đơn tổng). Trả (tiền nguyên VND, số
    dòng/bản ghi đã dùng, cờ other, tên nguồn) để caller ghi evidence.
    Dòng/bản ghi có tiền None được bỏ qua trong tổng (không tính là
    0), và caller nào cần chặt (ví dụ quantity) sẽ tự kiểm tra thêm.
    """
    if lines:
        filtered, other = _revenue_lines_mode(
            lines, parsed, rulebook, start, end, mode)
        return (_sum_money([line.get("line_amount") for line in filtered]),
                len(filtered), other, "lines")
    filtered, other = _revenue_orders_mode(
        orders, parsed, rulebook, start, end, mode)
    return (_sum_money([order.get("order_total") for order in filtered]),
            len(filtered), other, "orders")


def _copy_parsed(parsed, **changes):
    """Tạo bản sao ParsedQuestion với một số trường thay đổi.

    Dùng ``copy.copy`` (sao nông) rồi gán thuộc tính, thay vì
    ``dataclasses.replace``, để hàm chạy được cả khi ParsedQuestion
    của parser là dataclass hay class thường. Bản gốc không bị sửa,
    nên growth tính kỳ trước và revenue_share tính mẫu số không làm
    lệch ParsedQuestion của câu hỏi gốc.
    """
    import copy as _copy

    new_parsed = _copy.copy(parsed)
    for key, value in changes.items():
        setattr(new_parsed, key, value)
    return new_parsed


def _display_for_key(items, key):
    """Tra tên hiển thị của một key trong danh sách products/customers.

    Ưu tiên khớp chính xác theo khoá chuẩn hoá; nếu không khớp thì trả
    chính key (trường hợp key vốn đã là tên, hoặc mục không có trong
    danh sách master nhưng có trên dòng). Hàm này quan trọng cho đáp
    án top_*: dòng bán chỉ có mã (ví dụ ``SP0046``) nhưng đáp án phải
    là tên đầy đủ từ master (ví dụ ``Bạc xỉu (M)``) thì mới khớp đáp
    án tham chiếu và alias mã của nó.
    """
    if key is None:
        return ""
    target = _norm(key)
    for item in items or []:
        if _norm(item.get("key")) == target:
            display = item.get("display")
            if display:
                return display
            return key
    return key


# ---------------------------------------------------------------------------
# Các measure: doanh thu, đếm đơn, số lượng, huỷ, thị phần, tăng trưởng
# ---------------------------------------------------------------------------


def _solve_revenue(parsed, view, lines, orders, rulebook, start, end, mode):
    """Giải measure revenue và product_revenue (doanh thu trong kỳ).

    Cách tính: tổng ``_line_amount`` của dòng revenue trong kỳ (hoặc
    tổng ``_order_total`` của đơn revenue khi shop không có dòng).
    Nếu câu hỏi có ngưỡng tiền (``threshold_money``) thì chỉ cộng
    doanh thu của các đơn qua ngưỡng, với doanh thu cấp đơn lấy từ
    orders khi không lọc theo mặt hàng/nhóm (tổng bill mới là doanh
    thu của đơn), và từ tổng dòng đã lọc khi lọc theo mặt hàng/nhóm.
    Không có dòng doanh thu nào trong kỳ thì trả 0 (không bán được gì
    trong kỳ là đáp án đúng, không phải abstain), trừ khi mọi dòng đều
    thiếu tiền (khi đó tổng 0 là giả, phải abstain ``column``).
    """
    use_lines = bool(lines)
    if use_lines:
        filtered, other = _revenue_lines_mode(
            lines, parsed, rulebook, start, end, mode)
    else:
        filtered, other = _revenue_orders_mode(
            orders, parsed, rulebook, start, end, mode)
    filter_text = (f"don revenue trong ky "
                   f"{_period_str(start)}..{_period_str(end)}")
    if getattr(parsed, "entity_kind", "none") not in (None, "none"):
        filter_text += f"; thuc_the={parsed.entity_kind}"
    threshold = getattr(parsed, "threshold_money", None)
    if threshold is not None:
        # Doanh thu cấp đơn: orders khi không lọc mặt hàng/nhóm và có
        # orders, còn lại dùng tổng dòng đã lọc.
        threshold_use_lines = use_lines and (
            getattr(parsed, "entity_kind", "none") in ("product", "group")
            or not orders)
        if threshold_use_lines:
            ids, note = _threshold_order_ids(
                parsed, filtered, [], True)
            if ids == "unavailable":
                return _abstain(parsed, start, end, "column", note)
            total = _sum_money([
                line.get("line_amount") for line in filtered
                if line.get("order_id") in ids])
            rows_used = sum(1 for line in filtered
                            if line.get("order_id") in ids)
        else:
            orders_rev, other_orders = _revenue_orders_mode(
                orders, parsed, rulebook, start, end, mode)
            ids, note = _threshold_order_ids(parsed, [], orders_rev, False)
            if ids == "unavailable":
                return _abstain(parsed, start, end, "column", note)
            total = _sum_money([
                order.get("order_total") for order in orders_rev
                if order.get("order_id") in ids])
            rows_used = sum(1 for order in orders_rev
                            if order.get("order_id") in ids)
            other = other or other_orders
        filter_text += f"; {note}"
    else:
        if use_lines:
            total = _sum_money(
                [line.get("line_amount") for line in filtered])
        else:
            total = _sum_money(
                [order.get("order_total") for order in filtered])
        rows_used = len(filtered)
        if filtered and all(
                (line.get("line_amount") if use_lines
                 else line.get("order_total")) is None
                for line in filtered):
            return _abstain(
                parsed, start, end, "column",
                "co dong doanh thu trong ky nhung toan bo deu thieu "
                "tien (line_amount/order_total None), khong tinh "
                "duoc doanh thu")
    confidence, notes = _confidence_for(other, [])
    warnings = []
    if rows_used == 0:
        warnings.append("khong co dong/đon doanh thu trong ky, tra 0")
    return _answered(parsed, start, end, "number", total, rows_used,
                     filter_text, None, confidence, warnings, notes)


def _solve_order_count(parsed, view, lines, orders, rulebook, start, end,
                       mode):
    """Giải measure order_count (số đơn revenue trong kỳ).

    Đếm theo mã đơn phân biệt (distinct ``_order_id``), không đếm số
    dòng, vì một đơn có nhiều dòng vẫn là một đơn. Nguồn lines khi có
    dòng (để lọc được theo mặt hàng/nhóm), nguồn orders khi shop không
    có dòng. Ngưỡng tiền (nếu có) thì chỉ đếm đơn qua ngưỡng, với
    doanh thu cấp đơn tính như ``_solve_revenue``. Đơn không có mã bị
    loại khỏi phép đếm vì không xác định được đơn nào (và được ghi vào
    evidence qua rows_used thấp hơn số dòng thô, nếu cần đối chiếu).
    """
    if not lines and not orders:
        return _abstain(parsed, start, end, "column",
                        "shop khong co lines lan orders, khong dem "
                        "duoc don")
    if lines:
        filtered, other = _revenue_lines_mode(
            lines, parsed, rulebook, start, end, mode)
        order_ids = {line.get("order_id") for line in filtered
                     if line.get("order_id") is not None}
        source = "lines"
    else:
        filtered, other = _revenue_orders_mode(
            orders, parsed, rulebook, start, end, mode)
        order_ids = {order.get("order_id") for order in filtered
                     if order.get("order_id") is not None}
        source = "orders"
    filter_text = (f"dem don revenue phan biet trong ky "
                   f"{_period_str(start)}..{_period_str(end)} "
                   f"(nguon {source})")
    threshold = getattr(parsed, "threshold_money", None)
    if threshold is not None:
        if lines and getattr(parsed, "entity_kind", "none") not in (
                "product", "group") and orders:
            orders_rev, _other = _revenue_orders_mode(
                orders, parsed, rulebook, start, end, mode)
            ids, note = _threshold_order_ids(parsed, [], orders_rev, False)
        elif lines:
            ids, note = _threshold_order_ids(parsed, filtered, [], True)
        else:
            ids, note = _threshold_order_ids(parsed, [], filtered, False)
        if ids == "unavailable":
            return _abstain(parsed, start, end, "column", note)
        order_ids = order_ids & set(ids)
        filter_text += f"; {note}"
    confidence, notes = _confidence_for(other, [])
    return _answered(parsed, start, end, "number", len(order_ids),
                     len(filtered), filter_text, None, confidence, [],
                     notes)


def _quantity_total(lines_rev, view, parsed):
    """Cộng số lượng quy đổi của các dòng revenue đã lọc.

    Trả (trạng_thái, giá_trị_hoặc_ghi_chú, số_dòng_dùng, warnings,
    error_group). Trạng thái ``ok`` thì giá trị là số lượng (int nếu
    nguyên, float nếu lẻ). Trạng thái ``abstain`` thì giá trị là ghi
    chú lý do và error_group là nhóm lỗi.

    Luật thiếu hệ số (bắt buộc, theo nhiệm vụ):
        - Dòng có ``_qty_base`` thì cộng thẳng (đã quy đổi).
        - Dòng thiếu ``_qty_base`` nhưng có ``_qty``:
            + Mặt hàng nằm trong ``conversion_products`` của view (đã
              có bảng quy đổi) mà dòng không tra được hệ số -> abstain
              toàn câu với ``unit``, không đoán hệ số 1, vì đoán sẽ
              làm sai tổng mà không ai biết.
            + Mặt hàng hoàn toàn không có dòng quy đổi nào -> theo
              rulebook, đơn vị trên dòng chính là đơn vị cơ bản, hệ số
              1, nên cộng ``_qty`` và ghi rõ trong warnings.
        - Dòng thiếu cả ``_qty`` lẫn ``_qty_base`` thì bỏ qua (không
          có số lượng để cộng); nếu mọi dòng đều như vậy thì abstain
          ``column`` vì shop không có dữ liệu số lượng cho câu này.
    Khi câu hỏi ghi rõ đơn vị (``parsed.unit``):
        - Đơn vị hỏi trùng đơn vị cơ bản đã biết của mặt hàng -> cộng
          ``_qty_base`` như thường.
        - Đơn vị hỏi khác đơn vị cơ bản nhưng có hệ số quy đổi từ đơn
          vị hỏi sang cơ bản -> số lượng theo đơn vị hỏi bằng
          ``_qty_base / hệ_số``.
        - Đơn vị hỏi trùng đơn vị bán trên mọi dòng của mặt hàng (và
          mặt hàng có bảng quy đổi nhưng chưa biết đơn vị cơ bản) ->
          cộng ``_qty`` gốc (vì hỏi đúng đơn vị bán thì không cần quy
          đổi).
        - Các trường hợp còn lại -> abstain ``unit``, không đoán hệ số.
    So sánh đơn vị và key mặt hàng đều bằng khoá chuẩn hoá (không dấu)
    để khớp được cả khi key của view giữ dấu còn unit của parser đã
    normalize_key.
    """
    asked_unit = _norm(getattr(parsed, "unit", None))
    base_by_product = {}
    for key, base in (getattr(view, "base_unit_by_product", {}) or {}).items():
        base_by_product[_norm(key)] = _norm(base)
    factor_by_key = {}
    for (key, unit), factor in (
            getattr(view, "conversion_factor", {}) or {}).items():
        factor_by_key[(_norm(key), _norm(unit))] = factor
    conversion_products = {
        _norm(key)
        for key in (getattr(view, "conversion_products", set()) or set())}
    total = 0.0
    used = 0
    warnings = []
    # Nhóm dòng theo mặt hàng để xử lý đơn vị hỏi theo từng mặt hàng.
    # (Dòng của cùng một mặt hàng có cùng đơn vị cơ bản và cùng hệ số,
    # nên quyết định theo mặt hàng là đủ, không cần quyết theo dòng.)
    by_product = {}
    for line in lines_rev:
        by_product.setdefault(line.get("product_key"), []).append(line)
    for product_key, product_lines in by_product.items():
        norm_key = _norm(product_key)
        base_unit = base_by_product.get(norm_key)
        in_conversion = norm_key in conversion_products
        if not asked_unit:
            # Không hỏi đơn vị cụ thể: cộng theo đơn vị cơ bản.
            for line in product_lines:
                qty_base = line.get("qty_base")
                if qty_base is not None:
                    total += float(qty_base)
                    used += 1
                elif line.get("qty") is not None:
                    if in_conversion:
                        return ("abstain",
                                f"mat hang {product_key} co bang quy doi "
                                "nhung dong thieu _qty_base (khong tra "
                                "duoc he so), khong doan he so 1",
                                used, warnings, "unit")
                    total += float(line["qty"])
                    used += 1
                    warnings.append(
                        f"mat hang {product_key} khong co dong quy doi "
                        "nao, ap he so 1 theo rulebook cho dong thieu "
                        "_qty_base")
                # Dòng thiếu cả qty lẫn qty_base thì bỏ qua (xem dưới).
            continue
        # Có hỏi đơn vị cụ thể: quyết định theo từng mặt hàng.
        if base_unit is not None and base_unit == asked_unit:
            for line in product_lines:
                qty_base = line.get("qty_base")
                if qty_base is not None:
                    total += float(qty_base)
                    used += 1
                elif line.get("qty") is not None:
                    if in_conversion:
                        return ("abstain",
                                f"mat hang {product_key} co bang quy doi "
                                "nhung dong thieu _qty_base, khong doan "
                                "he so 1", used, warnings, "unit")
                    total += float(line["qty"])
                    used += 1
            continue
        if base_unit is not None:
            # Đơn vị hỏi khác đơn vị cơ bản: cần hệ số từ đơn vị hỏi.
            factor = factor_by_key.get((norm_key, asked_unit))
            if factor is None or factor == 0:
                return ("abstain",
                        f"khong co he so quy doi tu don vi hoi "
                        f"'{asked_unit}' sang don vi co ban cua mat hang "
                        f"{product_key}", used, warnings, "unit")
            for line in product_lines:
                qty_base = line.get("qty_base")
                if qty_base is not None:
                    total += float(qty_base) / float(factor)
                    used += 1
                else:
                    return ("abstain",
                            f"mat hang {product_key} thieu _qty_base nen "
                            "khong quy doi nguoc sang don vi hoi duoc",
                            used, warnings, "unit")
            continue
        # Chưa biết đơn vị cơ bản của mặt hàng.
        if not in_conversion:
            # Không có dòng quy đổi nào: đơn vị hỏi được coi là đơn vị
            # cơ bản theo rulebook, cộng qty_base (hoặc qty khi thiếu).
            for line in product_lines:
                qty_base = line.get("qty_base")
                if qty_base is not None:
                    total += float(qty_base)
                    used += 1
                elif line.get("qty") is not None:
                    total += float(line["qty"])
                    used += 1
            warnings.append(
                f"mat hang {product_key} khong co dong quy doi nao, "
                "coi don vi hoi la don vi co ban theo rulebook")
            continue
        # Có bảng quy đổi nhưng chưa biết đơn vị cơ bản: nếu đơn vị
        # hỏi trùng đơn vị bán trên mọi dòng thì cộng qty gốc.
        line_units = {_norm(line.get("line_unit"))
                      for line in product_lines
                      if line.get("line_unit") is not None}
        if line_units and line_units == {asked_unit}:
            for line in product_lines:
                if line.get("qty") is not None:
                    total += float(line["qty"])
                    used += 1
            continue
        factor = factor_by_key.get((norm_key, asked_unit))
        if factor is not None and factor != 0:
            for line in product_lines:
                qty_base = line.get("qty_base")
                if qty_base is not None:
                    total += float(qty_base) / float(factor)
                    used += 1
                else:
                    return ("abstain",
                            f"mat hang {product_key} thieu _qty_base nen "
                            "khong quy doi nguoc sang don vi hoi duoc",
                            used, warnings, "unit")
            continue
        return ("abstain",
                f"khong xac dinh duoc don vi co ban va he so quy doi "
                f"cho mat hang {product_key} voi don vi hoi "
                f"'{asked_unit}'", used, warnings, "unit")
    if used == 0 and lines_rev:
        return ("abstain",
                "co dong trong ky nhung toan bo deu thieu so luong "
                "(qty va qty_base deu None)", 0, warnings, "column")
    return ("ok", _num_quantity(total), used, warnings, None)


def _solve_quantity(parsed, view, lines, orders, rulebook, start, end, mode):
    """Giải measure quantity (tổng số lượng quy đổi trong kỳ).

    Shop không có dòng bán thì không có số lượng để tính -> abstain
    ``column`` (không dùng tổng đơn để đoán số lượng). Có ngưỡng tiền
    thì chỉ cộng số lượng của các đơn qua ngưỡng (doanh thu cấp đơn
    tính từ tổng dòng đã lọc, vì số lượng vốn đã gắn với dòng).
    """
    if not lines:
        return _abstain(parsed, start, end, "column",
                        "shop khong co dong ban nen khong co so lieu "
                        "so luong")
    filtered, other = _revenue_lines_mode(
        lines, parsed, rulebook, start, end, mode)
    filter_text = (f"tong so luong quy doi cua don revenue trong ky "
                   f"{_period_str(start)}..{_period_str(end)}")
    threshold = getattr(parsed, "threshold_money", None)
    if threshold is not None:
        ids, note = _threshold_order_ids(parsed, filtered, [], True)
        if ids == "unavailable":
            return _abstain(parsed, start, end, "column", note)
        filtered = [line for line in filtered
                    if line.get("order_id") in ids]
        filter_text += f"; {note}"
    if not filtered:
        confidence, notes = _confidence_for(other, [])
        return _answered(parsed, start, end, "number", 0, 0,
                         filter_text, None, confidence,
                         ["khong co dong revenue trong ky, tra 0"], notes)
    status, value, used, warnings, error_group = _quantity_total(
        filtered, view, parsed)
    if status == "abstain":
        return _abstain(parsed, start, end, error_group, value)
    confidence, notes = _confidence_for(other, [])
    return _answered(parsed, start, end, "number", value, used,
                     filter_text, None, confidence, warnings, notes)


def _solve_cancel_rate(parsed, view, lines, orders, rulebook, start, end,
                       mode):
    """Giải measure cancel_rate (tỉ lệ đơn huỷ trong kỳ, %).

    Mẫu số là MỌI đơn đặt trong kỳ (mọi trạng thái, kể cả ``other``),
    tử số là đơn huỷ (``cancelled``). Nguồn: orders khi lọc không cần
    mặt hàng/nhóm (orders là danh sách đơn đầy đủ nhất, gồm cả đơn
    không có dòng), lines khi lọc theo mặt hàng/nhóm (vì orders không
    có mặt hàng). Đếm theo mã đơn phân biệt ở cả tử và mẫu. Mẫu bằng 0
    thì tỉ lệ không xác định -> abstain ``filter`` (không trả 0%).
    Percent làm tròn 2 chữ số thập phân.
    """
    kind = getattr(parsed, "entity_kind", "none") or "none"
    if kind in ("product", "group") and lines:
        # Lọc theo mặt hàng/nhóm: phải qua lines, mọi trạng thái.
        period_lines = [
            line for line in lines
            if _in_period(line.get("date"), start, end)
            and _line_matches_entity_mode(line, parsed, mode)]
        all_ids = {line.get("order_id") for line in period_lines
                   if line.get("order_id") is not None}
        cancelled_ids = {line.get("order_id") for line in period_lines
                         if line.get("status_norm") == "cancelled"
                         and line.get("order_id") is not None}
        rows_used = len(period_lines)
        source = "lines"
    elif orders:
        period_orders = [
            order for order in orders
            if _in_period(order.get("date"), start, end)
            and _order_matches_entity_mode(order, parsed, mode)]
        all_ids = {order.get("order_id") for order in period_orders
                   if order.get("order_id") is not None}
        cancelled_ids = {order.get("order_id") for order in period_orders
                         if order.get("status_norm") == "cancelled"
                         and order.get("order_id") is not None}
        rows_used = len(period_orders)
        source = "orders"
    elif lines:
        period_lines = [
            line for line in lines
            if _in_period(line.get("date"), start, end)
            and _line_matches_entity_mode(line, parsed, mode)]
        all_ids = {line.get("order_id") for line in period_lines
                   if line.get("order_id") is not None}
        cancelled_ids = {line.get("order_id") for line in period_lines
                         if line.get("status_norm") == "cancelled"
                         and line.get("order_id") is not None}
        rows_used = len(period_lines)
        source = "lines"
    else:
        return _abstain(parsed, start, end, "column",
                        "shop khong co lines lan orders, khong tinh "
                        "duoc ti le huy")
    if not all_ids:
        return _abstain(parsed, start, end, "filter",
                        "khong co don nao dat trong ky nen ti le huy "
                        "khong xac dinh")
    percent = _round_percent(len(cancelled_ids), len(all_ids))
    filter_text = (f"ti le huy = {len(cancelled_ids)}/{len(all_ids)} "
                   f"don trong ky (nguon {source})")
    return _answered(parsed, start, end, "percent", percent, rows_used,
                     filter_text, None, 1.0, [], [])


def _solve_revenue_share(parsed, view, lines, orders, rulebook, start, end,
                         mode):
    """Giải measure revenue_share (thị phần doanh thu của thực thể, %).

    Tử số là doanh thu của thực thể hỏi trong kỳ, mẫu số là tổng doanh
    thu trong kỳ KHÔNG lọc thực thể (cùng nguồn lines/orders, cùng bộ
    lọc trạng thái revenue). Câu hỏi không có thực thể thì thị phần vô
    nghĩa -> abstain ``entity``. Mẫu bằng 0 thì thị phần không xác
    định -> abstain ``filter``; tử bằng 0 nhưng mẫu > 0 thì trả 0.0%
    (thực thể có trong shop nhưng không bán được trong kỳ).
    """
    if (getattr(parsed, "entity_kind", "none") or "none") == "none":
        return _abstain(parsed, start, end, "entity",
                        "cau revenue_share khong chi ro thuc the nao "
                        "can tinh thi phan")
    numerator, rows_num, other_num, _src = _revenue_value(
        parsed, view, lines, orders, rulebook, start, end, mode)
    plain_parsed = _copy_parsed(
        parsed, entity_kind="none", entity_key=None, entity_display=None)
    denominator, _rows_den, _other_den, _src2 = _revenue_value(
        plain_parsed, view, lines, orders, rulebook, start, end, "exact")
    if denominator == 0:
        return _abstain(parsed, start, end, "filter",
                        "tong doanh thu trong ky bang 0 nen thi phan "
                        "khong xac dinh")
    percent = _round_percent(numerator, denominator)
    confidence, notes = _confidence_for(other_num, [])
    filter_text = (f"thi phan = doanh thu thuc the / tong doanh thu "
                   f"trong ky ({numerator}/{denominator})")
    return _answered(parsed, start, end, "percent", percent, rows_num,
                     filter_text, None, confidence, [], notes)


def _solve_growth(parsed, view, lines, orders, rulebook, start, end, mode,
                  as_percent):
    """Giải growth_percent và growth_amount (tăng trưởng doanh thu).

    Kỳ hỏi phải có cả start và end (thiếu kỳ thì không biết so với kỳ
    nào -> abstain ``time``). Kỳ so tính bằng ``_previous_period``:
    tháng đầy đủ so tháng trước đầy đủ, tháng này (từ đầu tháng đến
    giữa tháng) so tháng trước cùng số ngày, kỳ khác so khoảng liền
    trước cùng độ dài. growth_amount = mới - cũ (nguyên VND).
    growth_percent = (mới - cũ)/cũ*100, làm tròn 2 chữ số; cũ bằng 0
    và mới bằng 0 thì trả 0.0%, cũ bằng 0 mà mới > 0 thì phần trăm
    không xác định -> abstain ``filter`` (không bịa số lớn).
    """
    if start is None or end is None:
        return _abstain(parsed, start, end, "time",
                        "cau tang truong can ky hoi day du (start va "
                        "end) de xac dinh ky so sanh")
    current, rows_cur, other_cur, _src = _revenue_value(
        parsed, view, lines, orders, rulebook, start, end, mode)
    prev_start, prev_end = _previous_period(start, end)
    prev_parsed = _copy_parsed(
        parsed, period_start=prev_start, period_end=prev_end)
    previous, _rows_prev, _other_prev, _src2 = _revenue_value(
        prev_parsed, view, lines, orders, rulebook,
        prev_start, prev_end, mode)
    confidence, notes = _confidence_for(other_cur, [])
    filter_text = (f"tang truong: ky hoi {start}..{end} so voi ky "
                   f"truoc {prev_start}..{prev_end}")
    if as_percent:
        if previous == 0:
            if current == 0:
                return _answered(parsed, start, end, "percent", 0.0,
                                 rows_cur, filter_text, None, confidence,
                                 [], notes)
            return _abstain(
                parsed, start, end, "filter",
                "doanh thu ky truoc bang 0, ky hoi > 0 nen phan tram "
                "tang truong khong xac dinh (chia cho 0)")
        percent = _round_percent(current - previous, previous)
        return _answered(parsed, start, end, "percent", percent,
                         rows_cur, filter_text, None, confidence, [],
                         notes)
    amount = current - previous
    return _answered(parsed, start, end, "number", amount, rows_cur,
                     filter_text, None, confidence, [], notes)


# ---------------------------------------------------------------------------
# Xếp hạng, ngày/tháng tốt nhất, nhóm doanh thu
# ---------------------------------------------------------------------------


def _format_ranking_answer(parsed, ranked, columns):
    """Đóng gói đáp án xếp hạng theo answer_type của câu hỏi.

    Đầu vào: ``ranked`` là danh sách (key, giá_trị, display) đã xếp
    theo tiêu chí giảm dần, hoà theo key tăng dần; ``columns`` là tên
    hai cột cho đáp án table (cột tên, cột giá trị).
    Đầu ra: (entry_type, answer).
        - answer_type ``table`` -> table với columns đã cho, các dòng
          là [display, giá trị], giới hạn theo ranking_k nếu có (None
          thì lấy toàn bộ danh sách đã xếp).
        - answer_type ``list`` -> list các display theo thứ tự xếp
          hạng, giới hạn theo ranking_k nếu có.
        - answer_type ``string`` -> display của hạng nhất.
        - answer_type khác/``unknown`` -> mặc định: ranking_k là 1 hoặc
          None thì string hạng nhất, ngược lại thì list.
    Giá trị tiền đã là int VND, giá trị số lượng đã chuẩn hoá bằng
    ``_num_quantity`` ở caller, nên ở đây giữ nguyên giá trị.
    """
    k = getattr(parsed, "ranking_k", None)
    answer_type = getattr(parsed, "answer_type", "unknown") or "unknown"
    if answer_type == "table":
        rows = [[display, value] for (_key, value, display) in ranked]
        if k is not None:
            rows = rows[:k]
        return "table", {"columns": list(columns), "rows": rows}
    if answer_type == "list":
        displays = [display for (_key, _value, display) in ranked]
        if k is not None:
            displays = displays[:k]
        return "list", displays
    if answer_type == "string":
        return "string", ranked[0][2] if ranked else ""
    # Mặc định khi parser không rõ dạng đáp án.
    if k is None or k == 1:
        return "string", ranked[0][2] if ranked else ""
    displays = [display for (_key, _value, display) in ranked]
    return "list", displays[:k]


def _solve_top(parsed, view, lines, orders, rulebook, start, end, mode,
               target):
    """Giải top_product và top_customer theo chỉ tiêu xếp hạng.

    Sửa tích hợp Giai đoạn 2 (bắt buộc, sửa lỗi solver cũ): solver cũ
    xếp MỌI top_* theo doanh thu vì ParsedQuestion chưa có trường
    chỉ tiêu. Nay hàm đọc ``parsed.ranking_metric`` (parser đã gán):

    - ``'quantity'`` (mặc định cho top_product, "bán chạy nhất"):
      xếp theo tổng ``_qty_base`` (số lượng quy đổi). Luật thiếu hệ
      số giống ``_quantity_total``: dòng thiếu ``_qty_base`` mà mặt
      hàng nằm trong ``conversion_products`` của view thì abstain
      ``unit`` toàn câu (không đoán hệ số 1); mặt hàng không có bảng
      quy đổi nào thì cộng ``_qty`` theo rulebook.
    - ``'revenue'`` (mặc định cho top_customer): xếp theo tổng tiền
      dòng revenue, như solver cũ.
    - ``'profit'`` (hàng lời/lãi nhất): xếp theo tổng
      ``line_amount - qty * cost_price``. Giả định ghi rõ: giá vốn
      ``_cost_price`` ghi theo đơn vị bán của dòng (cùng đơn vị với
      ``_qty``, vì cột giá vốn nằm trên bảng dòng cùng với số lượng
      và đơn giá bán); dòng thiếu giá vốn hoặc thiếu tiền dòng thì bỏ
      qua, và nếu không có dòng nào tính được lợi nhuận thì abstain
      ``column`` (shop không có giá vốn dùng được). Giai đoạn 3 cần
      rà lại giả định này nếu rulebook mới định nghĩa giá vốn theo
      đơn vị cơ bản.
    - ParsedQuestion cũ (không có trường ranking_metric, ví dụ test
      tự tạo) thì dự phòng: product -> ``'quantity'``,
      customer -> ``'revenue'``, để hành vi cũ theo doanh thu cho
      top_product không quay lại một cách lặng lẽ.

    Cách xếp chung: chỉ tiêu giảm dần, hoà thì key (mã/tên) tăng dần.
    Nguồn lines khi có dòng; nguồn orders khi shop không có dòng và
    target là customer (đơn vẫn có khách, chỉ xếp được theo doanh
    thu). top_product mà shop không có dòng thì abstain ``column``.
    Thực thể lọc thêm (ví dụ top_product trong một nhóm) được áp dụng
    trước khi nhóm, qua bộ lọc revenue đã có entity.
    """
    metric = getattr(parsed, "ranking_metric", None)
    if not metric:
        metric = "quantity" if target == "product" else "revenue"
    if target == "product" and not lines:
        return _abstain(parsed, start, end, "column",
                        "shop khong co dong ban nen khong xep hang "
                        "san pham duoc")
    if not lines and metric != "revenue":
        return _abstain(parsed, start, end, "column",
                        "shop khong co dong ban nen khong xep hang "
                        f"theo {metric} duoc")
    conversion_products = {
        _norm(key)
        for key in (getattr(view, "conversion_products", set()) or set())}
    if lines:
        filtered, other = _revenue_lines_mode(
            lines, parsed, rulebook, start, end, mode)
        groups = {}
        for line in filtered:
            if target == "product":
                key = line.get("product_key")
                display_raw = line.get("product_display")
            else:
                key = line.get("customer_key")
                display_raw = line.get("customer_display")
            if key is None:
                continue
            if metric == "quantity":
                qty_base = line.get("qty_base")
                if qty_base is not None:
                    value = float(qty_base)
                elif line.get("qty") is not None:
                    if _norm(key) in conversion_products:
                        return _abstain(
                            parsed, start, end, "unit",
                            f"mat hang {key} co bang quy doi nhung dong "
                            "thieu _qty_base, khong xep hang theo so "
                            "luong duoc (khong doan he so 1)")
                    value = float(line["qty"])
                else:
                    continue
            elif metric == "profit":
                if line.get("line_amount") is None \
                        or line.get("cost_price") is None:
                    continue
                qty = line.get("qty")
                if qty is None:
                    qty = line.get("qty_base")
                if qty is None:
                    continue
                value = float(line["line_amount"]) \
                    - float(qty) * float(line["cost_price"])
            else:  # revenue
                if line.get("line_amount") is None:
                    continue
                value = float(line["line_amount"])
            if key not in groups:
                groups[key] = [0.0, display_raw or key]
            groups[key][0] += value
        source = "lines"
        rows_used = len(filtered)
    else:
        filtered, other = _revenue_orders_mode(
            orders, parsed, rulebook, start, end, mode)
        groups = {}
        for order in filtered:
            key = order.get("customer_key")
            if key is None:
                continue
            if key not in groups:
                groups[key] = [0.0, order.get("customer_display") or key]
            if order.get("order_total") is not None:
                groups[key][0] += float(order["order_total"])
        source = "orders"
        rows_used = len(filtered)
    # Dự phòng cho top_customer theo doanh thu: dòng bán thiếu khách
    # nhưng đơn có khách (join thất bại) thì nhóm từ orders sẽ đầy đủ
    # hơn. Chỉ áp dụng khi metric là revenue, vì orders không có số
    # lượng hay giá vốn để xếp theo quantity/profit.
    if target == "customer" and metric == "revenue" and not groups and orders:
        filtered_o, other_o = _revenue_orders_mode(
            orders, parsed, rulebook, start, end, mode)
        for order in filtered_o:
            key = order.get("customer_key")
            if key is None:
                continue
            if key not in groups:
                groups[key] = [0, order.get("customer_display") or key]
            if order.get("order_total") is not None:
                groups[key][0] += float(order["order_total"])
        other = other or other_o
        rows_used = rows_used + len(filtered_o)
    if not groups:
        if metric == "profit":
            return _abstain(parsed, start, end, "column",
                            "khong co dong nao tinh duoc loi nhuan "
                            "(thieu gia von hoac tien dong) de xep hang")
        return _abstain(parsed, start, end, "filter",
                        "khong co nhom nao de xep hang trong ky "
                        "(khong co dong revenue hoac thieu key nhom)")
    items = getattr(view, "products", []) if target == "product" \
        else getattr(view, "customers", [])
    ranked = []
    for key, (total, display_raw) in groups.items():
        display = _display_for_key(items, key)
        if display == key and display_raw:
            display = display_raw
        if metric == "quantity":
            value = _num_quantity(total)
        else:
            value = _to_vnd_int(total)
        ranked.append((key, value, display))
    # Xếp hạng: chỉ tiêu giảm dần, hoà thì key tăng dần (so chuỗi
    # thô của key để ổn định, không phụ thuộc locale).
    ranked.sort(key=lambda item: (-item[1], str(item[0])))
    _metric_column = {"quantity": "so_luong", "profit": "loi_nhuan"}.get(
        metric, "doanh_thu")
    entry_type, answer = _format_ranking_answer(
        parsed, ranked,
        ["san_pham", _metric_column] if target == "product"
        else ["khach_hang", _metric_column])
    confidence, notes = _confidence_for(other, [])
    filter_text = (f"xep hang {target} theo {metric} trong ky "
                   f"(nguon {source})")
    return _answered(parsed, start, end, entry_type, answer, rows_used,
                     filter_text, ranked[0][0], confidence, [], notes)


def _solve_best(parsed, view, lines, orders, rulebook, start, end, mode,
                by_month):
    """Giải best_day và best_month (ngày/tháng doanh thu cao nhất).

    Nhóm doanh thu revenue theo ngày (best_day) hoặc theo tháng
    (best_month) trong kỳ, lấy nhóm có tổng lớn nhất. Hoà thì lấy
    ngày/tháng SỚM NHẤT và ghi rõ trong evidence (vì ngày/tháng không
    có mã để hoà theo mã như xếp hạng sản phẩm; contract ghi rõ điểm
    này cần Giai đoạn 3 rà lại nếu rulebook mới nói khác, nhưng test
    set đã loại câu hoà ở biên nên ảnh hưởng nhỏ). Không có nhóm nào
    (không có doanh thu trong kỳ) thì abstain ``filter``.
    """
    if lines:
        filtered, other = _revenue_lines_mode(
            lines, parsed, rulebook, start, end, mode)
        totals = {}
        for line in filtered:
            day = line.get("date")
            if day is None or line.get("line_amount") is None:
                continue
            key = (day.year, day.month) if by_month else day
            totals[key] = totals.get(key, 0.0) + float(line["line_amount"])
        rows_used = len(filtered)
    else:
        filtered, other = _revenue_orders_mode(
            orders, parsed, rulebook, start, end, mode)
        totals = {}
        for order in filtered:
            day = order.get("date")
            if day is None or order.get("order_total") is None:
                continue
            key = (day.year, day.month) if by_month else day
            totals[key] = totals.get(key, 0.0) + float(order["order_total"])
        rows_used = len(filtered)
    if not totals:
        return _abstain(parsed, start, end, "filter",
                        "khong co doanh thu trong ky de chon ngay/thang "
                        "tot nhat")
    best_value = max(totals.values())
    # Hoà thì lấy key sớm nhất: min trên các key có tổng bằng max.
    best_key = min(key for key, value in totals.items()
                   if value == best_value)
    if by_month:
        answer = f"{best_key[0]:04d}-{best_key[1]:02d}"
        group_key = answer
        filter_text = f"thang co doanh thu cao nhat trong ky (hoa lay thang som nhat)"
    else:
        answer = best_key.isoformat()
        group_key = answer
        filter_text = f"ngay co doanh thu cao nhat trong ky (hoa lay ngay som nhat)"
    confidence, notes = _confidence_for(other, [])
    return _answered(parsed, start, end, "date", answer, rows_used,
                     filter_text, group_key, confidence, [], notes)


def _solve_revenue_by(parsed, view, lines, orders, rulebook, start, end,
                      mode, dimension):
    """Giải revenue_by_payment/group/channel (bảng doanh thu theo nhóm).

    Nhóm doanh thu revenue theo phương thức thanh toán, nhóm mặt hàng
    hoặc kênh bán trong kỳ. Dòng thiếu key nhóm thì bỏ qua (không bịa
    nhóm); nếu mọi dòng đều thiếu key nhóm thì abstain ``column`` (vì
    shop không có dữ liệu nhóm đó), còn nếu kỳ không có dòng nào thì
    abstain ``filter``. Bảng xếp theo doanh thu giảm dần, hoà theo key
    nhóm tăng dần, cột theo thứ tự [tên nhóm, doanh thu].
    """
    if dimension == "group" and not lines:
        return _abstain(parsed, start, end, "column",
                        "shop khong co dong ban nen khong nhom doanh "
                        "thu theo nhom san pham duoc")
    if lines:
        filtered, other = _revenue_lines_mode(
            lines, parsed, rulebook, start, end, mode)
        key_field = {"payment": "payment", "group": "product_group",
                     "channel": "channel"}[dimension]
        totals = {}
        for line in filtered:
            key = line.get(key_field)
            if key is None or line.get("line_amount") is None:
                continue
            totals[key] = totals.get(key, 0.0) + float(line["line_amount"])
        rows_used = len(filtered)
    else:
        filtered, other = _revenue_orders_mode(
            orders, parsed, rulebook, start, end, mode)
        key_field = {"payment": "payment", "channel": "channel"}[dimension]
        totals = {}
        for order in filtered:
            key = order.get(key_field)
            if key is None or order.get("order_total") is None:
                continue
            totals[key] = totals.get(key, 0.0) + float(order["order_total"])
        rows_used = len(filtered)
    if not filtered:
        return _abstain(parsed, start, end, "filter",
                        "khong co dong revenue trong ky de nhom")
    if not totals:
        return _abstain(parsed, start, end, "column",
                        f"moi dong trong ky deu thieu {dimension}, "
                        "shop khong co du lieu nhom nay")
    ranked = sorted(totals.items(),
                    key=lambda item: (-item[1], str(item[0])))
    columns = {"payment": ["phuong_thuc_thanh_toan", "doanh_thu"],
               "group": ["nhom", "doanh_thu"],
               "channel": ["kenh", "doanh_thu"]}[dimension]
    rows = [[key, _to_vnd_int(total)] for key, total in ranked]
    confidence, notes = _confidence_for(other, [])
    return _answered(parsed, start, end, "table",
                     {"columns": columns, "rows": rows}, rows_used,
                     f"doanh thu theo {dimension} trong ky", None,
                     confidence, [], notes)


def _solve_quantity_by_unit(parsed, view, lines, orders, rulebook, start,
                            end, mode):
    """Giải quantity_by_unit (bảng số lượng theo đơn vị bán).

    Nhóm các dòng revenue trong kỳ theo đơn vị bán ghi trên dòng
    (``_line_unit``) và cộng SỐ LƯỢNG GỐC (``_qty``, chưa quy đổi) của
    từng nhóm. Cộng số gốc chứ không cộng số quy đổi, vì bảng này hỏi
    "theo đơn vị" nên mỗi nhóm phải giữ đúng đơn vị của nó; cộng số
    quy đổi (đơn vị cơ bản) vào nhóm đơn vị bán sẽ trộn hai đơn vị với
    nhau. Dòng thiếu đơn vị hoặc thiếu số lượng thì bỏ qua và ghi vào
    evidence; nếu mọi dòng đều thiếu đơn vị thì abstain ``column``.
    """
    if not lines:
        return _abstain(parsed, start, end, "column",
                        "shop khong co dong ban nen khong nhom so "
                        "luong theo don vi duoc")
    filtered, other = _revenue_lines_mode(
        lines, parsed, rulebook, start, end, mode)
    if not filtered:
        return _abstain(parsed, start, end, "filter",
                        "khong co dong revenue trong ky de nhom")
    totals = {}
    for line in filtered:
        unit = line.get("line_unit")
        if unit is None or line.get("qty") is None:
            continue
        totals[unit] = totals.get(unit, 0.0) + float(line["qty"])
    if not totals:
        return _abstain(parsed, start, end, "column",
                        "moi dong trong ky deu thieu don vi hoac thieu "
                        "so luong goc")
    ranked = sorted(totals.items(),
                    key=lambda item: (-item[1], str(item[0])))
    rows = [[unit, _num_quantity(total)] for unit, total in ranked]
    confidence, notes = _confidence_for(other, [])
    return _answered(parsed, start, end, "table",
                     {"columns": ["don_vi", "so_luong"], "rows": rows},
                     len(filtered),
                     "so luong goc theo don vi ban cua don revenue "
                     "trong ky", None, confidence, [], notes)


# ---------------------------------------------------------------------------
# Công nợ
# ---------------------------------------------------------------------------


def _is_cod_or_marketplace(payment_norm):
    """Kiểm tra phương thức thanh toán có phải COD/sàn TMĐT không.

    Công nợ loại COD và sàn TMĐT vì các đơn đó thu tiền ngay qua sàn
    hoặc khi giao hàng, không phải nợ phải thu của shop. Nhận diện
    bằng khoá chuẩn hoá (không dấu): chứa ``cod``, ``shopee``,
    ``lazada``, ``tiki``, ``tiktok``, cụm ``cash on delivery``, hoặc
    đúng bằng ``san``/chứa ``san tm`` (sàn thương mại điện tử). Phương
    thức ``debt`` (ghi nợ) thì KHÔNG loại, vì đó chính là đơn nợ.
    Phương thức trống (None -> ``''``) thì không loại, vì không có căn
    cứ nói nó là COD/sàn.
    """
    if not payment_norm:
        return False
    if "cod" in payment_norm:
        return True
    if "cash on delivery" in payment_norm:
        return True
    for token in ("shopee", "lazada", "tiki", "tiktok"):
        if token in payment_norm:
            return True
    if payment_norm == "san" or "san tm" in payment_norm:
        return True
    return False


def _debt_snapshot(view, lines, orders, snapshot):
    """Tính công nợ tại một ngày, theo từng khách và tổng.

    Trả (tổng_nợ_int, dict_khách, nguồn_tiền_trả, warnings,
    cờ_không_tính_được, số_đơn_xét). Dict khách: key -> [nợ_int,
    display]. Cờ không tính được True khi có ít nhất một đơn trong
    diện mà không tính được nợ (thiếu cả tổng, tiền trả và tiền nợ),
    để caller abstain toàn câu thay vì trả tổng thiếu.

    Các bước, đúng contract mục 3 cho debt_*:
        1. Chỉ xét đơn đặt đến hết ngày snapshot (``_date <= X``) và
           không huỷ (kể cả đơn trả nếu còn nợ, vì trả hàng chưa chắc
           đã hết nợ; đơn huỷ thì coi như không phát sinh nợ).
        2. Loại đơn COD/sàn TMĐT (xem ``_is_cod_or_marketplace``).
        3. Tổng đơn: ``_order_total`` của đơn, nếu thiếu thì tổng tiền
           dòng của đơn đó (mọi trạng thái dòng, vì tổng đơn là tổng
           các dòng của nó).
        4. Tiền trả đến hết ngày X:
           - Nếu shop có bảng thu theo ngày (``view.payments`` không
             rỗng): cộng các lần thu của đơn có ngày thu <= X. Đây là
             nguồn chính xác nhất cho "tại ngày X".
           - Nếu không có bảng thu: dùng ảnh chụp cuối kỳ của đơn:
             ``_amount_due`` nếu có (chính là số nợ còn lại theo sổ),
             ngược lại ``tổng - _amount_paid``. Trường hợp này evidence
             ghi rõ ``snapshot`` và trừ confidence, vì tiền trả sau
             ngày X (nếu có) cũng đã nằm trong ảnh chụp, làm nợ tại X
             bị tính thấp hơn thực tế.
        5. Chỉ tính đơn còn nợ > 0 (nợ bằng 0 hoặc âm do trả thừa thì
           không phải nợ phải thu).
    """
    # Tổng tiền dòng theo đơn, để điền tổng đơn khi đơn thiếu tổng.
    line_sum_by_order = {}
    for line in lines:
        oid = line.get("order_id")
        if oid is None or line.get("line_amount") is None:
            continue
        line_sum_by_order[oid] = line_sum_by_order.get(oid, 0.0) \
            + float(line["line_amount"])
    # Tiền trả theo ngày, theo đơn.
    payments_df = getattr(view, "payments", None)
    dated_paid = {}
    has_dated_payments = False
    if payments_df is not None and len(payments_df) > 0:
        pay_ids = _column_list(payments_df, "_order_id", len(payments_df))
        pay_dates = _column_list(payments_df, "_date", len(payments_df))
        pay_amounts = _column_list(payments_df, "_amount", len(payments_df))
        for oid_v, date_v, amount_v in zip(pay_ids, pay_dates, pay_amounts):
            oid = None if _is_blank(oid_v) else str(oid_v).strip()
            pay_date = _as_date(date_v)
            if oid is None or pay_date is None or _is_blank(amount_v):
                continue
            if pay_date <= snapshot:
                dated_paid[oid] = dated_paid.get(oid, 0.0) \
                    + float(amount_v)
        has_dated_payments = True
    per_customer = {}
    total_debt = 0
    considered = 0
    uncomputable = False
    warnings = []
    paid_source = "dated_payments" if has_dated_payments else "snapshot"
    if has_dated_payments:
        warnings.append("tien tra tinh theo bang thu co ngay (den het "
                        "ngay snapshot)")
    else:
        warnings.append("khong co bang thu theo ngay, dung anh chup "
                        "tien tra/tien no cuoi ky cua don (snapshot)")
    for order in orders:
        oid = order.get("order_id")
        order_date = order.get("date")
        if order_date is None or order_date > snapshot:
            continue
        if order.get("status_norm") == "cancelled":
            continue
        if _is_cod_or_marketplace(_norm(order.get("payment"))):
            continue
        considered += 1
        total = order.get("order_total")
        if total is None:
            total = line_sum_by_order.get(oid)
        if has_dated_payments:
            if total is None:
                uncomputable = True
                continue
            paid = dated_paid.get(oid, 0.0)
            debt = float(total) - paid
        else:
            due = order.get("amount_due")
            if due is not None:
                debt = float(due)
            elif total is not None and order.get("amount_paid") is not None:
                debt = float(total) - float(order["amount_paid"])
            else:
                uncomputable = True
                continue
        if debt <= 0:
            continue
        debt_int = _to_vnd_int(debt)
        if debt_int <= 0:
            continue
        total_debt += debt_int
        cust_key = order.get("customer_key")
        cust_display = order.get("customer_display") or cust_key
        if cust_key is None:
            # Đơn nợ nhưng không biết khách: vẫn tính vào tổng nợ,
            # nhưng không xếp hạng được theo khách (caller tự loại
            # khỏi bảng xếp hạng khách, tổng thì giữ nguyên).
            continue
        if cust_key not in per_customer:
            per_customer[cust_key] = [0, cust_display]
        per_customer[cust_key][0] += debt_int
    return (total_debt, per_customer, paid_source, warnings,
            uncomputable, considered)


def _solve_debt(parsed, view, lines, orders, rulebook, mode, top):
    """Giải debt_total và debt_top (công nợ tại ngày snapshot).

    Ngày snapshot: ``period_end`` nếu có, ngược lại ``period_start``,
    ngược lại ngày muộn nhất của shop. Kỳ snapshot mà parser cho dạng
    (X, X) thì cả hai đều là X nên cùng kết quả. Shop không có orders
    thì abstain ``column`` (không có đơn thì không có công nợ để
    tính, khác với tổng nợ 0 khi có đơn nhưng không ai nợ).
    debt_total trả tổng nợ (0 khi không ai nợ nhưng có đơn trong diện).
    debt_top xếp khách theo nợ giảm dần, hoà theo key khách tăng dần;
    dạng đáp án theo answer_type: table thì cột đúng như nhiệm vụ yêu
    cầu là ``['khach_hang', 'so_tien_no']``, list thì list tên khách,
    string thì tên khách nợ nhiều nhất. Không có khách nợ nào thì
    abstain ``filter`` (không có ai để xếp hạng).
    """
    if not orders:
        start = getattr(parsed, "period_start", None)
        end = getattr(parsed, "period_end", None)
        return _abstain(parsed, start, end, "column",
                        "shop khong co orders nen khong tinh duoc "
                        "cong no")
    snapshot = getattr(parsed, "period_end", None) \
        or getattr(parsed, "period_start", None) \
        or getattr(view, "date_max", None)
    if snapshot is None:
        start = getattr(parsed, "period_start", None)
        end = getattr(parsed, "period_end", None)
        return _abstain(parsed, start, end, "time",
                        "khong xac dinh duoc ngay snapshot cho cau "
                        "cong no (khong co ky, khong co date_max)")
    total, per_customer, paid_source, warnings, uncomputable, considered \
        = _debt_snapshot(view, lines, orders, snapshot)
    if uncomputable:
        return _abstain(parsed, snapshot, snapshot, "column",
                        "co don trong dien cong no nhung khong tinh "
                        "duoc no (thieu tong don, tien tra va tien no), "
                        "khong tra tong thieu")
    extra = []
    if paid_source == "snapshot":
        extra.append((0.1, "cong no dung anh chup tien tra cuoi ky "
                           "thay cho tien tra theo ngay, tru 0.1"))
    confidence, notes = _confidence_for(False, extra)
    filter_text = (f"cong no tai ngay {snapshot} (don khong huy, "
                   f"loai COD/san, chi don no > 0, nguon tra: "
                   f"{paid_source})")
    if not top:
        return _answered(parsed, snapshot, snapshot, "number", total,
                         considered, filter_text, None, confidence,
                         warnings, notes)
    if not per_customer:
        return _abstain(parsed, snapshot, snapshot, "filter",
                        "khong co khach nao con no tai ngay snapshot "
                        "nen khong xep hang duoc")
    items = getattr(view, "customers", []) or []
    ranked = []
    for key, (debt_value, display_raw) in per_customer.items():
        display = _display_for_key(items, key)
        if display == key and display_raw:
            display = display_raw
        ranked.append((key, debt_value, display))
    ranked.sort(key=lambda item: (-item[1], str(item[0])))
    entry_type, answer = _format_ranking_answer(
        parsed, ranked, ["khach_hang", "so_tien_no"])
    return _answered(parsed, snapshot, snapshot, entry_type, answer,
                     considered, filter_text, ranked[0][0], confidence,
                     warnings, notes)


# ---------------------------------------------------------------------------
# Giá ngày, tăng liên tục và hàm solve chính
# ---------------------------------------------------------------------------


def _solve_price_on_day(parsed, view, lines, orders, rulebook, mode):
    """Giải price_on_day (đơn giá một đơn vị cơ bản tại ngày D).

    Ngày D là ``period_start``; nếu parser cho kỳ nhiều ngày (start
    khác end) thì abstain ``time`` vì giá ngày cần đúng một ngày. Cần
    thực thể mặt hàng (entity_kind ``product``), thiếu thì abstain
    ``entity``. Tính CẢ dòng của đơn huỷ/trả (không lọc revenue), ngày
    đặt đúng bằng D, vì giá bán ngày đó không phụ thuộc việc đơn sau
    đó có bị huỷ/trả hay không.
    Đơn giá cho một đơn vị cơ bản, TRƯỚC chiết khấu dòng:
        - Nếu có đơn giá dòng (``_unit_price``): giá cơ bản bằng đơn
          giá chia hệ số (số đơn vị cơ bản trong một đơn vị bán =
          ``_qty_base / _qty``). Ví dụ bán theo thùng giá 560.000,
          một thùng quy ra 20 đơn vị lẻ thì giá cơ bản là 28.000.
        - Nếu không có đơn giá dòng: giá cơ bản bằng (tiền dòng +
          chiết khấu dòng, tức tiền trước chiết khấu) chia ``_qty_base``.
        - Dòng thiếu ``_qty_base``: mặt hàng có bảng quy đổi mà không
          tra được hệ số -> abstain ``unit``; mặt hàng không có dòng
          quy đổi nào -> hệ số 1 theo rulebook (ghi rõ trong warnings).
    Nhiều dòng cùng ngày có giá khác nhau (giá thay đổi trong ngày)
    thì lấy giá xuất hiện nhiều nhất (mode); hoà thì lấy giá của dòng
    xuất hiện sớm nhất trong file (thứ tự lines gốc), để kết quả ổn
    định. Giá trả về là int nếu nguyên, ngược lại float làm tròn 2 chữ
    số thập phân.
    """
    day = getattr(parsed, "period_start", None)
    end = getattr(parsed, "period_end", None)
    if day is None:
        return _abstain(parsed, day, end, "time",
                        "cau gia ngay can ngay D cu the (period_start), "
                        "nhung parser khong cho ngay")
    if end is not None and end != day:
        return _abstain(parsed, day, end, "time",
                        "cau gia ngay can dung mot ngay, nhung ky hoi "
                        "trai dai nhieu ngay")
    if (getattr(parsed, "entity_kind", "none") or "none") != "product":
        return _abstain(parsed, day, day, "entity",
                        "cau gia ngay can mat hang cu the (entity_kind "
                        "product)")
    if not lines:
        return _abstain(parsed, day, day, "column",
                        "shop khong co dong ban nen khong co gia ngay")
    day_lines = [
        line for line in lines
        if line.get("date") == day
        and _line_matches_entity_mode(line, parsed, mode)]
    if not day_lines:
        return _abstain(parsed, day, day, "filter",
                        "khong co dong ban mat hang do trong ngay D")
    base_by_product = {}
    for key, base in (getattr(view, "base_unit_by_product", {}) or {}).items():
        base_by_product[_norm(key)] = _norm(base)
    conversion_products = {
        _norm(key)
        for key in (getattr(view, "conversion_products", set()) or set())}
    values = []
    warnings = []
    for line in day_lines:
        qty = line.get("qty")
        qty_base = line.get("qty_base")
        unit_price = line.get("unit_price")
        amount = line.get("line_amount")
        discount = line.get("discount") or 0.0
        norm_key = _norm(line.get("product_key"))
        factor = None
        if qty is not None and qty_base is not None and qty != 0:
            factor = float(qty_base) / float(qty)
        elif qty_base is None and qty is not None:
            if norm_key in conversion_products:
                return _abstain(
                    parsed, day, day, "unit",
                    "dong trong ngay D thieu _qty_base ma mat hang co "
                    "bang quy doi, khong doan he so 1 de tinh gia co ban")
            factor = 1.0
            warnings.append("mat hang khong co dong quy doi nao, ap "
                            "he so 1 theo rulebook de tinh gia co ban")
        per_base = None
        if unit_price is not None and factor is not None and factor != 0:
            per_base = float(unit_price) / float(factor)
        elif amount is not None and qty_base is not None and qty_base != 0:
            per_base = (float(amount) + float(discount)) / float(qty_base)
        elif amount is not None and qty is not None and qty != 0:
            per_base = float(amount) / float(qty)
        if per_base is not None:
            values.append(round(per_base, 2))
    if not values:
        return _abstain(parsed, day, day, "column",
                        "co dong trong ngay D nhung khong dong nao "
                        "tinh duoc don gia co ban (thieu gia va tien)")
    # Mode: giá xuất hiện nhiều nhất; hoà thì Counter.most_common giữ
    # thứ tự xuất hiện đầu tiên (dòng sớm nhất trong file) do tính ổn
    # định của sắp xếp Python, đúng luật tie-break đã ghi.
    most_common = _Counter(values).most_common(1)[0][0]
    answer = int(most_common) if float(most_common).is_integer() \
        else most_common
    return _answered(parsed, day, day, "number", answer, len(day_lines),
                     f"don gia co ban truoc chiet khau cua mat hang "
                     f"trong ngay {day} (tinh ca don huy/tra)",
                     None, 1.0, warnings, [])


def _solve_continuous_growth(parsed, view, lines, orders, rulebook, mode,
                             start, end):
    """Giải continuous_growth (doanh thu tăng liên tục 3 tháng?).

    Dùng danh sách tháng ``period_months`` của parser; nếu parser không
    cho đủ 3 tháng nhưng kỳ hỏi trải dài từ 3 tháng trở lên thì suy
    danh sách tháng từ kỳ hỏi (tháng của start đến tháng của end).
    Ít hơn 3 tháng thì abstain ``time`` (không đủ tháng để kết luận
    tăng liên tục). Doanh thu mỗi tháng tính đầy đủ tháng dương lịch
    (không cắt theo ngày trong kỳ), cùng bộ lọc thực thể và trạng thái
    revenue như các measure doanh thu khác. Kết quả True khi doanh thu
    tăng nghiêm ngặt qua từng tháng liên tiếp (tháng sau > tháng
    trước, không chấp nhận bằng nhau), False trong các trường hợp còn
    lại. Tháng không có doanh thu thì tính là 0 (dữ liệu thật, không
    phải thiếu), nên nó sẽ phá vỡ chuỗi tăng nếu đứng giữa.
    """
    months = list(getattr(parsed, "period_months", None) or [])
    if len(months) < 3 and start is not None and end is not None:
        derived = []
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            derived.append((year, month))
            if month == 12:
                year, month = year + 1, 1
            else:
                month += 1
        if len(derived) >= 3:
            months = derived
    if len(months) < 3:
        return _abstain(parsed, start, end, "time",
                        "cau tang lien tuc can it nhat 3 thang "
                        "(period_months hoac ky hoi dai tu 3 thang), "
                        "nhung khong du thang")
    revenues = []
    rows_total = 0
    for year, month in months:
        month_start, month_end = _month_range(year, month)
        month_parsed = _copy_parsed(
            parsed, period_start=month_start, period_end=month_end,
            period_kind="absolute")
        value, rows_used, _other, _src = _revenue_value(
            month_parsed, view, lines, orders, rulebook,
            month_start, month_end, mode)
        revenues.append(value)
        rows_total += rows_used
    increasing = all(
        revenues[idx + 1] > revenues[idx]
        for idx in range(len(revenues) - 1))
    filter_text = (f"doanh thu cac thang {months} = {revenues}, "
                   "tang nghiem ngat qua tung thang lien tiep?")
    return _answered(parsed, start, end, "boolean", bool(increasing),
                     rows_total, filter_text, None, 1.0, [], [])


def _cached_records(view):
    """Trả (lines, orders) đã trích của view, dùng lại cho mọi câu hỏi.

    Sửa tích hợp Giai đoạn 2 (chỉ tối ưu hiệu năng, KHÔNG đổi luật
    tính): trước đây ``_solve_impl`` gọi ``_line_records`` /
    ``_order_records`` cho MỖI câu hỏi, tức trích lại toàn bộ bảng
    (hàng trăm nghìn dòng, mỗi ô đi qua ``_is_blank``) cho mỗi câu —
    một shop 119 câu phải trích 119 lần, đợt chạy toàn bộ không thể
    xong trong thời gian hợp lý. Nay bản ghi được trích một lần rồi
    gắn vào chính view dưới thuộc tính riêng
    ``_solver_records_cache`` (tên có tiền tố ``_`` nên ``cache.py``
    không đưa nó vào ``extras`` khi lưu cache, và view được runner
    giải phóng sau mỗi shop nên không rò rỉ sang shop khác).

    An toàn của cache này: bản ghi chỉ suy ra từ ``view.lines`` /
    ``view.orders`` và pipeline không sửa hai bảng đó trong lúc giải
    các câu của shop, nên dùng lại là đúng; nếu caller tự sửa bảng
    giữa chừng thì phải xoá thuộc tính này (hoặc dùng view mới).
    View không cho gán thuộc tính thì hàm rơi về trích mới mỗi lần
    (chậm như cũ nhưng vẫn đúng), không crash.
    """
    cached = getattr(view, "_solver_records_cache", None)
    if cached is not None:
        return cached
    lines = _line_records(view)
    orders = _order_records(view)
    try:
        setattr(view, "_solver_records_cache", (lines, orders))
    except (AttributeError, TypeError):
        pass
    return lines, orders


def _solve_impl(parsed, view, rulebook, calendar):
    """Phần ruột của solve(), tách riêng để solve() bắt lỗi tổng.

    Thứ tự kiểm tra (quan trọng, từ tổng quát đến cụ thể):
        1. Measure ``unknown`` -> abstain ``parser``.
        2. Measure thuộc nhóm chưa bao phủ hoặc không nằm trong nhóm
           bao phủ -> abstain ``uncovered``.
        3. Lọc theo nhân viên -> abstain ``uncovered`` (chưa bao phủ).
        4. Thực thể: thiếu key/display, hoặc không tồn tại trong shop
           (cả khớp chính xác lẫn khớp tên rút gọn) -> abstain
           ``entity``.
        5. Từng measure: thiếu cột/dữ liệu/kỳ -> abstain theo nhóm
           tương ứng, đủ dữ liệu thì trả đáp án.
    ``calendar`` được nhận theo contract nhưng không dùng ở solver, vì
    kỳ hỏi đã được parser giải thành ngày cụ thể bằng calendar ở tầng
    parser; solver chỉ làm việc với ngày đã giải, không giải lại Tết
    hay tuần ở đây.
    """
    measure = getattr(parsed, "measure", "unknown")
    raw_start = getattr(parsed, "period_start", None)
    raw_end = getattr(parsed, "period_end", None)
    if measure == "unknown":
        return _abstain(parsed, raw_start, raw_end, "parser",
                        "parser khong xac dinh duoc measure cua cau hoi")
    if measure in UNCOVERED_MEASURES or measure not in COVERED_MEASURES:
        return _abstain(parsed, raw_start, raw_end, "uncovered",
                        f"measure '{measure}' chua duoc solver bao phu, "
                        "khong doan dinh nghia")
    if (getattr(parsed, "entity_kind", "none") or "none") == "employee":
        return _abstain(parsed, raw_start, raw_end, "uncovered",
                        "loc/xep hang theo nhan vien chua duoc bao phu")
    lines, orders = _cached_records(view)
    mode = _resolve_entity_mode(view, lines, orders, parsed)
    if (getattr(parsed, "entity_kind", "none") or "none") != "none" \
            and mode is None:
        return _abstain(parsed, raw_start, raw_end, "entity",
                        "thuc the hoi khong ton tai trong shop (ca khop "
                        "chinh xac lan khop ten rut gon)")
    start, end = _effective_period(parsed, view)

    if measure in ("revenue", "product_revenue"):
        return _solve_revenue(parsed, view, lines, orders, rulebook,
                              start, end, mode)
    if measure == "order_count":
        return _solve_order_count(parsed, view, lines, orders, rulebook,
                                  start, end, mode)
    if measure == "quantity":
        return _solve_quantity(parsed, view, lines, orders, rulebook,
                               start, end, mode)
    if measure == "cancel_rate":
        return _solve_cancel_rate(parsed, view, lines, orders, rulebook,
                                  start, end, mode)
    if measure == "revenue_share":
        return _solve_revenue_share(parsed, view, lines, orders, rulebook,
                                    start, end, mode)
    if measure == "growth_percent":
        return _solve_growth(parsed, view, lines, orders, rulebook,
                             start, end, mode, True)
    if measure == "growth_amount":
        return _solve_growth(parsed, view, lines, orders, rulebook,
                             start, end, mode, False)
    if measure == "top_product":
        return _solve_top(parsed, view, lines, orders, rulebook,
                          start, end, mode, "product")
    if measure == "top_customer":
        return _solve_top(parsed, view, lines, orders, rulebook,
                          start, end, mode, "customer")
    if measure == "best_day":
        return _solve_best(parsed, view, lines, orders, rulebook,
                           start, end, mode, False)
    if measure == "best_month":
        return _solve_best(parsed, view, lines, orders, rulebook,
                           start, end, mode, True)
    if measure == "debt_total":
        return _solve_debt(parsed, view, lines, orders, rulebook, mode,
                           False)
    if measure == "debt_top":
        return _solve_debt(parsed, view, lines, orders, rulebook, mode,
                           True)
    if measure == "price_on_day":
        return _solve_price_on_day(parsed, view, lines, orders, rulebook,
                                   mode)
    if measure == "continuous_growth":
        return _solve_continuous_growth(parsed, view, lines, orders,
                                        rulebook, mode, start, end)
    if measure == "revenue_by_payment":
        return _solve_revenue_by(parsed, view, lines, orders, rulebook,
                                 start, end, mode, "payment")
    if measure == "revenue_by_group":
        return _solve_revenue_by(parsed, view, lines, orders, rulebook,
                                 start, end, mode, "group")
    if measure == "revenue_by_channel":
        return _solve_revenue_by(parsed, view, lines, orders, rulebook,
                                 start, end, mode, "channel")
    if measure == "quantity_by_unit":
        return _solve_quantity_by_unit(parsed, view, lines, orders,
                                       rulebook, start, end, mode)
    # Không thể tới đây vì đã chặn measure ngoài COVERED ở trên, nhưng
    # giữ nhánh này để code không bao giờ rơi vào trạng thái không trả
    # về SolveResult (trả abstain thay vì crash).
    return _abstain(parsed, raw_start, raw_end, "uncovered",
                    f"measure '{measure}' roi vao nhanh khong xu ly duoc")


def solve(parsed: ParsedQuestion, view: ShopView, rulebook, calendar) -> SolveResult:
    """Giải một câu hỏi đã parse thành SolveResult, theo contract mục 3.

    Đầu vào:
        parsed: ``ParsedQuestion`` của parser (4 trục: measure, thực
            thể, kỳ, dạng đáp án).
        view: ``ShopView`` của shop chứa câu hỏi.
        rulebook: rulebook của split đang chạy (object/dict/None), dùng
            cho ``is_revenue_order`` (doanh thu có loại huỷ/trả không).
        calendar: calendar của split (nhận theo contract, solver không
            dùng vì kỳ đã được parser giải thành ngày; giữ tham số để
            tương thích chữ ký contract).

    Đầu ra:
        :class:`SolveResult` với status ``answered`` hoặc ``abstain``
        (solver không tự trả ``unanswerable``; gate sẽ kiểm tra
        unanswerable trước ngưỡng). Mọi ngoại lệ không lường trước
        trong quá trình tính đều được bắt ở đây thành abstain với
        ``error_group='filter'`` và ghi rõ lỗi trong evidence, để một
        câu hỏi lỗi không giết cả đợt chạy của shop (run_phase2 vẫn
        ghi được các câu khác và báo cáo được câu lỗi này).
    """
    try:
        return _solve_impl(parsed, view, rulebook, calendar)
    except Exception as exc:  # noqa: BLE001 - bắt tổng để không crash đợt chạy
        evidence = {
            "measure": getattr(parsed, "measure", None),
            "period": (_period_str(getattr(parsed, "period_start", None)),
                       _period_str(getattr(parsed, "period_end", None))),
            "rows_used": 0,
            "filter": "loi solver khong luong truoc",
            "group_key": None,
            "warnings": [f"loi solver: {exc!r}"],
            "confidence_notes": [],
        }
        return SolveResult(
            status="abstain",
            entry={"id": None, "type": "abstain"},
            confidence=0.0,
            error_group="filter",
            evidence=evidence,
        )

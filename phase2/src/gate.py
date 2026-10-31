# -*- coding: utf-8 -*-
"""Cổng quyết định của Giai đoạn 2 DataMate (Zalo AI Challenge 2026).

Mục đích của module này là quyết định đáp án cuối cùng cho một câu hỏi:
trả lời, ``unanswerable`` hay ``abstain``. Module này không tính toán đáp
án (việc đó thuộc về solver), cũng không hiểu câu hỏi (việc đó thuộc về
parser); nó chỉ kiểm chứng kết quả solver đưa ra trước khi ghi submission.

Vì sao cần một cổng riêng, tách khỏi solver: theo công thức điểm PA, một
câu sai bị trừ 1 điểm trong khi một câu abstain được 0 điểm, nên "không
dám trả lời" tốt hơn "đoán mò". Cổng này là nơi duy nhất áp ngưỡng tin
cậy, kiểm tra format và phát hiện thiếu dữ liệu, vì vậy mọi đáp án đều
phải đi qua cùng một luật, không phụ thuộc vào shop hay loại câu hỏi.

Thứ tự kiểm tra trong :func:`decide` là bắt buộc và có lý do:

1. Kiểm tra ``unanswerable`` TRƯỚC ngưỡng tin cậy, vì thiếu dữ liệu là
   một kết luận về dữ liệu phát hành (đáp án tham chiếu cũng sẽ là
   unanswerable), không phải là sự không chắc chắn của hệ thống. Nếu làm
   ngược lại, một câu unanswerable nhưng confidence thấp sẽ bị ghi thành
   abstain và mất điểm đúng.
2. Áp ngưỡng tin cậy 0,65, vì ngưỡng này nằm trong khoảng vận hành
   0,65-0,70 mà MASTER_PLAN mục 5.3 / API_CONTRACT mục 4 quy định. Dưới
   ngưỡng thì chuyển thành abstain, giữ nguyên ``error_group`` để phân
   tích lỗi sau này biết câu đó hỏng ở khâu nào.
3. Kiểm tra format SAU khi solver trả entry, vì chỉ lúc đó mới có giá trị
   đáp án thật để kiểm tra kiểu dữ liệu. Sai format thì chuyển thành
   abstain với ``error_group='format'``, vì format sai khi chấm sẽ bị
   tính wrong dù giá trị có vẻ đúng.

Tổng quát hoá: module này KHÔNG import cứng parser, solver hay shop_view.
Các module đó có thể chưa tồn tại khi chạy kiểm thử riêng, và khi tích
hợp, tên class có thể được định nghĩa ở một module chung. Vì vậy mọi hàm
ở đây chỉ đọc thuộc tính bằng duck-typing (đọc thuộc tính nếu có, không
đòi hỏi kiểu cụ thể), không định nghĩa lại dataclass trùng tên
``ParsedQuestion`` / ``SolveResult`` / ``ShopView`` để tránh xung đột khi
tích hợp import chung. Mọi suy luận đều dựa trên tên measure, tên
semantic cột (theo từ điển ý nghĩa của phase1, không phải tên cột chính
xác của một shop train nào) và khoảng ngày của shop, nên áp dụng được
cho shop mới, ngành mới và rulebook mới mà không cần sửa code.
"""

from __future__ import annotations

import datetime as _datetime
import math
import re
from typing import Any

__all__ = [
    "GATE_THRESHOLD",
    "compute_confidence",
    "decide",
    "validate_entry_format",
]

#: Ngưỡng tin cậy vận hành, lấy từ API_CONTRACT mục 4 và MASTER_PLAN.
#:
#: Confidence dưới ngưỡng này thì đáp án bị chuyển thành abstain, vì
#: xác suất sai đủ cao để việc đoán mò làm PA giảm (sai trừ 1, abstain
#: được 0). Ngưỡng 0,65 là đầu dưới của khoảng đề xuất 0,65-0,70: chọn
#: đầu dưới để không abstain quá nhiều câu mà solver thực ra đã tính
#: đúng, trong khi vẫn chặn được các câu không chắc.
GATE_THRESHOLD = 0.65

#: Trọng số của độ không chắc khi parse câu hỏi.
#:
#: ``(1 - confidence_parse) * 0.4``: parse càng không chắc thì trừ càng
#: nhiều, nhưng tối đa chỉ trừ 0,4, vì parse sai chưa chắc đã làm đáp án
#: sai hoàn toàn (solver vẫn có thể tính đúng nhờ dữ liệu rõ ràng), và
#: vì parse hoàn toàn không chắc (confidence_parse = 0) cho ra 0,6, vẫn
#: dưới ngưỡng 0,65 nên cuối cùng vẫn abstain — hai lớp bảo vệ này bổ
#: sung cho nhau thay vì trùng lặp.
_PARSE_WEIGHT = 0.4

#: Mức trừ khi câu hỏi cần một thực thể (mặt hàng, khách, ...) nhưng
#: parser không match được thực thể đó trong dữ liệu shop.
#:
#: Trừ 0,5 vì không có thực thể thì solver không có đối tượng để tính,
#: đáp án gần như chắc chắn sai nếu cố trả lời; mức này đủ lớn để kéo
#: confidence xuống dưới ngưỡng 0,65 ngay cả khi các yếu tố khác hoàn
#: hảo (1,0 - 0,5 = 0,5).
_ENTITY_MISSING_DEDUCTION = 0.5

#: Mức trừ khi một cột bắt buộc cho loại câu hỏi bị thiếu.
#:
#: Trừ 0,5 với cùng lý do như thực thể thiếu: thiếu cột bắt buộc thì
#: không có dữ liệu đầu vào để tính, nên không được phép trả lời chắc
#: chắn; mức này cũng đủ để tự nó kéo confidence xuống dưới ngưỡng.
_COLUMN_MISSING_DEDUCTION = 0.5

#: Mức trừ tối đa cho tỉ lệ parse lỗi của shop.
#:
#: Phase1 ghi ``profile.parse_failures`` là số lần parse mẫu thất bại
#: trong quá trình kiểm chứng map cột và quy đổi, KHÔNG phải là đếm toàn
#: bộ mọi ô lỗi của shop. Vì vậy tỉ lệ này chỉ là tín hiệu chất lượng
#: mẫu, không được phép trừ quá 0,1: nếu trừ nhiều hơn, một shop lớn
#: với vài mẫu lỗi sẽ bị phạt oan dù phần lớn dữ liệu vẫn tốt.
_PARSE_FAILURE_CAP = 0.1

#: Mức trừ khi thiếu hệ số quy đổi ở dòng cần tính số lượng.
#:
#: Trừ 0,3 vì số lượng chưa quy về đơn vị cơ bản thì không so sánh được
#: giữa các dòng (ví dụ dòng bán theo thùng và dòng bán theo chai), nên
#: tổng số lượng sẽ sai; nhưng mức này nhỏ hơn 0,5 vì câu hỏi tiền
#: (không cần quy đổi) không bị ảnh hưởng, và vì một phần dòng vẫn có
#: thể đã ở đơn vị cơ bản.
_CONVERSION_MISSING_DEDUCTION = 0.3

#: Mức trừ khi kỳ hỏi chỉ nằm ngoài khoảng dữ liệu của shop một phần.
#:
#: Trừ 0,2 vì phần kỳ nằm trong dữ liệu vẫn tính được (theo rulebook:
#: kỳ chỉ có một phần trong khoảng dữ liệu thì tính trên phần có dữ
#: liệu), nên đây không phải lỗi nghiêm trọng như kỳ hoàn toàn ngoài
#: dữ liệu (trường hợp đó là unanswerable, không phải trừ điểm); mức
#: 0,2 chỉ phản ánh rằng đáp án không còn bao quát toàn bộ kỳ hỏi.
_PERIOD_PARTIAL_DEDUCTION = 0.2

#: Các giá trị của ``entity_kind`` được coi là "không cần thực thể".
#:
#: Parser dùng ``'none'`` khi câu hỏi không nhắc tới thực thể nào; các
#: giá trị rỗng khác cũng được coi tương đương để module khác truyền
#: thiếu trường không bị phạt oan thành "thực thể thiếu".
_NO_ENTITY_KINDS = frozenset({"none", "", None})

#: Các measure (theo API_CONTRACT mục 2) được coi là cần chi tiết mặt hàng.
#:
#: Ngoài các measure xếp hạng / doanh thu theo mặt hàng, một câu hỏi có
#: ``entity_kind == 'product'`` hoặc ``ranking_target == 'product'`` cũng
#: được coi là cần mặt hàng, vì khi đó solver phải lọc theo đúng mặt
#: hàng đó. Các measure tổng chung (doanh thu toàn shop, số đơn toàn
#: shop) không nằm trong tập này nên shop không có mặt hàng vẫn trả
#: được chúng.
_PRODUCT_MEASURES = frozenset({"top_product", "product_revenue"})

#: Các measure được coi là cần giá vốn hoặc lãi gộp.
#:
#: Ngoài việc kiểm tra tên measure chứa ``cost`` / ``profit`` / ``margin``
#: / ``gross`` (để bắt cả measure mới mà test set tương lai có thể thêm,
#: ví dụ ``profit_by_product``), tập này liệt kê các measure đã biết.
#: Solver cũng có thể báo tường minh qua evidence ``needs_cost=True``.
_COST_MEASURE_SUBSTRINGS = ("cost", "profit", "margin", "gross")

#: Ánh xạ từ cột chuẩn của ShopView (tiền tố ``_``) sang semantic phase1.
#:
#: Chỉ dùng để dịch evidence của solver (vốn có thể ghi tên cột chuẩn)
#: sang semantic khi đối chiếu với ``profile.columns_unmapped``. Đây là
#: ánh xạ theo ý nghĩa, không phải tên cột thật của bất kỳ shop nào.
_VIEW_COLUMN_TO_SEMANTIC = {
    "_cost_price": "cost_price",
    "_line_amount": "line_amount",
    "_order_total": "order_total",
    "_qty": "quantity",
    "_qty_base": "quantity_converted",
    "_unit_price": "unit_price",
    "_amount_paid": "amount_paid",
    "_amount_due": "amount_due",
    "_date": "order_date",
    "_status": "order_status",
    "_payment": "payment_method",
    "_customer_key": "customer_code",
    "_customer_display": "customer_name",
    "_product_key": "product_code",
    "_product_display": "product_name",
    "_product_group": "product_group",
}


# ---------------------------------------------------------------------------
# Tiện ích duck-typing: đọc/ghi thuộc tính mà không cần biết kiểu cụ thể
# ---------------------------------------------------------------------------


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Đọc một trường từ object hoặc dict, thiếu thì trả ``default``.

    Mục đích: cho phép :func:`compute_confidence` và :func:`decide` làm
    việc với ``ParsedQuestion`` / ``SolveResult`` / ``ShopView`` thật
    lẫn với các object giả trong kiểm thử, mà không cần import kiểu của
    chúng. Nếu ``obj`` là dict thì đọc theo khóa, nếu là object thì đọc
    theo thuộc tính; ``obj`` là ``None`` thì trả ``default``.

    Trường hợp biên: thuộc tính tồn tại nhưng giá trị là ``None`` thì
    trả ``None`` (không thay bằng ``default``), để bên gọi phân biệt
    được "có trường nhưng chưa có giá trị" với "không có trường".
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _set(obj: Any, name: str, value: Any) -> None:
    """Ghi một trường vào object hoặc dict, tương ứng với :func:`_get`.

    Chỉ dùng cho các object cho phép gán thuộc tính (dataclass thông
    thường) hoặc dict. Nếu object không cho gán (ví dụ frozen dataclass
    hoặc object lạ), hàm lặng lẽ bỏ qua thay vì crash, vì cổng quyết
    định không được làm hỏng cả đợt chạy chỉ vì không ghi được một
    trường phụ; trường hợp đó bên gọi vẫn nhận được giá trị trả về
    chính (status/entry) qua object gốc nếu có thể.
    """
    if obj is None:
        return
    try:
        if isinstance(obj, dict):
            obj[name] = value
        else:
            setattr(obj, name, value)
    except (AttributeError, TypeError):
        # Object không cho gán (frozen, __slots__ không có trường này):
        # bỏ qua, không crash đợt chạy. Bên gọi vẫn đọc được các trường
        # đã ghi thành công trước đó.
        pass


def _evidence_dict(solve_result: Any, fallback: Any) -> dict:
    """Lấy dict evidence từ ``solve_result``, nếu không có thì dùng ``fallback``.

    Mục đích: solver có thể để evidence trong thuộc tính ``evidence``
    của ``SolveResult`` hoặc truyền riêng qua tham số; hàm này thống
    nhất cả hai về một dict để các bước sau dùng chung. Nếu cả hai đều
    không phải dict thì trả dict rỗng mới, để bên gọi không bao giờ phải
    kiểm tra ``None`` trước khi ghi chú thích vào evidence.
    """
    evidence = _get(solve_result, "evidence", None)
    if isinstance(evidence, dict):
        return evidence
    if isinstance(fallback, dict):
        return fallback
    return {}


def _is_number_value(value: Any) -> bool:
    """Kiểm tra một giá trị có phải số dùng được cho number/percent không.

    Chấp nhận ``int`` / ``float`` và các kiểu số thực khác (ví dụ số
    numpy mà solver tính bằng pandas có thể trả về), nhưng LOẠI TRỪ:

    - ``bool``: trong Python ``bool`` là lớp con của ``int`` nên
      ``True == 1``; nếu không loại riêng, đáp án boolean sẽ bị nhận
      nhầm thành number.
    - ``str``: kể cả chuỗi trông giống số như ``"89,526,400"`` cũng là
      sai format theo luật chấm, không được parse lại.
    - NaN và vô cực: không phải đáp án hợp lệ, và không serialize được
      thành JSON submission một cách đáng tin.

    Vì sao chấp nhận số numpy dù contract ghi int/float: cổng này kiểm
    tra "kiểu dữ liệu" theo ý nghĩa (có phải số không), không kiểm tra
    kiểu Python chính xác; việc chuyển số numpy về int/float thuần là
    việc của tầng ghi submission, không phải lý do để abstain một đáp
    án đúng.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    # Số thực từ thư viện khác (numpy): có phép toán số học nhưng không
    # phải int/float thuần. Dùng numbers.Real qua kiểm tra nhẹ: có thể
    # chuyển thành float hữu hạn mà không phải chuỗi/bool.
    try:
        import numbers as _numbers

        if isinstance(value, _numbers.Real):
            return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False
    return False


def _parse_date_value(value: Any, is_end: bool = False) -> _datetime.date | None:
    """Chuyển một giá trị ngày tháng về ``datetime.date`` nếu parse được.

    Chấp nhận ``datetime.date`` (giữ nguyên), ``datetime.datetime`` (lấy
    phần ngày), chuỗi ``YYYY-MM-DD`` và chuỗi ``YYYY-MM``. Với chuỗi
    ``YYYY-MM``: nếu là mốc bắt đầu thì lấy ngày 1 của tháng, nếu là mốc
    kết thúc thì lấy ngày cuối tháng (tính bằng cách lấy ngày 1 tháng
    sau trừ 1 ngày, đúng cả tháng 2 năm nhuận), vì một kỳ hỏi theo tháng
    bao quát cả tháng đó.

    Trả ``None`` khi giá trị không parse được (chuỗi sai định dạng, kiểu
    lạ), để bên gọi coi như "không biết mốc này" thay vì crash.
    """
    if value is None:
        return None
    if isinstance(value, _datetime.datetime):
        return value.date()
    if isinstance(value, _datetime.date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    match_full = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match_full:
        try:
            return _datetime.date(
                int(match_full.group(1)),
                int(match_full.group(2)),
                int(match_full.group(3)),
            )
        except ValueError:
            return None
    match_month = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if match_month:
        year, month = int(match_month.group(1)), int(match_month.group(2))
        if not 1 <= month <= 12:
            return None
        if is_end:
            if month == 12:
                next_month = _datetime.date(year + 1, 1, 1)
            else:
                next_month = _datetime.date(year, month + 1, 1)
            return next_month - _datetime.timedelta(days=1)
        return _datetime.date(year, month, 1)
    return None


def _period_bounds(parsed: Any, evidence: dict) -> tuple[Any, Any, list]:
    """Lấy (start, end, months) của kỳ hỏi từ parsed hoặc evidence.

    Thứ tự ưu tiên: thuộc tính ``period_start`` / ``period_end`` /
    ``period_months`` của parsed trước, vì đó là kết quả parse chính
    thức; nếu parsed không có, thử evidence ``period`` dạng tuple
    ``(start, end)`` mà solver ghi để truy vết. Các giá trị có thể là
    date, datetime hoặc chuỗi, và được :func:`_parse_date_value` chuẩn
    hoá ở bước so sánh, nên ở đây chỉ thu thập, không parse vội.

    Trường hợp biên: ``period_months`` có thể là list các tuple hoặc
    list các list ``[năm, tháng]`` (dạng JSON); bên dùng sẽ chuẩn hoá
    từng phần tử, phần tử không đủ 2 số nguyên sẽ bị bỏ qua.
    """
    start = _get(parsed, "period_start", None)
    end = _get(parsed, "period_end", None)
    months = _get(parsed, "period_months", None)
    if start is None and end is None and isinstance(evidence, dict):
        period = evidence.get("period")
        if isinstance(period, (list, tuple)) and len(period) >= 2:
            start, end = period[0], period[1]
    if not isinstance(months, (list, tuple)):
        months = []
    return start, end, list(months)


def _month_interval(year: int, month: int) -> tuple[_datetime.date, _datetime.date] | None:
    """Trả khoảng [ngày 1, ngày cuối tháng] của một tháng, sai thì ``None``.

    Dùng chung cho kiểm tra kỳ nhiều tháng: mỗi tháng trong
    ``period_months`` được xem như một khoảng, để so sánh với khoảng dữ
    liệu của shop giống như kỳ có start/end.
    """
    if not 1 <= month <= 12:
        return None
    first = _datetime.date(year, month, 1)
    if month == 12:
        last = _datetime.date(year + 1, 1, 1) - _datetime.timedelta(days=1)
    else:
        last = _datetime.date(year, month + 1, 1) - _datetime.timedelta(days=1)
    return first, last


def _data_bounds(view: Any) -> tuple[_datetime.date | None, _datetime.date | None]:
    """Lấy (date_min, date_max) của shop từ view, chuẩn hoá về date.

    ``ShopView`` theo contract đã có sẵn hai trường này (ngày đặt sớm
    nhất / muộn nhất trong dữ liệu shop). Nếu view không có (object giả
    chưa đầy đủ), trả ``(None, None)`` để các bước kiểm tra kỳ coi như
    "không đủ căn cứ" thay vì kết luận sai là ngoài dữ liệu.
    """
    date_min = _parse_date_value(_get(view, "date_min", None), is_end=False)
    date_max = _parse_date_value(_get(view, "date_max", None), is_end=True)
    return date_min, date_max


# ---------------------------------------------------------------------------
# compute_confidence: công thức tường minh trong API_CONTRACT mục 4
# ---------------------------------------------------------------------------


def _entity_missing(parsed: Any, evidence: dict) -> bool:
    """Kiểm tra câu hỏi có cần thực thể nhưng parser không match được không.

    Hai nguồn tín hiệu, chỉ cần một là đủ (nhưng chỉ trừ điểm một lần):

    - Solver báo tường minh qua evidence: ``entity_missing`` /
      ``entity_unmatched`` / ``entity_not_found`` bằng True, hoặc
      ``entity_matched`` bằng False.
    - Parser báo gián tiếp: ``entity_kind`` khác ``'none'`` (tức câu hỏi
      có nhắc tới một thực thể) nhưng ``entity_key`` rỗng/None (tức
      không tìm thấy thực thể đó trong dữ liệu shop).

    Vì sao không phạt khi ``entity_kind == 'none'``: câu hỏi tổng chung
    (doanh thu toàn shop) vốn không cần thực thể, nên ``entity_key``
    rỗng ở đó là bình thường, không phải lỗi match.
    """
    if isinstance(evidence, dict):
        if evidence.get("entity_missing") is True:
            return True
        if evidence.get("entity_unmatched") is True:
            return True
        if evidence.get("entity_not_found") is True:
            return True
        if evidence.get("entity_matched") is False:
            return True
    entity_kind = _get(parsed, "entity_kind", None)
    if entity_kind in _NO_ENTITY_KINDS:
        return False
    entity_key = _get(parsed, "entity_key", None)
    return entity_key is None or (isinstance(entity_key, str) and not entity_key.strip())


def _missing_columns_from_evidence(evidence: dict) -> list[str]:
    """Thu thập danh sách cột thiếu mà solver báo trong evidence.

    Solver có thể báo bằng nhiều khóa khác nhau tùy cách viết của module
    đó (``missing_columns`` dạng list, ``required_columns_missing`` dạng
    list, ``missing_column`` dạng chuỗi hoặc list, hoặc ``column_missing``
    dạng bool kèm ``missing_column_name``). Hàm này gom tất cả về một
    danh sách chuỗi đã loại trùng, giữ thứ tự xuất hiện, để
    :func:`compute_confidence` trừ điểm một lần duy nhất và
    :func:`decide` đối chiếu được với profile của shop.

    Trả list rỗng khi không có tín hiệu cột thiếu nào.
    """
    found: list[str] = []
    if not isinstance(evidence, dict):
        return found
    for key in ("missing_columns", "required_columns_missing", "missing_column"):
        value = evidence.get(key)
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and item.strip() and item.strip() not in found:
                    found.append(item.strip())
    if evidence.get("column_missing") is True:
        name = evidence.get("missing_column_name")
        if isinstance(name, str) and name.strip():
            if name.strip() not in found:
                found.append(name.strip())
        elif not found:
            # Solver chỉ báo "thiếu cột" mà không nêu tên cột: vẫn ghi
            # một mục chung để bên gọi biết có khoản trừ này, nhưng
            # decide sẽ không dùng mục chung này để kết luận unanswerable
            # (vì không đối chiếu được với bảng nào của shop).
            found.append("__unknown__")
    if evidence.get("error_group") == "column" and not found:
        found.append("__unknown__")
    return found


def _conversion_missing(evidence: dict) -> bool:
    """Kiểm tra solver có báo thiếu hệ số quy đổi cho dòng cần tính không.

    Các khóa evidence được chấp nhận: ``missing_conversion``,
    ``conversion_missing``, ``unit_conversion_missing``,
    ``missing_unit_conversion``, ``conversion_factor_missing`` (dạng
    bool True), hoặc ``rows_missing_conversion`` / ``missing_conversion_rows``
    lớn hơn 0 (số dòng bị ảnh hưởng), hoặc ``error_group == 'unit'`` (solver
    đã phân loại lỗi này vào nhóm đơn vị). Chỉ cần một tín hiệu là đủ,
    vì khoản trừ chỉ tính một lần cho cả câu hỏi.
    """
    if not isinstance(evidence, dict):
        return False
    for key in (
        "missing_conversion",
        "conversion_missing",
        "unit_conversion_missing",
        "missing_unit_conversion",
        "conversion_factor_missing",
    ):
        if evidence.get(key) is True:
            return True
    for key in ("rows_missing_conversion", "missing_conversion_rows"):
        value = evidence.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return True
    return evidence.get("error_group") == "unit"


def _period_partially_outside(parsed: Any, view: Any, evidence: dict) -> bool:
    """Kiểm tra kỳ hỏi có nằm ngoài khoảng dữ liệu của shop MỘT PHẦN không.

    "Một phần" nghĩa là kỳ hỏi giao với khoảng dữ liệu nhưng không nằm
    trọn trong đó: có phần kỳ có dữ liệu để tính, và có phần kỳ không có
    dữ liệu. Trường hợp kỳ hoàn toàn ngoài dữ liệu thì KHÔNG tính ở đây
    (đó là unanswerable, xử lý ở :func:`decide`), vì vậy điều kiện bắt
    buộc là kỳ và khoảng dữ liệu phải giao nhau.

    Nguồn tín hiệu: solver báo tường minh ``period_partial_outside`` /
    ``period_partly_outside`` thì tin ngay; nếu không, tự tính từ
    period_start/period_end hoặc period_months so với date_min/date_max
    của view. Nếu thiếu bất kỳ mốc nào thì trả False (không đủ căn cứ,
    không trừ điểm oan).
    """
    if isinstance(evidence, dict):
        if evidence.get("period_partial_outside") is True:
            return True
        if evidence.get("period_partly_outside") is True:
            return True
    date_min, date_max = _data_bounds(view)
    if date_min is None or date_max is None:
        return False
    start_raw, end_raw, months = _period_bounds(parsed, evidence)
    start = _parse_date_value(start_raw, is_end=False)
    end = _parse_date_value(end_raw, is_end=True)
    if start is not None and end is not None:
        overlaps = start <= date_max and end >= date_min
        outside_part = start < date_min or end > date_max
        if overlaps and outside_part:
            return True
    # Kỳ nhiều tháng: mỗi tháng là một khoảng riêng. Kỳ được coi là nằm
    # ngoài một phần khi có ít nhất một tháng giao với dữ liệu và có ít
    # nhất một tháng hoàn toàn ngoài dữ liệu, hoặc một tháng straddle
    # biên của khoảng dữ liệu (một phần tháng nằm ngoài).
    if months:
        any_overlap = False
        any_outside = False
        for item in months:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            try:
                year, month = int(item[0]), int(item[1])
            except (TypeError, ValueError):
                continue
            interval = _month_interval(year, month)
            if interval is None:
                continue
            first, last = interval
            if first <= date_max and last >= date_min:
                any_overlap = True
                if first < date_min or last > date_max:
                    any_outside = True
            else:
                any_outside = True
        if any_overlap and any_outside:
            return True
    return False


def compute_confidence(parsed: Any, view: Any, solve_evidence: dict) -> float:
    """Tính confidence cuối từ parse, thực thể, cột, lỗi parse và kỳ hỏi.

    Công thức tường minh theo API_CONTRACT mục 4, bắt đầu từ 1,0 và trừ
    dần, kết quả kẹp trong [0, 1]:

    - Parse không chắc: trừ ``(1 - confidence_parse) * 0,4`` khi
      ``confidence_parse < 1``. Trọng số 0,4 vì parse không chắc chưa
      đồng nghĩa đáp án sai hoàn toàn.
    - Thực thể cần mà không match được: trừ 0,5.
    - Cột bắt buộc thiếu (solver báo trong evidence): trừ 0,5.
    - Tỉ lệ parse lỗi của shop cao: trừ tối đa 0,1, bằng
      ``min(0,1, parse_failures / số dòng của shop)``; nếu solver đã cho
      sẵn ``parse_failure_rate`` trong evidence thì dùng tỉ lệ đó. Mức
      trần 0,1 vì ``parse_failures`` của phase1 chỉ đếm mẫu kiểm chứng,
      không phải tỉ lệ ô lỗi toàn shop.
    - Thiếu hệ số quy đổi ở dòng cần tính số lượng: trừ 0,3.
    - Kỳ hỏi nằm ngoài khoảng dữ liệu một phần: trừ 0,2.

    Đầu vào:

    - ``parsed``: kết quả parser (duck-typing: đọc ``confidence_parse``,
      ``entity_kind``, ``entity_key``, ``period_*`` nếu có).
    - ``view``: view của shop (duck-typing: đọc ``profile`` với
      ``parse_failures`` / ``rows_out`` / ``rows_in``, và
      ``date_min`` / ``date_max`` nếu có).
    - ``solve_evidence``: dict evidence của solver; hàm GHI TRỰC TIẾP
      vào dict này các khóa ``confidence_deductions`` (danh sách từng
      khoản trừ dạng ``{'reason': ..., 'amount': ...}`` để báo cáo và
      phân tích lỗi), ``confidence_base`` (= 1,0) và ``confidence``
      (kết quả cuối), để bên gọi không phải tính lại.

    Đầu ra: float trong [0, 1].

    Trường hợp biên:

    - ``parsed`` / ``view`` là ``None`` hoặc thiếu trường: các khoản trừ
      tương ứng được bỏ qua (coi như không có bằng chứng trừ điểm), thay
      vì crash hay trừ điểm oan. Thiếu bằng chứng thì :func:`decide` vẫn
      có thể abstain bằng các kiểm tra khác.
    - ``solve_evidence`` không phải dict (ví dụ ``None``): hàm vẫn tính
      và trả số, chỉ không ghi được chi tiết khoản trừ vào đâu.
    - Tổng các khoản trừ vượt 1,0 thì kết quả kẹp về 0,0, không âm.
    - ``confidence_parse`` ngoài [0, 1] (do module khác tính lỗi) thì
      được kẹp về [0, 1] trước khi tính, để công thức không cho số vô lý.

    Liên quan tổng quát hoá: công thức chỉ dùng các tín hiệu chung
    (parse, thực thể, cột, lỗi, kỳ) mà mọi shop đều có, không dùng
    business_id, id câu hỏi hay tên cột cụ thể, nên cùng một công thức
    áp dụng cho shop mới và ngành mới.
    """
    evidence = solve_evidence if isinstance(solve_evidence, dict) else {}
    deductions: list[dict] = []

    # 1. Parse không chắc: trừ (1 - confidence_parse) * 0,4.
    # Lấy từ parsed trước, evidence sau (solver có thể ghi đè khi nó
    # biết rõ hơn về độ chắc của parse cho câu này).
    confidence_parse = _get(parsed, "confidence_parse", None)
    if confidence_parse is None and isinstance(evidence, dict):
        confidence_parse = evidence.get("confidence_parse")
    if isinstance(confidence_parse, (int, float)) and not isinstance(
        confidence_parse, bool
    ):
        clamped = max(0.0, min(1.0, float(confidence_parse)))
        if clamped < 1.0:
            amount = (1.0 - clamped) * _PARSE_WEIGHT
            deductions.append(
                {"reason": "parse_khong_chac", "amount": round(amount, 6)}
            )

    # 2. Thực thể cần mà không match được: trừ 0,5.
    if _entity_missing(parsed, evidence):
        deductions.append(
            {"reason": "entity_khong_match", "amount": _ENTITY_MISSING_DEDUCTION}
        )

    # 3. Cột bắt buộc thiếu: trừ 0,5 (chỉ một lần dù thiếu nhiều cột).
    if _missing_columns_from_evidence(evidence):
        deductions.append(
            {"reason": "cot_bat_buoc_thieu", "amount": _COLUMN_MISSING_DEDUCTION}
        )

    # 4. Tỉ lệ parse lỗi của shop: trừ tối đa 0,1.
    # Ưu tiên tỉ lệ solver cho sẵn; nếu không có thì tự tính từ profile
    # phase1: parse_failures / số dòng (rows_out, dự phòng rows_in).
    failure_rate: float | None = None
    if isinstance(evidence, dict):
        raw_rate = evidence.get("parse_failure_rate")
        if isinstance(raw_rate, (int, float)) and not isinstance(raw_rate, bool):
            failure_rate = max(0.0, float(raw_rate))
    if failure_rate is None:
        profile = _get(view, "profile", None)
        failures = _get(profile, "parse_failures", None)
        if isinstance(failures, (int, float)) and not isinstance(failures, bool):
            rows_out = _get(profile, "rows_out", None)
            rows_in = _get(profile, "rows_in", None)
            rows = rows_out if isinstance(rows_out, (int, float)) and rows_out else rows_in
            if isinstance(rows, (int, float)) and rows and rows > 0:
                failure_rate = float(failures) / float(rows)
            elif float(failures) > 0:
                # Shop không có dòng nào nhưng vẫn có lỗi parse: coi như
                # tỉ lệ cao nhất, nhưng vẫn bị trần 0,1 ở dưới kẹp lại.
                failure_rate = 1.0
            else:
                failure_rate = 0.0
    if failure_rate is not None and failure_rate > 0:
        amount = min(_PARSE_FAILURE_CAP, failure_rate)
        deductions.append({"reason": "parse_failures_cua_shop", "amount": round(amount, 6)})

    # 5. Thiếu hệ số quy đổi ở dòng cần tính số lượng: trừ 0,3.
    if _conversion_missing(evidence):
        deductions.append(
            {"reason": "thieu_he_so_quy_doi", "amount": _CONVERSION_MISSING_DEDUCTION}
        )

    # 6. Kỳ hỏi nằm ngoài khoảng dữ liệu một phần: trừ 0,2.
    if _period_partially_outside(parsed, view, evidence):
        deductions.append(
            {"reason": "ky_hoi_ngoai_du_lieu_mot_phan", "amount": _PERIOD_PARTIAL_DEDUCTION}
        )

    total_deduction = sum(item["amount"] for item in deductions)
    confidence = 1.0 - total_deduction
    # Kẹp trong [0, 1]: tổng trừ có thể vượt 1,0 khi nhiều lỗi cộng lại.
    confidence = max(0.0, min(1.0, confidence))
    if isinstance(solve_evidence, dict):
        solve_evidence["confidence_deductions"] = deductions
        solve_evidence["confidence_base"] = 1.0
        solve_evidence["confidence"] = confidence
    return confidence


# ---------------------------------------------------------------------------
# Format validator: chỉ kiểm tra type/kiểu dữ liệu, không cần đáp án mẫu
# ---------------------------------------------------------------------------


def validate_entry_format(entry: Any, expected_type: str | None = None) -> tuple[bool, str | None]:
    """Kiểm tra format của một entry đáp án, chỉ theo type và kiểu dữ liệu.

    Khác với ``answers_match`` của phase1 (cần đáp án tham chiếu để chấm
    đúng/sai), hàm này KHÔNG cần đáp án tham chiếu: nó chỉ kiểm tra xem
    giá trị có đúng kiểu mà type của nó đòi hỏi hay không, vì format sai
    khi chấm sẽ bị tính wrong dù giá trị có vẻ đúng.

    Luật kiểm tra theo từng type (theo API_CONTRACT mục 4 và MASTER_PLAN
    mục 1.2):

    - ``number``: đáp án phải là số (int/float, không phải bool/str).
    - ``percent``: đáp án phải là số (giống number; không kiểm tra thang
      0-100 ở đây vì percent âm là hợp lệ, ví dụ tăng trưởng âm).
    - ``string``: đáp án phải là str không rỗng (sau khi bỏ khoảng trắng
      đầu/cuối; chuỗi chỉ toàn khoảng trắng coi như rỗng).
    - ``date``: đáp án phải là chuỗi đúng ``YYYY-MM-DD`` (ngày thật trên
      lịch) hoặc ``YYYY-MM`` (tháng 01-12); các dạng khác như
      ``15/08/2026`` đều sai format.
    - ``boolean``: đáp án phải đúng kiểu bool (số 0/1 hay chuỗi
      ``"true"`` đều sai, vì luật boolean yêu cầu khớp tuyệt đối kiểu).
    - ``list``: đáp án phải là list (độ dài và nội dung từng phần tử
      thuộc về solver, không kiểm tra ở đây).
    - ``table``: đáp án phải là dict có ``columns`` (list các chuỗi) và
      ``rows`` (list các dòng), mỗi dòng là list/tuple có độ dài bằng số
      cột; thiếu một trong hai khóa, hoặc dòng lệch số cột, đều sai.
    - ``abstain`` / ``unanswerable``: không phải đáp án giá trị nên luôn
      coi là đúng format (decide chỉ gọi validator cho entry answered,
      nhưng hàm vẫn xử lý an toàn nếu được gọi với hai type này).
    - Type lạ, không nằm trong danh sách trên: sai format, vì không có
      luật nào để kiểm tra nó và submission với type lạ sẽ bị chấm wrong.

    Ngoài ra, nếu ``expected_type`` được truyền (thường là
    ``parsed.answer_type``) và khác với type của entry, cũng coi là sai
    format: type submission khác type câu hỏi yêu cầu thì khi chấm cũng
    bị tính wrong, nên thà abstain còn hơn ghi một đáp án chắc chắn sai.
    Trường hợp ``expected_type`` là ``'unknown'`` hoặc rỗng thì bỏ qua
    kiểm tra này, vì parser không xác định được type yêu cầu.

    Đầu vào: ``entry`` là dict ``{'id':..., 'type':..., 'answer':...}``
    (hoặc bất kỳ giá trị nào khác, khi đó đều sai format); ``expected_type``
    là chuỗi type kỳ vọng hoặc ``None``.

    Đầu ra: tuple ``(đúng_format, lý_do)``; ``lý_do`` là ``None`` khi đúng,
    là chuỗi mã lỗi ngắn (ví dụ ``'number_khong_phai_so'``) khi sai, để
    ghi vào evidence phục vụ phân tích lỗi.
    """
    if not isinstance(entry, dict):
        return False, "entry_khong_phai_dict"
    entry_type = entry.get("type")
    if entry_type in ("abstain", "unanswerable"):
        return True, None
    if expected_type not in (None, "", "unknown") and entry_type != expected_type:
        return False, "type_khong_khop_voi_cau_hoi"
    if entry_type not in (
        "number",
        "percent",
        "string",
        "date",
        "boolean",
        "list",
        "table",
    ):
        return False, "type_khong_biet"
    if "answer" not in entry:
        return False, "thieu_answer"
    answer = entry.get("answer")
    if entry_type in ("number", "percent"):
        if not _is_number_value(answer):
            return False, "number_khong_phai_so"
        return True, None
    if entry_type == "string":
        if not isinstance(answer, str) or not answer.strip():
            return False, "string_rong_hoac_khong_phai_chuoi"
        return True, None
    if entry_type == "date":
        if not isinstance(answer, str):
            return False, "date_khong_phai_chuoi"
        if _parse_date_value(answer, is_end=False) is None:
            return False, "date_sai_dinh_dang"
        # Chuỗi YYYY-MM-DD và YYYY-MM đều được _parse_date_value chấp
        # nhận; nhưng chuỗi như "2026-13" đã bị loại ở bước trên (tháng
        # ngoài 01-12) và "2026-02-30" cũng bị loại (ngày không thật).
        # Kiểm tra thêm dạng: chỉ chấp nhận đúng 7 hoặc 10 ký tự, để
        # chuỗi thừa ký tự như "2026-01-01 " đã strip? (strip đã xử lý
        # ở _parse_date_value) hay "2026/01/01" không lọt qua regex.
        if not re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", answer.strip()):
            return False, "date_sai_dinh_dang"
        return True, None
    if entry_type == "boolean":
        if not isinstance(answer, bool):
            return False, "boolean_khong_phai_bool"
        return True, None
    if entry_type == "list":
        if not isinstance(answer, list):
            return False, "list_khong_phai_list"
        return True, None
    if entry_type == "table":
        if not isinstance(answer, dict):
            return False, "table_khong_phai_dict"
        columns = answer.get("columns")
        rows = answer.get("rows")
        if not isinstance(columns, list) or not columns:
            return False, "table_thieu_columns"
        if any(not isinstance(col, str) or not col.strip() for col in columns):
            return False, "table_columns_khong_phai_chuoi"
        if not isinstance(rows, list):
            return False, "table_thieu_rows"
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) != len(columns):
                return False, "table_row_lech_so_cot"
        return True, None
    return False, "type_khong_biet"


# ---------------------------------------------------------------------------
# decide: unanswerable trước, ngưỡng sau, format cuối
# ---------------------------------------------------------------------------


def _measure_of(parsed: Any, evidence: dict) -> str:
    """Lấy tên measure của câu hỏi từ parsed, dự phòng evidence.

    Measure là chuỗi enum theo API_CONTRACT mục 2 (ví dụ ``'revenue'``,
    ``'top_product'``). Nếu parsed không có measure nhưng evidence có
    (solver ghi lại measure nó đã tính), dùng evidence. Trả chuỗi rỗng
    khi cả hai đều không có, để các bước kiểm tra theo measure bỏ qua
    thay vì kết luận sai.
    """
    measure = _get(parsed, "measure", None)
    if isinstance(measure, str) and measure.strip():
        return measure.strip().lower()
    if isinstance(evidence, dict):
        raw = evidence.get("measure")
        if isinstance(raw, str) and raw.strip():
            return raw.strip().lower()
    return ""


def _needs_cost(parsed: Any, evidence: dict) -> bool:
    """Kiểm tra câu hỏi có cần giá vốn hoặc lãi gộp không.

    Ba nguồn tín hiệu: solver báo tường minh ``needs_cost`` /
    ``needs_profit`` = True trong evidence; tên measure chứa một trong
    các chuỗi ``cost`` / ``profit`` / ``margin`` / ``gross``; hoặc parsed
    có thuộc tính ``needs_cost`` = True. Cách kiểm tra theo chuỗi con
    giúp bắt cả measure mới mà test set tương lai thêm, thay vì chỉ liệt
    kê các measure đã biết trong train.
    """
    if isinstance(evidence, dict):
        if evidence.get("needs_cost") is True or evidence.get("needs_profit") is True:
            return True
    if _get(parsed, "needs_cost", None) is True:
        return True
    measure = _measure_of(parsed, evidence)
    return any(sub in measure for sub in _COST_MEASURE_SUBSTRINGS)


def _needs_product(parsed: Any, evidence: dict) -> bool:
    """Kiểm tra câu hỏi có cần chi tiết mặt hàng không.

    Tín hiệu: solver báo ``needs_product`` = True; measure thuộc tập
    mặt hàng (``top_product``, ``product_revenue``); ``entity_kind``
    là ``'product'`` (câu hỏi nhắc tới một mặt hàng cụ thể); hoặc
    ``ranking_target`` là ``'product'`` (câu hỏi xếp hạng mặt hàng).
    Câu hỏi tổng chung không có các tín hiệu này thì không cần mặt hàng.
    """
    if isinstance(evidence, dict) and evidence.get("needs_product") is True:
        return True
    if _measure_of(parsed, evidence) in _PRODUCT_MEASURES:
        return True
    if _get(parsed, "entity_kind", None) == "product":
        return True
    return _get(parsed, "ranking_target", None) == "product"


def _shop_has_product(view: Any) -> bool | None:
    """Kiểm tra shop có chi tiết mặt hàng không, trả None nếu không biết.

    Thứ tự: nếu ``view.products`` có phần tử thì chắc chắn có mặt hàng;
    nếu không, dùng cờ ``profile.has_product_detail`` của phase1 (True
    nghĩa là có bảng giao dịch vừa có mặt hàng vừa có mã đơn/số lượng,
    False nghĩa là shop chỉ có hoá đơn tổng, không có mặt hàng). Nếu cả
    hai nguồn đều không có thì trả ``None`` (không đủ căn cứ), để decide
    không kết luận unanswerable oan.
    """
    products = _get(view, "products", None)
    if isinstance(products, (list, tuple)) and len(products) > 0:
        return True
    profile = _get(view, "profile", None)
    flag = _get(profile, "has_product_detail", None)
    if isinstance(flag, bool):
        return flag
    if isinstance(products, (list, tuple)) and len(products) == 0 and flag is None:
        # View có danh sách products rỗng nhưng không có profile để đối
        # chiếu: danh sách rỗng ở đây là bằng chứng shop không có mặt
        # hàng trong view đã xây (theo contract, products lấy từ lines +
        # bảng master, rỗng nghĩa là không tìm thấy mặt hàng nào).
        return False
    return None


def _shop_has_cost(view: Any) -> bool | None:
    """Kiểm tra shop có giá vốn không, trả None nếu không biết.

    Cờ ``profile.has_cost`` của phase1 là nguồn chính (True khi có cột
    giá vốn ở bảng nào đó). Nếu cờ là False nhưng view.lines vẫn có cột
    chuẩn ``_cost_price`` thì coi là có, vì cột chuẩn đó chỉ xuất hiện
    khi solver map được giá vốn. Nếu cả profile và lines đều không cho
    thông tin thì trả ``None``.
    """
    profile = _get(view, "profile", None)
    flag = _get(profile, "has_cost", None)
    lines = _get(view, "lines", None)
    columns = _get(lines, "columns", None)
    has_cost_column = False
    if columns is not None:
        try:
            has_cost_column = "_cost_price" in list(columns)
        except TypeError:
            has_cost_column = False
    if has_cost_column:
        return True
    if isinstance(flag, bool):
        return flag
    return None


def _absent_semantics(view: Any) -> set[str]:
    """Tập các semantic cột hoàn toàn không tồn tại trong shop.

    Lấy từ ``profile.columns_unmapped`` của phase1 (các semantic không
    map được ở bất kỳ bảng nào) cộng với các semantic mà
    ``profile.columns_mapped`` ghi giá trị rỗng. Đây là căn cứ cho điều
    kiện unanswerable "cột bắt buộc hoàn toàn không tồn tại trong bất kỳ
    bảng nào của shop": chỉ semantic nằm trong tập này mới được coi là
    hoàn toàn không tồn tại, còn semantic mà profile không nhắc tới
    (profile thiếu) thì không kết luận.

    Ngoài ra, các cột chuẩn của view cũng được dịch ngược sang semantic:
    nếu lines/orders có cột chuẩn tương ứng thì semantic đó không bị
    coi là vắng, dù profile có ghi unmapped (trường hợp view được xây
    từ nguồn khác profile).
    """
    absent: set[str] = set()
    profile = _get(view, "profile", None)
    unmapped = _get(profile, "columns_unmapped", None)
    if isinstance(unmapped, (list, tuple)):
        for item in unmapped:
            if isinstance(item, str) and item.strip():
                absent.add(item.strip())
    mapped = _get(profile, "columns_mapped", None)
    if isinstance(mapped, dict):
        for semantic, real_name in mapped.items():
            if isinstance(semantic, str) and not real_name:
                absent.add(semantic)
    # Cột chuẩn của view chứng minh semantic đó tồn tại: loại khỏi tập vắng.
    for table in (_get(view, "lines", None), _get(view, "orders", None)):
        columns = _get(table, "columns", None)
        if columns is None:
            continue
        try:
            column_list = list(columns)
        except TypeError:
            continue
        for column in column_list:
            semantic = _VIEW_COLUMN_TO_SEMANTIC.get(column)
            if semantic is not None:
                absent.discard(semantic)
    return absent


def _period_fully_outside(parsed: Any, view: Any, evidence: dict) -> bool:
    """Kiểm tra kỳ hỏi có nằm HOÀN TOÀN ngoài khoảng dữ liệu của shop không.

    "Hoàn toàn ngoài" nghĩa là không có ngày nào của kỳ hỏi giao với
    [date_min, date_max]: kỳ kết thúc trước ngày dữ liệu sớm nhất, hoặc
    kỳ bắt đầu sau ngày dữ liệu muộn nhất. Theo rulebook, trường hợp này
    khác với bằng 0 (không có dữ liệu khác với giá trị 0) và khác với kỳ
    nằm ngoài một phần (vẫn tính trên phần có dữ liệu), nên nó là
    unanswerable.

    Tính từ period_start/period_end nếu có; nếu chỉ có period_months thì
    xét từng tháng: chỉ khi MỌI tháng đều hoàn toàn ngoài khoảng dữ liệu
    mới kết luận. Thiếu mốc ngày hoặc thiếu date_min/date_max của shop
    thì trả False (không đủ căn cứ, để abstain xử lý thay vì kết luận
    unanswerable oan).
    """
    date_min, date_max = _data_bounds(view)
    if date_min is None or date_max is None:
        return False
    start_raw, end_raw, months = _period_bounds(parsed, evidence)
    start = _parse_date_value(start_raw, is_end=False)
    end = _parse_date_value(end_raw, is_end=True)
    if start is not None and end is not None:
        return end < date_min or start > date_max
    if start is not None:
        return start > date_max
    if end is not None:
        return end < date_min
    if months:
        intervals: list[tuple[_datetime.date, _datetime.date]] = []
        for item in months:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            try:
                year, month = int(item[0]), int(item[1])
            except (TypeError, ValueError):
                continue
            interval = _month_interval(year, month)
            if interval is not None:
                intervals.append(interval)
        if intervals:
            return all(last < date_min or first > date_max for first, last in intervals)
    return False


def _required_column_reason(parsed: Any, view: Any, evidence: dict) -> str | None:
    """Tìm lý do unanswerable do cột bắt buộc hoàn toàn không tồn tại.

    Mỗi loại câu hỏi có một hoặc nhiều nhóm yêu cầu, mỗi nhóm là danh
    sách các semantic mà chỉ cần MỘT trong số đó tồn tại là đủ (ví dụ
    khách xác định bằng mã HOẶC tên). Chỉ khi MỌI semantic trong một
    nhóm đều nằm trong tập vắng của shop thì nhóm đó mới bị coi là thiếu
    hoàn toàn; nếu bất kỳ semantic nào còn tồn tại, hoặc profile không
    có thông tin về semantic đó, thì nhóm đó không kết luận.

    Các nhóm được xét (theo ý nghĩa, không theo tên cột thật):

    - Câu về khách (entity/ranking/top_customer, debt_top): cần
      ``customer_code`` HOẶC ``customer_name``.
    - Câu doanh thu theo nhóm: cần ``product_group`` HOẶC
      ``customer_group`` (nhóm có thể là nhóm hàng hoặc nhóm khách).
    - Câu doanh thu theo hình thức thanh toán: cần ``payment_method``.
    - Câu về nhân viên: cần ``employee_code`` HOẶC ``employee_name``.
    - Câu công nợ: cần ``amount_due`` HOẶC ``order_total``.
    - Câu số lượng: cần ``quantity`` HOẶC ``quantity_converted``.
    - Câu tỉ lệ huỷ: cần ``order_status`` (không có trạng thái thì không
      phân biệt được đơn huỷ với đơn khác).
    - Câu giá bán / giá trung bình: cần ``unit_price`` HOẶC
      ``line_amount``.
    - Câu doanh thu / tăng trưởng / ngày-tháng tốt nhất: cần tiền, tức
      ``line_amount`` HOẶC ``order_total`` HOẶC (``unit_price`` VÀ
      ``quantity`` cùng tồn tại, vì solver tính thành tiền bằng
      số lượng nhân đơn giá khi không có cột thành tiền sẵn).
    - Evidence của solver báo cột thiếu mà cột đó nằm trong tập vắng của
      profile: cũng là lý do unanswerable (dịch tên cột chuẩn sang
      semantic trước khi đối chiếu).

    Trả mã lý do (ví dụ ``'cot_bat_buoc_khong_ton_tai:payment_method'``)
    hoặc ``None`` khi không có nhóm nào thiếu hoàn toàn.

    Trường hợp biên: khi profile hoàn toàn không có (view giả không có
    profile), tập vắng rỗng nên hàm luôn trả ``None`` — an toàn hơn là
    kết luận unanswerable từ việc thiếu thông tin về chính shop.
    """
    absent = _absent_semantics(view)
    if not absent:
        # Không có profile để đối chiếu thì không kết luận; nhưng vẫn
        # xét evidence của solver nếu nó nêu đích danh semantic vắng mà
        # view cũng không có cột chuẩn tương ứng — trường hợp này hiếm
        # và chỉ xảy ra khi solver đã tự kiểm tra kỹ.
        return None

    def _group_missing(group: list[str]) -> bool:
        """True khi mọi semantic trong nhóm đều vắng khỏi shop."""
        return bool(group) and all(semantic in absent for semantic in group)

    measure = _measure_of(parsed, evidence)
    entity_kind = _get(parsed, "entity_kind", None)
    ranking_target = _get(parsed, "ranking_target", None)

    if entity_kind == "customer" or ranking_target == "customer" or measure in (
        "top_customer",
        "debt_top",
    ):
        if _group_missing(["customer_code", "customer_name"]):
            return "cot_bat_buoc_khong_ton_tai:customer_code/customer_name"
    if entity_kind == "group" or measure == "revenue_by_group":
        if _group_missing(["product_group", "customer_group"]):
            return "cot_bat_buoc_khong_ton_tai:product_group/customer_group"
    if entity_kind == "payment" or measure == "revenue_by_payment":
        if _group_missing(["payment_method"]):
            return "cot_bat_buoc_khong_ton_tai:payment_method"
    if entity_kind == "employee" or ranking_target == "employee" or measure == "top_employee":
        if _group_missing(["employee_code", "employee_name"]):
            return "cot_bat_buoc_khong_ton_tai:employee_code/employee_name"
    if measure in ("debt_total", "debt_top"):
        if _group_missing(["amount_due", "order_total"]):
            return "cot_bat_buoc_khong_ton_tai:amount_due/order_total"
    if measure in ("quantity", "quantity_by_unit", "top_product"):
        if _group_missing(["quantity", "quantity_converted"]):
            return "cot_bat_buoc_khong_ton_tai:quantity/quantity_converted"
    if measure == "cancel_rate":
        if _group_missing(["order_status"]):
            return "cot_bat_buoc_khong_ton_tai:order_status"
    if measure in ("price_on_day", "avg_price"):
        if _group_missing(["unit_price", "line_amount"]):
            return "cot_bat_buoc_khong_ton_tai:unit_price/line_amount"
    if measure in (
        "revenue",
        "product_revenue",
        "revenue_by_payment",
        "revenue_by_group",
        "revenue_by_channel",
        "growth_percent",
        "growth_amount",
        "best_day",
        "best_month",
        "revenue_share",
    ):
        money_missing = _group_missing(["line_amount", "order_total"])
        price_qty_present = "unit_price" not in absent and "quantity" not in absent
        if money_missing and not price_qty_present:
            return "cot_bat_buoc_khong_ton_tai:line_amount/order_total"

    # Evidence của solver báo cột thiếu: chỉ kết luận khi cột đó (sau
    # khi dịch sang semantic) thực sự nằm trong tập vắng của profile.
    for column in _missing_columns_from_evidence(evidence):
        semantic = _VIEW_COLUMN_TO_SEMANTIC.get(column, column)
        if semantic in absent and semantic != "__unknown__":
            return f"cot_bat_buoc_khong_ton_tai:{semantic}"
    return None


def _unanswerable_reason(parsed: Any, view: Any, evidence: dict) -> str | None:
    """Xác định lý do unanswerable, hoặc ``None`` nếu không đủ căn cứ.

    Thứ tự các điều kiện theo nhiệm vụ (chỉ cần một điều kiện đúng là
    đủ, lý do đầu tiên tìm thấy được ghi lại để phân tích lỗi):

    1. Shop không có mặt hàng mà câu hỏi cần mặt hàng.
    2. Shop không có giá vốn mà câu hỏi cần giá vốn/lãi.
    3. Kỳ hỏi nằm HOÀN TOÀN ngoài [date_min, date_max] của shop.
    4. Cột bắt buộc cho loại câu hoàn toàn không tồn tại trong bất kỳ
       bảng nào của shop (xem :func:`_required_column_reason`).

    Vì sao các điều kiện này cho ra unanswerable chứ không phải abstain:
    chúng đều là bằng chứng mạnh rằng dữ liệu phát hành không chứa thứ
    cần để trả lời, nên đáp án tham chiếu (nếu chấm theo luật) cũng sẽ
    là unanswerable; trả abstain ở đây sẽ bỏ lỡ điểm đúng, còn trả một
    con số sẽ bị tính wrong. Ngược lại, khi không đủ căn cứ cho bất kỳ
    điều kiện nào, hàm trả ``None`` để decide đi tiếp tới ngưỡng và
    format, cuối cùng rơi về abstain nếu không chắc — đó là lựa chọn an
    toàn khi chưa phân biệt được "không có dữ liệu" với "không tính
    được nhưng không rõ vì sao".
    """
    if _needs_product(parsed, evidence):
        has_product = _shop_has_product(view)
        if has_product is False:
            return "shop_khong_co_mat_hang"
    if _needs_cost(parsed, evidence):
        has_cost = _shop_has_cost(view)
        if has_cost is False:
            return "shop_khong_co_gia_von"
    if _period_fully_outside(parsed, view, evidence):
        return "ky_hoi_ngoai_khoang_du_lieu"
    column_reason = _required_column_reason(parsed, view, evidence)
    if column_reason is not None:
        return column_reason
    return None


def _abstain_entry(solve_result: Any) -> dict:
    """Tạo entry abstain giữ nguyên id của entry gốc nếu có.

    Entry abstain theo contract có dạng ``{'id': ..., 'type': 'abstain'}``,
    không có khóa ``answer``. Giữ id là bắt buộc để tầng ghi submission
    vẫn biết câu này thuộc về câu hỏi nào, dù đáp án đã bị chuyển thành
    abstain ở cổng.
    """
    entry = _get(solve_result, "entry", None)
    question_id = entry.get("id") if isinstance(entry, dict) else None
    result: dict = {"type": "abstain"}
    if question_id is not None:
        result["id"] = question_id
    return result


def _unanswerable_entry(solve_result: Any, reason: str) -> dict:
    """Tạo entry unanswerable giữ id gốc và ghi lý do để truy vết.

    Dạng entry: ``{'id': ..., 'type': 'unanswerable', 'reason': ...}``.
    Khóa ``reason`` là mã lý do nội bộ (ví dụ
    ``'shop_khong_co_mat_hang'``), không phải đáp án; tầng ghi submission
    có thể giữ hoặc bỏ khóa này tùy format nộp, nhưng log phân tích lỗi
    cần nó để biết vì sao câu này được kết luận unanswerable.
    """
    entry = _get(solve_result, "entry", None)
    question_id = entry.get("id") if isinstance(entry, dict) else None
    result: dict = {"type": "unanswerable", "reason": reason}
    if question_id is not None:
        result["id"] = question_id
    return result


def decide(parsed: Any, view: Any, solve_result: Any, rulebook: Any = None) -> Any:
    """Quyết định cuối cho một câu hỏi: answered / unanswerable / abstain.

    Thứ tự bắt buộc (xem docstring module):

    1. Unanswerable TRƯỚC ngưỡng: nếu có bằng chứng mạnh là dữ liệu phát
       hành không đủ (shop không có mặt hàng/giá vốn mà câu cần, kỳ hỏi
       hoàn toàn ngoài khoảng dữ liệu, cột bắt buộc hoàn toàn không tồn
       tại), trả ``unanswerable`` với ``error_group='unanswerable'``,
       bất kể confidence cao hay thấp, bất kể solver trước đó trả gì
       (trừ khi solver đã trả unanswerable thì giữ nguyên).
    2. Ngưỡng 0,65: nếu confidence cuối nhỏ hơn 0,65 thì chuyển thành
       abstain. ``error_group`` GIỮ NGUYÊN như solver đã gán (kể cả khi
       là ``None``), để phân tích lỗi vẫn biết câu đó hỏng ở khâu nào;
       entry được thay bằng entry abstain giữ id gốc. Nếu solver chưa
       gán confidence (None hoặc không phải số) thì hàm tự tính bằng
       :func:`compute_confidence` từ evidence rồi mới áp ngưỡng.
    3. Format SAU solver: nếu còn ở trạng thái answered mà entry sai
       format (xem :func:`validate_entry_format`, so với
       ``parsed.answer_type`` khi parser xác định được), chuyển thành
       abstain với ``error_group='format'``.

    Đầu vào:

    - ``parsed``: kết quả parser (duck-typing, không import cứng).
    - ``view``: view của shop (duck-typing).
    - ``solve_result``: kết quả solver, có các trường ``status``,
      ``entry``, ``confidence``, ``error_group``, ``evidence`` theo
      contract (cũng chấp nhận dạng dict với cùng các khóa). Hàm sửa
      trực tiếp object/dict này và trả lại chính nó, để bên gọi không
      phải xử lý hai kiểu kết quả khác nhau.
    - ``rulebook``: rulebook của split đang chạy. Tham số này hiện KHÔNG
      làm thay đổi ngưỡng hay luật unanswerable, vì ngưỡng 0,65 và các
      điều kiện unanswerable là luật của contract, không phải luật
      nghiệp vụ trong rulebook train; giữ tham số để API ổn định khi
      rulebook tương lai có quy định riêng về ngưỡng, và để bên gọi
      không phải sửa chữ ký khi tích hợp.

    Đầu ra: chính ``solve_result`` đã sửa (status/entry/error_group/
    confidence/evidence được cập nhật theo quyết định).

    Trường hợp biên:

    - Solver đã trả ``abstain``: giữ nguyên (không áp format, vì không
      có giá trị để kiểm tra), nhưng vẫn kiểm tra unanswerable trước để
      nâng lên unanswerable khi có bằng chứng mạnh về thiếu dữ liệu.
    - Solver đã trả ``unanswerable``: giữ nguyên toàn bộ, kể cả lý do
      và error_group gốc.
    - ``solve_result`` là ``None``: không có gì để quyết định, trả
      ``None`` (bên gọi phải xử lý như abstain); hàm không tự tạo một
      SolveResult giả vì không biết kiểu mà bên gọi mong đợi.
    - Entry thiếu hoặc status lạ: coi như answered để đi qua kiểm tra
      format, và format sẽ bắt lỗi thiếu entry thay vì crash.
    """
    if solve_result is None:
        return None
    status = _get(solve_result, "status", None)
    evidence = _evidence_dict(solve_result, None)
    # Đồng bộ evidence trở lại solve_result nếu trước đó nó không phải
    # dict (ví dụ solver để evidence = None): các bước sau ghi chú thích
    # vào dict này, và bên gọi đọc lại được chúng.
    if not isinstance(_get(solve_result, "evidence", None), dict):
        _set(solve_result, "evidence", evidence)

    # Bước 0: solver đã kết luận unanswerable thì giữ nguyên.
    if status == "unanswerable":
        return solve_result

    # Bước 1: kiểm tra unanswerable TRƯỚC ngưỡng.
    reason = _unanswerable_reason(parsed, view, evidence)
    if reason is not None:
        _set(solve_result, "status", "unanswerable")
        _set(solve_result, "entry", _unanswerable_entry(solve_result, reason))
        _set(solve_result, "error_group", "unanswerable")
        evidence["unanswerable_reason"] = reason
        return solve_result

    # Solver đã abstain (và không đủ căn cứ nâng lên unanswerable): giữ nguyên.
    if status == "abstain":
        return solve_result

    # Bước 2: áp ngưỡng 0,65. Confidence lấy từ solver trước; nếu solver
    # chưa gán thì tự tính từ evidence (compute_confidence cũng ghi chi
    # tiết khoản trừ vào evidence để truy vết).
    confidence = _get(solve_result, "confidence", None)
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        confidence = compute_confidence(parsed, view, evidence)
        _set(solve_result, "confidence", confidence)
    if isinstance(confidence, (int, float)) and confidence < GATE_THRESHOLD:
        _set(solve_result, "status", "abstain")
        _set(solve_result, "entry", _abstain_entry(solve_result))
        # error_group GIỮ NGUYÊN theo contract, không ghi đè, kể cả None.
        evidence["gate_decision"] = "abstain_below_threshold"
        return solve_result

    # Bước 3: kiểm tra format SAU khi solver trả entry.
    entry = _get(solve_result, "entry", None)
    expected_type = _get(parsed, "answer_type", None)
    if not isinstance(expected_type, str):
        expected_type = None
    is_valid, format_reason = validate_entry_format(entry, expected_type)
    if not is_valid:
        _set(solve_result, "status", "abstain")
        _set(solve_result, "entry", _abstain_entry(solve_result))
        _set(solve_result, "error_group", "format")
        evidence["format_error"] = format_reason
        return solve_result

    # Qua cả ba bước: giữ nguyên đáp án của solver.
    return solve_result

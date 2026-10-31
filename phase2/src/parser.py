# -*- coding: utf-8 -*-
"""Phân tích câu hỏi tiếng Việt cho DataMate (Giai đoạn 2).

Mục đích của module này
------------------------
Biến một câu hỏi tiếng Việt đời thường (thường không dấu, viết tắt,
thiếu chủ ngữ) thành một :class:`ParsedQuestion` có 4 trục, để tầng
solver chỉ còn việc tính toán trên dữ liệu đã chuẩn hoá mà không phải
đoán lại ý câu hỏi:

- Đo cái gì (``measure``): doanh thu, số đơn, số lượng, tỉ lệ huỷ,
  tăng trưởng, công nợ, nhập hàng, xếp hạng...
- Của ai/cái gì (``entity_kind`` / ``entity_key``): sản phẩm, khách
  hàng, nhóm hàng, hình thức thanh toán, kênh, nhân viên, hay không có.
- Kỳ nào (``period_start`` / ``period_end`` / ``period_kind`` /
  ``period_months``): kỳ tuyệt đối, kỳ tương đối theo ``as_of``, ảnh
  chụp đến hết một ngày, hay nhiều tháng.
- Dạng đáp án (``answer_type``): number, percent, string, date,
  boolean, list, table, hay unknown.

Thứ tự xử lý là cố tình, và là điểm quan trọng nhất của module:

1. Chuẩn hoá không dấu + mở rộng viết tắt (xem
   :func:`expand_abbreviations`).
2. Tách tên sản phẩm/khách hàng TRƯỚC khi suy type. Nhờ vậy dấu ``%``
   trong tên thuốc (ví dụ ``Otrivin 0,1%``) không bị hiểu nhầm thành
   đáp án percent, và đơn vị trong tên sản phẩm không bị hiểu nhầm
   thành đơn vị được hỏi.
3. Xác định xếp hạng (câu "nhất", top-k) TRƯỚC khi suy type. Nhờ vậy
   "ngày nào doanh thu cao nhất" ra date, "sản phẩm bán chạy nhất" ra
   string, "top 3" ra list — thay vì mọi câu "nhất" đều bị đoán là một
   type duy nhất.
4. Chỉ sau đó mới suy ``measure`` và ``answer_type`` trên phần câu còn
   lại (đã che tên thực thể).

Nguyên tắc tổng quát hoá
------------------------
Module này không biết gì về business_id, id câu hỏi, tên cột train,
ngành train hay đáp án train. Mọi nhận diện đều dựa trên:

- Từ khoá ngôn ngữ chung (không dấu) như "doanh thu", "don huy",
  "ban chay", "nhat", "tuan roi"...
- Danh sách sản phẩm/khách hàng của chính shop đang chạy (tham số
  ``products`` / ``customers``), khớp bằng token, chấp nhận tên rút gọn.
- Các hàm khoảng thời gian của phase1 (``week_range``,
  ``month_range``, ``month_to_date_range``, ``last_n_days_range``,
  ``tet_range``...) và lịch của split đang chạy, không hard-code lịch
  Tết hay ngày lễ nào.

Khi không nhận diện được chắc chắn, module trả ``measure='unknown'``,
``answer_type='unknown'`` và ``confidence_parse`` thấp, kèm lý do trong
``parse_notes`` — để tầng gate abstain thay vì đoán số. Đây là hành vi
đúng theo luật chấm PA (sai bị -1, abstain được 0).

Chỉ dùng thư viện chuẩn Python, không gọi mạng/API, chạy offline và
deterministic (thứ tự thực thể được sắp xếp cố định trước khi khớp).
"""

from __future__ import annotations

import calendar as _calendar_mod
import datetime
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["ParsedQuestion", "expand_abbreviations", "parse_time", "parse_question"]

# ---------------------------------------------------------------------------
# Nạp hàm chuẩn hoá của phase1 bằng đường dẫn tương đối từ file này.
# ---------------------------------------------------------------------------
# File này nằm ở <datamate>/phase2/src/parser.py, nên phase1/src nằm ở
# parents[2]/phase1/src. Không hard-code đường dẫn tuyệt đối của máy,
# để code chạy được ở mọi nơi checkout repo, kể cả trong Docker.
_PHASE1_SRC = Path(__file__).resolve().parents[2] / "phase1" / "src"
if _PHASE1_SRC.is_dir() and str(_PHASE1_SRC) not in sys.path:
    sys.path.insert(0, str(_PHASE1_SRC))

try:  # Cố gắng dùng đúng hàm phase1 đã kiểm chứng ở Giai đoạn 1.
    from text_utils import normalize_key as _normalize_key
    from text_utils import strip_diacritics as _strip_diacritics
    from normalizers import parse_money as _parse_money
    from rulebook import last_n_days_range as _last_n_days_range
    from rulebook import month_range as _month_range
    from rulebook import month_to_date_range as _month_to_date_range
    from rulebook import tet_range as _tet_range
    from rulebook import week_range as _week_range
    _PHASE1_AVAILABLE = True
except Exception:  # pragma: no cover - dự phòng khi chạy file rời.
    # Nếu phase1 không nạp được (ví dụ file bị copy riêng ra ngoài),
    # dùng bản dự phòng tối thiểu viết lại cùng quy ước, để parser vẫn
    # chạy được thay vì crash cả pipeline. Bản dự phòng này không thay
    # thế phase1 khi phase1 có mặt: nhánh try ở trên luôn được ưu tiên.
    _PHASE1_AVAILABLE = False

    def _strip_diacritics(text):
        """Bản dự phòng: bỏ dấu, giữ nguyên hoa/thường và dấu câu."""
        if text is None:
            return ""
        s = text if isinstance(text, str) else str(text)
        decomposed = unicodedata.normalize("NFD", s)
        without = "".join(
            ch for ch in decomposed if unicodedata.category(ch) != "Mn"
        )
        return unicodedata.normalize(
            "NFC", without.translate(str.maketrans({"đ": "d", "Đ": "D"}))
        )

    def _normalize_key(text):
        """Bản dự phòng: thường hoá, bỏ dấu, chỉ giữ chữ/số."""
        if text is None:
            return ""
        s = unicodedata.normalize("NFC", str(text)).lower()
        s = _strip_diacritics(s)
        return " ".join("".join(ch if ch.isalnum() else " " for ch in s).split())

    def _parse_money(value):
        """Bản dự phòng rất gọn: chỉ parse số thuần và hậu tố k/tr."""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip().lower()
        s = _strip_diacritics(s)
        m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(trieu|tr|m)?", s)
        if m and m.group(2):
            return float(m.group(1).replace(",", ".")) * 1_000_000
        m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*k", s)
        if m:
            return float(m.group(1).replace(",", ".")) * 1000
        try:
            return float(s.replace(".", "").replace(",", "."))
        except ValueError:
            return None

    def _month_range(year, month):
        """Bản dự phòng: cả tháng từ ngày 1 đến hết ngày cuối tháng."""
        last = _calendar_mod.monthrange(year, month)[1]
        return (datetime.date(year, month, 1), datetime.date(year, month, last))

    def _week_range(any_date):
        """Bản dự phòng: tuần chứa ngày đó, thứ Hai đến Chủ nhật."""
        monday = any_date - datetime.timedelta(days=any_date.weekday())
        return (monday, monday + datetime.timedelta(days=6))

    def _month_to_date_range(as_of):
        """Bản dự phòng: ngày 1 của tháng đến hết ``as_of``."""
        return (datetime.date(as_of.year, as_of.month, 1), as_of)

    def _last_n_days_range(n, as_of):
        """Bản dự phòng: N ngày kể cả hôm nay, ngày cuối là ``as_of``."""
        if n <= 0:
            raise ValueError("n phải >= 1")
        return (as_of - datetime.timedelta(days=n - 1), as_of)

    def _tet_range(lunar_year, calendar):
        """Bản dự phòng: không tra được lịch thì trả None, không đoán."""
        return None


# ---------------------------------------------------------------------------
# Bảng viết tắt và từ vựng nhận diện (ngôn ngữ chung, không theo shop)
# ---------------------------------------------------------------------------

# Chỉ thay từ đứng độc lập (xem expand_abbreviations): "sp" trong
# "san pham" viết tắt thì đổi, nhưng "sp" là một phần của từ dài hơn
# (ví dụ mã sản phẩm) thì không đổi, vì regex dùng ranh giới từ.
_ABBREVIATIONS = {
    "dthu": "doanh thu",
    "bnhiu": "bao nhieu",
    "bnh": "bao nhieu",
    "nhiu": "bao nhieu",
    "bn": "bao nhieu",
    "dc": "duoc",
    "sp": "san pham",
    "kh": "khach hang",
    "nv": "nhan vien",
    "ncc": "nha cung cap",
    "sl": "so luong",
    "hnay": "hom nay",
    "hqua": "hom qua",
    "homnay": "hom nay",
    "homqua": "hom qua",
    "thangnay": "thang nay",
    "thangtruoc": "thang truoc",
    "tuantruoc": "tuan truoc",
    "tuanroi": "tuan roi",
    "tienmat": "tien mat",
    "chuyenkhoan": "chuyen khoan",
    "vs": "voi",
    "ko": "khong",
}

# Đơn vị số lượng thường gặp, đã chuẩn hoá không dấu (dạng normalize_key).
# "bao" được xử lý riêng (xem _extract_unit) vì "bao nhieu" cũng chứa
# từ "bao" nhưng đó không phải đơn vị.
_UNITS = [
    "chai", "ly", "vien", "hop", "thung", "goi", "lon", "tuyp", "vi",
    "cai", "chiec", "bo", "kg", "gam", "lit", "ml", "bao",
]

# Hình thức thanh toán: cụm không dấu -> cách hiển thị có dấu.
_PAYMENTS = {
    "tien mat": "tiền mặt",
    "chuyen khoan": "chuyển khoản",
    "cod": "COD",
    "the": "thẻ",
    "vi dien tu": "ví điện tử",
    "tra sau": "trả sau",
    "ghi no": "ghi nợ",
}

# Kênh bán: tên riêng giữ nguyên khi hiển thị.
_CHANNELS = [
    "shopee", "lazada", "tiktok", "facebook", "website", "grab", "shopeefood",
]


# ---------------------------------------------------------------------------
# Dataclass kết quả
# ---------------------------------------------------------------------------

@dataclass
class ParsedQuestion:
    """Kết quả phân tích một câu hỏi, theo đúng API_CONTRACT mục 2.

    Ý nghĩa từng trường (đọc kỹ trước khi dùng ở solver/gate):

    - ``measure``: cái cần đo. Một trong các giá trị của contract
      (``'revenue'``, ``'order_count'``, ``'quantity'``,
      ``'cancel_rate'``, ``'revenue_share'``, ``'growth_percent'``,
      ``'growth_amount'``, ``'top_product'``, ``'top_customer'``,
      ``'top_employee'``, ``'best_day'``, ``'best_month'``,
      ``'debt_total'``, ``'debt_top'``, ``'import_total'``,
      ``'avg_price'``, ``'price_on_day'``, ``'continuous_growth'``,
      ``'revenue_by_payment'``, ``'revenue_by_group'``,
      ``'revenue_by_channel'``, ``'quantity_by_unit'``,
      ``'unknown'``). Lưu ý: doanh thu của một sản phẩm cụ thể được
      trả về là ``'revenue'`` kèm ``entity_kind='product'`` (không dùng
      ``'product_revenue'`` riêng), để solver dùng chung một đường lọc
      doanh thu với entity, tránh một measure riêng không được bao phủ.
    - ``entity_kind`` / ``entity_key`` / ``entity_display``: thực thể
      chính của câu. ``entity_key`` lấy đúng trường ``'key'`` của
      danh sách products/customers (mã nếu có, nếu không là tên chuẩn
      hoá giữ dấu); với nhóm thì key là tên nhóm chuẩn hoá không dấu;
      với thanh toán thì key là cụm không dấu. Không có thực thể thì
      kind là ``'none'`` và key/display là ``None``.
    - ``period_start`` / ``period_end``: khoảng ngày đã giải xong.
      Với ảnh chụp (``period_kind='snapshot'``), ``period_start`` là
      ``None`` vì ý nghĩa là "từ đầu dữ liệu đến hết ngày đó", không
      phải một kỳ có ngày bắt đầu — solver phải hiểu theo nghĩa này,
      không được tự gán start = end hay start = đầu tháng.
    - ``period_kind``: ``'absolute'`` (tháng/năm/quý ghi rõ),
      ``'relative'`` (tháng này, tuần rồi, hôm nay... tính theo as_of),
      ``'snapshot'`` (đến hết ngày X), ``'multi_month'`` (nhiều tháng:
      khoảng tháng A–B, quý không tính vào đây, 3 tháng liên nhau...),
      ``'none'`` (không parse được kỳ, không đoán kỳ).
    - ``period_months``: chỉ điền khi ``period_kind='multi_month'``,
      là danh sách ``(năm, tháng)`` theo thứ tự thời gian, để solver
      tính từng tháng (ví dụ kiểm tra tăng liên tục). Các kỳ khác để
      rỗng, kể cả quý/năm, vì chúng đã được biểu diễn đủ bằng
      start/end.
    - ``ranking_k``: ``None`` nếu không phải câu xếp hạng; ``1`` nếu
      nói "nhất"; số N nếu nói "top N" hay "N ... nhất".
    - ``ranking_target``: xếp hạng cái gì: ``'product'``,
      ``'customer'``, ``'day'``, ``'month'``, ``'employee'``, hay
      ``'none'``. Được xác định TRƯỚC khi suy measure/answer_type.
    - ``ranking_metric``: chỉ tiêu xếp hạng (chỉ có ý nghĩa khi có
      xếp hạng, còn lại là ``None``): ``'quantity'`` cho sản phẩm
      bán chạy nhất (theo số lượng quy đổi, đúng rulebook), ``'revenue'``
      cho khách/top theo doanh thu và cho ngày/tháng tốt nhất,
      ``'profit'`` cho mặt hàng lời/lãi nhất (khi parser nhận diện
      được cụm lời/lãi), ``'debt'`` cho khách nợ nhiều nhất. Solver
      PHẢI dùng trường này thay vì mặc định mọi xếp hạng theo doanh
      thu; trường này được thêm ở Giai đoạn 2 tích hợp để sửa lỗi
      solver cũ xếp top_product theo doanh thu.
    - ``unit``: đơn vị hỏi số lượng, đã normalize_key (ví dụ ``'chai'``,
      ``'ly'``, ``'vien'``, ``'kg'``, ``'hop'``), hoặc ``None``.
    - ``threshold_money`` / ``threshold_op``: ngưỡng tiền cho câu
      "đơn có doanh thu từ X trở lên" (op mặc định ``'gte'``).
    - ``answer_type``: dạng đáp án kỳ vọng. ``'best_month'`` vẫn là
      ``'date'`` (định dạng YYYY-MM do solver/format lo, parser chỉ
      nói đây là đáp án ngày/tháng).
    - ``confidence_parse``: chỉ độ chắc của parse, 0..1, không phải
      confidence cuối của cả pipeline (gate sẽ trừ thêm theo bằng
      chứng khác). Khớp tên rút gọn, kỳ suy năm, measure không rõ đều
      làm giảm giá trị này và phải ghi lý do vào ``parse_notes``.
    - ``normalized_text``: câu hỏi sau chuẩn hoá không dấu + mở rộng
      viết tắt, dùng để truy vết và để solver kiểm tra lại từ khoá.
    """

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


# ---------------------------------------------------------------------------
# Chuẩn hoá và mở rộng viết tắt
# ---------------------------------------------------------------------------

def expand_abbreviations(text: str) -> str:
    """Chuẩn hoá câu hỏi: thường hoá, bỏ dấu, mở rộng viết tắt.

    Đầu vào:
        text: câu hỏi gốc, có thể có dấu hoặc không dấu, có viết tắt.

    Đầu ra:
        Chuỗi thường, không dấu, đã thay viết tắt bằng dạng đầy đủ,
        khoảng trắng đã gộp. Ví dụ: ``'dthu thang nay la bnhiu'`` ->
        ``'doanh thu thang nay la bao nhieu'``; ``'sp ban dc nhieu
        nhat tuan roi'`` -> ``'san pham ban duoc nhieu nhat tuan roi'``.

    Vì sao chỉ thay từ đứng độc lập:
        Viết tắt chỉ có nghĩa khi nó là một từ riêng. Nếu thay bằng
        chuỗi con (ví dụ thay "sp" ở mọi chỗ), mã sản phẩm hay tên
        riêng chứa "sp" sẽ bị phá hỏng và bước khớp tên sau đó sẽ sai.
        Hàm dùng regex ranh giới từ (``\\b``) trên bản không dấu, nên
        "sp" trong "sp001" hay "san pham" đều không bị chạm tới, và
        "tuan roi" được giữ nguyên ý nghĩa (tuần trước) để parse_time
        xử lý, không cần đổi thành "tuan truoc".

    Trường hợp biên:
        - ``None`` hoặc chuỗi rỗng -> ``''`` (không crash).
        - Giá trị không phải chuỗi được chuyển thành chuỗi trước.
        - Từ viết tắt không có trong bảng (ví dụ từ vay mượn "check",
          lỗi gõ "bans") được giữ nguyên: thà giữ từ lạ còn hơn đoán
          sai nghĩa rồi suy type sai theo.
    """
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    if not s.strip():
        return ""
    # Thường hoá + bỏ dấu trước: bảng viết tắt viết bằng dạng không dấu.
    s = _strip_diacritics(s.lower())
    # Thay từng từ đứng độc lập; dùng hàm thay để tra đúng bảng.
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(k) for k in _ABBREVIATIONS) + r")\b"
    )

    def _replace(match):
        """Đổi một từ viết tắt thành dạng đầy đủ theo bảng."""
        return _ABBREVIATIONS[match.group(1)]

    s = pattern.sub(_replace, s)
    return " ".join(s.split())


# ---------------------------------------------------------------------------
# Tiện ích nội bộ cho parse_time
# ---------------------------------------------------------------------------

def _safe_date(year, month, day):
    """Tạo ``datetime.date`` an toàn, trả ``None`` khi ngày không hợp lệ.

    Vì sao cần: câu hỏi có thể ghi ngày sai (31/2) hoặc tháng ngoài
    1–12; parse_time không được crash cả pipeline vì một ngày sai, mà
    phải trả "không parse được" để tầng trên abstain.
    """
    try:
        return datetime.date(int(year), int(month), int(day))
    except (ValueError, TypeError, OverflowError):
        return None


def _year_for_month(month, as_of):
    """Suy năm cho "tháng M" không ghi năm, theo luật rulebook.

    Luật: tháng M của năm nay nếu tháng đó không sau tháng của as_of;
    nếu tháng M sau tháng của as_of thì là tháng đó của năm trước.
    Ví dụ as_of 2026-03-04: "thang 3" -> 2026, "thang 8" -> 2025.
    So sánh theo tháng (không theo ngày trong tháng), vì câu hỏi chỉ
    nêu tháng, không nêu ngày, nên "sau as_of" chỉ có thể hiểu theo
    tháng. Hàm này là suy luận có luật rõ ràng, không phải đoán mò:
    khi năm đã suy, parse_question sẽ ghi chú và giảm nhẹ confidence.
    """
    if month > as_of.month:
        return as_of.year - 1
    return as_of.year


def _year_for_day_month(month, day, as_of):
    """Suy năm cho "ngày D tháng M" không ghi năm, cùng luật như tháng.

    Nếu ngày/tháng đó sau as_of thì lấy năm trước, ngược lại lấy năm
    nay. So sánh cả ngày lẫn tháng (vì ở đây đã có ngày cụ thể).
    """
    candidate = _safe_date(as_of.year, month, day)
    if candidate is not None and candidate > as_of:
        return as_of.year - 1
    return as_of.year


def _months_between(start_year, start_month, end_year, end_month):
    """Liệt kê các (năm, tháng) từ tháng bắt đầu đến tháng kết thúc.

    Trả list rỗng nếu khoảng không hợp lệ (tháng kết thúc trước tháng
    bắt đầu) hoặc khoảng quá dài (> 60 tháng, dấu hiệu parse sai do
    regex bắt nhầm số). Giới hạn 60 tháng là van an toàn chống vòng
    lặp vô hạn, không phải giới hạn nghiệp vụ: câu hỏi thực tế không
    bao giờ hỏi khoảng trên 5 năm liền một lúc.
    """
    months = []
    year, month = start_year, start_month
    while (year, month) <= (end_year, end_month):
        months.append((year, month))
        if len(months) > 60:
            return []
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return months


def _month_range_tuple(year, month):
    """Trả (start, end) của cả tháng, dùng hàm phase1 nếu có."""
    return _month_range(year, month)


# ---------------------------------------------------------------------------
# Giải nghĩa thời gian
# ---------------------------------------------------------------------------

def parse_time(text_norm: str, as_of: datetime.date, calendar, rulebook):
    """Giải nghĩa kỳ thời gian trong câu hỏi đã chuẩn hoá không dấu.

    Đầu vào:
        text_norm: câu hỏi đã chuẩn hoá (không dấu, thường hoá, đã mở
            rộng viết tắt — hàm vẫn tự chuẩn hoá lại để an toàn nếu
            caller đưa câu gốc có dấu).
        as_of: ngày "hôm nay" của câu hỏi, mốc của mọi cách nói tương
            đối (tháng này, tuần rồi, hôm qua, N ngày gần nhất...).
        calendar: đối tượng Calendar của phase1 (dùng để tra Tết) hoặc
            ``None`` nếu không có lịch — khi đó câu Tết trả "none".
        rulebook: đối tượng Rulebook của phase1; hiện chỉ giữ để đúng
            chữ ký contract và để tương lai dùng luật tuần/tháng mới,
            vì các hàm phase1 đã cài đúng luật train (tuần bắt đầu thứ
            Hai, tháng này cắt ở as_of). Không dùng rulebook để đoán
            luật ngoài file.

    Đầu ra:
        Tuple ``(start, end, kind, months)`` theo contract:
        ``start``/``end`` là ``datetime.date`` hoặc ``None``;
        ``kind`` là ``'absolute'`` / ``'relative'`` / ``'snapshot'`` /
        ``'multi_month'`` / ``'none'``; ``months`` chỉ điền khi kind là
        ``'multi_month'``. Không parse được thì trả
        ``(None, None, 'none', [])`` — không đoán kỳ, vì đoán kỳ sai sẽ
        tính sai toàn bộ đáp án, tệ hơn abstain.

    Thứ tự thử là cố tình, từ đặc thù đến tổng quát, để cụm dài không
    bị cụm ngắn "cướp" mất:

    1. Tết (cần tra lịch, và chứa từ "nam" dễ bị hiểu thành kỳ năm).
    2. Khoảng nhiều tháng: "3 tháng liên nhau kết thúc ở tháng M/Y",
       "từ tháng A đến tháng B".
    3. Khoảng ngày "từ X đến Y".
    4. Ảnh chụp "đến hết ngày D" (kind snapshot, start None).
    5. "từ đầu năm", "N ngày gần nhất", "hôm nay/hôm qua",
       "tuần này/trước", "tháng này/trước".
    6. Ngày đơn lẻ "ngày D".
    7. Quý, tháng có năm, tháng không năm, năm.
    8. Không khớp gì -> 'none'.

    Trường hợp biên cần nhớ:
    - "tháng này" cắt ở as_of (month-to-date), không phải cả tháng.
    - "tuần này" từ thứ Hai đến hết hôm nay, không phải đến Chủ nhật.
    - "tuần trước/tuần rồi" là CẢ tuần liền trước (thứ Hai–Chủ nhật).
    - "tháng trước" đứng một mình là cả tháng trước (tháng trước đã
      kết thúc trọn vẹn nên không cần cắt cùng số ngày; việc so sánh
      cùng số ngày là việc của solver khi tính tăng trưởng, không phải
      của parser khi giải kỳ được hỏi).
    - "N ngày gần nhất" gồm cả hôm nay, ngày cuối là hôm nay.
    - Kỳ lịch sử (tháng/năm có năm) KHÔNG bị cắt theo as_of: as_of chỉ
      dùng cho cách nói tương đối và suy năm khi thiếu năm.
    """
    if text_norm is None:
        return (None, None, "none", [])
    # Chuẩn hoá lại để hàm chịu được cả câu gốc có dấu lẫn câu đã chuẩn.
    text = expand_abbreviations(text_norm)
    if not text:
        return (None, None, "none", [])

    # --- 1. Tết: "tet nam Y", "dip tet nam Y", "tet" không năm. ---
    # Phải thử trước kỳ năm, vì "tet nam 2026" cũng chứa "nam 2026".
    tet_match = re.search(r"\btet\b(?:\s+nam\s+(\d{4}))?", text)
    if tet_match:
        lunar_year = (
            int(tet_match.group(1)) if tet_match.group(1) else as_of.year
        )
        tet = None
        if calendar is not None:
            try:
                tet = _tet_range(lunar_year, calendar)
            except Exception:
                # Lịch không đủ mốc hoặc sai dạng: coi như không tra được.
                tet = None
        if tet is not None:
            return (tet[0], tet[1], "absolute", [])
        # Có nhắc Tết nhưng không tra được lịch: không đoán khoảng Tết
        # bằng lịch dương cố định, trả 'none' để tầng trên abstain.
        return (None, None, "none", [])

    # --- 2a. 3 tháng liên nhau kết thúc ở tháng M/Y. ---
    # Dạng: "3 thang lien nhau ket thuc o thang 8/2026",
    # "3 thang lien tiep ket thuc thang 8 nam 2026".
    three_match = re.search(
        r"3 thang (?:lien nhau|lien tiep|gan nhat)?.*?ket thuc (?:o )?thang\s+"
        r"(\d{1,2})\s*(?:/\s*(\d{4})|nam\s+(\d{4}))?",
        text,
    )
    if three_match:
        end_month = int(three_match.group(1))
        end_year = None
        if three_match.group(2):
            end_year = int(three_match.group(2))
        elif three_match.group(3):
            end_year = int(three_match.group(3))
        else:
            end_year = _year_for_month(end_month, as_of)
        if 1 <= end_month <= 12:
            # Lùi 2 tháng từ tháng kết thúc để được 3 tháng liên nhau.
            months = []
            y, m = end_year, end_month
            for _ in range(3):
                months.append((y, m))
                if m == 1:
                    y, m = y - 1, 12
                else:
                    m -= 1
            months.reverse()
            start = _month_range_tuple(months[0][0], months[0][1])[0]
            end = _month_range_tuple(months[-1][0], months[-1][1])[1]
            return (start, end, "multi_month", months)

    # --- 2b. Từ tháng A đến tháng B (có thể có năm ở một hoặc hai đầu). ---
    month_range_match = re.search(
        r"tu thang\s+(\d{1,2})(?:\s+nam\s+(\d{4})|\s*/\s*(\d{4}))?"
        r"\s+den thang\s+(\d{1,2})(?:\s+nam\s+(\d{4})|\s*/\s*(\d{4}))?",
        text,
    )
    if month_range_match:
        start_month = int(month_range_match.group(1))
        start_year = None
        if month_range_match.group(2):
            start_year = int(month_range_match.group(2))
        elif month_range_match.group(3):
            start_year = int(month_range_match.group(3))
        end_month = int(month_range_match.group(4))
        end_year = None
        if month_range_match.group(5):
            end_year = int(month_range_match.group(5))
        elif month_range_match.group(6):
            end_year = int(month_range_match.group(6))
        # Suy năm khi thiếu: nếu chỉ có một năm thì năm đó áp cho cả hai
        # đầu khi khoảng không quấn năm; nếu không có năm nào thì suy
        # theo as_of (tháng kết thúc sau as_of -> cả khoảng lùi 1 năm,
        # khoảng quấn năm thì đầu thuộc năm trước).
        if start_year is None and end_year is None:
            if start_month <= end_month:
                if end_month > as_of.month:
                    start_year = end_year = as_of.year - 1
                else:
                    start_year = end_year = as_of.year
            else:  # Quấn năm, ví dụ từ tháng 11 đến tháng 2.
                start_year, end_year = as_of.year - 1, as_of.year
        elif start_year is None:
            start_year = end_year if start_month <= end_month else end_year - 1
        elif end_year is None:
            # Khoảng quấn năm (ví dụ từ tháng 11 đến tháng 2) thì đầu
            # và cuối khác năm; ngược lại cùng năm với đầu.
            end_year = start_year + 1 if start_month > end_month else start_year
        if 1 <= start_month <= 12 and 1 <= end_month <= 12:
            start = _month_range_tuple(start_year, start_month)[0]
            end = _month_range_tuple(end_year, end_month)[1]
            if start <= end:
                months = _months_between(
                    start_year, start_month, end_year, end_month
                )
                if len(months) > 1:
                    return (start, end, "multi_month", months)
                return (start, end, "absolute", [])

    # --- 3. Từ ngày X đến ngày Y (nhiều cách viết ngày). ---
    # Dạng ISO: "tu 2026-01-01 den 2026-01-31".
    iso_range = re.search(
        r"tu\s+(\d{4})-(\d{1,2})-(\d{1,2})\s+den\s+"
        r"(\d{4})-(\d{1,2})-(\d{1,2})",
        text,
    )
    if iso_range:
        start = _safe_date(
            iso_range.group(1), iso_range.group(2), iso_range.group(3)
        )
        end = _safe_date(
            iso_range.group(4), iso_range.group(5), iso_range.group(6)
        )
        if start is not None and end is not None and start <= end:
            return (start, end, "absolute", [])
    # Dạng D/M/Y hoặc D/M, có hoặc không có từ "ngay".
    date_range = re.search(
        r"tu\s+(?:ngay\s+)?(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\s+den\s+"
        r"(?:ngay\s+)?(\d{1,2})/(\d{1,2})(?:/(\d{4}))?",
        text,
    )
    if date_range:
        s_day, s_month = int(date_range.group(1)), int(date_range.group(2))
        e_day, e_month = int(date_range.group(4)), int(date_range.group(5))
        s_year = (
            int(date_range.group(3))
            if date_range.group(3)
            else _year_for_day_month(s_month, s_day, as_of)
        )
        e_year = (
            int(date_range.group(6))
            if date_range.group(6)
            else _year_for_day_month(e_month, e_day, as_of)
        )
        start = _safe_date(s_year, s_month, s_day)
        end = _safe_date(e_year, e_month, e_day)
        if start is not None and end is not None and start <= end:
            return (start, end, "absolute", [])
    # Dạng "tu ngay D thang M den ngay D2 thang M2 (nam Y)".
    date_range_words = re.search(
        r"tu\s+(?:ngay\s+)?(\d{1,2})\s+thang\s+(\d{1,2})"
        r"(?:\s+nam\s+(\d{4}))?\s+den\s+(?:ngay\s+)?(\d{1,2})\s+thang\s+"
        r"(\d{1,2})(?:\s+nam\s+(\d{4}))?",
        text,
    )
    if date_range_words:
        s_day, s_month = int(date_range_words.group(1)), int(date_range_words.group(2))
        e_day, e_month = int(date_range_words.group(4)), int(date_range_words.group(5))
        s_year = (
            int(date_range_words.group(3))
            if date_range_words.group(3)
            else _year_for_day_month(s_month, s_day, as_of)
        )
        e_year = (
            int(date_range_words.group(6))
            if date_range_words.group(6)
            else (s_year if (s_month, s_day) <= (e_month, e_day) else s_year)
        )
        # Nếu đầu không có năm mà cuối có năm và khoảng quấn năm thì
        # đầu thuộc năm trước năm cuối; trường hợp này hiếm nên chỉ
        # điều chỉnh khi ngày cuối rõ ràng trước ngày đầu cùng năm.
        if date_range_words.group(3) is None and date_range_words.group(6):
            probe_start = _safe_date(e_year, s_month, s_day)
            probe_end = _safe_date(e_year, e_month, e_day)
            if (
                probe_start is not None
                and probe_end is not None
                and probe_start > probe_end
            ):
                s_year = e_year - 1
        start = _safe_date(s_year, s_month, s_day)
        end = _safe_date(e_year, e_month, e_day)
        if start is not None and end is not None and start <= end:
            return (start, end, "absolute", [])

    # --- 4. Ảnh chụp: "den het ngay D", "tinh den ngay D", "het ngay D". ---
    # start = None theo định nghĩa snapshot (xem docstring ParsedQuestion).
    snapshot_match = re.search(
        r"(?:den het|tinh den|den|het)\s+ngay\s+"
        r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?",
        text,
    )
    if snapshot_match:
        day, month = int(snapshot_match.group(1)), int(snapshot_match.group(2))
        year = (
            int(snapshot_match.group(3))
            if snapshot_match.group(3)
            else _year_for_day_month(month, day, as_of)
        )
        day_date = _safe_date(year, month, day)
        if day_date is not None:
            return (None, day_date, "snapshot", [])
    snapshot_words = re.search(
        r"(?:den het|tinh den|den|het)\s+ngay\s+(\d{1,2})\s+thang\s+"
        r"(\d{1,2})(?:\s+nam\s+(\d{4}))?",
        text,
    )
    if snapshot_words:
        day, month = int(snapshot_words.group(1)), int(snapshot_words.group(2))
        year = (
            int(snapshot_words.group(3))
            if snapshot_words.group(3)
            else _year_for_day_month(month, day, as_of)
        )
        day_date = _safe_date(year, month, day)
        if day_date is not None:
            return (None, day_date, "snapshot", [])
    # "den het hom nay", "tinh den hom nay", "den nay": ảnh chụp tại as_of.
    # Ngoại lệ: "tu dau nam den nay" phải do nhánh "tu dau nam" xử lý
    # (khoảng từ 01/01 đến as_of), nên không được nhận ở đây thành snapshot.
    if "tu dau nam" not in text and re.search(
        r"(?:den het|tinh den|den)\s+(?:hom nay|nay)\b", text
    ):
        return (None, as_of, "snapshot", [])

    # --- 5a. Từ đầu năm tới nay. ---
    if "tu dau nam" in text:
        return (
            datetime.date(as_of.year, 1, 1),
            as_of,
            "relative",
            [],
        )

    # --- 5b. N ngày gần nhất / N ngày qua / N ngày gần đây. ---
    last_n_match = re.search(
        r"(\d+)\s+ngay\s+(?:gan nhat|gan day|qua|vua qua|tro lai day)", text
    )
    if last_n_match:
        n = int(last_n_match.group(1))
        if n >= 1:
            try:
                start, end = _last_n_days_range(n, as_of)
            except ValueError:
                return (None, None, "none", [])
            return (start, end, "relative", [])

    # --- 5c. Hôm nay / hôm qua. ---
    # "hom qua" phải thử trước "hom nay" không? Hai cụm không chồng nhau,
    # nhưng câu "hom nay" có thể đi kèm "hom qua" trong câu so sánh thì
    # kỳ chính vẫn là hôm nay nếu nó xuất hiện trước; ở đây ưu tiên cụm
    # xuất hiện trước trong câu để tránh chọn nhầm kỳ so sánh.
    today_at = text.find("hom nay")
    yesterday_at = text.find("hom qua")
    if today_at >= 0 and (yesterday_at < 0 or today_at < yesterday_at):
        return (as_of, as_of, "relative", [])
    if yesterday_at >= 0:
        yesterday = as_of - datetime.timedelta(days=1)
        return (yesterday, yesterday, "relative", [])

    # --- 5d. Tuần này / tuần trước / tuần rồi. ---
    if "tuan nay" in text:
        monday, _sunday = _week_range(as_of)
        # Tuần này chỉ tính đến hết hôm nay, không tính đến Chủ nhật
        # tương lai (bản ghi sau as_of coi như chưa xảy ra).
        return (monday, as_of, "relative", [])
    if (
        "tuan truoc" in text
        or "tuan roi" in text
        or "tuan vua roi" in text
        or "tuan vua qua" in text
    ):
        monday, sunday = _week_range(as_of - datetime.timedelta(days=7))
        return (monday, sunday, "relative", [])

    # --- 5e. Tháng này / tháng trước. ---
    if "thang nay" in text:
        start, end = _month_to_date_range(as_of)
        return (start, end, "relative", [])
    if "thang truoc" in text:
        if as_of.month == 1:
            prev_year, prev_month = as_of.year - 1, 12
        else:
            prev_year, prev_month = as_of.year, as_of.month - 1
        start, end = _month_range_tuple(prev_year, prev_month)
        return (start, end, "relative", [])

    # --- 6. Ngày đơn lẻ: "ngay D/M/Y", "ngay D thang M nam Y". ---
    single_slash = re.search(
        r"\bngay\s+(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", text
    )
    if single_slash:
        day, month = int(single_slash.group(1)), int(single_slash.group(2))
        year = (
            int(single_slash.group(3))
            if single_slash.group(3)
            else _year_for_day_month(month, day, as_of)
        )
        day_date = _safe_date(year, month, day)
        if day_date is not None:
            return (day_date, day_date, "absolute", [])
    single_words = re.search(
        r"\bngay\s+(\d{1,2})\s+thang\s+(\d{1,2})(?:\s+nam\s+(\d{4}))?",
        text,
    )
    if single_words:
        day, month = int(single_words.group(1)), int(single_words.group(2))
        year = (
            int(single_words.group(3))
            if single_words.group(3)
            else _year_for_day_month(month, day, as_of)
        )
        day_date = _safe_date(year, month, day)
        if day_date is not None:
            return (day_date, day_date, "absolute", [])

    # --- 7a. Quý Q (có năm hoặc suy năm như tháng). ---
    quarter_match = re.search(
        r"\bquy\s+([1-4])\s*(?:/\s*(\d{4})|nam\s+(\d{4}))?", text
    )
    if quarter_match:
        quarter = int(quarter_match.group(1))
        year = None
        if quarter_match.group(2):
            year = int(quarter_match.group(2))
        elif quarter_match.group(3):
            year = int(quarter_match.group(3))
        else:
            # Quý không năm: quý chứa tháng hiện tại thì năm nay, quý
            # sau tháng hiện tại thì năm trước (cùng luật suy năm).
            first_month = (quarter - 1) * 3 + 1
            year = as_of.year if first_month <= as_of.month else as_of.year - 1
        first_month = (quarter - 1) * 3 + 1
        last_month = first_month + 2
        start = _month_range_tuple(year, first_month)[0]
        end = _month_range_tuple(year, last_month)[1]
        return (start, end, "absolute", [])

    # --- 7b. Tháng M có năm: "thang M/nam Y", "thang M nam Y". ---
    month_year_match = re.search(
        r"\bthang\s+(\d{1,2})\s*(?:/\s*(\d{4})|nam\s+(\d{4}))", text
    )
    if month_year_match:
        month = int(month_year_match.group(1))
        year = int(month_year_match.group(2) or month_year_match.group(3))
        if 1 <= month <= 12:
            start, end = _month_range_tuple(year, month)
            return (start, end, "absolute", [])

    # --- 7c. Tháng M không năm: suy năm theo luật ở _year_for_month. ---
    month_only_match = re.search(r"\bthang\s+(\d{1,2})\b", text)
    if month_only_match:
        month = int(month_only_match.group(1))
        if 1 <= month <= 12:
            year = _year_for_month(month, as_of)
            start, end = _month_range_tuple(year, month)
            return (start, end, "absolute", [])

    # --- 7d. Năm Y đứng một mình. ---
    year_match = re.search(r"\bnam\s+(\d{4})\b", text)
    if year_match:
        year = int(year_match.group(1))
        return (
            datetime.date(year, 1, 1),
            datetime.date(year, 12, 31),
            "absolute",
            [],
        )

    # --- 8. Không khớp mẫu nào: không đoán kỳ. ---
    return (None, None, "none", [])


# ---------------------------------------------------------------------------
# Tiện ích nội bộ cho parse_question
# ---------------------------------------------------------------------------

def _tokens_of(normalized_text):
    """Tách câu đã chuẩn hoá thành danh sách token bằng normalize_key.

    Vì sao dùng normalize_key thay vì split thường: nó bỏ dấu câu còn
    sót (``%``, ``,``, ``.``) thành khoảng trắng, nên "0,1%" thành các
    token "0" "1" và khớp được với tên thuốc đã chuẩn hoá cùng cách.
    Nhược điểm là dấu ``%`` gốc bị mất ở tầng token — vì vậy việc kiểm
    tra ``%`` có nằm trong tên sản phẩm hay không được làm riêng bằng
    cách xem display gốc của thực thể đã khớp (xem parse_question).
    """
    return _normalize_key(normalized_text).split()


def _find_contiguous(haystack, needle):
    """Tìm vị trí bắt đầu của dãy token ``needle`` trong ``haystack``.

    Trả chỉ số bắt đầu, hoặc -1 nếu không có dãy liên tiếp đó. Dùng để
    nhận diện tên đầy đủ (mọi token theo đúng thứ tự) trước khi thử
    tên rút gọn.
    """
    if not needle:
        return -1
    n_len = len(needle)
    for start in range(len(haystack) - n_len + 1):
        if haystack[start:start + n_len] == needle:
            return start
    return -1


def _match_one_entity(question_tokens, entity_tokens):
    """Khớp một thực thể với câu hỏi, trả (vị trí, độ dài, có phải tên đầy đủ).

    Thứ tự thử, từ chắc đến lỏng:

    1. Tên đầy đủ liên tiếp trong câu -> chính xác, độ dài = số token
       của tên đầy đủ.
    2. Tên rút gọn: một đoạn liên tiếp trong câu, dài từ 2 token trở
       lên, mà mọi token đều nằm trong tập token của tên đầy đủ.
       Ví dụ "sting dau" trong câu khớp với "sting dau 330ml" vì cả hai
       token đều nằm trong tên đầy đủ. Đoạn được lấy dài nhất có thể
       (mở rộng khi token tiếp theo vẫn thuộc tên đầy đủ).

    Trả ``None`` nếu không khớp. Tên rút gọn 1 token không được chấp
    nhận, vì một từ chung (ví dụ "sua", "dau") xuất hiện ở quá nhiều
    sản phẩm và sẽ gây khớp nhầm hàng loạt.

    Sửa tích hợp Giai đoạn 2 (khóa lỗi khớp nhầm thực thể): đoạn rút
    gọn phải gồm các token KHÁC NHAU. Trước đây đoạn được mở rộng chỉ
    cần token tiếp theo thuộc tập tên đầy đủ, nên câu "bao nhiu" sau
    khi mở rộng viết tắt thành "bao bao nhieu" (từ "bao" gốc cộng từ
    "bao" do "nhiu" mở rộng thành) tạo ra đoạn "bao bao" dài 2 token
    và khớp nhầm với mọi khách/sản phẩm có tên chứa "bao" (ví dụ "Cao
    Anh Bảo") — câu hỏi tổng chung bị hiểu thành câu hỏi của một
    khách cụ thể và tính sai hoàn toàn. Nay token đã xuất hiện trong
    đoạn thì không được tính thêm lần nữa, nên "bao bao" chỉ còn dài
    1 token và bị loại; tên đầy đủ có token lặp thật (ví dụ "Huong
    Huong") vẫn khớp bằng nhánh tên đầy đủ ở trên, không bị ảnh hưởng.
    """
    if not entity_tokens:
        return None
    # 1. Tên đầy đủ liên tiếp.
    pos = _find_contiguous(question_tokens, entity_tokens)
    if pos >= 0:
        return (pos, len(entity_tokens), True)
    # 2. Tên rút gọn: đoạn liên tiếp dài nhất mà mọi token thuộc tên
    # đầy đủ VÀ các token trong đoạn khác nhau (xem docstring trên).
    entity_set = set(entity_tokens)
    best = None
    for start in range(len(question_tokens)):
        if question_tokens[start] not in entity_set:
            continue
        seen = {question_tokens[start]}
        end = start
        while end + 1 < len(question_tokens) \
                and question_tokens[end + 1] in entity_set \
                and question_tokens[end + 1] not in seen:
            seen.add(question_tokens[end + 1])
            end += 1
        length = end - start + 1
        if length >= 2:
            if best is None or length > best[1]:
                best = (start, length, False)
    return best


def _match_entities(question_tokens, products, customers):
    """Khớp thực thể dài nhất trong products rồi customers.

    Đầu vào:
        question_tokens: token của câu hỏi (đã normalize_key).
        products/customers: list dict với các trường ``'key'``,
            ``'display'``, ``'code'``, ``'group'`` (theo ShopView).

    Đầu ra:
        Tuple ``(kind, entity_dict, start, length, is_exact, ambiguous)``
        hoặc ``None`` nếu không khớp thực thể nào. ``ambiguous`` là
        True khi có từ hai thực thể khác nhau trở lên cùng đạt độ dài
        khớp tốt nhất (ví dụ "sting dau" vừa khớp "sting dau 330ml"
        vừa khớp "sting dau 500ml") — khi đó thực thể trả về chỉ là
        ứng viên đầu tiên theo thứ tự key/display (deterministic) và
        confidence phải giảm, vì không phân biệt được hai sản phẩm.

    Thứ tự ưu tiên khi chọn ứng viên tốt nhất:

    1. Khớp mã (code) đúng một token trong câu: mạnh nhất, vì mã là
       định danh duy nhất, không như tên có thể rút gọn gây mơ hồ.
    2. Độ dài đoạn khớp dài nhất (tên dài khớp được nhiều token hơn).
    3. Tên đầy đủ thắng tên rút gọn khi cùng độ dài.
    4. Thứ tự key/display tăng dần để deterministic khi hòa hoàn toàn.

    Các thực thể được sắp xếp trước theo (key, display) để kết quả
    không phụ thuộc thứ tự list đầu vào — điều kiện để chạy lặp lại
    cho cùng một kết quả.
    """
    candidates = []
    for kind, entities in (("product", products or []), ("customer", customers or [])):
        ordered = sorted(
            entities,
            key=lambda e: (
                str(e.get("key") or ""),
                str(e.get("display") or ""),
            ),
        )
        for entity in ordered:
            display = entity.get("display") or ""
            entity_tokens = _normalize_key(display).split()
            # Ưu tiên 1: mã đúng một token trong câu.
            code = entity.get("code")
            if code:
                code_norm = _normalize_key(code)
                if code_norm and code_norm in question_tokens:
                    pos = question_tokens.index(code_norm)
                    candidates.append((kind, entity, pos, 1, True, True))
                    continue
            # Ưu tiên 2-3: tên đầy đủ rồi tên rút gọn.
            matched = _match_one_entity(question_tokens, entity_tokens)
            if matched is not None:
                start, length, is_exact = matched
                candidates.append((kind, entity, start, length, is_exact, False))
    if not candidates:
        return None
    # Sắp xếp: mã trước, rồi độ dài giảm dần, rồi tên đầy đủ trước,
    # rồi key/display để hòa hoàn toàn vẫn deterministic.
    def _sort_key(item):
        """Khóa sắp xếp ứng viên: mã mạnh nhất, dài nhất, đầy đủ nhất."""
        kind, entity, _start, length, is_exact, is_code = item
        return (
            0 if is_code else 1,
            -length,
            0 if is_exact else 1,
            str(entity.get("key") or ""),
            str(entity.get("display") or ""),
        )

    candidates.sort(key=_sort_key)
    best = candidates[0]
    # Mơ hồ khi ứng viên thứ hai cùng (mã/dài/đầy đủ) nhưng khác thực thể.
    ambiguous = False
    if len(candidates) > 1:
        second = candidates[1]
        if (
            _sort_key(best)[:3] == _sort_key(second)[:3]
            and second[1] is not best[1]
            and str(second[1].get("key") or "")
            != str(best[1].get("key") or "")
        ):
            # Chỉ tính là mơ hồ thật khi hai thực thể khác nhau cùng
            # khớp cùng một đoạn (cùng độ dài và cùng vị trí tương đương).
            # Nếu đoạn khớp khác vị trí thì câu nhắc hai thực thể khác
            # nhau, không phải một tên mơ hồ — trường hợp đó không xảy
            # ra ở câu hỏi một thực thể nên vẫn giữ cờ để solver biết.
            ambiguous = True
    return (best[0], best[1], best[2], best[3], best[4], ambiguous)


def _match_group(question_tokens, products):
    """Tìm tên nhóm hàng/khách trong câu, nếu có.

    Trả ``(key, display)`` của nhóm, hoặc ``None``. Nhóm chỉ được nhận
    khi tên nhóm xuất hiện liên tiếp trong câu và hoặc đứng sau từ
    "nhom", hoặc dài từ 2 token trở lên. Nhóm 1 token đứng lẻ (ví dụ
    "thuoc") không nhận, vì từ đó quá chung và dễ khớp nhầm với từ
    thường trong câu.
    """
    groups = {}
    for product in products or []:
        group = product.get("group")
        if group:
            groups.setdefault(_normalize_key(group), group)
    if not groups:
        return None
    # Thử nhóm dài nhất trước để "nuoc giai khat" thắng "nuoc".
    ordered_groups = sorted(
        groups.items(), key=lambda kv: (-len(kv[0].split()), kv[0])
    )
    has_nhom = "nhom" in question_tokens
    for group_norm, group_display in ordered_groups:
        group_tokens = group_norm.split()
        if not group_tokens:
            continue
        if _find_contiguous(question_tokens, group_tokens) < 0:
            continue
        if has_nhom or len(group_tokens) >= 2:
            return (_normalize_key(group_display), group_display)
    return None


def _find_payment(masked_text):
    """Tìm hình thức thanh toán trong câu đã che tên thực thể.

    Trả ``(key, display)`` hoặc ``None``. Cụm dài được thử trước cụm
    ngắn ("tien mat" trước "the") để không bắt nhầm một từ con.
    """
    padded = " " + masked_text + " "
    for phrase in sorted(_PAYMENTS, key=len, reverse=True):
        if " " + phrase + " " in padded:
            return (_normalize_key(phrase), _PAYMENTS[phrase])
    return None


def _find_channel(masked_text):
    """Tìm kênh bán (tên sàn/thương hiệu kênh) trong câu đã che tên.

    Trả ``(key, display)`` hoặc ``None``. Chỉ nhận khi kênh xuất hiện
    như một từ độc lập, để "shopee" trong một từ dài hơn không bị bắt.
    """
    tokens = set(masked_text.split())
    for channel in _CHANNELS:
        if channel in tokens:
            return (channel, channel)
    return None


def _extract_unit(masked_tokens):
    """Trích đơn vị hỏi số lượng từ token đã che tên thực thể.

    Vì sao phải che tên trước: tên sản phẩm thường chứa đơn vị dung
    tích ("330ml", "bao 50kg") — nếu không che, "bao" hay "kg" trong
    tên sẽ bị hiểu nhầm là đơn vị được hỏi. Vì sao phải bỏ cụm "bao
    nhieu" trước: từ "bao" trong "bao nhieu" không phải đơn vị; chỉ
    "bao" đứng ngoài cụm đó mới được tính (ví dụ "bao nhieu bao").

    Trả đơn vị đầu tiên xuất hiện theo thứ tự câu (đã normalize_key),
    hoặc ``None`` nếu câu không nêu đơn vị.
    """
    tokens = list(masked_tokens)
    # Bỏ cụm "bao nhieu" để "bao" trong đó không bị tính là đơn vị.
    cleaned = []
    index = 0
    while index < len(tokens):
        if tokens[index] == "bao" and index + 1 < len(tokens) and tokens[index + 1] == "nhieu":
            index += 2
            continue
        cleaned.append(tokens[index])
        index += 1
    unit_set = set(_UNITS)
    for token in cleaned:
        if token in unit_set:
            return token
    return None


def _extract_threshold(masked_text):
    """Trích ngưỡng tiền cho câu "đơn có doanh thu từ X trở lên".

    Trả ``(money, op)`` hoặc ``(None, None)``. Chỉ nhận khi có cụm
    "tro len" / "tro xuong" / "lon hon" / "nho hon" đi kèm một số tiền
    rõ ràng (có hậu tố tiền như trieu/tr/k/dong, hoặc số >= 1000).
    Ngưỡng không có hậu tố và nhỏ hơn 1000 (ví dụ "tu 5 tro len" nói
    về số lượng) không được coi là ngưỡng tiền, vì sẽ nhầm số lượng
    thành tiền.

    Op: "tro len" / "tu ... tro len" -> ``'gte'`` (gồm X, theo
    rulebook "từ N trở lên thì gồm N"); "tren" / "lon hon" -> ``'gt'``
    (không gồm N); "tro xuong" -> ``'lte'``; "duoi" / "nho hon" ->
    ``'lt'``.
    """
    # "tu X tro len" và "X tro len".
    match = re.search(
        r"(?:tu\s+)?(\d+(?:[.,]\d+)?\s*(?:trieu|tr|k|nghin|ngan|dong)?)"
        r"\s+tro len\b",
        masked_text,
    )
    if match:
        candidate = match.group(1).strip()
        money = _parse_money(candidate)
        has_suffix = bool(
            re.search(r"(trieu|tr|k|nghin|ngan|dong)\s*$", candidate)
        )
        if money is not None and (has_suffix or money >= 1000):
            return (float(money), "gte")
    match = re.search(
        r"(?:tu\s+)?(\d+(?:[.,]\d+)?\s*(?:trieu|tr|k|nghin|ngan|dong)?)"
        r"\s+tro xuong\b",
        masked_text,
    )
    if match:
        candidate = match.group(1).strip()
        money = _parse_money(candidate)
        has_suffix = bool(
            re.search(r"(trieu|tr|k|nghin|ngan|dong)\s*$", candidate)
        )
        if money is not None and (has_suffix or money >= 1000):
            return (float(money), "lte")
    # "lon hon X dong" / "tren X dong": chỉ nhận khi có từ "dong" để
    # tránh bắt "tren 5" (số lượng) thành ngưỡng tiền.
    match = re.search(
        r"(?:lon hon|tren)\s+(\d+(?:[.,]\d+)?\s*(?:trieu|tr|k|nghin|ngan)?)"
        r"\s+dong\b",
        masked_text,
    )
    if match:
        money = _parse_money(match.group(1).strip())
        if money is not None:
            return (float(money), "gt")
    match = re.search(
        r"(?:nho hon|duoi)\s+(\d+(?:[.,]\d+)?\s*(?:trieu|tr|k|nghin|ngan)?)"
        r"\s+dong\b",
        masked_text,
    )
    if match:
        money = _parse_money(match.group(1).strip())
        if money is not None:
            return (float(money), "lt")
    return (None, None)


def _detect_ranking(masked_text, masked_tokens, entity_kind):
    """Xác định xếp hạng (ranking_k, ranking_target) TRƯỚC khi suy type.

    Đầu vào là câu đã che tên thực thể, để tên sản phẩm chứa từ "nhat"
    (nếu có) không làm lệch việc phát hiện xếp hạng.

    ranking_k:
        - "top N" -> N.
        - Một số đứng gần trước "nhat" (ví dụ "3 san pham ban chay
          nhat") -> số đó, trừ khi số đó là tháng/năm/ngày (đứng sau
          từ "thang"/"nam"/"ngay").
        - Chỉ có "nhat" -> 1.
        - Không có "nhat"/"top" -> (None, 'none').

    ranking_target, theo thứ tự ưu tiên (cụm rõ nghĩa trước):
        1. "thang nao" -> month; "ngay nao" -> day.
        2. "khach" -> customer; "nhan vien" -> employee.
        3. Thực thể sản phẩm đã khớp, hoặc "san pham"/"mat hang"/
           "ban chay"/"hot" -> product.
        4. "doanh thu cao nhat" không nêu đối tượng -> day (ngày có
           doanh thu cao nhất là cách hỏi "nhất" theo ngày phổ biến
           nhất trong train; nếu câu thực ra hỏi tháng thì cụm "thang
           nao" ở bước 1 đã bắt được, nên bước này chỉ là dự phòng).
        5. Không xác định được -> 'none' (giữ ranking_k để solver/gate
           biết có xếp hạng nhưng chưa rõ xếp cái gì, thay vì đoán).
    """
    # "gan nhat" (trong "N ngay gan nhat") KHÔNG phải xếp hạng: từ
    # "nhat" ở đó chỉ là một phần của cụm thời gian. Vì vậy phải bỏ
    # cụm "gan nhat" khỏi văn bản/token dùng để phát hiện xếp hạng,
    # trước khi kiểm tra "nhat". Nếu câu vừa có "gan nhat" vừa có một
    # "nhat" xếp hạng khác thì "nhat" còn lại vẫn được phát hiện.
    effective_text = re.sub(r"\bgan nhat\b", "gan", masked_text)
    tokens = effective_text.split()
    masked_text = effective_text
    has_top = "top" in tokens
    has_nhat = "nhat" in tokens
    if not has_top and not has_nhat:
        return (None, "none")
    ranking_k = None
    top_match = re.search(r"\btop\s+(\d+)\b", masked_text)
    if top_match:
        ranking_k = int(top_match.group(1))
    elif has_nhat:
        # Tìm số đứng trước "nhat" trong vòng 5 token, bỏ qua số là
        # tháng/năm/ngày (số đứng ngay sau thang/nam/ngay).
        nhat_pos = tokens.index("nhat")
        for pos in range(max(0, nhat_pos - 5), nhat_pos):
            if tokens[pos].isdigit():
                if pos > 0 and tokens[pos - 1] in ("thang", "nam", "ngay", "quy"):
                    continue
                ranking_k = int(tokens[pos])
                break
        if ranking_k is None:
            ranking_k = 1
    else:
        ranking_k = 1
    # Xác định target theo thứ tự ưu tiên đã nêu.
    if "thang nao" in masked_text:
        return (ranking_k, "month")
    if "ngay nao" in masked_text:
        return (ranking_k, "day")
    if "khach" in tokens:
        return (ranking_k, "customer")
    if "nhan vien" in tokens:
        return (ranking_k, "employee")
    if entity_kind == "product":
        return (ranking_k, "product")
    if (
        "san pham" in masked_text
        or "mat hang" in masked_text
        or "ban chay" in masked_text
        or "hot nhat" in masked_text
        or "ban nhieu" in masked_text
    ):
        return (ranking_k, "product")
    if "doanh thu" in masked_text and "cao" in tokens:
        return (ranking_k, "day")
    if entity_kind == "customer":
        return (ranking_k, "customer")
    if entity_kind == "employee":
        return (ranking_k, "employee")
    return (ranking_k, "none")


# ---------------------------------------------------------------------------
# Hàm chính
# ---------------------------------------------------------------------------

def parse_question(question_text: str, as_of: datetime.date, calendar,
                   rulebook, products: list, customers: list) -> ParsedQuestion:
    """Phân tích một câu hỏi thành ParsedQuestion 4 trục.

    Đầu vào:
        question_text: câu hỏi gốc (có dấu/không dấu, viết tắt đều được).
        as_of: ngày "hôm nay" của câu hỏi.
        calendar: lịch của split (để tra Tết) hoặc None.
        rulebook: rulebook của split (giữ đúng chữ ký contract; hiện
            parse_time dùng hàm phase1 đã cài luật, xem parse_time).
        products/customers: list dict của shop (``'key'``,
            ``'display'``, ``'code'``, ``'group'``).

    Đầu ra:
        ParsedQuestion theo contract. Xem docstring của ParsedQuestion
        để hiểu từng trường, đặc biệt ``period_kind='snapshot'`` có
        ``period_start=None`` và ``period_months`` chỉ điền khi
        multi_month.

    Các bước (đúng thứ tự đã giải thích ở docstring module):

    1. Chuẩn hoá + mở rộng viết tắt.
    2. Khớp thực thể (dài nhất, chấp nhận tên rút gọn >= 2 token) rồi
       che đoạn tên đó bằng token "entity" trước khi suy type — nhờ
       vậy ``%`` hay đơn vị trong tên không làm lệch type.
    3. Nếu không có sản phẩm/khách, thử nhóm, thanh toán, kênh.
    4. Xác định ranking trước khi suy measure/answer_type.
    5. Giải kỳ bằng parse_time trên câu đã chuẩn hoá (không che tên,
       vì tên không chứa kỳ; nhưng kỳ đã suy không phụ thuộc tên).
    6. Suy measure theo thứ tự ưu tiên: tăng liên tục -> công nợ ->
       nhập hàng -> xếp hạng -> tỉ lệ huỷ -> tỉ trọng -> tăng trưởng
       -> doanh thu theo chiều (thanh toán/nhóm/kênh) -> số đơn ->
       giá -> doanh thu -> số lượng -> unknown.
    7. Suy answer_type từ measure + ranking + dấu %/phần trăm còn lại
       sau khi đã che tên (nên % trong tên thuốc đã bị loại).
    8. Tính confidence_parse và ghi parse_notes cho mọi điểm không chắc.

    Trường hợp biên:
    - Câu không có kỳ: period_kind 'none', confidence giảm, không đoán.
    - Câu không nhận diện được measure: measure/answer_type 'unknown',
      confidence_parse thấp (0.3), để gate abstain.
    - Tên rút gọn hoặc tên mơ hồ (nhiều sản phẩm cùng khớp): vẫn trả
      ứng viên tốt nhất nhưng giảm confidence và ghi chú rõ.
    - Câu hỏi về sản phẩm nhưng shop không có sản phẩm đó trong list:
      entity_kind 'none' và confidence giảm — solver/gate sau đó sẽ
      xét unanswerable (shop không có mặt hàng), parser không tự kết
      luận unanswerable vì parser không biết hồ sơ đầy đủ của shop.
    """
    notes: list[str] = []
    confidence = 1.0

    # --- Bước 1: chuẩn hoá + mở rộng viết tắt. ---
    normalized = expand_abbreviations(question_text)
    question_tokens = _tokens_of(normalized)

    # --- Bước 2: khớp thực thể TRƯỚC khi suy type. ---
    matched = _match_entities(question_tokens, products, customers)
    entity_kind = "none"
    entity_key = None
    entity_display = None
    masked_tokens = list(question_tokens)
    percent_in_entity = False
    matched_is_exact = True
    if matched is not None:
        kind, entity, start, length, is_exact, ambiguous = matched
        entity_kind = kind
        entity_key = entity.get("key")
        entity_display = entity.get("display")
        matched_is_exact = is_exact
        # Dấu % trong tên gốc của thực thể đã khớp: nếu có thì mọi dấu
        # % trong câu đều được coi là thuộc tên, không suy ra percent.
        if entity_display and "%" in str(entity_display):
            percent_in_entity = True
        # Che đoạn tên bằng một token "entity" để bước sau không bị
        # đơn vị/%/từ trong tên làm lệch.
        masked_tokens = (
            question_tokens[:start] + ["entity"] + question_tokens[start + length:]
        )
        if not is_exact:
            confidence -= 0.2
            notes.append(
                "Khop ten rut gon (khong phai ten day du), do chac giam."
            )
        if ambiguous:
            confidence -= 0.15
            notes.append(
                "Nhieu thuc the cung khop doan ten nay, chua phan biet duoc."
            )
    masked_text = " ".join(masked_tokens)

    # --- Bước 3: nhóm / thanh toán / kênh khi chưa có thực thể chính. ---
    # Nhóm được xét trước thanh toán/kênh vì "nhom" là từ khoá rõ nghĩa.
    if matched is None:
        group_match = _match_group(question_tokens, products)
        if group_match is not None:
            entity_kind = "group"
            entity_key, entity_display = group_match
            # Che tên nhóm khỏi masked_text để từ trong tên nhóm không
            # làm lệch suy measure (ví dụ nhóm "nuoc giai khat" chứa
            # từ "nuoc" không liên quan đến đo lường).
            group_tokens = _normalize_key(entity_display).split()
            pos = _find_contiguous(masked_tokens, group_tokens)
            if pos >= 0:
                masked_tokens = (
                    masked_tokens[:pos]
                    + ["entity"]
                    + masked_tokens[pos + len(group_tokens):]
                )
                masked_text = " ".join(masked_tokens)
        else:
            payment_match = _find_payment(masked_text)
            if payment_match is not None:
                entity_kind = "payment"
                entity_key, entity_display = payment_match
            else:
                channel_match = _find_channel(masked_text)
                if channel_match is not None:
                    entity_kind = "channel"
                    entity_key, entity_display = channel_match
    # Nhân viên là thực thể chung (không có list nhân viên trong tham số):
    # chỉ nhận khi câu nêu "nhan vien" và chưa có thực thể cụ thể.
    if matched is None and entity_kind == "none" and "nhan vien" in masked_tokens:
        entity_kind = "employee"
        entity_key = None
        entity_display = None

    # --- Bước 4: xếp hạng TRƯỚC khi suy type. ---
    ranking_k, ranking_target = _detect_ranking(
        masked_text, masked_tokens, entity_kind
    )
    # Sửa tích hợp Giai đoạn 2: câu "hàng nào lời nhất / lãi gộp cao
    # nhất" mà _detect_ranking chưa gán target (vì chỉ có từ "hang"
    # chung chung, không có "san pham"/"ban chay") vẫn là xếp hạng
    # mặt hàng theo lợi nhuận. Chỉ nhận khi có cụm lời/lãi rõ nghĩa
    # và chưa có target khác (khách/nhân viên/ngày/tháng), để không
    # cướp target của câu xếp hạng khác.
    _profit_phrases = (
        "loi nhuan", "lai gop", "loi nhat", "lai nhat", "loi cao",
        "lai cao", "sinh loi", "loi gop",
    )
    _is_profit_ranking = any(
        phrase in masked_text for phrase in _profit_phrases
    )
    if (
        ranking_k is not None
        and ranking_target == "none"
        and _is_profit_ranking
    ):
        ranking_target = "product"
    has_ranking = ranking_k is not None
    if has_ranking and ranking_target == "none":
        confidence -= 0.2
        notes.append("Co tu 'nhat'/'top' nhung chua xac dinh duoc xep hang cai gi.")

    # --- Bước 5: giải kỳ. ---
    period_start, period_end, period_kind, period_months = parse_time(
        normalized, as_of, calendar, rulebook
    )
    if period_kind == "none":
        confidence -= 0.3
        notes.append("Khong giai duoc ky thoi gian trong cau hoi.")
    # Kỳ suy năm (tháng/ngày không ghi năm) là suy luận có luật nhưng
    # vẫn kém chắc hơn kỳ ghi rõ năm: ghi chú, giảm nhẹ.
    if period_kind in ("absolute", "relative") and re.search(
        r"\bthang\s+\d{1,2}\b(?!\s*(?:/|nam\s+\d))", normalized
    ):
        # Chỉ ghi chú khi thực sự có tháng không năm trong câu và kỳ
        # đã parse thành công (tránh ghi chú thừa cho câu tháng nay).
        if "thang nay" not in normalized and "thang truoc" not in normalized:
            confidence -= 0.1
            notes.append("Thang khong ghi nam, suy nam theo as_of (luat rulebook).")

    # --- Bước 6: ngưỡng tiền (cho câu đếm đơn theo doanh thu). ---
    threshold_money, threshold_op = _extract_threshold(masked_text)

    # --- Bước 7: suy measure theo thứ tự ưu tiên. ---
    # Các cờ từ khoá trên câu đã che tên (masked_text) để tránh từ
    # trong tên sản phẩm/khách làm lệch.
    tokens_set = set(masked_tokens)
    has_percent_sign = ("%" in str(question_text)) and not percent_in_entity
    has_percent_word = "phan tram" in masked_text
    has_percent = has_percent_sign or has_percent_word
    measure = "unknown"

    # 7a. Tăng liên tục: khuôn mẫu boolean, ưu tiên cao nhất vì cụm
    # "lien tuc" rất đặc trưng và không chồng với các measure khác.
    if "lien tuc" in masked_text or "lien tiep" in masked_text:
        # Chỉ nhận là continuous khi đi kèm tăng/trưởng hoặc hỏi 3 tháng;
        # "lien tuc" đứng một mình vẫn đủ đặc trưng trong dữ liệu train.
        measure = "continuous_growth"
    # 7b. Công nợ: "cong no", "con no", "tien no", "khach hang no",
    # hoặc từ "no" đi kèm "khach" (ví dụ "khach hang no nhieu nhat").
    # Lưu ý: "doanh thu" là cụm hai từ, nên phải kiểm tra trên
    # masked_text (chuỗi), không phải trên set token đơn — kiểm tra
    # cụm trong set token sẽ luôn sai, lỗi này từng làm mọi câu doanh
    # thu rơi về unknown và đã được sửa ở mọi nhánh dưới đây.
    elif (
        "cong no" in masked_text
        or "con no" in masked_text
        or "tien no" in masked_text
        or "khach no" in masked_text
        or "khach hang no" in masked_text
        or "no cua" in masked_text
        or ("no" in tokens_set and "khach" in tokens_set)
    ):
        if has_ranking and ranking_target == "customer":
            measure = "debt_top"
        elif has_ranking and "khach" in tokens_set:
            measure = "debt_top"
        else:
            measure = "debt_total"
    # 7c. Nhập hàng.
    elif "nhap hang" in masked_text or "tien nhap" in masked_text or "nhap vao" in masked_text:
        measure = "import_total"
    # 7d. Xếp hạng đã xác định được target.
    elif has_ranking and ranking_target != "none":
        if ranking_target == "day":
            measure = "best_day"
        elif ranking_target == "month":
            measure = "best_month"
        elif ranking_target == "product":
            measure = "top_product"
        elif ranking_target == "customer":
            measure = "top_customer"
        elif ranking_target == "employee":
            measure = "top_employee"
    # 7e. Tỉ lệ huỷ.
    elif (
        "ti le huy" in masked_text
        or "ty le huy" in masked_text
        or "ti le don huy" in masked_text
        or "phan tram don huy" in masked_text
        or "don huy chiem" in masked_text
    ):
        measure = "cancel_rate"
    # 7f. Tỉ trọng doanh thu.
    elif "ti trong" in masked_text or "chiem" in tokens_set:
        # "chiem" ở đây là chiếm tỉ trọng; nếu câu hỏi tỉ lệ huỷ thì
        # nhánh 7e đã bắt trước, nên không sợ nhầm.
        measure = "revenue_share"
    # 7g. Tăng trưởng: percent trước amount, vì "tang bao nhieu phan
    # tram" và "tang bao nhieu tien" khác nhau hoàn toàn về type.
    elif (
        "tang truong" in masked_text
        or (has_percent and ("tang" in tokens_set or "giam" in tokens_set or "thay doi" in masked_text))
    ):
        measure = "growth_percent"
    elif (
        ("tang" in tokens_set or "giam" in tokens_set or "chenh lech" in masked_text or "thay doi" in masked_text)
        and (
            "so voi" in masked_text
            or "so sanh" in masked_text
            or "ky truoc" in masked_text
            or "thang truoc" in masked_text
            or "quy truoc" in masked_text
            or "nam truoc" in masked_text
            or "tien" in tokens_set
        )
    ):
        measure = "growth_amount"
    # 7h. Doanh thu theo chiều: thanh toán / nhóm / kênh.
    elif "doanh thu" in masked_text and (
        entity_kind == "payment" or "tien mat" in masked_text or "chuyen khoan" in masked_text or "cod" in tokens_set
    ):
        measure = "revenue_by_payment"
    elif "doanh thu" in masked_text and (
        entity_kind == "group" or "theo nhom" in masked_text or "nhom hang" in masked_text or "nhom khach" in masked_text
    ):
        measure = "revenue_by_group"
    elif "doanh thu" in masked_text and (
        entity_kind == "channel" or "theo kenh" in masked_text or "kenh ban" in masked_text
    ):
        measure = "revenue_by_channel"
    # 7i. Câu đếm đơn theo ngưỡng doanh thu: "bao nhieu don ... tu X
    # tro len" là đếm đơn, không phải doanh thu.
    elif threshold_money is not None and "don" in tokens_set:
        measure = "order_count"
    # 7j. Số đơn.
    elif (
        "so don" in masked_text
        or "bao nhieu don" in masked_text
        or "may don" in masked_text
        or "so don hang" in masked_text
    ):
        measure = "order_count"
    # 7k. Giá: trung bình trước, giá ngày sau (giá ngày cần kỳ là 1 ngày
    # hoặc câu nêu "gia ban ... ngay"; ở đây chỉ dựa vào từ khoá, solver
    # sẽ kiểm tra kỳ là ngày đơn lẻ trước khi tính).
    elif "gia trung binh" in masked_text or "trung binh" in masked_text and "gia" in tokens_set:
        measure = "avg_price"
    elif ("gia ban" in masked_text or "don gia" in masked_text) and (
        period_start is not None and period_start == period_end
    ):
        measure = "price_on_day"
    elif "gia ban" in masked_text or "don gia" in masked_text:
        # Giá bán không gắn ngày đơn lẻ: vẫn là price_on_day nếu câu
        # hỏi giá tại một thời điểm, nhưng kém chắc hơn -> ghi chú.
        measure = "price_on_day"
        notes.append("Cau gia ban nhung ky khong phai mot ngay don le.")
        confidence -= 0.1
    # 7l. Doanh thu (chung, kể cả doanh thu của sản phẩm/khách cụ thể).
    elif (
        "doanh thu" in masked_text
        or "thu ve" in masked_text
        or "tien ban" in masked_text
        or "tien hang" in masked_text
        or "ban duoc bao nhieu tien" in masked_text
    ):
        measure = "revenue"
    # 7m. Số lượng: "so luong", "ban duoc bao nhieu" kèm đơn vị, hoặc
    # "ban bao nhieu" không kèm tiền. Phân biệt quantity_by_unit khi
    # không có sản phẩm cụ thể mà chỉ hỏi theo đơn vị (luật rulebook:
    # cộng số lượng quy về đơn vị cơ bản của các mặt hàng có đơn vị cơ
    # bản đó).
    elif (
        "so luong" in masked_text
        or "ban duoc bao nhieu" in masked_text
        or "ban bao nhieu" in masked_text
    ):
        unit_tmp = _extract_unit(masked_tokens)
        if entity_kind == "product":
            measure = "quantity"
        elif unit_tmp is not None:
            measure = "quantity_by_unit"
        else:
            measure = "quantity"
    # 7n. Câu xếp hạng nhưng chưa rõ target: không đoán measure.
    elif has_ranking:
        measure = "unknown"
    # 7o. Không nhận diện được.
    else:
        measure = "unknown"

    # --- Bước 8: đơn vị (chỉ có ý nghĩa cho quantity/quantity_by_unit,
    # nhưng vẫn trích để solver kiểm tra; câu doanh thu có đơn vị trong
    # tên đã bị che nên không bị nhầm). ---
    # Chỉ giữ unit cho câu số lượng. Đơn vị ngoài câu số lượng không
    # giữ, để tránh solver hiểu nhầm doanh thu hay giá là số lượng
    # (đơn vị trong tên sản phẩm thì đã bị che ở bước 2 nên không lọt
    # tới đây; unit còn lại ở đây là đơn vị được hỏi thật sự).
    unit = _extract_unit(masked_tokens)
    if measure not in ("quantity", "quantity_by_unit"):
        unit = None

    # --- Bước 9: suy answer_type từ measure + ranking + percent còn lại. ---
    if measure == "unknown":
        answer_type = "unknown"
    elif measure in ("cancel_rate", "revenue_share", "growth_percent"):
        answer_type = "percent"
    elif measure == "continuous_growth":
        answer_type = "boolean"
    elif measure == "best_day" or measure == "best_month":
        answer_type = "date"
    elif measure in ("top_product", "top_customer", "top_employee", "debt_top"):
        # Top-k > 1 ra list; "nhất" (k=1) ra string (tên duy nhất).
        if ranking_k is not None and ranking_k > 1:
            answer_type = "list"
        else:
            answer_type = "string"
    elif measure in ("revenue_by_payment", "revenue_by_group", "revenue_by_channel"):
        # Hỏi theo chiều mà không nêu chiều cụ thể -> bảng; nêu cụ thể
        # (một hình thức/nhóm/kênh) -> một con số.
        if entity_kind in ("payment", "group", "channel"):
            answer_type = "number"
        else:
            answer_type = "table"
    elif measure in (
        "revenue", "order_count", "quantity", "quantity_by_unit",
        "growth_amount", "debt_total", "import_total", "avg_price",
        "price_on_day",
    ):
        answer_type = "number"
    else:
        answer_type = "unknown"

    # --- Bước 10: chốt confidence và notes cho trường hợp unknown. ---
    if measure == "unknown":
        confidence = min(confidence, 0.3)
        notes.append("Khong nhan dien duoc measure, can abstain thay vi doan.")
    if matched is None and measure in ("revenue", "quantity", "price_on_day"):
        # Câu nêu "san pham"/"mat hang" chung chung thì không cần thực
        # thể cụ thể; chỉ giảm khi câu dường như nhắc một tên riêng mà
        # list shop không có. Dấu hiệu tên riêng: câu dài và chứa từ
        # không phải từ khoá chung — heuristic này cố tình rất nhẹ:
        # chỉ ghi chú, không trừ nặng, để gate/solver quyết unanswerable.
        pass
    confidence = max(0.0, min(1.0, confidence))

    # Chỉ tiêu xếp hạng mặc định theo measure (sửa tích hợp Giai đoạn 2):
    # top_product theo số lượng quy đổi (rulebook: "bán chạy nhất"),
    # trừ câu lời/lãi nhất thì theo lợi nhuận; top_customer và
    # best_day/best_month theo doanh thu; debt_top theo số nợ.
    if measure == "top_product":
        ranking_metric = "profit" if _is_profit_ranking else "quantity"
    elif measure == "top_customer":
        ranking_metric = "revenue"
    elif measure in ("best_day", "best_month"):
        ranking_metric = "revenue"
    elif measure == "debt_top":
        ranking_metric = "debt"
    else:
        ranking_metric = None

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
        unit=unit,
        threshold_money=threshold_money,
        threshold_op=threshold_op,
        answer_type=answer_type,
        confidence_parse=confidence,
        parse_notes=notes,
        normalized_text=normalized,
    )

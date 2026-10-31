# -*- coding: utf-8 -*-
"""Chuẩn hoá giá trị: tiền, ngày, số lượng cho DataMate (Giai đoạn 1).

Mục đích của module này
------------------------
Dữ liệu các shop trộn nhiều cách ghi cùng một giá trị trong cùng một cột:
tiền thì ``'55.000đ'``, ``'25k'``, ``'1tr440'``, ``'16,56tr'`` xen với số
thuần; ngày thì Excel serial (``45717``) xen với ``'01/02/2025'`` và
``'1 thg 1 2025'``; số lượng thì ``'1 ly'``, ``'3 chai'`` xen với số thuần.
Module này biến mọi cách ghi đó về một dạng chuẩn duy nhất để code tính
toán phía sau không phải đoán mò theo từng shop.

Nguyên tắc quan trọng nhất, nhắc lại ở cả ba hàm:
    **Giá trị nào không parse được thì trả ``None``, KHÔNG BAO GIỜ gán 0.**
    Gán 0 sẽ biến "không biết" thành "bằng không", làm sai doanh thu, sai
    tồn kho và sai cả quyết định unanswerable/abstain. ``None`` là tín
    hiệu để tầng ingest đánh dấu dòng nghi ngờ và tầng quyết định abstain
    khi tỉ lệ lỗi quá cao.

Quy ước tiền (theo API_CONTRACT, không theo một shop cụ thể):
    - ``'k'`` / ``'nghìn'`` / ``'ngàn'`` = 1.000.
    - ``'tr'`` / ``'triệu'`` / ``'m'`` = 1.000.000.
    - ``'1tr440'`` = 1.440.000 (phần sau ``tr`` là số nghìn, nếu nhỏ hơn
      1.000 thì nhân 1.000; nếu lớn hơn thì cộng thẳng, để ``'1tr440000'``
      cũng ra 1.440.000).
    - ``'16,56tr'`` = 16,56 triệu = 16.560.000 (dấu phẩy trước hậu tố là
      dấu thập phân, không phải dấu phân cách nghìn).

Quy ước ngày:
    - Excel serial tính từ gốc 1899-12-30 (gốc này đã bù lỗi nhuận năm
      1900 của Excel, nên không cần trừ thêm).
    - Ngày Việt Nam đọc theo thứ tự ngày/tháng (``01/02/2025`` là
      1 tháng 2, không phải 2 tháng 1).
    - Ngày không ghi năm (``'1/1'``, ``'1 thg 1'``) CHỈ được suy ra khi
      caller đưa ``sheet_year`` (năm của sheet/file chứa dòng đó, theo
      luật rulebook). Không có ``sheet_year`` thì trả ``None``, không
      đoán năm hiện tại.

Chỉ dùng thư viện chuẩn Python. Module này không đọc file, không biết
shop nào, không hard-code tên cột hay tên shop.
"""

import datetime as _datetime
import math as _math
import re as _re

try:
    # Chạy dưới dạng package: from src.normalizers import ...
    from .text_utils import normalize_key, strip_diacritics
except ImportError:  # pragma: no cover - chạy file rời, không qua package
    # Chạy trực tiếp trong thư mục src/ thì import tuyệt đối mới được.
    from text_utils import normalize_key, strip_diacritics

__all__ = ["parse_money", "parse_date", "parse_quantity"]

# Gốc Excel: serial 1 = 1900-01-01, nhưng vì Excel tính sai 1900 là năm
# nhuận nên dùng gốc 1899-12-30 sẽ cho ngày đúng với mọi serial thực tế
# (serial >= 61, tức sau tháng 2/1900). Dữ liệu train toàn serial 2025+
# nên vùng này không bao giờ chạm tới lỗi 1900-02-29.
_EXCEL_EPOCH = _datetime.date(1899, 12, 30)
# Serial hợp lệ: chặn dưới để số năm đơn lẻ (2025) hay mã đơn không bị
# hiểu nhầm thành ngày; chặn trên để số tiền lớn không bị hiểu nhầm.
# 20000 ~ năm 1954, 100000 ~ năm 2173, đủ rộng cho dữ liệu doanh nghiệp.
_SERIAL_MIN = 20000
_SERIAL_MAX = 100000

# Các chuỗi coi là "không có giá trị", không phải số 0. Chữ 'x' xuất hiện
# trong cột số lượng của train với nghĩa "không ghi", nên cũng là None.
_EMPTY_TOKENS = {"", "nan", "none", "null", "na", "n/a", "-", "x", "--"}


def _is_finite_number(value):
    """Kiểm tra nhanh: value có phải số hữu hạn, không phải bool không.

    Vì sao cần: trong Python ``bool`` là lớp con của ``int`` (``True``
    == 1), nên nếu không loại ``bool`` trước thì ``True`` sẽ bị parse
    thành tiền 1đ hay số lượng 1, rất sai. Hàm này dùng chung cho cả
    ba parser để khỏi lặp logic.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return _math.isfinite(float(value))
    return False


def _excel_serial_to_date(serial):
    """Đổi Excel serial (số) thành ``datetime.date``, hoặc ``None``.

    Chỉ nhận serial nguyên trong khoảng :data:`_SERIAL_MIN`..``_SERIAL_MAX``.
    Phần thập phân (giờ trong ngày, ví dụ 45717.5) được bỏ, vì hàm này
    chỉ trả ngày, không trả giờ. Serial ngoài khoảng -> ``None`` để
    tránh hiểu nhầm năm (2025) hay số tiền thành ngày.
    """
    try:
        whole = int(_math.floor(float(serial)))
    except (TypeError, ValueError, OverflowError):
        return None
    if whole < _SERIAL_MIN or whole > _SERIAL_MAX:
        return None
    try:
        return _EXCEL_EPOCH + _datetime.timedelta(days=whole)
    except OverflowError:
        return None


def _plain_number(text):
    """Parse một số thuần chỉ gồm chữ số và dấu ``.``/``,``.

    Đây là bước cuối của :func:`parse_money` và cũng dùng cho số lượng.
    Quy tắc phân biệt dấu thập phân với dấu phân cách nghìn (theo cách
    ghi Việt Nam, và khớp mọi ví dụ trong API_CONTRACT):

    - Có cả ``.`` và ``,``: dấu xuất hiện CUỐI cùng là dấu thập phân
      nếu sau nó không đúng 3 chữ số (ví dụ ``'25,000.0000'`` -> 25000.0);
      nếu sau nó đúng 3 chữ số thì cả hai đều là phân cách nghìn
      (ví dụ ``'1.234.567'`` thì không rơi vào nhánh này vì dấu cuối
      cũng theo sau bởi 3 chữ số, nên bị coi là nghìn).
    - Chỉ một loại dấu, xuất hiện nhiều lần: luôn là phân cách nghìn
      (``'1.234.567'``, ``'1,234,567'``).
    - Chỉ một loại dấu, xuất hiện một lần: sau dấu đúng 3 chữ số thì là
      phân cách nghìn (``'55.000'`` -> 55000, ``'560,000'`` -> 560000);
      sau dấu 1-2 chữ số thì là thập phân (``'12.5'`` -> 12.5,
      ``'16,56'`` -> 16.56).

    Trường hợp biên không thể phân biệt tuyệt đối (ví dụ ``'1.500'`` vừa
    có thể là 1500 vừa có thể là 1.5): quy tắc trên chọn 1500, vì trong
    dữ liệu tiền Việt Nam, số có đúng 3 chữ số sau dấu chấm gần như
    luôn là phân cách nghìn. Đây là heuristic có ghi rõ, không phải
    đoán theo shop nào.
    """
    s = text.replace(" ", "").replace("\xa0", "")
    if not s or not _re.fullmatch(r"[+-]?[\d.,]+", s):
        return None
    negative = s.startswith("-")
    if s[0] in "+-":
        s = s[1:]
    if not s:
        return None
    if "." in s and "," in s:
        # Dấu cuối cùng quyết định: sau nó khác 3 chữ số -> thập phân.
        last_dot = s.rfind(".")
        last_comma = s.rfind(",")
        if last_dot > last_comma:
            after = s[last_dot + 1:]
            if len(after) != 3:
                s = s.replace(",", "").replace(".", ".", 1)
                # Sau khi bỏ dấu phẩy, chỉ còn một dấu chấm thập phân.
                # Trường hợp còn nhiều chấm (không xảy ra ở nhánh này)
                # thì float() sẽ lỗi và trả None bên dưới.
                try:
                    value = float(s)
                except ValueError:
                    return None
                return -value if negative else value
        else:
            after = s[last_comma + 1:]
            if len(after) != 3:
                s = s.replace(".", "").replace(",", ".", 1)
                try:
                    value = float(s)
                except ValueError:
                    return None
                return -value if negative else value
        # Cả hai đều là phân cách nghìn: bỏ hết dấu.
        s = s.replace(".", "").replace(",", "")
    elif "." in s:
        if s.count(".") > 1:
            s = s.replace(".", "")
        else:
            after = s.split(".", 1)[1]
            if len(after) == 3:
                s = s.replace(".", "")  # phân cách nghìn
            # Ngược lại giữ dấu chấm làm thập phân.
    elif "," in s:
        if s.count(",") > 1:
            s = s.replace(",", "")
        else:
            after = s.split(",", 1)[1]
            if len(after) == 3:
                s = s.replace(",", "")  # phân cách nghìn
            else:
                s = s.replace(",", ".")  # thập phân kiểu Việt Nam
    try:
        value = float(s)
    except ValueError:
        return None
    return -value if negative else value


def _decimal_part(text):
    """Đổi phần số trước hậu tố k/tr (``'16,56'``, ``'2.5'``) thành float.

    Trước hậu tố, dấu phẩy/chấm luôn là thập phân (không ai viết
    ``'1.000tr'`` với nghĩa 1000 triệu theo cách ghi này), nên chỉ cần
    đổi phẩy thành chấm. Trả ``None`` nếu không phải số.
    """
    s = text.replace(" ", "").replace("\xa0", "")
    if not s:
        return None
    # Nếu có cả hai dấu thì phần này không còn là số thập phân đơn giản.
    if "." in s and "," in s:
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def parse_money(value):
    """Parse tiền thành số VND (float), hoặc ``None`` nếu không parse được.

    Đầu vào:
        value: số (int/float), chuỗi tiền, ``None``, ngày/giờ, bool...

    Đầu ra:
        Số tiền dạng float (thường là số nguyên VND, ví dụ 55000.0).
        ``None`` nếu không parse được — KHÔNG gán 0.

    Các dạng xử lý được (thứ tự thử, từ đặc thù đến tổng quát):
        1. Số thuần: trả thẳng (trừ NaN/vô cực/bool -> ``None``).
        2. Hậu tố triệu: ``'1tr440'``, ``'16,56tr'``, ``'2 triệu'``,
           ``'1 triệu 440 nghìn'``, ``'5m'``.
        3. Hậu tố nghìn: ``'25k'``, ``'2,5k'``, ``'30 nghìn'``.
        4. Số có đơn vị tiền tệ: ``'55.000đ'``, ``'100.000 VND'``.
        5. Số phân cách nghìn/thập phân: ``'1.234.567'``,
           ``'1,234,567'``, ``'25,000.0000'``, ``'12.5'``.
        6. Số âm: ``'-45k'``, ``'-30.500đ'``, ``'(10.000)'``.

    Vì sao thứ tự này quan trọng:
        Nếu parse số thuần trước hậu tố, ``'25k'`` sẽ bị rớt chữ ``k``
        hoặc hiểu sai. Nếu bỏ dấu tiền tệ sau khi tách hậu tố, ``'đ'``
        có thể làm hỏng regex hậu tố. Nên: tách dấu âm/ngoặc -> bỏ đơn
        vị tiền tệ ở cuối -> thử hậu tố triệu/nghìn -> cuối cùng mới
        parse số thuần.

    Trường hợp biên:
        - Chuỗi rỗng, ``'x'``, ``'-'``, ``'nan'`` -> ``None``.
        - Chuỗi có chữ lạ (``'abc'``, ``'1 ly'``) -> ``None`` (đây là
          việc của :func:`parse_quantity`, không phải tiền).
        - ``True``/``False`` -> ``None`` (tránh bool bị tính là 1/0).
        - Đối tượng ngày/giờ -> ``None`` (không ép thành số).
    """
    # 1. Số thuần: nhanh nhất, và tránh str() làm mất độ chính xác.
    if _is_finite_number(value):
        return float(value)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (_datetime.date, _datetime.datetime)):
        return None
    # Đối tượng kiểu pandas Timestamp cũng có to_pydatetime nhưng không
    # phải ngày theo nghĩa trên; nó sẽ rơi xuống nhánh chuỗi và trả None
    # vì chuỗi của nó chứa chữ và dấu '-', không khớp số thuần. Để chắc
    # chắn, chặn sớm mọi giá trị có phương thức to_pydatetime.
    if hasattr(value, "to_pydatetime"):
        return None

    s = str(value).strip().replace("\xa0", " ").strip()
    if not s or s.lower() in _EMPTY_TOKENS:
        return None

    # 2. Dấu âm: trừ ở đầu, hoặc ngoặc đơn bao quanh (kế toán ghi âm).
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    if s.startswith("-"):
        negative = True
        s = s[1:].strip()
    elif s.endswith("-"):
        negative = True
        s = s[:-1].strip()
    if not s:
        return None

    # 3. Bỏ đơn vị tiền tệ ở cuối (lặp để xử lý '55.000 đ' có khoảng
    # trắng, hay '100.000đ' dính liền). Chỉ bỏ ở CUỐI chuỗi để không
    # làm hỏng chữ 'đ' ở giữa (nếu có).
    lowered = s.lower()
    for suffix in ("vnd", "vnđ", "đồng", "dong", "₫", "đ"):
        if lowered.endswith(suffix):
            s = s[: -len(suffix)].strip()
            lowered = s.lower()
    if not s:
        return None

    # Chuẩn hoá khoảng trắng quanh hậu tố để regex dưới gọn hơn.
    compact = _re.sub(r"\s+", " ", s.strip()).lower()
    # Bỏ dấu để 'triệu' và 'trieu' về một dạng, 'nghìn'/'nghin' cũng vậy.
    ascii_compact = strip_diacritics(compact)

    # 4. Hậu tố triệu: '1tr440', '16,56tr', '2 trieu', '1 trieu 440 nghin'.
    # Nhóm 1 là phần triệu (có thể thập phân), nhóm 2 là phần nghìn lẻ.
    m = _re.fullmatch(
        r"(\d+(?:[.,]\d+)?)\s*(?:tr|trieu|m)\s*(\d+)?\s*(?:nghin|ngan)?",
        ascii_compact,
    )
    if m:
        base = _decimal_part(m.group(1))
        if base is not None:
            rest = m.group(2)
            if rest is None or rest == "":
                total = base * 1_000_000
            else:
                rest_int = int(rest)
                # Phần lẻ dưới 1000 hiểu là số nghìn ('440' -> 440.000);
                # từ 1000 trở lên cộng thẳng ('440000' -> 440.000).
                total = base * 1_000_000 + (
                    rest_int * 1000 if rest_int < 1000 else rest_int
                )
            # Làm tròn 2 chữ số thập phân để tránh sai số float kiểu
            # 16.56 * 1_000_000 = 16559999.999... (tiền VND thực tế
            # không có phần lẻ nhỏ hơn đồng, làm tròn này không đổi
            # giá trị kinh tế mà chỉ dọn sai số biểu diễn).
            return -round(total, 2) if negative else round(total, 2)
        # base None thì rơi xuống thử dạng khác, cuối cùng trả None.

    # 5. Hậu tố nghìn: '25k', '2,5k', '30 nghin'. Không có phần lẻ sau.
    m = _re.fullmatch(
        r"(\d+(?:[.,]\d+)?)\s*(?:k|nghin|ngan)", ascii_compact
    )
    if m:
        base = _decimal_part(m.group(1))
        if base is not None:
            total = base * 1000
            return -round(total, 2) if negative else round(total, 2)

    # 6. Số thuần còn lại: phải toàn chữ số và dấu . , (và khoảng trắng
    # đã được _plain_number tự bỏ). Còn chữ cái khác -> None.
    if not _re.fullmatch(r"[+-]?[\d\s.,]+", s):
        return None
    result = _plain_number(s)
    if result is None:
        return None
    # Cùng lý do làm tròn như nhánh hậu tố ở trên.
    result = round(result, 2)
    return -result if negative else result


def _make_date(year, month, day):
    """Tạo ``datetime.date`` an toàn, trả ``None`` khi ngày không hợp lệ.

    Ví dụ ngày 32/13, 31/2, 29/2 năm không nhuận đều trả ``None`` thay vì
    crash, vì dữ liệu bẩn có thể chứa ngày sai và tầng trên cần đếm lỗi
    chứ không được dừng cả pipeline.
    """
    try:
        return _datetime.date(int(year), int(month), int(day))
    except (ValueError, TypeError, OverflowError):
        return None


def parse_date(value, sheet_year=None):
    """Parse ngày thành ``datetime.date``, hoặc ``None`` nếu không được.

    Đầu vào:
        value: Excel serial (số hoặc chuỗi số), chuỗi ngày, đối tượng
            ``date``/``datetime``, ``None``.
        sheet_year: năm của sheet/file chứa dòng đó (int), dùng DUY
            NHẤT cho ngày không ghi năm. ``None`` nghĩa là "không biết
            năm" -> ngày không năm trả ``None``.

    Đầu ra:
        ``datetime.date`` hoặc ``None``. Không đoán ngày, không gán ngày
        mặc định khi thiếu năm.

    Các dạng xử lý được (thứ tự thử):
        1. Đối tượng ``date``/``datetime`` -> lấy phần ngày.
        2. Excel serial: số nguyên trong khoảng hợp lệ, hoặc chuỗi số
           4-6 chữ số (ví dụ ``'45689'`` trong file XLSX khi openpyxl
           đọc ô serial dưới dạng chuỗi).
        3. ``YYYY-MM-DD``, ``YYYY/MM/DD`` và dạng kèm giờ
           ``'2025-02-01 06:00:25'`` (chỉ lấy phần ngày).
        4. ``DD/MM/YYYY`` (đọc ngày trước, tháng sau, theo Việt Nam),
           chấp nhận ``-``, ``/``, ``.`` làm dấu tách.
        5. ``'1 thg 1 2025'``, ``'ngày 1 tháng 1 năm 2025'`` và biến thể
           không dấu (bỏ dấu trước khi khớp regex).
        6. Ngày không năm ``'1/1'``, ``'1 thg 1'``: chỉ khi có
           ``sheet_year`` thì ghép năm đó vào.
        7. ``YYYYMMDD`` dạng 8 chữ số (ví dụ ``'20250101'``).

    Trường hợp biên:
        - ``'01/02/2025'`` luôn là 1/2, không phải 2/1 (ngày trước).
        - ``'32/13/2025'``, ``'31/02/2025'`` -> ``None``.
        - Năm 2 chữ số (``'01/02/25'``) KHÔNG hỗ trợ, vì đoán thế kỷ là
          đoán mò; trả ``None`` để tầng trên đánh dấu nghi ngờ.
        - Chuỗi rỗng, ``'x'``, ``'-'`` -> ``None``.
        - Số năm đơn lẻ (``2025``) -> ``None`` (năm không phải là ngày).
    """
    # 1. Đối tượng ngày/giờ sẵn có.
    if isinstance(value, _datetime.datetime):
        return value.date()
    if isinstance(value, _datetime.date):
        return value
    if value is None or isinstance(value, bool):
        return None
    if hasattr(value, "to_pydatetime"):
        # pandas Timestamp và tương tự: đổi về date thật nếu được.
        try:
            return value.to_pydatetime().date()
        except Exception:
            return None

    # 2. Excel serial dạng số.
    if isinstance(value, (int, float)) and _math.isfinite(float(value)):
        # Số nguyên (hoặc thập phân có phần nguyên) trong khoảng serial.
        return _excel_serial_to_date(value)

    s = str(value).strip()
    if not s or s.lower() in _EMPTY_TOKENS:
        return None

    # 3. Dạng YYYY-MM-DD kèm giờ: '2025-02-01 06:00:25' -> lấy 3 nhóm đầu.
    m = _re.match(
        r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[T ].*)?$", s
    )
    if m:
        return _make_date(m.group(1), m.group(2), m.group(3))

    # 4. Dạng DD/MM/YYYY (dấu tách -, /, . đều chấp nhận).
    m = _re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
    if m:
        return _make_date(m.group(3), m.group(2), m.group(1))

    # 5. Dạng tiếng Việt: '1 thg 1 2025', '1 thang 1 nam 2025',
    # 'ngay 1 thang 1 nam 2025', và dạng không năm '1 thg 1'.
    # Bỏ dấu trước để một regex ăn cả có dấu lẫn không dấu.
    ascii_s = strip_diacritics(s.lower())
    m = _re.fullmatch(
        r"(?:ngay\s*)?(\d{1,2})\s*(?:thg|thang|th)\.?\s*(\d{1,2})"
        r"(?:\s*(?:nam)?\s*(\d{4}))?",
        ascii_s,
    )
    if m:
        year = m.group(3)
        if year is None:
            # Ngày không năm: bắt buộc phải có sheet_year.
            if sheet_year is None:
                return None
            year = sheet_year
        return _make_date(year, m.group(2), m.group(1))

    # 6. Ngày không năm dạng số: '1/1', '1-1', '1.1'.
    m = _re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})", s)
    if m:
        if sheet_year is None:
            return None
        return _make_date(sheet_year, m.group(2), m.group(1))

    # 7. Dạng 8 chữ số YYYYMMDD.
    m = _re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
    if m:
        return _make_date(m.group(1), m.group(2), m.group(3))

    # 8. Chuỗi số thuần còn lại: thử hiểu là Excel serial (ví dụ ô
    # serial bị đọc thành chuỗi '45689' hoặc '45689.0').
    m = _re.fullmatch(r"(\d{4,6})(?:\.0+)?", s)
    if m:
        return _excel_serial_to_date(int(m.group(1)))

    # Mọi dạng khác (chữ tự do, mã đơn, tên...) đều không phải ngày.
    return None


def parse_quantity(value):
    """Parse số lượng thành ``(số, đơn vị)``, hoặc ``(None, None)``.

    Đầu vào:
        value: số thuần, chuỗi ``'1 ly'`` / ``'3 chai'`` / ``'2 thùng'``,
            ``'1 bao 25kg'``, phân số ``'1/2 hộp'``, ``None``, ``'x'``...

    Đầu ra:
        Tuple ``(số_lượng, đơn_vị)`` trong đó:
        - ``số_lượng`` là float (âm được, vì dòng trả hàng ghi ``-1``).
        - ``đơn_vị`` là chuỗi đã :func:`normalize_key` (không dấu,
          ví dụ ``'ly'``, ``'chai'``, ``'thung'``, ``'bao 25kg'``),
          hoặc ``None`` nếu giá trị gốc không ghi đơn vị.
        - ``(None, None)`` nếu không parse được (``'x'``, rỗng, chỉ có
          đơn vị mà không có số, chữ tự do...). KHÔNG gán 0.

    Cách làm:
        - Số thuần -> ``(số, None)`` (đơn vị nằm ở cột riêng, tầng
          ingest sẽ đọc cột đơn vị để ghép).
        - Chuỗi toàn số (kể cả khoảng trắng phân cách nghìn) -> số,
          đơn vị ``None``.
        - Phân số ``'a/b đơn vị'`` -> ``a/b`` (dữ liệu bán lẻ đôi khi
          ghi nửa đơn vị kiểu này; hỗ trợ để khỏi mất dòng).
        - Còn lại: tách số ở đầu chuỗi, phần còn lại là đơn vị. Số
          dùng cùng quy tắc dấu thập phân/phân cách nghìn như tiền
          (``'1,5'`` -> 1.5, ``'1.000'`` -> 1000), vì cùng cách ghi
          Việt Nam.

    Trường hợp biên:
        - ``'1 ly'`` -> ``(1.0, 'ly')``; ``'3 chai'`` -> ``(3.0, 'chai')``.
        - ``5`` -> ``(5.0, None)``; ``'x'`` -> ``(None, None)``.
        - ``'-1 ly'`` -> ``(-1.0, 'ly')`` (dòng trả hàng, giữ dấu âm
          để tầng tính toán biết đây là trả, không phải bán).
        - ``'ly'`` (chỉ đơn vị, không số) -> ``(None, None)``: không
          đoán số lượng là 1, vì đoán sẽ làm sai tổng bán.
        - ``True``/``False`` -> ``(None, None)``.
    """
    if value is None or isinstance(value, bool):
        return (None, None)
    if _is_finite_number(value):
        return (float(value), None)
    if isinstance(value, (_datetime.date, _datetime.datetime)):
        return (None, None)
    if hasattr(value, "to_pydatetime"):
        return (None, None)

    s = str(value).strip()
    if not s or s.lower() in _EMPTY_TOKENS:
        return (None, None)

    # Chuỗi toàn số (có thể có khoảng trắng phân cách nghìn): bỏ khoảng
    # trắng rồi parse, đơn vị là None.
    if _re.fullmatch(r"[+-]?[\d\s.,]+", s):
        number = _plain_number(s)
        return (number, None) if number is not None else (None, None)

    # Phân số: '1/2 hộp' -> 0.5 hộp. Chỉ nhận khi mẫu số khác 0.
    m = _re.fullmatch(r"([+-]?\d+)\s*/\s*(\d+)\s*(.*)", s)
    if m and int(m.group(2)) != 0:
        unit = normalize_key(m.group(3)) if m.group(3).strip() else None
        sign = -1.0 if m.group(1).startswith("-") else 1.0
        return (sign * abs(int(m.group(1))) / int(m.group(2)), unit or None)

    # Dạng chung: số ở đầu, phần còn lại là đơn vị.
    m = _re.match(r"([+-]?\d+(?:[.,]\d+)?)\s*(.*)", s)
    if not m:
        # Không bắt đầu bằng số (ví dụ chỉ có đơn vị 'ly', hay chữ tự
        # do) -> không parse được, không đoán.
        return (None, None)
    number = _plain_number(m.group(1))
    if number is None:
        return (None, None)
    rest = m.group(2).strip()
    if not rest:
        return (number, None)
    unit = normalize_key(rest)
    return (number, unit if unit else None)

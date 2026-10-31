# -*- coding: utf-8 -*-
"""Tiện ích chuẩn hoá văn bản tiếng Việt cho DataMate (Giai đoạn 1).

Mục đích của module này
------------------------
Mọi bước nhận diện cột, khái niệm, tên sản phẩm/khách hàng đều phải so sánh
được các cách viết khác nhau của cùng một thứ, ví dụ ``'Tên sản phẩm'``,
``'Ten hang'``, ``'ten_hang'`` hay ``'Product'``. Module này cung cấp một
không gian so sánh chung:

- :func:`normalize_key`: chuẩn hoá mạnh, bỏ dấu, dùng làm **khoá so sánh**
  (tên cột, từ điển đồng nghĩa, đơn vị).
- :func:`normalize_vn_display`: chuẩn hoá nhẹ, **giữ dấu**, dùng khi cần
  hiển thị hoặc so sánh tên mà vẫn tôn trọng cách viết có dấu trong file.
- :func:`strip_diacritics`: chỉ bỏ dấu, giữ nguyên hoa/thường và dấu câu.

Nguyên tắc chống hard-code
--------------------------
Module này không biết gì về shop, ngành hay tên cột cụ thể nào. Nó chỉ
biến đổi chuỗi một cách tổng quát, nên shop mới, header mới đều đi qua
cùng một đường chuẩn hoá.

Chỉ dùng thư viện chuẩn Python, không gọi mạng, không đọc file.
"""

import re
import unicodedata

__all__ = ["normalize_key", "normalize_vn_display", "strip_diacritics"]

# Bảng đổi riêng cho chữ đ/Đ: khi tách dấu bằng NFD thì đ không tách thành
# ký tự gốc + dấu, nên phải đổi tay. Các ký tự có dấu còn lại đều tách được.
_D_MAP = str.maketrans({"đ": "d", "Đ": "D"})


def strip_diacritics(text):
    """Bỏ dấu tiếng Việt nhưng giữ nguyên hoa/thường và dấu câu.

    Đầu vào:
        text: chuỗi bất kỳ, hoặc ``None``/giá trị không phải chuỗi.

    Đầu ra:
        Chuỗi đã bỏ dấu. Trả ``''`` nếu đầu vào rỗng/``None``.

    Cách làm, và vì sao làm vậy:
        Dùng ``unicodedata.normalize('NFD', ...)`` để tách mỗi ký tự có dấu
        thành ký tự gốc + dấu tổ hợp, rồi loại bỏ các dấu tổ hợp (nhóm
        Unicode ``Mn``). Riêng ``đ/Đ`` không tách được nên đổi tay qua
        bảng :data:`_D_MAP`. Cuối cùng gộp lại bằng NFC để chuỗi kết quả
        ổn định khi so sánh hay ghi file.

    Trường hợp biên:
        - Ký tự không phải tiếng Việt (ví dụ chữ Hán, emoji) được giữ
          nguyên, vì bước loại dấu chỉ chạm vào dấu tổ hợp.
        - Giá trị không phải chuỗi (ví dụ số) được chuyển thành chuỗi
          trước khi xử lý, để hàm không crash khi cột chứa lẫn kiểu.
    """
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    if not s:
        return ""
    # Tách dấu rồi bỏ dấu tổ hợp; đ/Đ đổi tay vì không tách được.
    decomposed = unicodedata.normalize("NFD", s)
    without_marks = "".join(
        ch for ch in decomposed if unicodedata.category(ch) != "Mn"
    )
    without_marks = without_marks.translate(_D_MAP)
    return unicodedata.normalize("NFC", without_marks)


def normalize_key(text):
    """Chuẩn hoá mạnh để làm khoá so sánh tên cột/khái niệm.

    Đầu vào:
        text: tên cột, khái niệm, đơn vị... hoặc ``None``.

    Đầu ra:
        Chuỗi thường, không dấu, chỉ còn chữ/số và khoảng trắng đơn.
        Ví dụ: ``'Tên sản phẩm'`` -> ``'ten san pham'``,
        ``'ten_hang'`` -> ``'ten hang'``, ``'Mã-HĐ!'`` -> ``'ma hd'``.
        Trả ``''`` nếu đầu vào rỗng/``None``.

    Vì sao làm vậy:
        Dữ liệu train trộn lẫn có dấu/không dấu, hoa/thường, gạch dưới,
        gạch ngang, khoảng trắng thừa. Nếu so sánh chuỗi thô, cùng một
        khái niệm sẽ bị coi là khác nhau. Bỏ dấu + thường hoá + thay
        ký tự không phải chữ/số bằng khoảng trắng sẽ đưa mọi cách viết
        về cùng một khoá, để từ điển đồng nghĩa trong ``ingest.py`` chỉ
        cần viết một lần bằng dạng không dấu.

    Trường hợp biên:
        - ``None``, ``''``, chuỗi chỉ có dấu câu -> ``''``.
        - Số (ví dụ tên cột là ``2025``) -> ``'2025'`` (giữ số để vẫn
          phân biệt được, không biến thành chuỗi rỗng).
        - Khoảng trắng đầu/cuối và khoảng trắng thừa ở giữa đều bị gộp.
    """
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    if not s:
        return ""
    # NFC trước để ổn định, rồi thường hoá, rồi bỏ dấu.
    s = unicodedata.normalize("NFC", s).lower()
    s = strip_diacritics(s)
    # Mọi ký tự không phải chữ/số (gạch dưới, gạch ngang, dấu câu,
    # khoảng trắng...) đều thành khoảng trắng, rồi gộp lại.
    s = "".join(ch if ch.isalnum() else " " for ch in s)
    return " ".join(s.split())


def normalize_vn_display(text):
    """Chuẩn hoá nhẹ để hiển thị/so sánh mà vẫn giữ dấu tiếng Việt.

    Đầu vào:
        text: tên sản phẩm, tên khách hàng... hoặc ``None``.

    Đầu ra:
        Chuỗi NFC, thường hoá, đã gộp khoảng trắng thừa và cắt khoảng
        trắng đầu/cuối. Dấu tiếng Việt và dấu câu được giữ nguyên.
        Trả ``''`` nếu đầu vào rỗng/``None``.

    Vì sao làm vậy, và khi nào dùng hàm này thay vì :func:`normalize_key`:
        Đáp án dạng tên phải đúng dấu và đúng cách viết trong file (hoặc
        dùng mã). Nếu dùng ``normalize_key`` để so sánh tên đáp án thì
        sẽ mất dấu và có thể nhận nhầm hai tên khác nhau chỉ khác dấu.
        Hàm này chỉ dọn khoảng trắng và chuẩn hoá Unicode, nên dùng khi
        cần giữ dấu, ví dụ gom nhóm tên khách hàng chỉ khác cách viết
        khoảng trắng/hoa thường, hoặc hiển thị tên trong báo cáo.

    Trường hợp biên:
        - ``None``/``''`` -> ``''``.
        - Khoảng trắng thừa, tab, xuống dòng ở giữa chuỗi đều gộp thành
          một khoảng trắng, vì file Excel/CSV hay lẫn các ký tự này.
        - Dấu câu trong tên (ví dụ ``'Cà phê (M)'``) được giữ nguyên.
    """
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    if not s:
        return ""
    s = unicodedata.normalize("NFC", s).lower()
    # Chỉ gộp khoảng trắng, không chạm vào dấu hay dấu câu.
    return " ".join(s.split())


# Giữ lại tên cũ nếu code khác từng gọi nhầm: không có, module này chỉ
# có đúng ba hàm public trên, theo API_CONTRACT.

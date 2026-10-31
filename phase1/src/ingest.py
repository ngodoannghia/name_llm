# -*- coding: utf-8 -*-
"""Ingest dữ liệu shop cho DataMate (Giai đoạn 1).

Mục đích của module này
------------------------
Đọc toàn bộ dữ liệu của một shop (CSV, XLSX nhiều sheet, SQLite nhiều
bảng) và biến nó thành các bảng pandas đã làm sạch nhẹ, kèm một hồ sơ
shop (:class:`ShopProfile`) nói rõ shop đó thực sự có loại dữ liệu nào.
Tầng tính toán phía sau chỉ cần nhìn vào ``columns_mapped`` và các cờ
``has_*`` là biết câu nào trả được, câu nào phải unanswerable/abstain,
mà không cần biết shop đó dùng tên cột hay tên file gì.

Luồng xử lý cho một shop (theo đúng API_CONTRACT và yêu cầu tổng quát hoá):

1.  Liệt kê file trong thư mục shop, bỏ qua file lạ bằng warning (không
    crash). README được liệt kê trong ``files`` nhưng không phải bảng.
2.  Đọc từng file:
    - CSV: dò dấu tách, quét dòng đầu tìm header thật.
    - XLSX: mỗi sheet quét dòng đầu tìm header thật (header có thể ở
      dòng 3-4, hoặc dòng 2 sau dòng nhãn nhóm 2 tầng), trích năm từ
      tên sheet để hỗ trợ ngày không năm.
    - SQLite: mỗi bảng là một bảng dữ liệu, đọc trực tiếp bằng tên cột
      trong DB (không cần header).
3.  Hợp nhất: sheet theo tháng của cùng một file, và file tách
    (``_1``/``_2``, ``_2025``/``_2026``) có cùng bộ cột, được gộp thành
    một bảng duy nhất trước khi tính toán.
4.  Map cột theo ý nghĩa (:func:`map_columns`): chuẩn hoá tiếng Việt +
    từ điển đồng nghĩa mở rộng + kiểm chứng bằng mẫu giá trị thật.
5.  Forward-fill ô trống theo nhóm đơn, khử trùng theo (mã đơn + toàn
    bộ dòng) cho bảng có mã đơn.
6.  Quy đổi số lượng về đơn vị cơ bản (cột ``'_qty_base'``) nếu shop có
    bảng quy đổi hoặc cột quy đổi sẵn; nếu không thì ghi warning và
    giữ nguyên số lượng gốc.
7.  Lập :class:`ShopProfile`: có chi tiết mặt hàng không, có mã/tên
    khách không, có giá vốn, lô/hạn dùng không, có bảng trả hàng riêng
    không.

Nguyên tắc chống hard-code (bắt buộc, xem
``analysis/05_generalization_requirements.md``):
    - Không hard-code business_id, tên shop, tên ngành, id câu hỏi.
    - Không hard-code tên cột chính xác của train: mọi nhận diện đều qua
      :func:`normalize_key` + :data:`COLUMN_SYNONYMS` (từ điển ý nghĩa)
      + mẫu giá trị. Một cột mới chưa từng thấy vẫn map được nếu tên
      của nó cùng ý nghĩa, và sẽ KHÔNG map nếu mẫu giá trị không khớp
      (ví dụ cột tên mà toàn số thì không thể là cột tiền).
    - Không hard-code thứ tự cột, vị trí header, tên sheet, quy ước tên
      file theo tháng: tất cả đều phát hiện lúc chạy.
    - Không hard-code schema theo ngành: shop mới đi qua đúng pipeline
      này, hồ sơ shop được suy ra từ dữ liệu thật của shop đó.

Giới hạn có ghi rõ trong docstring từng hàm:
    - Bảng trong ``IngestResult.tables`` giữ tên cột gốc (chỉ làm sạch
      nhẹ: bỏ dòng hoàn toàn trống, forward-fill, khử trùng). Việc đổi
      tiền/ngày/số lượng thành cột chuẩn là việc của code dùng ingest,
      dựa vào ``columns_mapped`` + các hàm trong ``normalizers.py``.
    - ``parse_failures`` trong profile là số lỗi ghi nhận được qua mẫu
      kiểm chứng và quá trình quy đổi, KHÔNG phải đếm toàn bộ mọi ô
      (đếm toàn bộ 4 triệu dòng mỗi lần load sẽ quá chậm); nó là tín
      hiệu chất lượng, không phải con số kiểm toán tuyệt đối.
    - Dòng không parse được thì giữ nguyên giá trị gốc, không gán 0,
      không xoá dòng (trừ dòng hoàn toàn trống và dòng trùng đúng nghĩa
      lỗi xuất file).
"""

import csv as _csv
import re as _re
import sqlite3 as _sqlite3
from dataclasses import dataclass, field as _field
from pathlib import Path as _Path

try:
    import pandas as _pd
except ImportError:  # pragma: no cover - pandas là dependency bắt buộc
    _pd = None

try:
    # Chạy dưới dạng package: from src.ingest import ...
    from .normalizers import parse_date, parse_money, parse_quantity
    from .text_utils import normalize_key, strip_diacritics
except ImportError:  # pragma: no cover - chạy file rời trong src/
    from normalizers import parse_date, parse_money, parse_quantity
    from text_utils import normalize_key, strip_diacritics

__all__ = [
    "COLUMN_SYNONYMS",
    "ShopProfile",
    "IngestResult",
    "detect_header_row",
    "map_columns",
    "load_shop",
    "load_all_shops",
]

# ---------------------------------------------------------------------------
# Từ điển đồng nghĩa theo Ý NGHĨA (không phải tên cột của một shop nào)
# ---------------------------------------------------------------------------
# Quy ước viết trong từ điển này:
#   - Khoá là tên semantic dùng chung cho mọi shop (theo API_CONTRACT).
#   - Giá trị là danh sách từ khoá ĐÃ normalize_key (thường, không dấu,
#     khoảng trắng đơn). Viết thêm đồng nghĩa mới vào đây là cách hợp lệ
#     duy nhất để dạy hệ thống một cách gọi mới, thay vì sửa code map.
#   - So khớp không chỉ so chuỗi bằng nhau: xem _name_score(), nó còn
#     chấp nhận dạng dính liền kiểu SQLite ('mahoadon' ~ 'ma hoa don'),
#     dạng chứa từ khoá ('ten san pham ban' chứa 'ten san pham').
#   - Từ khoá một từ (ví dụ 'gia', 'ten', 'ngay') CHỈ khớp chính xác,
#     để cột tên chung chung không bị gán bừa thành tên sản phẩm hay
#     giá bán. Xem _name_score() để rõ ngưỡng.
#   - Một số khái niệm cố tình xuất hiện ở hai semantic ('tong tien' ở
#     cả line_amount và order_total): map_columns() dùng ngữ cảnh bảng
#     (bảng có chi tiết mặt hàng hay không) để chọn, xem hàm đó.
COLUMN_SYNONYMS = {
    # --- Đơn hàng ---
    "order_id": [
        "ma don hang", "ma don", "ma hoa don", "ma bill", "ma hd",
        "ma phieu", "so hoa don", "ma chung tu", "ma don dat hang",
        "ma giao dich", "ma don ban", "order id", "order code",
        "bill id", "invoice id",
    ],
    "order_date": [
        "ngay dat hang", "ngay ban", "ngay giao dich", "ngay lap",
        "ngay dat", "ngay", "ngay tao don", "ngay ban hang",
        "ngay xuat", "ngay mua", "ngay thang", "order date", "date",
    ],
    "order_datetime": [
        "gio", "thoi gian lap", "thoi gian tao", "ngay gio lap",
        "ngay gio", "thoi gian", "thoi gian dat hang", "gio tao don",
        "datetime", "timestamp",
    ],
    "order_status": [
        "trang thai don hang", "trang thai", "ket qua", "tinh trang",
        "trang thai xu ly", "trang thai don", "ket qua don",
        "trang thai giao", "status", "order status",
    ],
    "payment_method": [
        "phuong thuc thanh toan", "phuong thuc tt", "hinh thuc thanh toan",
        "hinh thuc tt", "cach thanh toan", "phuong thuc",
        "pttt", "payment method",
    ],
    # --- Khách hàng ---
    "customer_code": [
        "ma khach hang", "ma khach", "ma kh", "ma doi tac",
        "ma ben mua", "customer id", "customer code",
    ],
    "customer_name": [
        "ten khach hang", "ten khach", "khach hang", "khach",
        "ben mua", "doi tac", "ten doi tac", "ten ben mua",
        "nguoi mua", "ten nguoi mua", "customer name", "customer",
    ],
    "customer_group": [
        "nhom khach", "loai khach hang", "nhom khach hang",
        "loai khach", "customer group",
    ],
    # --- Mặt hàng ---
    "product_code": [
        "ma hang hoa", "ma hang", "ma san pham", "ma sp", "ma mon",
        "ma mat hang", "ma thuoc", "sku", "ma vach", "barcode",
        "ma barcode", "product code", "product id", "id sp",
    ],
    "product_name": [
        "ten hang hoa", "ten hang", "ten san pham", "ten mon", "mon",
        "noi dung", "hang hoa", "hang", "san pham", "ten thuoc",
        "ten mat hang", "product name", "product", "ten", "item",
    ],
    "product_group": [
        "nhom hang", "nhom san pham", "danh muc", "loai hang",
        "loai san pham", "loai hang hoa", "nhom", "nhom hang hoa",
        "category", "group",
    ],
    # --- Số lượng và đơn vị ---
    "quantity": [
        "so luong", "so luong san pham", "so luong ban", "sl",
        "so luong dat", "so luong mua", "so luong xuat",
        "quantity", "qty",
    ],
    "quantity_converted": [
        "so luong quy doi", "quy ra dv le", "sl quy doi",
        "so luong le", "so luong quy ve dv le", "quy doi",
        "so luong co so", "converted quantity",
    ],
    "unit": [
        "don vi tinh", "don vi", "unit", "dv", "don vi ban",
        "don vi xuat", "uom",
    ],
    "from_unit": [
        "don vi quy doi", "don vi lon", "don vi doi", "don vi quy",
        "from unit",
    ],
    "base_unit": [
        "don vi co ban", "don vi co so", "don vi nho nhat",
        "dv co ban", "don vi goc", "don vi le", "base unit",
    ],
    "conversion_factor": [
        "ti le quy doi", "he so quy doi", "he so", "ti le",
        "ty le quy doi", "he so doi", "conversion factor", "factor",
    ],
    # --- Tiền ---
    "unit_price": [
        "gia ban", "don gia", "gia", "gia le", "gia san pham",
        "gia don vi", "gia niem yet", "gia ban le", "don gia ban",
        "unit price", "price",
    ],
    "line_amount": [
        "thanh tien", "tri gia", "tong tien", "thanh tien hang",
        "tien hang", "tong tien hang", "gia tri",
        "amount", "line total", "line amount",
    ],
    "cost_price": [
        "gia von", "gia von hang", "gia von luc ban", "gia nhap",
        "don gia nhap", "gia von don vi", "gia goc", "gia von ban",
        "cost price", "cost",
    ],
    "order_total": [
        "tong thanh toan", "tong tien", "tong cong", "tong tien don",
        "tong don", "tong phai tra", "tong tien phai tra",
        "tong gia tri", "total", "total amount", "order total",
    ],
    "amount_paid": [
        "da tra", "khach da tra", "so tien thu", "tien da tra",
        "tien thu", "da thanh toan", "amount paid", "paid",
    ],
    "amount_due": [
        "chua tra", "con phai thu", "con no", "so tien no", "tien no",
        "con lai", "phai thu", "amount due",
    ],
    "discount": [
        "chiet khau", "giam gia", "tien giam gia", "giam gia don",
        "giam gia dong", "discount",
    ],
    # --- Đối tác, nhân sự, chi nhánh ---
    "supplier_code": [
        "ma nha cung cap", "ma ncc", "ma nha cung ung",
        "supplier id", "supplier code",
    ],
    "supplier_name": [
        "ten nha cung cap", "ten ncc", "nha cung cap", "ncc",
        "ten nha cung ung", "supplier name", "supplier",
    ],
    "employee_code": [
        "ma nhan vien", "ma nv", "employee id", "staff id",
        "ma nhan vien ban",
    ],
    "employee_name": [
        "ten nhan vien", "nhan vien", "ten nv", "nhan vien ban",
        "nguoi ban", "ten nhan vien ban hang", "employee name",
    ],
    "branch": [
        "chi nhanh", "ma chi nhanh", "ten chi nhanh", "cua hang",
        "branch", "store",
    ],
    # --- Lô, hạn dùng, kho, nhập, trả ---
    "lot_code": [
        "ma lo", "lo hang", "ma lo hang", "lot", "lot code",
        "batch", "so lo",
    ],
    "expiry_date": [
        "han su dung", "hsd", "han dung", "ngay het han",
        "expiry date", "expiry", "han", "ngay han",
    ],
    "stock_quantity": [
        "ton kho", "ton", "so luong ton", "ton cuoi", "ton dau",
        "so luong ton kho", "ton hien tai", "stock", "inventory",
    ],
    "import_date": [
        "ngay nhap kho", "ngay nhap", "ngay phieu nhap",
        "ngay nhap hang", "import date",
    ],
    "delivery_date": [
        "ngay giao", "ngay giao hang", "ngay nhan",
        "delivery date",
    ],
    "payment_date": [
        "ngay thu", "ngay thanh toan", "ngay tra", "ngay gio thu",
        "ngay tra no", "payment date",
    ],
    "return_quantity": [
        "so luong tra", "sl tra", "so luong tra hang",
        "sl tra hang", "so luong tra quy doi", "sl tra quy doi",
        "return quantity",
    ],
    "return_amount": [
        "tien hoan tra", "so tien tra", "tien tra hang",
        "return amount",
    ],
}

# Các semantic tiền/ngày/số lượng/mã dùng để kiểm chứng bằng mẫu giá trị.
# Tách riêng để map_columns() biết cột nào phải thử parse mẫu, thay vì
# thử mọi cột (vừa chậm vừa dễ nhận nhầm).
_MONEY_SEMANTICS = {
    "unit_price", "line_amount", "cost_price", "order_total",
    "amount_paid", "amount_due", "discount", "return_amount",
}
_DATE_SEMANTICS = {
    "order_date", "expiry_date", "import_date", "delivery_date",
    "payment_date",
}
_QUANTITY_SEMANTICS = {"quantity", "quantity_converted", "stock_quantity",
                       "return_quantity"}
# Các cột cấp đơn: khi forward-fill, chỉ những cột này mới được kế thừa
# trong nhóm đơn. Cột mặt hàng/số lượng/giá dòng KHÔNG fill, vì mỗi dòng
# là một mặt hàng khác nhau.
_ORDER_LEVEL_SEMANTICS = {
    "order_date", "order_datetime", "customer_code", "customer_name",
    "customer_group", "order_status", "payment_method", "branch",
    "employee_code", "employee_name", "supplier_code", "supplier_name",
}
# Tên cột generic kiểu 'TenGoi' (SQLite) không thuộc từ điển trên vì nó
# không tự nói rõ là tên của cái gì; ý nghĩa của nó suy từ TÊN BẢNG.
# Bảng gợi ý này dùng token của tên bảng (đã normalize_key), hoàn toàn
# tổng quát, không gắn với shop nào.
_TABLE_NAME_HINTS = [
    (("khach",), "customer_name"),
    (("san", "pham"), "product_name"),
    (("hang", "hoa"), "product_name"),
    (("thuoc",), "product_name"),
    (("cung", "cap"), "supplier_name"),
    (("ncc",), "supplier_name"),
    (("nhan", "vien"), "employee_name"),
    (("chi", "nhanh"), "branch"),
]

# ---------------------------------------------------------------------------
# Dataclass kết quả
# ---------------------------------------------------------------------------


@dataclass
class ShopProfile:
    """Hồ sơ dữ liệu của một shop, suy ra từ dữ liệu thật của shop đó.

    Các trường:
        business_id: mã shop (tên thư mục shop, không dùng để suy luận
            gì khác; chỉ để định danh trong báo cáo).
        files: tên file trong thư mục shop (kể cả README), xếp theo
            thứ tự bảng chữ cái, để đối chiếu shop có bao nhiêu file.
        tables: tên các bảng đã hợp nhất (xem IngestResult.tables).
        rows_in: tổng số dòng dữ liệu đọc được, trước khử trùng (đã bỏ
            dòng hoàn toàn trống và dòng tiêu đề/header).
        rows_out: tổng số dòng còn lại sau khử trùng.
        columns_mapped: semantic -> tên cột thật đầu tiên tìm thấy trong
            shop (gộp qua mọi bảng). Semantic nào không map được ở bảng
            nào thì nhận giá trị ``''`` và đồng thời có mặt trong
            ``columns_unmapped``. Cách ghi ``''`` này theo đúng ghi chú
            trong API_CONTRACT cho dataclass này.
        columns_unmapped: danh sách semantic chưa map được ở shop này.
            Tầng quyết định dùng nó để abstain thay vì đoán cột.
        has_product_detail: True khi có bảng giao dịch vừa có mặt hàng
            (mã hoặc tên) vừa có mã đơn hoặc số lượng — tức là có dòng
            đơn chi tiết, không chỉ danh mục hàng. Shop chỉ có hoá đơn
            tổng (không có mặt hàng) sẽ là False, và mọi câu hỏi sản
            phẩm của shop đó phải unanswerable/abstain.
        has_customer_code / has_customer_name: có cột mã/tên khách ở
            bảng nào đó hay không (hai cờ độc lập, vì có shop chỉ có
            tên mà không có mã).
        has_cost: có cột giá vốn ở bảng nào đó hay không.
        has_lot_expiry: có cột mã lô hoặc hạn dùng ở bảng nào đó.
        has_return_table: có bảng riêng về trả hàng hay không (nhận
            diện bằng tên bảng chứa token 'tra'/'return', hoặc cột số
            lượng/tiền trả). Trả hàng thể hiện bằng trạng thái đơn thì
            KHÔNG tính là bảng riêng, nên cờ này False là đúng.
        warnings: mọi cảnh báo trong quá trình ingest của shop.
        parse_failures: số lần parse mẫu thất bại ghi nhận được (mẫu
            kiểm chứng map cột + quá trình quy đổi). Đây là tín hiệu
            chất lượng, KHÔNG phải đếm toàn bộ mọi ô, xem ghi chú ở
            đầu module.
    """

    business_id: str
    files: list = _field(default_factory=list)
    tables: list = _field(default_factory=list)
    rows_in: int = 0
    rows_out: int = 0
    columns_mapped: dict = _field(default_factory=dict)
    columns_unmapped: list = _field(default_factory=list)
    has_product_detail: bool = False
    has_customer_code: bool = False
    has_customer_name: bool = False
    has_cost: bool = False
    has_lot_expiry: bool = False
    has_return_table: bool = False
    warnings: list = _field(default_factory=list)
    parse_failures: int = 0


@dataclass
class IngestResult:
    """Kết quả ingest của một shop.

    Các trường:
        business_id: mã shop.
        tables: tên bảng đã hợp nhất -> DataFrame đã làm sạch nhẹ.
            Tên cột gốc được giữ nguyên (chỉ cắt khoảng trắng đầu/cuối
            và đánh số khi trùng tên), để code dùng ingest đối chiếu
            được với file gốc. Cột phụ (nếu có) đều bắt đầu bằng ``'_'``:
            ``'_sheet_year'`` (năm suy từ tên sheet/file, khi có) và
            ``'_qty_base'`` (số lượng quy về đơn vị cơ bản, khi quy đổi
            được). Cột phụ không bao giờ ghi đè cột gốc.
        profile: :class:`ShopProfile` của shop.
        warnings: trùng với ``profile.warnings`` (giữ ở cả hai chỗ để
            code dùng một trong hai đều thấy; API_CONTRACT liệt kê cả
            hai trường nên không bỏ trường nào).
    """

    business_id: str
    tables: dict = _field(default_factory=dict)
    profile: ShopProfile = None  # type: ignore[assignment]
    warnings: list = _field(default_factory=list)


# ---------------------------------------------------------------------------
# Phát hiện dòng header
# ---------------------------------------------------------------------------


def _header_score(row):
    """Chấm điểm một dòng thô xem nó giống dòng header đến đâu.

    Cách chấm (càng giống header càng cao):
        - Ô rỗng/không có nội dung không tính điểm.
        - Ô chứa chữ (có ít nhất một ký tự chữ) được tính là ô "chữ":
          header thường toàn ô chữ, còn dòng dữ liệu thì nhiều ô số,
          ngày, mã. Ô số thuần hay ngày tháng không tính là ô chữ.
        - Dòng chỉ có 1 ô (tiêu đề sổ, tên công ty) bị trừ nặng, vì đó
          gần như chắc chắn là tiêu đề, không phải header.
        - Dòng có ít ô mà chứa từ tiêu đề ('bao cao', 'ky bao cao',
          'tong cong') cũng bị trừ, để dòng 'Kỳ báo cáo: ...' không
          thắng dòng header thật.
        - Dòng có từ 3 ô chữ trở lên và tỉ lệ ô chữ >= 50% được cộng
          thêm, vì đó là dấu hiệu mạnh của header.
        - Ô là số (int/float hoặc chuỗi số) bị trừ nhẹ, vì header hiếm
          khi chứa số thuần (trừ năm, nhưng năm thường nằm trong tiêu
          đề chứ không phải header).

    Vì sao không dùng một luật đơn giản kiểu "dòng đầu tiên có chữ":
        Dòng tiêu đề ('SỔ CHI TIẾT BÁN HÀNG') cũng có chữ, dòng kỳ báo
        cáo cũng có chữ, dòng nhãn nhóm 2 tầng ('Hàng', 'Tiền', 'Đơn')
        cũng có chữ. Chỉ chấm điểm tổng hợp mới phân biệt được header
        thật (nhiều ô chữ, phủ kín các cột) với các dòng đó (ít ô chữ,
        nhiều ô trống).
    """
    cells = []
    for cell in row:
        if cell is None:
            continue
        if isinstance(cell, str):
            if cell.strip():
                cells.append(cell)
        else:
            cells.append(cell)
    if not cells:
        return -100.0
    text_cells = [
        c for c in cells
        if isinstance(c, str) and any(ch.isalpha() for ch in c)
    ]
    n_text = len(text_cells)
    n_total = len(cells)
    if n_total <= 1:
        # Tiêu đề một ô: chắc chắn không phải header.
        return -50.0 + n_text
    joined = normalize_key(" ".join(text_cells))
    if n_total <= 3 and any(
        word in joined for word in ("bao cao", "ky bao cao", "tong cong",
                                    "cong ty", "bang ke")
    ):
        return -20.0
    numeric_cells = 0
    for c in cells:
        if isinstance(c, (int, float)):
            numeric_cells += 1
        elif isinstance(c, str):
            stripped = c.strip().replace(",", "").replace(".", "")
            if stripped and stripped.replace("-", "").isdigit():
                numeric_cells += 1
    score = n_text * 10.0 + n_total - numeric_cells * 5.0
    if n_text >= 3 and n_text / max(n_total, 1) >= 0.5:
        score += 20.0
    return score


def detect_header_row(rows):
    """Tìm chỉ số dòng header thật trong các dòng thô đầu file/sheet.

    Đầu vào:
        rows: danh sách các dòng thô (mỗi dòng là list các ô, có thể
            chứa ``None`` cho ô trống). Thường là toàn bộ dòng của
            sheet, nhưng hàm chỉ quét N dòng đầu.

    Đầu ra:
        Chỉ số (0-based) của dòng header thật. Không bao giờ crash:
        nếu không có dòng nào giống header (toàn trống) thì trả 0.

    Cách làm:
        Quét tối đa 15 dòng đầu, chấm điểm mỗi dòng bằng
        :func:`_header_score`, chọn dòng điểm cao nhất; hoà điểm thì
        lấy dòng sớm nhất (dùng so sánh ``>`` thay vì ``>=``). Con số
        15 đủ bao phủ header ở dòng 3-4 sau tiêu đề + kỳ báo cáo +
        dòng trống (trường hợp bẩn nhất thấy trong train), mà vẫn hạn
        chế nguy cơ dòng dữ liệu ở xa thắng header (dòng dữ liệu càng
        xa header càng không được xét).

    Xử lý header 2 tầng:
        Dòng trên là nhãn nhóm ('Hàng'/'Tiền'/'Đơn') chỉ có vài ô chữ
        và nhiều ô trống nên điểm thấp; dòng dưới mới là tên cột thật
        với đầy đủ ô chữ nên điểm cao và được chọn. Nhãn nhóm bị bỏ,
        đúng theo API_CONTRACT ('dòng dưới mới là tên cột').

    Trường hợp biên:
        - Header ở dòng 0 (CSV phẳng, SQLite không qua hàm này) -> 0.
        - Sheet chỉ có tiêu đề rồi hết -> dòng tiêu đề vẫn được chọn
          (điểm cao nhất trong các dòng trống), tầng đọc file sau đó
          sẽ tạo bảng rỗng và ghi warning, không crash.
        - ``rows`` rỗng -> 0.
    """
    if not rows:
        return 0
    limit = min(len(rows), 15)
    best_idx = 0
    best_score = None
    for idx in range(limit):
        score = _header_score(rows[idx])
        if best_score is None or score > best_score:
            best_score = score
            best_idx = idx
    return best_idx


# ---------------------------------------------------------------------------
# Map cột theo ý nghĩa
# ---------------------------------------------------------------------------


def _tokens(normalized):
    """Tách chuỗi đã normalize_key thành danh sách token (từ)."""
    return normalized.split() if normalized else []


def _nospace(normalized):
    """Bỏ khoảng trắng để so dạng dính liền kiểu SQLite.

    Ví dụ 'ma hoa don' -> 'mahoadon', khớp với tên cột 'MaHoaDon'
    sau normalize_key cũng là 'mahoadon'. Nhờ vậy từ điển chỉ cần viết
    dạng có khoảng trắng mà vẫn ăn được tên cột dính liền.
    """
    return normalized.replace(" ", "")


def _name_score(column_normalized, synonym):
    """Chấm độ khớp tên giữa một cột và một từ khoá đồng nghĩa (0-100).

    Thang điểm, từ mạnh đến yếu:
        - 100: bằng nhau tuyệt đối ('ten san pham' == 'ten san pham').
        - 95: bằng nhau khi bỏ khoảng trắng ('mahoadon' == 'ma hoa don').
        - 85: mọi token của từ khoá đều có trong tên cột ('ten san pham
          ban' chứa 'ten san pham') — cột dài hơn nhưng vẫn cùng ý.
        - 75: mọi token của tên cột đều có trong từ khoá, và tên cột có
          từ 2 token trở lên (cột ngắn hơn nhưng không mơ hồ).
        - 0: không khớp.

    Cố tình KHÔNG có luật "chuỗi con dính liền" (ví dụ 'tongtien' nằm
    trong 'tongtienhang'): luật đó từng nhận nhầm cột 'NgayNhanViec'
    (ngày nhận việc, không phải ngày giao) thành delivery_date chỉ vì
    'ngaynhan' là chuỗi con của 'ngaynhanviec'. Các trường hợp chuỗi
    con thật sự cần ('TongTienHang', 'NgayNhanViec' loại trừ...) đều
    đã có từ khoá đầy đủ trong từ điển ('tong tien hang') hoặc khớp
    bằng nhau khi bỏ khoảng trắng, nên bỏ luật chuỗi con chỉ mất rất
    ít, mà tránh được nhận nhầm từ ghép dài hơn.

    Vì sao từ khoá một từ chỉ khớp chính xác:
        Từ như 'gia', 'ten', 'ngay', 'ma' xuất hiện trong hàng chục khái
        niệm khác nhau. Nếu cho 'gia' khớp một phần với 'gia von' hay
        'gia ban' thì cột 'GiaVon' sẽ tranh nhau giữa nhiều semantic.
        Chỉ khi tên cột đúng bằng từ đó ('Gia' đứng một mình) mới được
        tính, còn lại phải khớp từ khoá dài hơn, cụ thể hơn.
    """
    if not column_normalized or not synonym:
        return 0
    if column_normalized == synonym:
        return 100
    col_nospace = _nospace(column_normalized)
    syn_nospace = _nospace(synonym)
    if col_nospace == syn_nospace:
        return 95
    col_tokens = set(_tokens(column_normalized))
    syn_tokens = set(_tokens(synonym))
    if len(syn_tokens) == 1:
        # Từ khoá một từ: chỉ khớp chính xác (đã xét ở trên).
        return 0
    if syn_tokens <= col_tokens:
        return 85
    if len(col_tokens) >= 2 and col_tokens <= syn_tokens:
        return 75
    return 0


def _is_empty_value(value):
    """Ô có coi là trống không (None, NaN, chuỗi toàn khoảng trắng)."""
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _sample_values(series, limit=40):
    """Lấy tối đa ``limit`` giá trị không trống đầu tiên của một cột.

    Chỉ lấy mẫu đầu tiên thay vì toàn cột để map_columns() nhanh trên
    file hàng trăm nghìn dòng: kiểm chứng ý nghĩa cột không cần đọc
    hết cột, 40 mẫu đã đủ phân biệt cột tiền (parse được đa số) với
    cột tên (parse tiền gần như bằng 0). Nhược điểm là mẫu đầu có thể
    không đại diện nếu đầu file toàn lỗi; chấp nhận được vì điểm tên
    vẫn là chính, mẫu chỉ dùng để phá hoà và loại ứng viên yếu.
    """
    samples = []
    for value in series:
        if _is_empty_value(value):
            continue
        samples.append(value)
        if len(samples) >= limit:
            break
    return samples


def _date_like(value):
    """Giá trị có dạng ngày không (kể cả ngày không năm).

    Dùng riêng cho kiểm chứng cột ngày, vì parse_date() không có
    sheet_year sẽ trả None cho '1/1'. Một cột toàn '1/1' vẫn là cột
    ngày, nên ngoài parse_date() thành công, các mẫu khớp regex ngày
    (d/m, d thg m, yyyy-mm-dd, serial) cũng tính là giống ngày.
    """
    if parse_date(value) is not None:
        return True
    if isinstance(value, str):
        s = value.strip()
        if _re.fullmatch(r"\d{1,2}[-/.]\d{1,2}", s):
            return True
        # Ngày không năm kiểu '1 thg 1': bỏ dấu trước khi khớp để ăn
        # cả '1 thg 1' lẫn 'ngày 1 tháng 1' (parse_date không có
        # sheet_year sẽ trả None cho dạng này, nhưng cột vẫn là cột
        # ngày nên tính là giống ngày ở bước kiểm chứng).
        ascii_s = strip_diacritics(s.lower())
        if _re.fullmatch(
            r"(?:ngay\s*)?\d{1,2}\s*(?:thg|thang|th)\.?\s*\d{1,2}",
            ascii_s,
        ):
            return True
    return False


def _value_rate(series, semantic):
    """Tỉ lệ mẫu giá trị parse được của một cột theo semantic (0-1).

    Trả ``None`` nếu cột không có mẫu nào (toàn trống): khi đó không có
    bằng chứng chống lại tên cột, nên để tên quyết định, không trừ điểm.
    Ngược lại trả tỉ lệ mẫu parse thành công:
        - semantic tiền -> :func:`parse_money` != None.
        - semantic ngày -> giống ngày (xem :func:`_date_like`).
        - semantic số lượng -> :func:`parse_quantity` có số != None.
        - semantic mã đơn -> giống mã: chuỗi chứa chữ+số hoặc mã có
          tiền tố chữ, dài từ 3 ký tự; số thuần dài không tính là mã.
        - semantic khác -> ``None`` (không kiểm chứng bằng giá trị, vì
          tên/khách/trạng thái không có dạng parse đặc thù).
    """
    samples = _sample_values(series)
    if not samples:
        return None
    if semantic in _MONEY_SEMANTICS:
        ok = sum(1 for v in samples if parse_money(v) is not None)
    elif semantic in _DATE_SEMANTICS:
        ok = sum(1 for v in samples if _date_like(v))
    elif semantic in _QUANTITY_SEMANTICS:
        ok = sum(1 for v in samples if parse_quantity(v)[0] is not None)
    elif semantic == "order_id":
        ok = 0
        for v in samples:
            s = str(v).strip()
            if len(s) >= 3 and any(ch.isalpha() for ch in s) \
                    and any(ch.isdigit() for ch in s):
                ok += 1
    else:
        return None
    return ok / len(samples)


def map_columns(df):
    """Map cột của một DataFrame theo ý nghĩa, trả dict semantic -> cột thật.

    Đầu vào:
        df: DataFrame đã có header (tên cột là tên gốc trong file).

    Đầu ra:
        Dict ``semantic -> tên cột thật``. Semantic nào không map được
        với độ tin cậy đủ thì KHÔNG có trong dict (không đoán cột theo
        vị trí, để tầng quyết định abstain đúng lúc).

    Cách làm, theo 3 bước (đúng yêu cầu tổng quát hoá):
        Bước 1 — Chấm tên: mỗi cột chấm với mọi từ khoá trong
            :data:`COLUMN_SYNONYMS` bằng :func:`_name_score`, giữ điểm
            cao nhất cho từng cặp (cột, semantic). Chỉ giữ ứng viên
            >= 75 điểm, vì dưới ngưỡng đó chỉ là trùng từ chung chung
            (điểm 60 cho chuỗi con đã bị bỏ, xem _name_score).
        Bước 2 — Ngữ cảnh bảng: bảng có mặt hàng + số lượng thì cột
            'tong tien' thiên về line_amount (tiền dòng), bảng không
            có mặt hàng thì thiên về order_total (tiền đơn). Cộng/trừ
            5 điểm cho hai semantic này để phá hoà, thay vì hard-code
            'tong tien' luôn là một trong hai.
        Bước 3 — Kiểm chứng mẫu + gán tham lam: với ứng viên tiền/ngày/
            số lượng/mã đơn, tính tỉ lệ mẫu parse được. Ứng viên điểm
            tên < 90 mà tỉ lệ mẫu < 0.25 thì bị loại (tên na ná nhưng
            giá trị không khớp thì không phải cột đó). Các ứng viên
            còn lại xếp theo (điểm tên + thưởng mẫu) giảm dần, gán tham
            lam sao cho mỗi semantic nhận một cột và mỗi cột chỉ nhận
            một semantic — tránh một cột 'TongTien' vừa là line_amount
            vừa là order_total gây double-count phía sau.

    Trường hợp biên:
        - DataFrame rỗng hoặc không có cột -> dict rỗng.
        - Tên cột trùng nhau: chỉ xét lần xuất hiện đầu tiên, vì dict
          trả về dùng tên cột làm giá trị nên trùng tên không phân
          biệt được; tầng đọc file đã đánh số tên trùng trước khi tới
          đây nên trường hợp này hiếm.
        - Cột nhiễu ('Hinh anh', 'Vi tri ke', 'Bao hanh', 'Mau ghi chu'):
          không có từ khoá nào khớp đủ điểm nên tự động không map, và
          không làm lệch các cột khác vì gán tham lam xét theo điểm.
        - Cột toàn trống: tỉ lệ mẫu là None nên không bị loại, vẫn map
          theo tên (vì cột trống vẫn có thể là cột thật của shop, chỉ
          là kỳ đó không có dữ liệu).
    """
    if _pd is None:
        raise ImportError("map_columns cần pandas, nhưng pandas chưa có.")
    result = {}
    if df is None or len(df.columns) == 0:
        return result

    # Bước 0: ngữ cảnh bảng, tính từ ĐIỂM TÊN thô (chưa cần mẫu), để
    # biết bảng này có phải bảng chi tiết mặt hàng hay không.
    normalized_cols = {}
    seen_names = set()
    for col in df.columns:
        name = str(col)
        if name in seen_names:
            continue
        seen_names.add(name)
        normalized_cols[name] = normalize_key(name)
    has_product_hint = False
    has_quantity_hint = False
    for name, norm in normalized_cols.items():
        for syn in COLUMN_SYNONYMS.get("product_name", []) + \
                COLUMN_SYNONYMS.get("product_code", []):
            if _name_score(norm, syn) >= 85:
                has_product_hint = True
        for syn in COLUMN_SYNONYMS.get("quantity", []):
            if _name_score(norm, syn) >= 85:
                has_quantity_hint = True
    has_line_context = has_product_hint and has_quantity_hint

    # Bước 1+2: lập danh sách ứng viên (điểm, semantic, cột).
    candidates = []
    for name, norm in normalized_cols.items():
        for semantic, synonyms in COLUMN_SYNONYMS.items():
            best = 0
            for syn in synonyms:
                best = max(best, _name_score(norm, syn))
            if best < 75:
                continue
            # Phá hoà cho cột tiền tổng mơ hồ theo ngữ cảnh bảng.
            if semantic == "line_amount" and has_line_context:
                best += 5
            if semantic == "order_total" and not has_line_context:
                best += 5
            candidates.append([best, semantic, name])

    # Bước 3: kiểm chứng mẫu, loại ứng viên yếu, rồi gán tham lam.
    scored = []
    for best, semantic, name in candidates:
        rate = _value_rate(df[name], semantic)
        if rate is not None and best < 90 and rate < 0.25:
            # Tên na ná nhưng giá trị không khớp: không phải cột đó.
            continue
        bonus = 0.0
        if rate is not None:
            bonus = rate * 10.0  # mẫu khớp càng nhiều càng cộng điểm
        scored.append((best + bonus, semantic, name))
    # Điểm cao trước; hoà điểm thì semantic/cột theo thứ tự bảng chữ
    # cái để kết quả ổn định giữa các lần chạy (tái lập).
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    used_columns = set()
    used_semantics = set()
    for _score, semantic, name in scored:
        if semantic in used_semantics or name in used_columns:
            continue
        used_semantics.add(semantic)
        used_columns.add(name)
        result[semantic] = name
    return result


def _apply_table_name_hints(table_name, df, mapping):
    """Bổ sung map cho cột tên generic ('TenGoi') dựa vào tên bảng.

    Vấn đề: một số DB đặt tên cột tên chung là 'TenGoi' cho mọi bảng
    (bảng khách thì đó là tên khách, bảng hàng thì là tên hàng). Tên
    cột một mình không đủ ý nghĩa, nhưng tên bảng thì đủ. Hàm này chỉ
    chạy khi cột 'TenGoi' (sau normalize_key là 'ten goi') chưa được
    map, và tên bảng chứa token gợi ý trong :data:`_TABLE_NAME_HINTS`.

    Vì sao đây không phải hard-code theo shop: luật chỉ dựa vào từ
    chung của tên bảng ('khach', 'san pham', 'cung cap'...), áp dụng
    cho mọi DB nào dùng quy ước tương tự, không nhắc tới shop, ngành
    hay bảng cụ thể nào của train.
    """
    # So sánh dạng bỏ khoảng trắng, vì cột SQLite viết dính liền
    # 'TenGoi' -> normalize_key ra 'tengoi', không phải 'ten goi'.
    if "tengoi" not in [_nospace(normalize_key(c)) for c in df.columns]:
        return mapping
    generic_col = None
    for col in df.columns:
        if _nospace(normalize_key(col)) == "tengoi":
            generic_col = col
            break
    if generic_col is None or generic_col in mapping.values():
        return mapping
    table_tokens = set(_tokens(normalize_key(table_name)))
    for hint_tokens, semantic in _TABLE_NAME_HINTS:
        if semantic in mapping:
            continue
        if set(hint_tokens) <= table_tokens:
            mapping[semantic] = generic_col
            break
    return mapping


# ---------------------------------------------------------------------------
# Đọc file: CSV / XLSX / SQLite
# ---------------------------------------------------------------------------


def _year_from_name(name):
    """Trích năm (20xx) đầu tiên trong tên sheet/file, hoặc None.

    Ví dụ 'T01_2025' -> 2025, 'Thang 02 2025' -> 2025, '04.2025' -> 2025,
    'Sheet1' -> None. Chỉ dùng để hỗ trợ ngày không năm; nếu tên không
    chứa năm thì trả None và ngày không năm sẽ không parse được (đúng
    luật: không đoán năm).
    """
    if not name:
        return None
    m = _re.search(r"(20\d{2})", str(name))
    return int(m.group(1)) if m else None


def _dedupe_columns(columns):
    """Đánh số tên cột trùng nhau để pandas không bị lẫn cột.

    Ví dụ ['Gia', 'Gia'] -> ['Gia', 'Gia_2']. Tên gốc của lần đầu được
    giữ nguyên để map_columns() vẫn nhận ra; lần sau thêm hậu tố số.
    Cột header trống/None thành 'col_<chỉ số>' để không bị mất cột.
    """
    seen = {}
    result = []
    for idx, col in enumerate(columns):
        if col is None or (isinstance(col, str) and not col.strip()):
            base = f"col_{idx}"
        else:
            base = str(col).strip()
        if base not in seen:
            seen[base] = 1
            result.append(base)
        else:
            seen[base] += 1
            result.append(f"{base}_{seen[base]}")
    return result


def _df_from_rows(header, data_rows):
    """Dựng DataFrame từ dòng header + các dòng dữ liệu, bỏ dòng trống.

    - Dòng dữ liệu ngắn hơn header thì đệm None, dài hơn thì cắt bớt
      (file bẩn hay lệch số ô ở dòng cuối).
    - Dòng mà mọi ô đều trống thì bỏ, vì đó là dòng trống phân cách
      trong file, không phải bản ghi.
    - Không ép kiểu cột ở đây: giữ giá trị gốc (số, chuỗi, ngày) để
      tầng sau parse theo ngữ cảnh cột, tránh ép kiểu sớm làm mất
      dạng text như '55.000đ' hay serial ngày.
    """
    if _pd is None:
        raise ImportError("Đọc file cần pandas, nhưng pandas chưa có.")
    columns = _dedupe_columns(header)
    width = len(columns)
    cleaned = []
    for row in data_rows:
        cells = list(row[:width])
        if len(cells) < width:
            cells.extend([None] * (width - len(cells)))
        if all(_is_empty_value(c) for c in cells):
            continue
        cleaned.append(cells)
    if not cleaned:
        return _pd.DataFrame(columns=columns)
    return _pd.DataFrame(cleaned, columns=columns)


def _trim_trailing_empty_columns(rows):
    """Cắt các cột toàn trống ở cuối mỗi sheet (do định dạng Excel).

    Sheet Excel hay có hàng nghìn cột trống do người dùng từng chạm
    vào ô xa; giữ chúng sẽ làm header detection và DataFrame phình to
    vô ích. Chỉ cắt cột mà MỌI dòng đều trống, cột có dữ liệu ở bất kỳ
    dòng nào cũng giữ.
    """
    if not rows:
        return rows
    # Quét từng dòng từ cuối lên, dừng ngay khi gặp ô có dữ liệu: với
    # sheet bình thường (khoảng 10 cột, ô trống cuối rất ít) cách này
    # nhanh hơn nhiều so với quét toàn bộ ma trận cột x dòng.
    last_used = -1
    for row in rows:
        for col_idx in range(len(row) - 1, -1, -1):
            if not _is_empty_value(row[col_idx]):
                if col_idx > last_used:
                    last_used = col_idx
                break
    if last_used < 0:
        return rows
    return [list(r[: last_used + 1]) for r in rows]


def _read_csv_file(path):
    """Đọc một file CSV thành một bảng, kèm năm suy từ tên file.

    Trả (tên_bảng_thô, DataFrame, sheet_year). Tên bảng thô là tên file
    không đuôi. Năm từ tên file chỉ dùng khi file tách theo năm
    ('..._2025.csv'); nếu không có thì None.
    """
    # Thử encoding theo thứ tự: utf-8-sig (CSV Việt Nam hay có BOM),
    # utf-8, rồi cp1252 (file Excel xuất ra ở Windows cũ). Đọc sai
    # encoding sẽ làm mất dấu tiếng Việt nên phải thử, không đoán một
    # encoding duy nhất.
    text = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if text is None:
        text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if not lines:
        return (path.stem, _pd.DataFrame(), None)
    # Dò dấu tách từ mẫu đầu file; thất bại thì mặc định dấu phẩy.
    try:
        dialect = _csv.Sniffer().sniff("\n".join(lines[:5]),
                                       delimiters=",;\t|")
        delimiter = dialect.delimiter
    except Exception:
        delimiter = ","
    rows = list(_csv.reader(lines, delimiter=delimiter))
    if not rows:
        return (path.stem, _pd.DataFrame(), None)
    header_idx = detect_header_row(rows)
    header = rows[header_idx]
    df = _df_from_rows(header, rows[header_idx + 1:])
    return (path.stem, df, _year_from_name(path.stem))


def _read_xlsx_file(path):
    """Đọc một file XLSX thành danh sách bảng theo sheet.

    Trả list các tuple (tên_bảng_thô, DataFrame, sheet_year), trong đó
    tên bảng thô có dạng 'TenFile:TenSheet' để về sau còn biết sheet
    nào thuộc file nào khi hợp nhất. Sheet trống hoàn toàn hoặc không
    tìm được header có dữ liệu thì trả DataFrame rỗng và tầng trên sẽ
    bỏ qua kèm warning (không crash cả file vì một sheet hỏng).
    """
    import openpyxl

    results = []
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet_name in workbook.sheetnames:
            worksheet = workbook[sheet_name]
            rows = [list(r) for r in worksheet.iter_rows(values_only=True)]
            rows = _trim_trailing_empty_columns(rows)
            if not rows:
                results.append((f"{path.stem}:{sheet_name}",
                                _pd.DataFrame(), None))
                continue
            header_idx = detect_header_row(rows)
            header = rows[header_idx]
            df = _df_from_rows(header, rows[header_idx + 1:])
            year = _year_from_name(sheet_name) or _year_from_name(path.stem)
            results.append((f"{path.stem}:{sheet_name}", df, year))
    finally:
        try:
            workbook.close()
        except Exception:
            pass
    return results


def _read_sqlite_file(path):
    """Đọc một file SQLite thành danh sách bảng theo tên bảng trong DB.

    Trả list các tuple (tên_bảng_thô, DataFrame, None). Mở ở chế độ
    chỉ đọc (mode=ro) để tuyệt đối không sửa dữ liệu gốc. Bảng nào đọc
    lỗi thì tầng trên ghi warning và bỏ bảng đó, không crash cả file.
    Tên bảng thô có dạng 'TenFile:TenBang' để phân biệt với sheet XLSX
    khi hợp nhất (bảng SQLite KHÔNG hợp nhất với nhau, xem _merge_key).
    """
    uri = f"file:{path}?mode=ro"
    connection = _sqlite3.connect(uri, uri=True)
    try:
        tables = [
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ]
        results = []
        for table in tables:
            try:
                df = _pd.read_sql_query(f'SELECT * FROM "{table}"', connection)
            except Exception:
                # Bảng lỗi thì trả DataFrame rỗng kèm tên để tầng trên
                # ghi warning bảng rỗng; không để một bảng hỏng giết
                # cả shop. (Chi tiết lỗi không giữ lại vì không dùng
                # cho logic nghiệp vụ, chỉ cần biết bảng đó bị bỏ.)
                df = _pd.DataFrame()
                results.append((f"{path.stem}:{table}", df, None))
                continue
            results.append((f"{path.stem}:{table}", df, None))
        return results
    finally:
        connection.close()


# Hậu tố tách file: '_1', '_2', '_2025', '_2026'... ở cuối tên file.
# Lặp bỏ nhiều lần để 'SoBanHang_1' và 'SoBanHang_2026' cùng về
# 'SoBanHang'. Chỉ bỏ khi hậu tố là số (1-2 chữ số hoặc năm 4 chữ số),
# để tên như 'DonHang_ChiTiet' không bị cắt nhầm.
_SPLIT_SUFFIX_RE = _re.compile(r"_(\d{4}|\d{1,2})$")


def _merge_base(raw_name, kind):
    """Tính tên gốc để hợp nhất cho một bảng thô.

    - XLSX/CSV: tên file (phần trước ':') đã bỏ hậu tố tách file, vì
      sheet theo tháng và file tách đều thuộc cùng một sổ. Ví dụ
      'SoBanHang_1:Thang 02 2025' và 'SoBanHang_2026:Thang 01 2026'
      cùng về 'SoBanHang'.
    - SQLite: tên bảng trong DB (phần sau ':'), vì mỗi bảng là một
      thực thể khác nhau (đơn, chi tiết, khách...), KHÔNG được gộp
      hai bảng khác nhau chỉ vì cùng file.
    """
    if kind == "sqlite":
        return raw_name.split(":", 1)[1] if ":" in raw_name else raw_name
    base = raw_name.split(":", 1)[0]
    # Bỏ lặp hậu tố số ở cuối tên file.
    while True:
        new_base = _SPLIT_SUFFIX_RE.sub("", base)
        if new_base == base or not new_base:
            break
        base = new_base
    return base


def _column_signature(df):
    """Chữ ký bộ cột của một bảng: tuple tên cột đã normalize, xếp thứ tự.

    Hai bảng chỉ hợp nhất khi chữ ký bằng nhau (cùng bộ cột, không kể
    thứ tự cột trong file). Nhờ vậy một file XLSX chứa cả sheet đơn
    hàng lẫn sheet danh mục sẽ KHÔNG bị gộp nhầm thành một bảng, dù
    cùng tên file gốc.
    """
    return tuple(sorted(normalize_key(c) for c in df.columns
                        if not str(c).startswith("_")))


def _merge_tables(raw_tables):
    """Hợp nhất các bảng thô thành bảng cuối theo (tên gốc, chữ ký cột).

    Đầu vào:
        raw_tables: list tuple (tên_bảng_thô, DataFrame, sheet_year,
            kind) với kind là 'csv'/'xlsx'/'sqlite'.

    Đầu ra:
        Dict tên_bảng_hợp_nhất -> DataFrame. Tên hợp nhất là tên gốc
        nếu nhóm chỉ có một bảng, hoặc tên gốc khi nhóm gộp nhiều bảng;
        nếu hai nhóm khác chữ ký trùng tên gốc thì nhóm sau thêm hậu tố
        ' #2', ' #3'... để khoá dict không bị đè nhau.

    Cách gộp:
        - Nối dòng theo thứ tự đọc (file theo bảng chữ cái, sheet theo
          thứ tự trong workbook, dòng theo thứ tự trong sheet) để
          forward-fill theo nhóm đơn vẫn đúng thứ tự thời gian ghi.
        - Cột phụ '_sheet_year' được giữ theo từng dòng (dòng nào từ
          sheet nào thì mang năm của sheet đó); cột gốc thiếu ở bảng
          này thì thành NaN ở dòng đó (pandas concat tự làm).
        - Bảng rỗng (không dòng, chỉ header hoặc không header) không
          tham gia gộp, tầng trên ghi warning riêng cho nó.
    """
    groups = {}
    order = []
    for raw_name, df, year, kind in raw_tables:
        if df is None or len(df) == 0:
            continue
        work = df.copy()
        if year is not None:
            # Ghi năm theo dòng để ngày không năm vẫn parse được sau
            # khi gộp nhiều tháng/năm. Cột này bắt đầu bằng '_' nên
            # không ảnh hưởng chữ ký cột và map cột.
            work["_sheet_year"] = year
        key = (_merge_base(raw_name, kind), _column_signature(work))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(work)
    merged = {}
    used_names = {}
    for key in order:
        base, _sig = key
        frames = groups[key]
        combined = _pd.concat(frames, ignore_index=True, sort=False)
        name = base
        if name in used_names:
            used_names[name] += 1
            name = f"{base} #{used_names[name]}"
        else:
            used_names[name] = 1
        merged[name] = combined
    return merged


# ---------------------------------------------------------------------------
# Forward-fill, khử trùng, quy đổi đơn vị
# ---------------------------------------------------------------------------


def _forward_fill_and_dedup(df, mapping):
    """Forward-fill ô trống theo nhóm đơn và khử trùng, trả (df, số_dòng_bớt).

    Thứ tự làm (quan trọng):
        Bước 1 — Nếu bảng là bảng dòng (có mặt hàng) thì forward-fill
            chính cột mã đơn trước: dòng sau của cùng một đơn hay để
            trống mã đơn/ngày/khách (ví dụ shop ghi dòng đầu đủ, các
            dòng sau trống). Không có bước này thì các dòng sau sẽ
            không biết mình thuộc đơn nào.
        Bước 2 — Trong từng nhóm mã đơn, forward-fill các cột cấp đơn
            (ngày, khách, trạng thái, hình thức thanh toán...). Cột
            mặt hàng/số lượng/giá dòng KHÔNG fill, vì mỗi dòng là một
            mặt hàng khác nhau; fill chúng sẽ nhân bản sai mặt hàng.
        Bước 3 — Khử trùng: nếu có mã đơn thì bỏ dòng trùng tuyệt đối
            trên toàn bộ cột gốc (mã đơn + mọi ô). Hai dòng giống nhau
            của cùng một đơn là lỗi xuất file, tính một lần; hai dòng
            giống nhau của hai đơn khác nhau thì mã đơn khác nhau nên
            không bị coi là trùng. Bảng không có mã đơn thì KHÔNG khử
            trùng, vì mỗi dòng là một lần ghi độc lập theo rulebook.

    Ô trống ở đây gồm None, NaN và chuỗi toàn khoảng trắng: phải đổi
    chuỗi trống thành NaN trước khi fill, nếu không pandas coi '' là
    giá trị thật và không fill.
    """
    if df is None or len(df) == 0:
        return df, 0
    work = df.copy()
    order_col = mapping.get("order_id")
    has_product = ("product_name" in mapping) or ("product_code" in mapping)

    def _blank_to_none(series):
        """Đổi chuỗi trống thành None trong một cột.

        Dùng apply thay vì replace(regex): cột chứa lẫn số và chuỗi mà
        dùng replace regex có thể đổi kiểu hoặc bỏ sót, còn apply kiểm
        tra từng ô bằng cùng định nghĩa trống của _is_empty_value nên
        nhất quán, và fill sau đó mới nhận ra ô trống để kế thừa.
        """
        return series.apply(
            lambda v: None if _is_empty_value(v) else v)

    # Đổi chuỗi trống thành None ở cột mã đơn và cột cấp đơn để fill được.
    fill_targets = []
    if order_col is not None and order_col in work.columns:
        work[order_col] = _blank_to_none(work[order_col])
        fill_targets.append(order_col)
        if has_product:
            # Bảng dòng: mã đơn trống ở dòng sau kế thừa dòng trước.
            # Bảng không phải dòng (một dòng một đơn) thì KHÔNG fill
            # mã đơn, vì mỗi dòng là một đơn riêng.
            work[order_col] = work[order_col].ffill()
    for semantic in sorted(_ORDER_LEVEL_SEMANTICS):
        col = mapping.get(semantic)
        if col is not None and col in work.columns and col != order_col:
            work[col] = _blank_to_none(work[col])
            fill_targets.append(col)
    # Fill trong từng nhóm mã đơn, giữ nguyên thứ tự dòng gốc.
    # Dùng groupby(...).ffill() vector hoá của pandas thay vì lặp từng
    # nhóm bằng .loc: lặp từng nhóm trên file 50k dòng/mấy chục nghìn
    # đơn tốn hàng phút, còn groupby ffill chỉ tốn mili-giây và cho
    # cùng kết quả. groupby bỏ nhóm NaN theo mặc định, nên dòng chưa
    # có mã đơn (dòng đầu file bị trống thật) sẽ không bị fill bừa.
    fill_cols = [c for c in fill_targets if c != order_col]
    if order_col is not None and order_col in work.columns and fill_cols:
        try:
            work[fill_cols] = work.groupby(
                work[order_col], sort=False)[fill_cols].ffill()
        except Exception:
            # Dự phòng cực hiếm (ví dụ cột có kiểu không fill được):
            # giữ nguyên bảng chưa fill thay vì crash cả shop.
            pass
    before = len(work)
    if order_col is not None and order_col in work.columns:
        # Chỉ khử trùng trên cột gốc (bỏ cột phụ '_...'), vì cột phụ
        # là suy ra, không phải nội dung dòng gốc.
        original_cols = [c for c in work.columns
                         if not str(c).startswith("_")]
        if original_cols:
            work = work.drop_duplicates(subset=original_cols, keep="first")
    work = work.reset_index(drop=True)
    return work, before - len(work)


def _to_float_loose(value):
    """Đổi giá trị về float một cách nới lỏng cho cột quy đổi sẵn.

    Cột quy đổi sẵn ('Quy ra ĐV lẻ', 'SLQuyDoi') thường là số thuần
    hoặc chuỗi số; dùng parse_quantity() để ăn cả dạng '20' lẫn số
    thuần. Trả None nếu không parse được (dòng đó sẽ có '_qty_base'
    trống, không gán 0).
    """
    if _is_empty_value(value):
        return None
    number, _unit = parse_quantity(value)
    return number


def _apply_conversion(tables, mappings, warnings):
    """Quy đổi số lượng về đơn vị cơ bản, thêm cột '_qty_base' khi được.

    Cách tìm nguồn quy đổi của shop (theo thứ tự ưu tiên):
        1. Cột quy đổi sẵn trong chính bảng dòng ('quantity_converted'):
           giá trị đó CHÍNH là số lượng đơn vị cơ bản, dùng thẳng.
        2. Bảng quy đổi riêng: bảng nào vừa có mặt hàng, vừa có hệ số
           quy đổi, vừa có đơn vị nguồn (cột 'from_unit' hoặc 'unit').
           Hệ số tra theo (mặt hàng, đơn vị nguồn). Đơn vị cơ bản của
           mỗi mặt hàng lấy từ cột 'base_unit' (cùng bảng quy đổi hoặc
           bảng danh mục có cột đó): dòng nào bán đúng đơn vị cơ bản
           thì hệ số 1, không cần dòng quy đổi.
        3. Không có nguồn nào: ghi warning cho bảng đó và KHÔNG thêm
           cột '_qty_base' (giữ nguyên số lượng gốc, để tầng tính toán
           biết là chưa quy đổi và tự quyết định, thay vì tưởng đã
           quy đổi mà thực ra hệ số là 1 bừa).

    Khoá mặt hàng khi tra bảng: ưu tiên mã hàng (chính xác nhất), nếu
    bảng dòng và bảng quy đổi đều không có mã thì dùng tên hàng đã
    normalize_key. Đơn vị tra cũng normalize_key; nếu không khớp đúng
    cả chuỗi thì thử khớp từ đầu tiên (ví dụ 'bao 25kg' thử 'bao'),
    vì cách ghi đơn vị lớn hay kèm quy cách ('bao 25kg' so với 'bao').

    Trả về: (tables_mới, số_dòng_quy_đổi_được, số_dòng_thất_bại).
    Số dòng thất bại được cộng vào profile.parse_failures ở load_shop.
    """
    # Bước 1: gom đơn vị cơ bản theo mặt hàng từ mọi bảng có cột đó.
    base_unit_by_product = {}
    conversion = {}  # (product_key, from_unit) -> hệ số
    for table_name, df in tables.items():
        mapping = mappings.get(table_name, {})
        prod_col = mapping.get("product_code") or mapping.get("product_name")
        if prod_col is None or prod_col not in df.columns:
            continue
        base_col = mapping.get("base_unit")
        if base_col is not None and base_col in df.columns:
            for _idx, row in df.iterrows():
                key = _product_key(row[prod_col])
                base = normalize_key(row[base_col])
                if key and base and key not in base_unit_by_product:
                    base_unit_by_product[key] = base
        factor_col = mapping.get("conversion_factor")
        if factor_col is None or factor_col not in df.columns:
            continue
        from_col = mapping.get("from_unit") or mapping.get("unit")
        if from_col is None or from_col not in df.columns:
            continue
        for _idx, row in df.iterrows():
            key = _product_key(row[prod_col])
            from_unit = normalize_key(row[from_col])
            factor = _to_float_loose(row[factor_col])
            if key and from_unit and factor is not None and factor != 0:
                conversion[(key, from_unit)] = factor

    # Tập mặt hàng có mặt trong bảng quy đổi: dùng cho luật rulebook
    # "đơn vị không có dòng quy đổi nào là đơn vị cơ bản, hệ số 1".
    # Mặt hàng hoàn toàn không có trong bảng quy đổi (và không có đơn
    # vị cơ bản ghi trong danh mục) thì đơn vị ghi trên dòng chính là
    # đơn vị cơ bản, hệ số 1. Ngược lại, mặt hàng ĐÃ có trong bảng
    # quy đổi mà đơn vị dòng không khớp đơn vị nào đã biết thì KHÔNG
    # đoán hệ số 1, vì đơn vị cơ bản của nó đã biết và khác đơn vị đó.
    conversion_products = {key for (key, _unit) in conversion}
    converted_ok = 0
    converted_fail = 0
    new_tables = {}
    for table_name, df in tables.items():
        mapping = mappings.get(table_name, {})
        qty_col = mapping.get("quantity")
        if qty_col is None or qty_col not in df.columns:
            new_tables[table_name] = df
            continue
        prod_col = mapping.get("product_code") or mapping.get("product_name")
        if prod_col is None or prod_col not in df.columns:
            new_tables[table_name] = df
            continue
        work = df.copy()
        converted_col = mapping.get("quantity_converted")
        if converted_col is not None and converted_col in work.columns:
            # Nguồn 1: cột quy đổi sẵn, dùng thẳng từng dòng.
            values = []
            for value in work[converted_col]:
                number = _to_float_loose(value)
                values.append(number)
                if number is None and not _is_empty_value(value):
                    converted_fail += 1
                elif number is not None:
                    converted_ok += 1
            work["_qty_base"] = values
            new_tables[table_name] = work
            continue
        if not conversion and not base_unit_by_product:
            warnings.append(
                f"Bảng '{table_name}': không tìm được bảng quy đổi hay "
                "cột quy đổi sẵn nên giữ nguyên số lượng gốc, chưa thêm "
                "'_qty_base'."
            )
            new_tables[table_name] = work
            continue
        # Nguồn 2: tra bảng quy đổi theo (mặt hàng, đơn vị).
        # Lặp bằng zip() trên list giá trị thay vì iterrows(): iterrows
        # tạo Series cho mỗi dòng nên chậm gấp hàng chục lần trên file
        # ~100k dòng, còn zip() chỉ đọc thẳng giá trị cột.
        unit_col = mapping.get("unit")
        qty_list = work[qty_col].tolist()
        prod_list = work[prod_col].tolist()
        if unit_col is not None and unit_col in work.columns:
            unit_list = work[unit_col].tolist()
        else:
            unit_list = [None] * len(work)
        values = []
        table_ok = 0
        table_fail = 0
        for qty_value, prod_value, unit_value in zip(
                qty_list, prod_list, unit_list):
            qty_number, qty_unit_text = parse_quantity(qty_value)
            if qty_number is None:
                values.append(None)
                if not _is_empty_value(qty_value):
                    table_fail += 1
                continue
            # Đơn vị của dòng: ưu tiên đơn vị ghi trong số lượng text
            # ('3 chai' -> 'chai'), nếu không thì lấy cột đơn vị.
            unit = qty_unit_text
            if not unit and unit_value is not None:
                unit = normalize_key(unit_value)
            key = _product_key(prod_value)
            factor = None
            if key and unit:
                factor = conversion.get((key, unit))
                if factor is None and " " in unit:
                    # Thử từ đầu tiên: 'bao 25kg' -> 'bao'.
                    factor = conversion.get((key, unit.split()[0]))
                if factor is None and base_unit_by_product.get(key) == unit:
                    factor = 1.0
                if factor is None and key not in conversion_products \
                        and key not in base_unit_by_product:
                    # Luật rulebook: mặt hàng không có dòng quy đổi
                    # nào thì đơn vị trên dòng là đơn vị cơ bản.
                    factor = 1.0
                if factor is None and not unit:
                    factor = 1.0
            if factor is None:
                # Không tra được hệ số: để trống, không đoán hệ số 1,
                # vì đoán sẽ làm sai tổng quy đổi mà không ai biết.
                values.append(None)
                table_fail += 1
            else:
                values.append(qty_number * factor)
                table_ok += 1
        if table_ok == 0:
            warnings.append(
                f"Bảng '{table_name}': có bảng quy đổi nhưng không tra "
                "được hệ số cho dòng nào nên chưa thêm '_qty_base'."
            )
            new_tables[table_name] = work
        else:
            if table_fail:
                warnings.append(
                    f"Bảng '{table_name}': {table_fail} dòng không tra "
                    "được hệ số quy đổi nên '_qty_base' để trống ở các "
                    "dòng đó (không gán 0)."
                )
            work["_qty_base"] = values
            new_tables[table_name] = work
        converted_ok += table_ok
        converted_fail += table_fail
    return new_tables, converted_ok, converted_fail


def _product_key(value):
    """Khoá mặt hàng để tra bảng quy đổi: mã thì giữ nguyên chuỗi cắt
    khoảng trắng, tên thì normalize_key.

    Vì sao không normalize_key luôn cả mã: mã hàng ('SP0001') không có
    dấu và normalize_key cũng giữ nguyên nó, nhưng tên hàng thì cần
    normalize để 'Panadol Extra' khớp 'panadol extra'. Hàm này phân
    biệt bằng cách: nếu giá trị sau khi cắt khoảng trắng không chứa
    khoảng trắng thì giữ nguyên (coi như mã), ngược lại normalize_key.
    Mã có khoảng trắng hiếm, và khi đó normalize_key vẫn khớp hai đầu
    nếu cả hai đầu cùng dùng hàm này, nên không mất tính nhất quán.
    """
    if _is_empty_value(value):
        return ""
    s = str(value).strip()
    if not s:
        return ""
    if " " not in s and "\t" not in s:
        return s
    return normalize_key(s)


def _count_sample_failures(tables, mappings):
    """Đếm lỗi parse trên mẫu kiểm chứng của các cột đã map.

    Với mỗi bảng và mỗi cột tiền/ngày/số lượng đã map, lấy tối đa 100
    giá trị không trống đầu tiên và đếm số parse thất bại. Con số này
    cộng vào profile.parse_failures cùng với lỗi quy đổi. Nó là tín
    hiệu chất lượng mẫu, không phải đếm toàn bộ, vì đếm toàn bộ trên
    file lớn sẽ làm load_shop chậm đi hàng chục lần mà không đổi quyết
    định nào (tầng quyết định chỉ cần biết tỉ lệ lỗi có cao không).
    """
    failures = 0
    for table_name, df in tables.items():
        mapping = mappings.get(table_name, {})
        for semantic, col in mapping.items():
            if col not in df.columns:
                continue
            if semantic in _MONEY_SEMANTICS:
                parser = lambda v: parse_money(v) is not None  # noqa: E731
            elif semantic in _DATE_SEMANTICS:
                parser = _date_like  # noqa: E731
            elif semantic in _QUANTITY_SEMANTICS:
                parser = lambda v: parse_quantity(v)[0] is not None  # noqa: E731
            else:
                continue
            samples = _sample_values(df[col], limit=100)
            for value in samples:
                if not parser(value):
                    failures += 1
    return failures


# ---------------------------------------------------------------------------
# load_shop / load_all_shops
# ---------------------------------------------------------------------------


def _resolve_shop_dir(business_id, data_dir):
    """Tìm thư mục shop từ (business_id, data_dir), linh hoạt đường dẫn.

    Trường hợp chuẩn theo API_CONTRACT: ``data_dir/<business_id>``.
    Trường hợp linh hoạt (để coordinator gọi sai một mức vẫn chạy):
    nếu ``data_dir`` bản thân đã là thư mục shop (chứa file dữ liệu
    trực tiếp) thì dùng chính nó. Nếu cả hai đều không phải thư mục thì
    trả None để load_shop báo lỗi rõ ràng thay vì crash khó hiểu.
    """
    base = _Path(data_dir)
    candidate = base / business_id
    if candidate.is_dir():
        return candidate
    if base.is_dir() and base.name == business_id:
        return base
    # data_dir trỏ thẳng vào thư mục shop nhưng tên khác business_id:
    # chấp nhận nếu trong đó có file dữ liệu (CSV/XLSX/SQLite).
    if base.is_dir() and any(
        p.suffix.lower() in (".csv", ".xlsx", ".sqlite", ".db")
        for p in base.iterdir() if p.is_file()
    ):
        return base
    return None


def load_shop(business_id, data_dir):
    """Đọc mọi file CSV/XLSX/SQLite của một shop thành bảng + hồ sơ shop.

    Đầu vào:
        business_id: mã shop, cũng là tên thư mục shop (không dùng
            mã này để suy luận gì khác ngoài định danh trong báo cáo).
        data_dir: thư mục cha chứa các thư mục shop (ví dụ
            '.../train/data'), hoặc chính thư mục shop (xem
            :func:`_resolve_shop_dir`).

    Đầu ra:
        :class:`IngestResult` với ``tables`` là các bảng đã hợp nhất,
        làm sạch nhẹ, forward-fill, khử trùng, quy đổi (khi được), và
        ``profile`` mô tả shop có loại dữ liệu nào.

    Cam kết không crash vì file lạ:
        - File có đuôi lạ (.txt, .xls cũ, .pdf...) -> warning, bỏ qua.
        - File dữ liệu đọc lỗi (XLSX hỏng, SQLite không phải DB thật)
          -> warning, bỏ file đó, các file khác vẫn đọc bình thường.
        - Sheet/bảng rỗng -> warning, bỏ sheet/bảng đó.
        - Thư mục shop không tồn tại -> raise FileNotFoundError nói rõ
          shop nào và đường dẫn nào, vì đây là lỗi gọi sai, không phải
          dữ liệu bẩn; load_all_shops() sẽ bắt lỗi này và vẫn giữ shop
          đó trong dict kèm warning.

    Không sửa dữ liệu gốc: mọi file chỉ đọc (SQLite mở mode=ro), mọi
    biến đổi chỉ diễn ra trên DataFrame trong bộ nhớ.
    """
    if _pd is None:
        raise ImportError("load_shop cần pandas, nhưng pandas chưa có.")
    shop_dir = _resolve_shop_dir(business_id, data_dir)
    if shop_dir is None:
        raise FileNotFoundError(
            f"Không tìm thấy thư mục shop '{business_id}' trong "
            f"'{data_dir}'."
        )

    warnings = []
    all_files = sorted(
        p.name for p in shop_dir.iterdir() if p.is_file()
    )
    raw_tables = []  # (tên_thô, df, sheet_year, kind)
    empty_tables = []
    for filename in all_files:
        path = shop_dir / filename
        suffix = path.suffix.lower()
        if suffix == ".csv":
            try:
                raw_name, df, year = _read_csv_file(path)
                if len(df) == 0:
                    empty_tables.append(raw_name)
                else:
                    raw_tables.append((raw_name, df, year, "csv"))
            except Exception as exc:
                warnings.append(
                    f"Bỏ qua file '{filename}': đọc CSV lỗi ({exc})."
                )
        elif suffix == ".xlsx":
            try:
                for raw_name, df, year in _read_xlsx_file(path):
                    if len(df) == 0:
                        empty_tables.append(raw_name)
                    else:
                        raw_tables.append((raw_name, df, year, "xlsx"))
            except Exception as exc:
                warnings.append(
                    f"Bỏ qua file '{filename}': đọc XLSX lỗi ({exc})."
                )
        elif suffix in (".sqlite", ".db"):
            try:
                for raw_name, df, year in _read_sqlite_file(path):
                    if len(df) == 0:
                        empty_tables.append(raw_name)
                    else:
                        raw_tables.append((raw_name, df, year, "sqlite"))
            except Exception as exc:
                warnings.append(
                    f"Bỏ qua file '{filename}': đọc SQLite lỗi ({exc})."
                )
        elif filename.lower() == "readme.md":
            # README chỉ để đọc tay/làm ngữ cảnh, không phải bảng.
            continue
        else:
            warnings.append(
                f"Bỏ qua file '{filename}': đuôi file lạ, ingest chỉ "
                "đọc CSV/XLSX/SQLite."
            )
    for name in empty_tables:
        warnings.append(
            f"Bảng '{name}' rỗng hoặc không có dòng dữ liệu nên bị bỏ qua."
        )

    rows_in = sum(len(df) for _n, df, _y, _k in raw_tables)
    merged = _merge_tables(raw_tables)

    # Map cột cho từng bảng hợp nhất, kèm gợi ý theo tên bảng.
    mappings = {}
    for table_name, df in merged.items():
        try:
            mapping = map_columns(df)
        except Exception as exc:
            warnings.append(
                f"Bảng '{table_name}': map cột lỗi ({exc}), coi như "
                "không map được cột nào."
            )
            mapping = {}
        mapping = _apply_table_name_hints(table_name, df, mapping)
        mappings[table_name] = mapping

    # Forward-fill + khử trùng, đếm dòng bị bớt do trùng.
    deduped = {}
    total_removed = 0
    for table_name, df in merged.items():
        try:
            new_df, removed = _forward_fill_and_dedup(df, mappings[table_name])
        except Exception as exc:
            warnings.append(
                f"Bảng '{table_name}': forward-fill/khử trùng lỗi "
                f"({exc}), giữ nguyên bảng chưa xử lý."
            )
            new_df, removed = df, 0
        deduped[table_name] = new_df
        total_removed += removed
    if total_removed:
        warnings.append(
            f"Đã khử {total_removed} dòng trùng (mã đơn + toàn bộ dòng) "
            "theo luật lỗi xuất file."
        )

    # Quy đổi đơn vị cơ bản (thêm '_qty_base' khi làm được).
    try:
        final_tables, _ok, fail = _apply_conversion(
            deduped, mappings, warnings)
    except Exception as exc:
        warnings.append(
            f"Quy đổi đơn vị lỗi ({exc}), giữ nguyên số lượng gốc."
        )
        final_tables, fail = deduped, 0

    # Đếm lỗi parse trên mẫu (tín hiệu chất lượng, không phải đếm hết).
    try:
        sample_failures = _count_sample_failures(final_tables, mappings)
    except Exception:
        sample_failures = 0
    parse_failures = sample_failures + fail

    rows_out = sum(len(df) for df in final_tables.values())

    # Gộp map cột toàn shop: semantic -> cột thật đầu tiên tìm thấy.
    # Bảng được duyệt theo thứ tự tên bảng để kết quả ổn định.
    columns_mapped = {}
    for semantic in COLUMN_SYNONYMS:
        found = ""
        for table_name in sorted(mappings):
            col = mappings[table_name].get(semantic)
            if col:
                found = col
                break
        columns_mapped[semantic] = found
    columns_unmapped = [s for s, c in columns_mapped.items() if not c]

    # Các cờ hồ sơ shop, suy từ map của mọi bảng (không suy từ tên shop).
    has_product_detail = False
    for table_name, mapping in mappings.items():
        has_product = ("product_name" in mapping) \
            or ("product_code" in mapping)
        has_order_or_qty = ("order_id" in mapping) \
            or ("quantity" in mapping)
        if has_product and has_order_or_qty:
            has_product_detail = True
            break
    has_return_table = False
    for table_name in mappings:
        # Chỉ nhận diện bằng TÊN bảng, không dùng cột trả: bảng chi
        # tiết lô hay bảng dòng bán cũng có cột số lượng trả theo lô,
        # nhưng đó không phải bảng trả hàng riêng, tính True sẽ sai.
        # Tên bảng chứa token 'tra'/'return' ('tra_hang', 'tra hang'),
        # hoặc dạng dính liền kết thúc bằng 'tra' ('PhieuTra'), hoặc
        # chứa 'trahang'/'return' thì tính là bảng trả riêng. Cách
        # kiểm tra này tránh nhận nhầm tên kiểu 'TrangThai' (không
        # kết thúc bằng 'tra', không chứa 'trahang').
        norm_name = normalize_key(table_name)
        tokens = set(_tokens(norm_name))
        nospace = _nospace(norm_name)
        if "tra" in tokens or "return" in tokens \
                or "trahang" in nospace or "return" in nospace \
                or nospace.endswith("tra"):
            has_return_table = True
            break

    profile = ShopProfile(
        business_id=business_id,
        files=all_files,
        tables=sorted(final_tables.keys()),
        rows_in=int(rows_in),
        rows_out=int(rows_out),
        columns_mapped=columns_mapped,
        columns_unmapped=columns_unmapped,
        has_product_detail=has_product_detail,
        has_customer_code=bool(columns_mapped.get("customer_code")),
        has_customer_name=bool(columns_mapped.get("customer_name")),
        has_cost=bool(columns_mapped.get("cost_price")),
        has_lot_expiry=bool(
            columns_mapped.get("lot_code")
            or columns_mapped.get("expiry_date")
        ),
        has_return_table=has_return_table,
        warnings=list(warnings),
        parse_failures=int(parse_failures),
    )
    return IngestResult(
        business_id=business_id,
        tables=final_tables,
        profile=profile,
        warnings=list(warnings),
    )


def load_all_shops(train_data_dir):
    """Load mọi thư mục con 'biz_*' trong thư mục dữ liệu train.

    Đầu vào:
        train_data_dir: thư mục chứa các shop (ví dụ '.../train/data').
            Nếu thư mục này không chứa 'biz_*' trực tiếp mà chứa thư
            mục con 'data' thì tự đi xuống 'data' (để coordinator gọi
            bằng '.../train' cũng chạy được).

    Đầu ra:
        Dict ``business_id -> IngestResult``, xếp theo business_id.

    Cam kết không thiếu shop:
        Shop nào load lỗi (thư mục hỏng, file hỏng toàn bộ...) vẫn có
        mặt trong dict với bảng rỗng, profile rows = 0 và warnings ghi
        rõ lỗi, để báo cáo 30 shop không bị thiếu shop và coordinator
        thấy ngay shop nào cần xem lại. Chỉ khi chính thư mục dữ liệu
        không tồn tại thì raise FileNotFoundError, vì đó là lỗi đường
        dẫn, không phải lỗi từng shop.
    """
    base = _Path(train_data_dir)
    if not base.is_dir():
        raise FileNotFoundError(
            f"Không tìm thấy thư mục dữ liệu '{train_data_dir}'."
        )
    # Linh hoạt một mức: train/ -> train/data/.
    if not any(p.is_dir() and p.name.startswith("biz_")
               for p in base.iterdir()):
        data_sub = base / "data"
        if data_sub.is_dir():
            base = data_sub
    shop_dirs = sorted(
        p for p in base.iterdir()
        if p.is_dir() and p.name.startswith("biz_")
    )
    # Nếu không có thư mục biz_* nào (ví dụ thư mục test giả lập đặt
    # tên khác), vẫn load mọi thư mục con để không bỏ sót shop.
    if not shop_dirs:
        shop_dirs = sorted(p for p in base.iterdir() if p.is_dir())
    results = {}
    for shop_dir in shop_dirs:
        business_id = shop_dir.name
        try:
            results[business_id] = load_shop(business_id, base)
        except Exception as exc:
            # Shop lỗi vẫn phải có mặt, kèm warning rõ ràng.
            profile = ShopProfile(
                business_id=business_id,
                files=sorted(
                    p.name for p in shop_dir.iterdir() if p.is_file()
                ) if shop_dir.is_dir() else [],
                tables=[],
                rows_in=0,
                rows_out=0,
                columns_mapped={s: "" for s in COLUMN_SYNONYMS},
                columns_unmapped=list(COLUMN_SYNONYMS.keys()),
                warnings=[f"load_shop thất bại: {exc}"],
                parse_failures=0,
            )
            results[business_id] = IngestResult(
                business_id=business_id,
                tables={},
                profile=profile,
                warnings=list(profile.warnings),
            )
    return results

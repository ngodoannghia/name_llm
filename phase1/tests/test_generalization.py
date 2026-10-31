# -*- coding: utf-8 -*-
"""Bộ kiểm thử tổng quát hoá — Giai đoạn 1 DataMate (Zalo AI Challenge 2026).

Mục đích của file này
----------------------
Kiểm chứng rằng pipeline ingest / normalizer / rulebook chạy được trên dữ liệu
CHƯA TỪNG THẤY (header dời, định dạng đổi, shop mới, rulebook mới, dữ liệu
thiếu), chứ không chỉ chạy đúng trên 30 shop train. Đây là 5 acceptance test
trong mục 6 của ``analysis/05_generalization_requirements.md`` và mục 6 của
``phase1/API_CONTRACT.md``.

Nguyên tắc viết test (bắt buộc, đọc kỹ trước khi sửa file này)
--------------------------------------------------------------
- CHỈ dùng dữ liệu tổng hợp tự tạo trong ``tempfile`` (hoặc DataFrame tạo
  trực tiếp trong test). Không đọc, không sửa, không phụ thuộc vào kết quả
  của bất kỳ shop train thật nào, để test chạy được ngay cả khi ingest
  chưa hoàn hảo trên train.
- KHÔNG hard-code theo business_id train, tên cột chính xác của train, tên
  file của train hay nội dung rulebook train. Tên cột trong test là các
  từ đồng nghĩa nghiệp vụ thông dụng (ví dụ "Mã đơn hàng", "Tên sản phẩm")
  mà contract yêu cầu ``map_columns`` phải nhận diện theo Ý NGHĨA, không
  theo chuỗi ký tự chính xác của một shop.
- KHÔNG tự viết lại module của agent khác để test "chạy cho bằng được".
  Nếu một hàm trong contract chưa tồn tại lúc chạy (agent khác chưa xong),
  test đó sẽ SKIP với lý do rõ ràng (xem ``_require``), và coordinator
  nhìn vào báo cáo chạy là biết module nào còn thiếu.
- Mọi test đều có assert cụ thể, không chỉ in ra. Một test PASS nghĩa là
  hành vi tổng quát hoá đó đã được chứng minh trên dữ liệu tổng hợp;
  một test FAIL nghĩa là code thật (không phải test) chưa đáp ứng contract;
  một test SKIP nghĩa là hàm contract tương ứng chưa tồn tại.

Cách chạy (từ thư mục ``phase1/``)::

    python3 -m unittest discover -s tests -v

Cấu trúc import linh hoạt
--------------------------
File này thêm thư mục ``phase1/src`` vào ``sys.path`` rồi import trực tiếp
``text_utils`` / ``normalizers`` / ``ingest`` / ``rulebook`` theo đúng
``API_CONTRACT.md`` mục 1-4. Mỗi import được bọc ``try/except ImportError``
riêng, để một module chưa xong không làm hỏng việc thu thập (collection)
của cả file test: các test phụ thuộc module đó sẽ skip, các test còn lại
vẫn chạy bình thường.
"""

import csv
import datetime
import sys
import tempfile
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Thêm thư mục src vào sys.path (linh hoạt, không giả định thư mục làm việc)
# ---------------------------------------------------------------------------
# File này nằm ở phase1/tests/, nên phase1/src là parents[1] / "src".
# Thêm cả "src" tương đối từ thư mục làm việc hiện tại để phòng trường hợp
# test được chạy với cwd khác nhưng vẫn ở trong phase1/.
_PHASE1_DIR = Path(__file__).resolve().parents[1]
_SRC_DIR = _PHASE1_DIR / "src"
for _candidate in (str(_SRC_DIR), "src"):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

# ---------------------------------------------------------------------------
# Import theo đúng API_CONTRACT.md, từng hàm bọc riêng để thiếu hàm nào
# thì chỉ skip test dùng hàm đó (không crash cả file).
# ---------------------------------------------------------------------------
try:
    from text_utils import normalize_key  # noqa: F401  (giữ để kiểm tra contract mục 1)
except ImportError:  # Module của agent khác chưa được tạo xong.
    normalize_key = None

try:
    from normalizers import parse_date, parse_money, parse_quantity
except ImportError:  # Module normalizers chưa xong.
    parse_date = None
    parse_money = None
    parse_quantity = None

try:
    from ingest import detect_header_row, load_shop, map_columns
except ImportError:  # Module ingest chưa xong.
    detect_header_row = None
    load_shop = None
    map_columns = None

try:
    from rulebook import load_rulebook
except ImportError:  # Module rulebook chưa xong.
    load_rulebook = None

try:
    import pandas as pd
except ImportError:  # pandas là thư viện contract cho phép; nếu thiếu thì skip test cần DataFrame.
    pd = None


def _missing_names(*pairs):
    """Trả về tên các hàm còn thiếu trong các cặp (tên, đối tượng).

    Dùng để tạo thông báo skip rõ ràng: coordinator đọc là biết ngay
    module/hàm nào của agent khác chưa xong, thay vì đoán từ lỗi import.
    """
    return [name for name, obj in pairs if obj is None]


class _GeneralizationTestCase(unittest.TestCase):
    """Lớp cơ sở chung: tiện ích skip khi hàm contract chưa tồn tại.

    Vì sao cần skip thay vì fail: các agent làm song song, test được viết
    trước / cùng lúc với module thật. Fail vì "chưa có hàm" sẽ che mất
    fail thật vì "có hàm nhưng sai logic". Skip + ghi rõ lý do giúp
    coordinator phân biệt hai trường hợp này trong báo cáo chạy thử.
    """

    def _require(self, *pairs):
        """Skip test này nếu bất kỳ hàm nào trong pairs còn thiếu.

        Args:
            *pairs: các tuple (tên hàm, đối tượng hàm) cần dùng trong test.
        """
        missing = _missing_names(*pairs)
        if missing:
            self.skipTest(
                "Thiếu hàm contract chưa được triển khai: %s "
                "(agent phụ trách module đó chưa xong, không phải lỗi logic "
                "của test này)" % ", ".join(missing)
            )


# ===========================================================================
# Nhóm 1 — Nhiễu header (acceptance test 2 trong 05_generalization_requirements)
# ===========================================================================
class TestHeaderNoise(_GeneralizationTestCase):
    """Kiểm tra phát hiện header khi header dời xuống và có cột nhiễu.

    Vì sao quan trọng: test set sẽ có header ở dòng 3-4 (sau dòng tiêu đề,
    dòng kỳ báo cáo, dòng trống) và cột nhiễu không liên quan tới tính toán.
    Nếu ingest cứ lấy dòng 0 làm header, hoặc để cột nhiễu làm lệch map cột,
    mọi đáp án của shop đó đều sai dù dữ liệu gốc vẫn đúng.
    Pass nghĩa là: detect_header_row tìm đúng dòng header thật, và
    map_columns vẫn map đúng các cột có ý nghĩa nghiệp vụ, bất kể cột nhiễu.
    """

    # Tên cột tổng hợp dùng chung cho nhóm này. Đây là các từ đồng nghĩa
    # nghiệp vụ thông dụng, KHÔNG phải tên cột riêng của một shop train:
    # "Mã đơn hàng" -> order_id, "Ngày bán hàng" -> order_date,
    # "Tên sản phẩm" -> product_name, "Số lượng" -> quantity,
    # "Đơn giá" -> unit_price. Hai cột cuối là cột nhiễu, không có ý nghĩa
    # nghiệp vụ cho việc tính doanh thu và không được map vào cột nào.
    HEADER = [
        "Mã đơn hàng",
        "Ngày bán hàng",
        "Tên sản phẩm",
        "Số lượng",
        "Đơn giá",
        "Hình ảnh",
        "Vị trí kệ",
    ]
    DATA_ROWS = [
        ["DH001", "05/01/2025", "Cà phê sữa", 2, 55000, "anh1.jpg", "A1"],
        ["DH002", "06/01/2025", "Trà đào", 3, 40000, "anh2.jpg", "A2"],
        ["DH003", "07/01/2025", "Bánh mì", 1, 25000, "anh3.jpg", "B1"],
    ]

    def test_header_noise(self):
        """Header dời xuống dòng 3 (index 3) + cột nhiễu -> vẫn map đúng.

        Kiểm tra cái gì: (1) ``detect_header_row`` trên các dòng thô gồm
        dòng tiêu đề 1 ô, dòng trống, dòng kỳ báo cáo 1 ô rồi mới tới
        header phải trả về 3 (không phải 0); (2) DataFrame dựng từ đúng
        dòng header đó, khi qua ``map_columns``, phải map đúng 5 cột
        ý nghĩa và KHÔNG map 2 cột nhiễu vào bất kỳ semantic nào.
        Pass nghĩa là pipeline không giả định header ở dòng 0 và không
        bị cột nhiễu làm lệch.
        """
        self._require(
            ("detect_header_row", detect_header_row),
            ("map_columns", map_columns),
            ("pandas", pd),
        )
        # Dòng thô mô phỏng file bẩn: tiêu đề, dòng trống, kỳ báo cáo,
        # rồi mới tới header thật ở index 3, sau đó là dữ liệu.
        rows = [
            ["CỬA HÀNG TỔNG HỢP - BÁO CÁO BÁN HÀNG"],
            [],
            ["Kỳ báo cáo: Tháng 01/2025"],
            list(self.HEADER),
            *self.DATA_ROWS,
        ]
        header_idx = detect_header_row(rows)
        # Assert cụ thể: header thật là dòng index 3, không phải dòng 0.
        self.assertEqual(
            header_idx, 3,
            "detect_header_row phải tìm đúng dòng header ở index 3, "
            "không được lấy dòng tiêu đề (index 0) làm header",
        )
        # Dựng DataFrame từ đúng dòng header đã tìm được, rồi map cột.
        df = pd.DataFrame(self.DATA_ROWS, columns=self.HEADER)
        mapping = map_columns(df)
        # Assert cụ thể cho từng cột ý nghĩa (semantic -> tên cột thật).
        self.assertEqual(mapping.get("order_id"), "Mã đơn hàng")
        self.assertEqual(mapping.get("order_date"), "Ngày bán hàng")
        self.assertEqual(mapping.get("product_name"), "Tên sản phẩm")
        self.assertEqual(mapping.get("quantity"), "Số lượng")
        self.assertEqual(mapping.get("unit_price"), "Đơn giá")
        # Assert cụ thể: cột nhiễu không được xuất hiện ở phía giá trị
        # của mapping (không được map nhầm thành cột nghiệp vụ nào).
        mapped_values = set(mapping.values())
        self.assertNotIn("Hình ảnh", mapped_values)
        self.assertNotIn("Vị trí kệ", mapped_values)

    def test_header_row_at_index_4(self):
        """Header dời sâu hơn, xuống dòng 4 (index 4) -> vẫn tìm đúng.

        Kiểm tra cái gì: thêm một dòng ghi chú nữa phía trên header
        (tình huống thường gặp: tiêu đề + kỳ + ghi chú + dòng trống rồi
        mới header). Nếu heuristic chỉ quét đúng 3 dòng đầu thì sẽ fail.
        Pass nghĩa là việc quét header không bị khóa cứng ở một vị trí.
        """
        self._require(("detect_header_row", detect_header_row))
        rows = [
            ["CỬA HÀNG TỔNG HỢP"],
            ["Kỳ báo cáo: Tháng 01/2025"],
            ["Ghi chú: số liệu chưa bao gồm khuyến mãi"],
            [],
            list(self.HEADER),
            *self.DATA_ROWS,
        ]
        self.assertEqual(
            detect_header_row(rows), 4,
            "detect_header_row phải tìm đúng header ở index 4 khi có "
            "thêm dòng ghi chú phía trên",
        )


# ===========================================================================
# Nhóm 2 — Nhiễu định dạng (acceptance test 3)
# ===========================================================================
class TestFormatNoise(_GeneralizationTestCase):
    """Kiểm tra parse không đổi giá trị khi đổi dạng biểu diễn.

    Vì sao quan trọng: cùng một giá trị tiền/ngày/số lượng trong test set
    có thể xuất hiện dưới nhiều dạng (text có "đ"/"k", Excel serial, text
    kèm đơn vị). Nếu parser chỉ nhận một dạng, đáp án sẽ đổi chỉ vì
    định dạng đổi, dù nghiệp vụ không đổi — vi phạm yêu cầu "đáp án
    không được đổi khi đổi định dạng".
    Pass nghĩa là: cùng một giá trị, khác dạng biểu diễn, cho cùng một
    kết quả parse; và giá trị không parse được trả về None, KHÔNG gán 0.
    """

    def test_money_text_vs_number(self):
        """'55.000đ' phải bằng 55000 (số thuần); '25k', '1tr440' đúng quy ước.

        Kiểm tra cái gì: ``parse_money`` theo contract mục 2: "55.000đ"
        = 55.000 đồng, "25k" = 25.000, "1tr440" = 1.440.000.
        Pass nghĩa là tiền text mới không làm đổi giá trị tiền.
        """
        self._require(("parse_money", parse_money))
        # Cặp tương đương quan trọng nhất theo yêu cầu bài toán.
        self.assertEqual(parse_money("55.000đ"), 55000)
        self.assertEqual(parse_money(55000), 55000)
        self.assertEqual(parse_money("55.000đ"), parse_money(55000))
        # Các dạng text khác trong contract, để khóa quy ước k/tr.
        self.assertEqual(parse_money("25k"), 25000)
        self.assertEqual(parse_money("1tr440"), 1440000)

    def test_date_serial_vs_string(self):
        """Ngày Excel serial phải bằng ngày 'DD/MM/YYYY' và 'YYYY-MM-DD'.

        Kiểm tra cái gì: ``parse_date`` theo contract mục 2 dùng gốc
        Excel 1899-12-30 (đã trừ bug 1900). Ngày 15/01/2025 có serial
        (2025-01-15 - 1899-12-30) = 45672. Serial được tính động trong
        test từ mốc contract, không hard-code đáp án từ train.
        Pass nghĩa là đổi ngày sang serial không làm đổi kỳ tính toán.
        """
        self._require(("parse_date", parse_date))
        expected = datetime.date(2025, 1, 15)
        # Serial tính từ mốc contract, để test tự giải thích được.
        serial = (expected - datetime.date(1899, 12, 30)).days
        self.assertEqual(serial, 45672)  # Khóa kiểm tra mốc tính serial.
        self.assertEqual(parse_date(serial), expected)
        self.assertEqual(parse_date("15/01/2025"), expected)
        self.assertEqual(parse_date("2025-01-15"), expected)
        # Ba dạng biểu diễn phải cho cùng một kết quả.
        self.assertEqual(parse_date(serial), parse_date("15/01/2025"))

    def test_date_without_year_needs_sheet_year(self):
        """Ngày thiếu năm: có sheet_year thì parse được, không có thì None.

        Kiểm tra cái gì: contract cấm đoán năm khi thiếu năm và không có
        sheet_year. "1/1" + sheet_year=2025 -> 2025-01-01, nhưng "1/1"
        đứng một mình phải trả None (không tự đoán năm hiện tại).
        Pass nghĩa là parser không bịa năm, không suy ra kỳ sai.
        """
        self._require(("parse_date", parse_date))
        self.assertEqual(
            parse_date("1/1", sheet_year=2025), datetime.date(2025, 1, 1)
        )
        self.assertIsNone(
            parse_date("1/1"),
            "Ngày thiếu năm mà không có sheet_year phải trả None, "
            "không được tự đoán năm",
        )

    def test_quantity_text_vs_number(self):
        """'3 chai' và 3 phải cho cùng số lượng 3.0; đơn vị giữ dạng chuẩn hoá.

        Kiểm tra cái gì: ``parse_quantity`` theo contract mục 2:
        "3 chai" -> (3.0, "chai"), 3 -> (3.0, None), "1 ly" -> (1.0, "ly").
        Pass nghĩa là đổi số lượng sang text kèm đơn vị không làm đổi
        số lượng tính toán (chỉ khác ở thông tin đơn vị đi kèm).
        """
        self._require(("parse_quantity", parse_quantity))
        qty_text, unit_text = parse_quantity("3 chai")
        qty_num, unit_num = parse_quantity(3)
        # Số lượng (phần tử đầu) phải không đổi khi đổi dạng biểu diễn.
        self.assertEqual(qty_text, 3.0)
        self.assertEqual(qty_num, 3.0)
        self.assertEqual(qty_text, qty_num)
        # Đơn vị text được giữ ở dạng normalize_key, số thuần thì không có đơn vị.
        self.assertEqual(unit_text, "chai")
        self.assertIsNone(unit_num)
        self.assertEqual(parse_quantity("1 ly"), (1.0, "ly"))

    def test_unparseable_returns_none_not_zero(self):
        """Giá trị không parse được phải là None (hoặc (None, None)), không phải 0.

        Kiểm tra cái gì: đây là chốt chặn chống "bịa số" của yêu cầu
        tổng quát hoá mục 1: dòng không parse được phải vào diện nghi ngờ,
        không được tự gán 0 rồi tính tiếp (0 sẽ làm lệch tổng, trung bình).
        Pass nghĩa là parser phân biệt được "thiếu/không đọc được" với "bằng 0".
        """
        self._require(
            ("parse_money", parse_money),
            ("parse_date", parse_date),
            ("parse_quantity", parse_quantity),
        )
        self.assertIsNone(parse_money("không phải tiền"))
        self.assertIsNone(parse_money(""))
        self.assertIsNone(parse_date("không phải ngày"))
        self.assertEqual(parse_quantity("x"), (None, None))


# ===========================================================================
# Tiện ích chung cho các test tạo shop / rulebook giả lập trong tempfile
# ===========================================================================
def _write_csv(path, header, rows):
    """Ghi một file CSV UTF-8 dùng chung cho các shop giả lập.

    Args:
        path: đường dẫn file cần ghi (thư mục cha phải tồn tại).
        header: danh sách tên cột.
        rows: danh sách các dòng dữ liệu.
    """
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def _write_calendar(path):
    """Ghi calendar.csv tối thiểu, dùng đúng header của calendar thật.

    Chỉ cần đủ để ``load_rulebook`` nhận được calendar_path hợp lệ; nội
    dung lịch âm không phải đối tượng kiểm tra của nhóm rulebook này
    (tuần/tháng/doanh thu), nên chỉ ghi vài dòng mẫu, không copy dữ liệu
    lịch thật của train.
    """
    _write_csv(
        path,
        ["date", "weekday", "lunar_day", "lunar_month", "lunar_year",
         "lunar_leap", "holiday"],
        [
            ["2025-01-01", "4", "2", "12", "2024", "false", ""],
            ["2025-01-02", "5", "3", "12", "2024", "false", ""],
        ],
    )


def _make_shop_dir(tmp_root, business_id, filename, header, rows):
    """Tạo thư mục shop giả lập ``tmp_root/<business_id>/`` với 1 file CSV.

    Trả về thư mục gốc ``tmp_root`` để gọi ``load_shop(business_id, tmp_root)``
    đúng chữ ký contract (load_shop tự tìm thư mục con theo business_id).
    """
    shop_dir = Path(tmp_root) / business_id
    shop_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(shop_dir / filename, header, rows)
    return tmp_root


# ===========================================================================
# Nhóm 3 — Shop giả lập ngành mới (acceptance test 4)
# ===========================================================================
class TestSyntheticNewShop(_GeneralizationTestCase):
    """Kiểm tra load_shop chạy được trên shop hoàn toàn mới, chưa thấy.

    Vì sao quan trọng: test set có shop mới với tên file, tên cột khác
    train nhưng cùng logic nghiệp vụ (có mã đơn, ngày, mặt hàng, số lượng).
    Nếu ingest chỉ nhận đúng tên file/cột của train thì shop mới sẽ crash
    hoặc mất dữ liệu. Pass nghĩa là: không crash, đọc được dữ liệu, và
    hồ sơ shop (profile) nhận diện đúng là shop CÓ chi tiết mặt hàng.
    """

    # business_id tổng hợp, cố tình không trùng bất kỳ biz_101..biz_130 nào
    # của train, để chứng minh test không phụ thuộc vào shop train thật.
    BUSINESS_ID = "biz_synthetic_moi"

    def test_synthetic_new_shop(self):
        """Shop mới (file + cột khác train) -> load được, has_product_detail=True.

        Kiểm tra cái gì: tạo shop chỉ có 1 file CSV tên lạ
        "so_ghi_ban_hang_tong_hop.csv" với các cột đồng nghĩa nghiệp vụ
        ("Mã giao dịch", "Ngày giao dịch", "Tên hàng", "Số lượng bán",
        "Giá bán"), rồi gọi ``load_shop``. Assert: không crash (tới được
        assert đã là bằng chứng), business_id đúng, có ít nhất 1 bảng
        với đủ 3 dòng dữ liệu, và profile.has_product_detail là True
        (vì shop này thực sự có cột mặt hàng).
        Pass nghĩa là pipeline phát hiện schema lúc chạy, không khóa
        cứng theo file/cột của train.
        """
        self._require(("load_shop", load_shop))
        header = ["Mã giao dịch", "Ngày giao dịch", "Tên hàng",
                  "Số lượng bán", "Giá bán"]
        rows = [
            ["GD001", "05/01/2025", "Cà phê sữa", 2, 55000],
            ["GD002", "06/01/2025", "Trà đào", 3, 40000],
            ["GD003", "07/01/2025", "Bánh mì", 1, 25000],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = _make_shop_dir(
                tmp, self.BUSINESS_ID,
                "so_ghi_ban_hang_tong_hop.csv", header, rows)
            # load_shop không được crash trên shop mới — nếu crash thì
            # test fail ngay tại dòng này, đó chính là tín hiệu cần báo.
            result = load_shop(self.BUSINESS_ID, data_dir)
            self.assertEqual(result.profile.business_id, self.BUSINESS_ID)
            # Có dữ liệu thật được đọc vào, không phải bảng rỗng.
            self.assertTrue(result.tables, "load_shop phải trả về ít nhất 1 bảng")
            total_rows = sum(len(df) for df in result.tables.values())
            self.assertGreaterEqual(
                total_rows, 3,
                "Tổng số dòng đọc được phải ít nhất bằng 3 dòng đã ghi",
            )
            # Shop này CÓ chi tiết mặt hàng nên profile phải đánh dấu True.
            self.assertIs(
                result.profile.has_product_detail, True,
                "Shop có cột mặt hàng thì has_product_detail phải là True",
            )


# ===========================================================================
# Nhóm 4 — Rulebook mới giả lập (acceptance test 5)
# ===========================================================================
class TestNewRulebook(_GeneralizationTestCase):
    """Kiểm tra load_rulebook nạp luật lúc chạy, không dùng luật train cũ.

    Vì sao quan trọng: rulebook của split đang chạy là nguồn luật duy nhất.
    Nếu code hard-code luật train (tuần bắt đầu thứ Hai, tháng cắt tại
    ngày chốt, doanh thu loại đơn hủy) thì khi split mới đổi luật, đáp án
    vẫn giữ luật cũ — sai hệ thống. Pass nghĩa là: luật đổi thì field
    tương ứng đổi theo, và luật bị bỏ khỏi file thì không còn được trả
    về như luật cũ (None, hoặc ghi rõ trong unparsed_notes), không bịa.
    """

    # Rulebook A: giữ các luật quen thuộc nhưng viết bằng câu chữ riêng
    # (không copy nguyên văn rulebook train), để kiểm tra trích xuất theo
    # ý nghĩa chứ không phải khớp chuỗi nguyên văn của train.
    RULEBOOK_A = """# Rulebook giả lập A

Doanh thu chỉ tính đơn không hủy và không trả hàng.
Doanh thu là tổng thành tiền các dòng hàng.
Tuần bắt đầu từ thứ Hai và kết thúc vào Chủ nhật.
Tháng này: từ ngày 1 đến hết hôm nay.
"""

    # Rulebook B: ĐỔI luật tuần (sang Chủ nhật) và luật tháng (sang cả
    # tháng, không cắt tại ngày chốt), đồng thời BỎ HẲN mọi câu về đơn
    # hủy / trả hàng. Lưu ý: trong B không được xuất hiện từ "hủy" hay
    # "trả hàng" ở bất kỳ đâu, nếu không test "luật bị bỏ" sẽ không còn
    # ý nghĩa (extractor có cớ để suy ra luật từ chữ đó).
    RULEBOOK_B = """# Rulebook giả lập B

Doanh thu là tổng thành tiền các dòng hàng.
Tuần bắt đầu từ Chủ nhật và kết thúc vào thứ Bảy.
Tháng này: cả tháng, từ ngày 1 đến hết ngày cuối tháng.
"""

    def _load_pair(self, tmp):
        """Ghi rulebook A/B + calendar mẫu, trả về (rulebook_a, rulebook_b)."""
        tmp_path = Path(tmp)
        calendar_path = tmp_path / "calendar.csv"
        _write_calendar(calendar_path)
        path_a = tmp_path / "rulebook_a.md"
        path_b = tmp_path / "rulebook_b.md"
        path_a.write_text(self.RULEBOOK_A, encoding="utf-8")
        path_b.write_text(self.RULEBOOK_B, encoding="utf-8")
        return (
            load_rulebook(str(path_a), str(calendar_path)),
            load_rulebook(str(path_b), str(calendar_path)),
        )

    def test_week_and_month_rules_change_with_rulebook(self):
        """Đổi câu chữ tuần/tháng trong rulebook -> field tương ứng phải đổi.

        Kiểm tra cái gì: nạp cả A (tuần thứ Hai, tháng cắt tại hôm nay)
        và B (tuần Chủ nhật, tháng cả tháng). Ít nhất một trong hai
        field ``week_starts_monday`` / ``month_to_date_cut_at_as_of``
        phải khác nhau giữa A và B; đồng thời B không được tiếp tục
        khẳng định luật cũ (tuần thứ Hai, tháng cắt tại ngày chốt).
        Pass nghĩa là luật mới được nạp lúc chạy và thắng luật cũ.
        """
        self._require(("load_rulebook", load_rulebook))
        with tempfile.TemporaryDirectory() as tmp:
            rb_a, rb_b = self._load_pair(tmp)
            # Điểm đổi phải được phản ánh ở ít nhất một field thời gian.
            changed = (
                rb_a.week_starts_monday != rb_b.week_starts_monday
                or rb_a.month_to_date_cut_at_as_of
                != rb_b.month_to_date_cut_at_as_of
            )
            self.assertTrue(
                changed,
                "Đổi luật tuần/tháng trong rulebook mà không field nào "
                "đổi theo thì load_rulebook đang dùng luật hard-code, "
                "không phải luật trong file (A: week=%r, month_to_date=%r; "
                "B: week=%r, month_to_date=%r)"
                % (rb_a.week_starts_monday, rb_a.month_to_date_cut_at_as_of,
                   rb_b.week_starts_monday, rb_b.month_to_date_cut_at_as_of),
            )
            # B không được giữ luật cũ: tuần B không bắt đầu thứ Hai,
            # tháng B không cắt tại ngày chốt.
            self.assertIsNot(
                rb_b.week_starts_monday, True,
                "Rulebook B nói tuần bắt đầu Chủ nhật, không được trả "
                "week_starts_monday=True như luật cũ",
            )
            self.assertIsNot(
                rb_b.month_to_date_cut_at_as_of, True,
                "Rulebook B nói tháng tính cả tháng, không được trả "
                "month_to_date_cut_at_as_of=True như luật cũ",
            )

    def test_removed_revenue_rule_is_not_invented(self):
        """Luật doanh thu bị bỏ khỏi file -> không được trả về như luật cũ.

        Kiểm tra cái gì: A có câu "doanh thu chỉ tính đơn không hủy và
        không trả hàng" nên ``revenue_excludes_cancelled`` /
        ``revenue_excludes_returned`` của A phải là True (chứng minh
        extractor thực sự đọc file, không phải lúc nào cũng trả None);
        B bỏ hẳn luật này nên field tương ứng của B phải là None (hoặc
        được ghi rõ trong ``unparsed_notes``), và tuyệt đối không phải
        True như luật cũ. Pass nghĩa là: có luật thì trích được, không
        có luật thì không bịa.
        """
        self._require(("load_rulebook", load_rulebook))
        with tempfile.TemporaryDirectory() as tmp:
            rb_a, rb_b = self._load_pair(tmp)
            # Chiều dương: A có luật thì phải trích được luật đó.
            self.assertIs(
                rb_a.revenue_excludes_cancelled, True,
                "Rulebook A nói rõ doanh thu không tính đơn hủy thì "
                "revenue_excludes_cancelled phải là True",
            )
            self.assertIs(
                rb_a.revenue_excludes_returned, True,
                "Rulebook A nói rõ doanh thu không tính đơn trả hàng thì "
                "revenue_excludes_returned phải là True",
            )
            # Chiều âm: B không còn luật đó thì không được trả True.
            self.assertIsNot(
                rb_b.revenue_excludes_cancelled, True,
                "Rulebook B đã bỏ luật về đơn hủy, không được trả "
                "revenue_excludes_cancelled=True như luật cũ",
            )
            # Theo contract, luật không tìm thấy bằng chứng thì để None
            # và/hoặc ghi vào unparsed_notes — chấp nhận một trong hai
            # dạng "thừa nhận không biết" này, nhưng không chấp nhận True.
            cancelled_unknown = (
                rb_b.revenue_excludes_cancelled is None
                or any("hủy" in note.lower() or "huy" in note.lower()
                       or "cancel" in note.lower()
                       for note in rb_b.unparsed_notes)
            )
            self.assertTrue(
                cancelled_unknown,
                "Luật đơn hủy bị bỏ phải được thể hiện là None hoặc "
                "ghi trong unparsed_notes, không được âm thầm giữ True "
                "hay suy ra giá trị khác (nhận được: %r, unparsed_notes=%r)"
                % (rb_b.revenue_excludes_cancelled, rb_b.unparsed_notes),
            )


# ===========================================================================
# Nhóm 5 — Thiếu dữ liệu (acceptance test 6)
# ===========================================================================
class TestMissingData(_GeneralizationTestCase):
    """Kiểm tra shop thiếu dữ liệu: không crash, profile False, không bịa số.

    Vì sao quan trọng: có shop thật không ghi chi tiết mặt hàng (ví dụ
    chỉ ghi tổng đơn) hoặc không ghi giá vốn. Nếu ingest crash, hoặc
    đánh dấu has_product_detail/has_cost=True một cách bịa đặt, thì tầng
    trả lời sẽ tính ra con số từ dữ liệu không tồn tại thay vì abstain /
    unanswerable — đúng dạng sai bị trừ điểm nặng nhất trong PA.
    Pass nghĩa là: load được, field tương ứng là False, và cột ý nghĩa
    tương ứng không được map (không có số bịa để tính).
    """

    def test_shop_without_product_detail(self):
        """Shop chỉ có tổng đơn, không có cột mặt hàng -> has_product_detail=False.

        Kiểm tra cái gì: shop giả lập chỉ có "Mã giao dịch", "Ngày giao
        dịch", "Tổng tiền đơn" (không có bất kỳ cột tên/mã mặt hàng nào).
        Assert: load không crash, has_product_detail là False, và
        ``columns_mapped`` không map được product_name ('' hoặc vắng mặt).
        Pass nghĩa là shop thiếu mặt hàng được nhận diện đúng là thiếu,
        để tầng trên abstain các câu về mặt hàng thay vì bịa.
        """
        self._require(("load_shop", load_shop))
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = _make_shop_dir(
                tmp, "biz_synthetic_thieu_hang",
                "tong_don.csv",
                ["Mã giao dịch", "Ngày giao dịch", "Tổng tiền đơn"],
                [
                    ["GD001", "05/01/2025", 110000],
                    ["GD002", "06/01/2025", 120000],
                ],
            )
            result = load_shop("biz_synthetic_thieu_hang", data_dir)
            self.assertIs(
                result.profile.has_product_detail, False,
                "Shop không có cột mặt hàng thì has_product_detail "
                "phải là False, không được đoán là True",
            )
            mapped_product = result.profile.columns_mapped.get(
                "product_name", "")
            self.assertEqual(
                mapped_product, "",
                "Không có cột mặt hàng thì columns_mapped['product_name'] "
                "phải trống ('' hoặc vắng mặt), không được map bừa sang "
                "cột tổng tiền hay cột khác",
            )

    def test_shop_without_cost(self):
        """Shop có mặt hàng nhưng không có giá vốn -> has_cost=False, vẫn có hàng.

        Kiểm tra cái gì: shop giả lập có đủ mã đơn, ngày, mặt hàng, số
        lượng, giá bán, nhưng KHÔNG có bất kỳ cột giá vốn / giá nhập nào.
        Assert: load không crash, has_product_detail vẫn True (phần có
        thì nhận đúng là có), has_cost là False, và columns_mapped
        không map được cost_price. Pass nghĩa là profile phân biệt
        được "có" và "không có" theo từng loại thông tin, không đánh
        đồng cả shop là đủ hay thiếu, và không bịa ra cột giá vốn.
        """
        self._require(("load_shop", load_shop))
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = _make_shop_dir(
                tmp, "biz_synthetic_thieu_gia_von",
                "ban_hang_khong_gia_von.csv",
                ["Mã giao dịch", "Ngày giao dịch", "Tên hàng",
                 "Số lượng bán", "Giá bán"],
                [
                    ["GD001", "05/01/2025", "Cà phê sữa", 2, 55000],
                    ["GD002", "06/01/2025", "Trà đào", 3, 40000],
                ],
            )
            result = load_shop("biz_synthetic_thieu_gia_von", data_dir)
            # Phần có: mặt hàng vẫn phải nhận đúng là có.
            self.assertIs(
                result.profile.has_product_detail, True,
                "Shop có cột mặt hàng thì has_product_detail phải là True",
            )
            # Phần thiếu: giá vốn phải nhận đúng là không có, không bịa số.
            self.assertIs(
                result.profile.has_cost, False,
                "Shop không có cột giá vốn thì has_cost phải là False",
            )
            mapped_cost = result.profile.columns_mapped.get("cost_price", "")
            self.assertEqual(
                mapped_cost, "",
                "Không có cột giá vốn thì columns_mapped['cost_price'] "
                "phải trống, không được map bừa sang giá bán",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)

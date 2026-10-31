# -*- coding: utf-8 -*-
"""View thống nhất cho một shop trong Giai đoạn 2 DataMate.

Mục đích của module này
------------------------
Giai đoạn 1 (``phase1/src/ingest.py``) trả về nhiều bảng rời rạc, mỗi bảng
giữ tên cột gốc của shop đó. Tầng tính toán (``solver.py``) không thể làm
việc trực tiếp với các bảng đó, vì mỗi shop đặt tên cột khác nhau, có shop
tách bảng dòng và bảng đơn, có shop gộp chung một bảng. Module này ghép
các bảng đó thành một :class:`ShopView` duy nhất với các cột chuẩn đã
định trong ``API_CONTRACT`` mục 1, để solver chỉ cần đọc cột chuẩn mà
không cần biết shop gốc dùng tên cột gì.

Vì sao phải ghép theo GIÁ TRỊ chứ không theo tên cột:
    Hai bảng của cùng một shop có thể gọi mã đơn bằng hai tên khác nhau
    (ví dụ bảng dòng gọi là ``Ma don``, bảng đơn gọi là ``Mã chứng từ``).
    Ghép theo tên cột sẽ thất bại, trong khi ghép theo giá trị mã đơn đã
    chuẩn hoá (cắt khoảng trắng, giữ nguyên chuỗi) thì luôn đúng, miễn là
    hai bảng cùng nói về một đơn. Khi bảng đơn có nhiều cột mã, module
    thử từng cột và chọn cột có tỉ lệ khớp cao nhất với mã đơn của bảng
    dòng, thay vì đoán cột đầu tiên.

Các trường hợp biên được xử lý ở đây (xem docstring từng hàm):
    - Shop chỉ có một bảng vừa là dòng vừa là đơn (không cần join).
    - Shop tách bảng dòng và bảng đơn (join theo giá trị mã đơn).
    - Shop không có mặt hàng (bảng đơn tổng): ``lines`` rỗng, chỉ có
      ``orders``; solver phải dựa vào đó mà abstain câu sản phẩm.
    - Bảng trả riêng chỉ phát hiện theo tên ở Giai đoạn 1, nên module
      này KHÔNG dựa vào cờ đó: trả hàng thể hiện qua trạng thái đơn
      (đã chuẩn hoá thành ``returned``) vẫn được xử lý đúng.
    - ``profile.columns_mapped`` là mức shop nên module này gọi lại
      ``map_columns(df)`` cho từng bảng, đúng cảnh báo ở báo cáo
      Giai đoạn 1 mục 7.2.

Tổng quát hoá:
    Không hard-code business_id, tên cột train, tên bảng train hay ngành
    của shop. Mọi nhận diện đều qua ``map_columns`` của Giai đoạn 1, qua
    từ điển ý nghĩa, hoặc qua độ trùng lặp giá trị (overlap) giữa các
    bảng của chính shop đó. Một shop mới với tên cột khác vẫn đi qua
    đúng pipeline này.

Module này chỉ đọc dữ liệu đã ingest, không sửa file gốc, không gọi
mạng, không dùng ngẫu nhiên. Thứ tự các bảng và các dòng được giữ ổn
định (duyệt theo tên bảng, dòng theo thứ tự gốc) để kết quả tái lập.
"""

import sys as _sys
from dataclasses import dataclass, field as _field
from pathlib import Path as _Path

# --- Nạp phase1 bằng đường dẫn tương đối từ file này, không hard-code
# đường dẫn tuyệt đối của máy. phase2/src/shop_view.py -> datamate/ là
# parents[2], phase1/src nằm ngay dưới đó. Cách này đúng cả khi chạy
# từ phase2/ lẫn khi import trực tiếp file trong src/.
_PHASE1_SRC = _Path(__file__).resolve().parents[2] / "phase1" / "src"
if str(_PHASE1_SRC) not in _sys.path:
    _sys.path.insert(0, str(_PHASE1_SRC))

import pandas as _pd  # noqa: E402

from ingest import COLUMN_SYNONYMS, map_columns  # noqa: E402
from normalizers import parse_date, parse_money, parse_quantity  # noqa: E402
from text_utils import normalize_key, normalize_vn_display  # noqa: E402

__all__ = ["ShopView", "build_shop_view", "normalize_status"]


# ---------------------------------------------------------------------------
# Dataclass ShopView
# ---------------------------------------------------------------------------


@dataclass
class ShopView:
    """View thống nhất của một shop, dùng chung cho mọi câu hỏi của shop.

    Các trường theo đúng ``API_CONTRACT`` mục 1:
        business_id: mã shop, chỉ dùng để định danh, không dùng để suy
            luận gì khác.
        profile: ``ShopProfile`` của Giai đoạn 1, giữ nguyên để gate và
            báo cáo còn đọc được hồ sơ shop (cờ has_*, warnings...).
        lines: DataFrame các dòng bán, mỗi dòng một mặt hàng của một đơn.
            Cột chuẩn (thêm tiền tố ``_`` để không đè cột gốc):
            ``_order_id``, ``_date`` (datetime.date hoặc None),
            ``_status`` (chuỗi gốc), ``_status_norm`` (một trong
            ``completed``/``cancelled``/``returned``/``delivering``/
            ``other``), ``_payment``, ``_customer_key``,
            ``_customer_display``, ``_product_key``,
            ``_product_display``, ``_product_group``, ``_qty``,
            ``_qty_base``, ``_unit_price``, ``_line_amount``,
            ``_cost_price``. Ngoài ra còn hai cột phụ phục vụ solver:
            ``_line_unit`` (đơn vị bán ghi trên dòng, giữ dấu) và
            ``_discount`` (chiết khấu dòng nếu map được), ``_channel``
            (kênh bán nếu có). Cột phụ không làm sai logic đọc cột
            chuẩn, vì solver chỉ đọc đúng tên cột nó cần.
            Có thể rỗng nếu shop không có dòng bán (ví dụ shop chỉ có
            hoá đơn tổng, không có mặt hàng).
        orders: DataFrame mỗi đơn một dòng, gộp từ bảng đơn hoặc từ
            lines. Cột chuẩn: ``_order_id``, ``_date``, ``_status``,
            ``_status_norm``, ``_payment``, ``_customer_key``,
            ``_customer_display``, ``_order_total``, ``_amount_paid``,
            ``_amount_due``, cộng cột phụ ``_channel`` nếu có.
            Có thể rỗng nếu shop không có thông tin đơn nào.
        date_min/date_max: ngày đặt sớm nhất/muộn nhất trong dữ liệu
            shop (lấy từ lines và orders, bỏ qua ngày None). Cả hai
            đều None khi shop không có ngày nào parse được.
        products: danh sách dict ``{'key', 'display', 'code', 'group'}``
            của mọi mặt hàng thấy trong lines và bảng master, khử trùng
            theo key. Key là mã nếu có mã, nếu không là tên đã chuẩn
            hoá giữ dấu (``normalize_vn_display``).
        customers: tương tự cho khách hàng, từ lines, orders và bảng
            master khách hàng.

    Các trường bổ sung ngoài contract (đều có giá trị mặc định, nên
    code cũ khởi tạo ShopView với 8 trường contract vẫn chạy được):
        payments: DataFrame các lần thu tiền theo đơn, cột ``_order_id``,
            ``_date``, ``_amount``. Cần cho câu công nợ "tại ngày X":
            tiền trả đến hết ngày X chỉ tính được khi biết từng lần thu
            xảy ra ngày nào, còn số ``amount_paid`` trên bảng đơn chỉ
            là ảnh chụp cuối kỳ. Rỗng khi shop không có bảng thu riêng.
        base_unit_by_product: dict key mặt hàng -> tên đơn vị cơ bản đã
            chuẩn hoá (chỉ từ cột ``base_unit`` tường minh của shop,
            không đoán từ đơn vị bán mặc định).
        conversion_factor: dict (key mặt hàng, đơn vị nguồn đã chuẩn
            hoá) -> hệ số quy đổi sang đơn vị cơ bản, lấy từ bảng quy
            đổi của chính shop đó.
        conversion_products: tập key mặt hàng có ít nhất một dòng quy
            đổi. Solver dùng tập này để phân biệt hai trường hợp:
            mặt hàng ĐÃ có bảng quy đổi mà dòng không tra được hệ số
            (phải abstain, không đoán hệ số 1) với mặt hàng hoàn toàn
            không có dòng quy đổi nào (theo rulebook thì hệ số 1).
    """

    business_id: str
    profile: object
    lines: object
    orders: object
    date_min: object = None
    date_max: object = None
    products: list = _field(default_factory=list)
    customers: list = _field(default_factory=list)
    payments: object = _field(default_factory=lambda: _pd.DataFrame(
        columns=["_order_id", "_date", "_amount"]))
    base_unit_by_product: dict = _field(default_factory=dict)
    conversion_factor: dict = _field(default_factory=dict)
    conversion_products: set = _field(default_factory=set)


# ---------------------------------------------------------------------------
# Hàm tiện ích nhỏ (đều có docstring tiếng Việt, kể cả hàm private)
# ---------------------------------------------------------------------------


def _is_blank(value):
    """Kiểm tra một ô có coi là trống không (None, NaN, chuỗi rỗng).

    Vì sao cần hàm riêng thay vì ``not value``: số 0 là giá trị thật
    (tiền 0, số lượng 0) nên không được coi là trống, trong khi NaN
    của pandas và chuỗi toàn khoảng trắng thì phải coi là trống để
    logic "thiếu thì join từ bảng đơn" và "thiếu thì abstain" hoạt
    động đúng. Hàm này dùng chung cho mọi bước đọc ô trong module.
    """
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    try:
        if _pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _as_str(value):
    """Đổi một ô thành chuỗi đã cắt khoảng trắng, hoặc None nếu trống.

    Mã đơn, mã hàng, mã khách đều được giữ nguyên chuỗi (không ép
    thành số, không bỏ số 0 ở đầu), vì mã là định danh, không phải số
    lượng. Chuỗi rỗng sau khi cắt khoảng trắng thì trả None để bước
    join và bước khử trùng không coi chuỗi rỗng là một mã thật.
    """
    if _is_blank(value):
        return None
    return str(value).strip()


def _as_float(value):
    """Đổi một ô đã là số (hoặc chuỗi số) thành float, hoặc None.

    Dùng cho cột ``_qty_base`` của Giai đoạn 1, vốn đã là số sau quy
    đổi. Giá trị không phải số (chuỗi lạ) thì trả None chứ không gán 0,
    đúng nguyên tắc "không parse được thì None" của normalizers.
    """
    if _is_blank(value):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            import math as _math

            result = float(value)
            return result if _math.isfinite(result) else None
        except (TypeError, ValueError, OverflowError):
            return None
    number, _unit = parse_quantity(value)
    return number


def _sheet_year_of(value):
    """Trích năm từ cột phụ ``_sheet_year`` của một dòng, hoặc None.

    Giai đoạn 1 ghi năm của sheet/file vào cột này để ngày không năm
    (``1/1``) vẫn parse được. Năm có thể là int, float nguyên (do
    pandas) hoặc chuỗi số; các dạng khác trả None để ``parse_date``
    không đoán năm, đúng luật rulebook.
    """
    if _is_blank(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != int(number):
        return None
    year = int(number)
    return year if 1900 <= year <= 2999 else None


def _parse_row_date(value, sheet_year):
    """Parse ngày của một dòng, kèm năm sheet khi ngày thiếu năm.

    Đầu vào là giá trị gốc trong ô (số serial Excel, chuỗi ngày, đối
    tượng datetime...) và năm sheet của dòng đó. Trả datetime.date
    hoặc None. Không bao giờ đoán năm hiện tại khi ngày thiếu năm mà
    không có sheet_year, vì đoán năm sẽ đặt sai kỳ của đơn.
    """
    if _is_blank(value):
        return None
    return parse_date(value, sheet_year=sheet_year)


def _name_score(column_normalized, synonym):
    """Chấm độ khớp tên giữa một cột và một từ khoá đồng nghĩa (0-100).

    Đây là cùng thang điểm với ``_name_score`` trong phase1
    (``ingest.py``), viết lại ở đây vì hàm đó là private của phase1
    và không được export: 100 khi bằng nhau tuyệt đối, 95 khi bằng
    nhau sau khi bỏ khoảng trắng (dạng dính liền kiểu SQLite), 85 khi
    mọi token của từ khoá đều có trong tên cột, 75 khi mọi token của
    tên cột đều có trong từ khoá và tên cột có từ 2 token trở lên.
    Từ khoá một từ chỉ khớp chính xác, để từ chung như ``gia``/``ten``
    không gán bừa cột. Cần hàm này để liệt kê MỌI cột ứng viên cho một
    semantic (ví dụ cả ``Mã barcode`` lẫn ``SKU`` đều ứng viên cho
    ``product_code``), trong khi ``map_columns`` chỉ trả một cột thắng
    cuộc; bước chọn theo độ trùng lặp giá trị ở dưới cần danh sách
    ứng viên đầy đủ này.
    """
    if not column_normalized or not synonym:
        return 0
    if column_normalized == synonym:
        return 100
    col_nospace = column_normalized.replace(" ", "")
    syn_nospace = synonym.replace(" ", "")
    if col_nospace == syn_nospace:
        return 95
    col_tokens = set(column_normalized.split())
    syn_tokens = set(synonym.split())
    if len(syn_tokens) == 1:
        return 0
    if syn_tokens <= col_tokens:
        return 85
    if len(col_tokens) >= 2 and col_tokens <= syn_tokens:
        return 75
    return 0


def _candidate_columns(df, semantic):
    """Liệt kê các cột của bảng có thể là một semantic, theo tên.

    Đầu vào: DataFrame và tên semantic trong ``COLUMN_SYNONYMS``.
    Đầu ra: danh sách tên cột có điểm tên >= 75 với ít nhất một từ
    khoá của semantic đó. Danh sách này dùng để chọn cột theo độ trùng
    lặp giá trị (ví dụ cột mã hàng nào trùng với mã hàng trên bảng
    dòng thì mới là mã hàng thật), thay vì tin tuyệt đối vào cột mà
    ``map_columns`` chọn khi có nhiều ứng viên cùng điểm.
    """
    synonyms = COLUMN_SYNONYMS.get(semantic, [])
    result = []
    for col in df.columns:
        if str(col).startswith("_"):
            continue
        norm = normalize_key(col)
        if any(_name_score(norm, syn) >= 75 for syn in synonyms):
            result.append(col)
    return result


def _overlap_fraction(values, target_set):
    """Tính tỉ lệ giá trị không trống của một cột nằm trong tập đích.

    Đầu vào: danh sách giá trị thô và tập các chuỗi đích (ví dụ tập mã
    đơn của bảng dòng). Đầu ra: tỉ lệ 0..1, hoặc 0.0 khi cột toàn trống.
    Tỉ lệ này là bằng chứng khách quan để chọn cột mã: cột nào trùng
    nhiều nhất với mã đơn thật thì mới là cột mã đơn thật, còn cột mã
    phiếu thu hay mã chứng từ khác sẽ có tỉ lệ gần 0 dù tên na ná.
    """
    if not target_set:
        return 0.0
    total = 0
    hit = 0
    for value in values:
        text = _as_str(value)
        if text is None:
            continue
        total += 1
        if text in target_set:
            hit += 1
    return (hit / total) if total else 0.0


def _pick_best_overlap_column(df, candidates, target_set, fallback):
    """Chọn trong các cột ứng viên cột có độ trùng lặp cao nhất.

    Đầu vào: DataFrame, danh sách cột ứng viên, tập đích và cột dự
    phòng (thường là cột mà ``map_columns`` đã chọn). Đầu ra: cột thắng
    cuộc. Nếu mọi ứng viên đều có độ trùng bằng 0 (ví dụ chưa có tập
    đích để đối chiếu) thì giữ cột dự phòng, vì khi đó không có bằng
    chứng nào đủ mạnh để lật quyết định của ``map_columns``. Hoà độ
    trùng thì ưu tiên cột dự phòng, để kết quả ổn định và không phụ
    thuộc thứ tự cột trong file.
    """
    if not candidates:
        return fallback
    best_col = fallback
    best_score = -1.0
    if fallback is not None and fallback in candidates:
        best_score = _overlap_fraction(
            df[fallback].tolist(), target_set)
        best_col = fallback
    for col in candidates:
        if col == fallback:
            continue
        score = _overlap_fraction(df[col].tolist(), target_set)
        # Chỉ lật cột dự phòng khi trùng rõ ràng cao hơn, không lật
        # vì chênh lệch nhỏ do nhiễu (dùng so sánh > nghiêm ngặt).
        if score > best_score:
            best_score = score
            best_col = col
    return best_col


# Từ khoá trạng thái, viết dạng đã normalize_key (không dấu, thường).
# Thứ tự kiểm tra trong normalize_status() mới là quan trọng, xem đó.
_COMPLETED_MARKERS = (
    "hoan thanh", "thanh cong", "completed", "hoan tat", "da lay",
    "da xong", "da nhan", "da thanh toan", "thanh toan xong",
)


def normalize_status(value, has_status_column):
    """Chuẩn hoá một giá trị trạng thái về nhãn chuẩn của contract.

    Đầu vào:
        value: chuỗi trạng thái gốc (có thể None/rỗng).
        has_status_column: True khi bảng gốc CÓ cột trạng thái (dù
            dòng này có thể trống), False khi shop hoàn toàn không ghi
            trạng thái ở bảng nào.

    Đầu ra: một trong ``completed``/``cancelled``/``returned``/
    ``delivering``/``other``.

    Thứ tự kiểm tra, và vì sao thứ tự này bắt buộc:
        1. Không có cột trạng thái, hoặc giá trị trống -> ``other``.
           Không được đoán completed, vì shop không ghi trạng thái thì
           không có căn cứ loại hay giữ đơn (solver sẽ vẫn tính đơn
           ``other`` vào doanh thu nhưng kèm cảnh báo, theo contract).
        2. Huỷ trước: chứa ``huy`` hoặc ``cancel`` -> ``cancelled``.
        3. Hoàn thành tường minh trước khi xét trả: ``hoan thanh``,
           ``thanh cong``, ``completed``... -> ``completed``. Bước này
           PHẢI đứng trước bước trả, vì ``Hoàn thành`` sau khi bỏ dấu
           là ``hoan thanh`` và chứa token ``hoan``; nếu xét ``hoan``
           -> trả trước thì mọi đơn hoàn thành sẽ bị nhận nhầm thành
           đơn trả, doanh thu sẽ sụp đổ (đây là bẫy thật gặp ở shop
           chỉ có hoá đơn tổng với trạng thái ``Hoàn thành``).
        4. Trả: token ``tra``/``hoan`` đứng riêng, hoặc ``doi tra``,
           ``tra hang``, ``hoan tra``, ``hoan tien``, ``return`` ->
           ``returned``. ``Đổi trả`` (``doi tra``) rơi vào đây.
        5. Giao/chờ: chứa ``giao``, token ``cho``, ``ship``/``deliver``
           (tiếng Anh cho ``shipping``), ``xu ly`` -> ``delivering``.
           ``Đang giao`` và ``shipping`` đều rơi vào đây.
        6. Còn lại mà có trạng thái -> ``completed`` theo contract
           (ví dụ ``Xong``, ``Thành công`` đã bắt ở bước 3, trạng thái
           lạ không thuộc nhóm trên thì coi như đã xong, vì đơn đã
           được ghi nhận mà không nằm trong nhóm huỷ/trả/giao).

    Trường hợp biên: trạng thái là số hay đối tượng lạ thì đổi thành
    chuỗi rồi chuẩn hoá như thường, không crash.
    """
    if not has_status_column:
        return "other"
    if _is_blank(value):
        return "other"
    norm = normalize_key(value)
    if not norm:
        return "other"
    tokens = set(norm.split())
    # Bước 2: huỷ.
    if "huy" in norm or "cancel" in norm:
        return "cancelled"
    # Bước 3: hoàn thành tường minh, trước bước trả (xem docstring đầu).
    if any(marker in norm for marker in _COMPLETED_MARKERS):
        return "completed"
    if norm in ("xong", "done", "complete"):
        return "completed"
    # Bước 4: trả hàng.
    if ("return" in norm or "doi tra" in norm or "tra hang" in norm
            or "tra lai" in norm or "hoan tra" in norm
            or "hoan tien" in norm or "tra" in tokens
            or "hoan" in tokens):
        return "returned"
    # Bước 5: đang giao / chờ / đang xử lý.
    if ("giao" in norm or "ship" in norm or "deliver" in norm
            or "cho" in tokens or "xu ly" in norm):
        return "delivering"
    # Bước 6: còn lại mà có trạng thái thì coi là hoàn thành.
    return "completed"


# Các tên kênh bán theo ý nghĩa (không phải tên cột của một shop nào):
# ``ban qua`` (bán qua store/zalo/phone...), ``kenh``/``kenh ban``,
# ``nguon`` (nguồn đơn), ``channel``. So khớp theo tên đã normalize.
_CHANNEL_SYNONYMS = (
    "ban qua", "kenh ban", "kenh ban hang", "kenh", "nguon don",
    "nguon", "channel",
)


def _detect_channel_column(df, mapping):
    """Tìm cột kênh bán trong một bảng, hoặc None nếu không có.

    Kênh bán không nằm trong từ điển semantic của Giai đoạn 1, nên hàm
    này nhận diện riêng bằng tên theo ý nghĩa: cột nào sau khi
    normalize_key bằng đúng một từ trong ``_CHANNEL_SYNONYMS`` hoặc
    chứa ``kenh``/``ban qua`` thì là cột kênh. Cột ``branch`` (chi
    nhánh) không được tính là kênh, vì chi nhánh và kênh bán là hai
    khái niệm khác nhau. Chỉ xét cột chưa được map thành semantic khác
    (để không cướp cột đã có ý nghĩa rõ ràng), và cột đầu tiên khớp
    theo thứ tự bảng chữ cái của tên cột để kết quả ổn định.
    """
    used = set(mapping.values())
    for col in sorted(df.columns, key=lambda c: str(c)):
        if col in used or str(col).startswith("_"):
            continue
        norm = normalize_key(col)
        if norm in _CHANNEL_SYNONYMS or "kenh" in norm \
                or "ban qua" in norm:
            return col
    return None


def _detect_customer_code_column(df, mapping, master_customer_keys):
    """Tìm cột mã khách trong bảng đơn khi map_columns không map được.

    Vấn đề thật: có bảng đơn ghi mã khách bằng cột tên không nằm trong
    từ điển Giai đoạn 1 (ví dụ ``Số KH`` với giá trị ``KH0114``), trong
    khi bảng master khách hàng thì có cột mã chuẩn. Khi đó cột mã
    khách thật là cột mà phần lớn giá trị của nó trùng với tập mã
    khách của bảng master. Hàm này chỉ chạy khi mapping chưa có
    ``customer_code`` và ``customer_name``, và chỉ chấp nhận cột có độ
    trùng >= 0.5 với tập mã master, để cột mã đơn hay cột số điện
    thoại (trùng thấp) không bị nhận nhầm thành mã khách.
    """
    if "customer_code" in mapping or "customer_name" in mapping:
        return None
    if not master_customer_keys:
        return None
    best_col = None
    best_score = 0.5  # Ngưỡng chấp nhận, xem docstring đầu.
    for col in df.columns:
        if str(col).startswith("_"):
            continue
        score = _overlap_fraction(df[col].tolist(), master_customer_keys)
        if score > best_score:
            best_score = score
            best_col = col
    return best_col


def _detect_order_total_column(df, mapping):
    """Tìm cột tổng đơn trong bảng đơn khi map_columns không map được.

    Vấn đề thật: có bảng đơn ghi tổng đơn bằng tên không nằm trong từ
    điển Giai đoạn 1 (ví dụ ``Tổng bill`` với giá trị là tổng tiền của
    đơn), trong khi ``map_columns`` chỉ nhận các tên đã biết như ``tổng
    tiền``/``tổng cộng``. Nếu bỏ qua cột này, tổng đơn sẽ bị tính lại
    từ tổng tiền dòng và có thể thiếu phần phí/dịch vụ cộng thêm trên
    đơn, làm sai doanh thu cấp đơn và công nợ.

    Cách nhận diện, hẹp để không nhận nhầm tiền dòng:
        - Chỉ xét cột chưa được map thành semantic nào khác.
        - Tên cột sau normalize phải bắt đầu bằng ``tong`` (tổng) và
          chứa thêm một từ chỉ đơn/tiền (``bill``, ``tien``, ``cong``,
          ``don``, ``hoa don``, ``thanh toan``, ``phai tra``,
          ``gia tri``), hoặc đúng bằng ``tong``.
        - Mẫu giá trị đầu bảng (tối đa 200 ô không trống) phải parse
          tiền được từ 80% trở lên.
    Trả về tên cột đầu tiên khớp theo thứ tự bảng chữ cái, hoặc None.
    Hàm này chỉ dùng cho bảng đơn riêng (không phải bảng dòng), vì ở
    bảng dòng cột tổng tương tự thường là tiền dòng, không phải tổng
    đơn, và contract đã quy định tổng đơn ở bảng dòng lấy từ tổng dòng.
    """
    if "order_total" in mapping:
        return None
    used = set(mapping.values())
    money_tokens = ("bill", "tien", "cong", "don", "hoa don",
                    "thanh toan", "phai tra", "gia tri")
    for col in sorted(df.columns, key=lambda c: str(c)):
        if col in used or str(col).startswith("_"):
            continue
        norm = normalize_key(col)
        if norm == "tong":
            ok_name = True
        elif norm.startswith("tong ") and any(
                token in norm for token in money_tokens):
            ok_name = True
        else:
            ok_name = False
        if not ok_name:
            continue
        samples = [v for v in df[col].tolist()[:200]
                   if not _is_blank(v)]
        if samples and sum(
                1 for v in samples if parse_money(v) is not None) \
                / len(samples) >= 0.8:
            return col
    return None


def _empty_lines_frame():
    """Tạo DataFrame lines rỗng nhưng đủ cột chuẩn của contract.

    Cần khung rỗng này khi shop không có dòng bán: solver vẫn đọc được
    các cột chuẩn mà không phải kiểm tra cột tồn tại, và code gọi
    ``len(view.lines)`` vẫn đúng là 0. Các cột phụ cũng có mặt để
    solver không phải phân biệt shop có/không có cột phụ.
    """
    columns = [
        "_order_id", "_date", "_status", "_status_norm", "_payment",
        "_customer_key", "_customer_display", "_product_key",
        "_product_display", "_product_group", "_qty", "_qty_base",
        "_unit_price", "_line_amount", "_cost_price", "_line_unit",
        "_discount", "_channel",
    ]
    return _pd.DataFrame(columns=columns)


def _empty_orders_frame():
    """Tạo DataFrame orders rỗng nhưng đủ cột chuẩn của contract.

    Lý do giống ``_empty_lines_frame``: shop không có thông tin đơn
    nào vẫn có khung orders để solver đọc cột chuẩn an toàn, thay vì
    phải xử lý trường hợp DataFrame thiếu cột ở khắp nơi.
    """
    columns = [
        "_order_id", "_date", "_status", "_status_norm", "_payment",
        "_customer_key", "_customer_display", "_order_total",
        "_amount_paid", "_amount_due", "_channel",
    ]
    return _pd.DataFrame(columns=columns)


def _merge_master_entry(existing, new):
    """Gộp một mục master (sản phẩm/khách) mới vào mục đã có, theo key.

    Quy tắc gộp, để tên hiển thị không bị mã lấn át:
        - Mục chưa có thì lấy mục mới nguyên vẹn.
        - Display: nếu display cũ trống, hoặc display cũ chính là key
          (tức trước đó chỉ biết mã, chưa biết tên), mà display mới
          khác key và không trống, thì thay bằng display mới. Nhờ vậy
          dòng bán chỉ có mã ``SP0046`` vẫn hiển thị được tên đầy đủ
          từ bảng master (ví dụ ``Bạc xỉu (M)``) khi master có tên.
        - Group: chỉ điền khi group cũ trống và group mới có giá trị.
        - Code: chỉ điền khi code cũ trống và code mới có giá trị.
    Trả về dict đã gộp (sửa tại chỗ và trả lại để caller gán tiện).
    """
    if existing is None:
        return dict(new)
    if (not existing.get("display")) or (
            existing.get("display") == existing.get("key")
            and new.get("display") and new.get("display") != new.get("key")):
        if new.get("display"):
            existing["display"] = new["display"]
    if not existing.get("group") and new.get("group"):
        existing["group"] = new["group"]
    if not existing.get("code") and new.get("code"):
        existing["code"] = new["code"]
    return existing


# ---------------------------------------------------------------------------
# build_shop_view
# ---------------------------------------------------------------------------


def build_shop_view(ingest_result, business_id: str) -> ShopView:
    """Ghép các bảng của Giai đoạn 1 thành view thống nhất cho một shop.

    Đầu vào:
        ingest_result: ``IngestResult`` của Giai đoạn 1 (``tables`` là
            dict tên bảng -> DataFrame đã làm sạch nhẹ, ``profile`` là
            hồ sơ shop). Hàm chỉ đọc, không sửa các DataFrame gốc.
        business_id: mã shop, chỉ dùng để ghi vào ``ShopView``.

    Đầu ra:
        :class:`ShopView` với lines/orders đã chuẩn hoá theo contract.

    Các bước thực hiện, theo đúng thứ tự này (thứ tự quan trọng vì
    bước sau dùng kết quả bước trước để đối chiếu giá trị):
        Bước 1 — Map cột cho TỪNG bảng bằng ``map_columns(df)``. Không
            dùng ``profile.columns_mapped`` mức shop, vì cột thật đầu
            tiên tìm thấy ở mức shop có thể không phải cột của bảng
            đang xét khi hai bảng đặt tên khác nhau (cảnh báo ở báo
            cáo Giai đoạn 1 mục 7.2).
        Bước 2 — Chọn bảng dòng: bảng có mặt hàng (tên hoặc mã) VÀ
            (số lượng hoặc tiền dòng). Nhiều bảng dòng thì lấy bảng
            lớn nhất (nhiều dòng nhất, hoà thì tên bảng nhỏ nhất theo
            bảng chữ cái để ổn định). Bảng danh mục hàng không phải
            bảng dòng vì nó không có số lượng/tiền dòng.
        Bước 3 — Chọn bảng đơn: bảng có mã đơn VÀ ngày đặt VÀ (trạng
            thái hoặc tổng đơn). Nếu chính bảng dòng đã đủ ba điều
            kiện này thì nó vừa là bảng dòng vừa là bảng đơn, không
            join. Ngược lại, trong các bảng đơn ứng viên, ưu tiên bảng
            có mã đơn trùng nhiều nhất với mã đơn của bảng dòng (độ
            trùng lặp giá trị), rồi mới xét kích thước bảng.
        Bước 4 — Dựng tập mã khách master và tập mã hàng của bảng dòng
            để tinh chỉnh cột: cột mã khách lạ trong bảng đơn (ví dụ
            ``Số KH``) được nhận diện bằng độ trùng với mã khách
            master; cột mã hàng trong bảng master được chọn lại bằng
            độ trùng với mã hàng trên bảng dòng (ví dụ cột ``SKU``
            thắng cột ``Mã barcode`` khi mã dòng trùng với ``SKU``).
        Bước 5 — Dựng dict đơn từ bảng đơn (nếu có): mỗi mã đơn một
            mục, gộp các dòng trùng mã bằng cách điền các trường còn
            trống từ dòng sau (không ghi đè giá trị đã có, để dòng đầu
            tiên của đơn được ưu tiên). Tổng đơn để trống ở bước này
            sẽ được điền từ tổng tiền dòng ở bước 7.
        Bước 6 — Dựng lines: từng dòng của bảng dòng, điền ngày/trạng
            thái/thanh toán/khách/kênh từ bảng đơn khi dòng thiếu (chỉ
            điền khi dòng trống, không ghi đè giá trị dòng đã có).
            ``_line_amount`` ưu tiên tiền dòng đã parse, nếu không có
            thì bằng ``_qty * _unit_price``. ``_qty_base`` ưu tiên cột
            ``_qty_base`` của Giai đoạn 1 theo từng dòng (kể cả giá trị
            None do không tra được hệ số, để solver phân biệt được
            "thiếu hệ số" với "không cần quy đổi"); chỉ khi bảng dòng
            hoàn toàn không có cột ``_qty_base`` thì mới lấy ``_qty``.
        Bước 7 — Dựng orders: nếu có bảng đơn thì từ dict đơn (điền
            tổng đơn từ tổng tiền dòng khi bảng đơn không có tổng);
            nếu không có bảng đơn nhưng có lines thì nhóm lines theo
            mã đơn; nếu không có cả hai thì orders rỗng. Đơn không có
            mã (dòng lẻ) không tạo thành đơn trong orders, vì không
            xác định được đơn nào.
        Bước 8 — Dựng bảng thu (payments): quét các bảng còn lại, bảng
            nào có cột ngày/giờ thu, có cột tiền, và có một cột mà
            >= 80% giá trị trùng với mã đơn đã biết thì là bảng thu.
            Cột mã đơn của bảng thu được chọn theo độ trùng, không
            theo tên (ví dụ cột ``Số HĐ`` thắng cột ``Mã phiếu thu``
            vì giá trị của nó mới là mã đơn, còn mã phiếu thu là mã
            chứng từ thu riêng). Dòng thu thiếu ngày hoặc thiếu tiền
            thì bỏ, vì không dùng được cho nợ tại ngày X.
        Bước 9 — Dựng products/customers: từ lines, orders và bảng
            master (bảng còn lại mà có mặt hàng hoặc khách hàng nhưng
            không phải bảng dòng/đơn), khử trùng theo key, gộp tên
            hiển thị theo ``_merge_master_entry``. Cuối cùng xếp theo
            key tăng dần để thứ tự ổn định giữa các lần chạy.
        Bước 10 — Tính date_min/date_max từ mọi ngày không None trong
            lines và orders; thu thập thông tin quy đổi (đơn vị cơ bản
            và hệ số) từ mọi bảng có cột quy đổi, để solver quyết định
            hệ số 1 hay abstain khi thiếu hệ số.

    Trường hợp biên:
        - Shop không có bảng nào: lines/orders/payments rỗng,
          date_min/date_max None, products/customers rỗng, không crash.
        - Bảng dòng thiếu mã đơn ở một số dòng: dòng đó vẫn giữ trong
          lines với ``_order_id`` None (không bịa mã), và không join
          được với bảng đơn nên ngày/trạng thái của nó lấy từ chính
          dòng (có thể None).
        - Bảng đơn có mã đơn trùng nhau ở nhiều dòng: gộp thành một
          đơn, không nhân đôi đơn trong orders.
        - Tiền dòng parse thất bại (None) nhưng có số lượng và đơn giá:
          vẫn tính được ``_line_amount`` từ tích, để không mất dòng
          chỉ vì một ô tiền ghi dạng lạ.
    """
    tables = dict(getattr(ingest_result, "tables", {}) or {})
    profile = getattr(ingest_result, "profile", None)

    # Bước 1: map cột cho từng bảng, duyệt theo tên bảng để ổn định.
    mappings = {}
    for table_name in sorted(tables):
        df = tables[table_name]
        try:
            mappings[table_name] = map_columns(df)
        except Exception:
            # Bảng map lỗi thì coi như không map được cột nào, các
            # bảng khác vẫn xử lý bình thường, không crash cả shop.
            mappings[table_name] = {}

    def _has(mapping, *semantics):
        """Kiểm tra mapping có ít nhất một trong các semantic không."""
        return any(sem in mapping for sem in semantics)

    # Bước 2: chọn bảng dòng.
    line_table_name = None
    line_mapping = {}
    for table_name in sorted(tables):
        mapping = mappings[table_name]
        if not _has(mapping, "product_name", "product_code"):
            continue
        if not _has(mapping, "quantity", "line_amount"):
            continue
        if line_table_name is None:
            line_table_name, line_mapping = table_name, mapping
        else:
            # Bảng lớn hơn thì thắng; hoà số dòng thì tên bảng nhỏ
            # hơn thắng (duyệt theo thứ tự tên nên chỉ thay khi lớn
            # hơn hẳn, không thay khi bằng nhau).
            if len(tables[table_name]) > len(tables[line_table_name]):
                line_table_name, line_mapping = table_name, mapping
    line_df = tables.get(line_table_name) if line_table_name else None

    # Bước 3: chọn bảng đơn.
    order_table_name = None
    order_mapping = {}
    line_order_ids = set()
    if line_df is not None and "order_id" in line_mapping:
        order_col = line_mapping["order_id"]
        for value in line_df[order_col].tolist():
            text = _as_str(value)
            if text is not None:
                line_order_ids.add(text)
    if line_table_name is not None:
        # Chính bảng dòng có thể là bảng đơn luôn (đủ ngày + trạng
        # thái/tổng): khi đó không join, tránh tự join với chính mình.
        # Sửa tích hợp Giai đoạn 2: ngày đặt có thể nằm ở cột
        # order_datetime (phase1 map "Thời gian" thành order_datetime,
        # không phải order_date) nên cả hai semantic đều được chấp
        # nhận ở đây và ở vòng chọn bảng đơn dưới; nếu không, các
        # shop chỉ có cột "Thời gian" sẽ mất toàn bộ ngày và mọi câu
        # theo kỳ đều trả 0 (lỗi thật gặp ở 10 shop train).
        if _has(line_mapping, "order_id") \
                and _has(line_mapping, "order_date", "order_datetime") \
                and _has(line_mapping, "order_status", "order_total"):
            order_table_name, order_mapping = line_table_name, line_mapping
    if order_table_name is None:
        best_overlap = -1.0
        for table_name in sorted(tables):
            mapping = mappings[table_name]
            if not _has(mapping, "order_id"):
                continue
            if not _has(mapping, "order_date", "order_datetime"):
                continue
            if not _has(mapping, "order_status", "order_total"):
                continue
            if table_name == line_table_name:
                continue
            overlap = 0.0
            if line_order_ids and "order_id" in mapping:
                overlap = _overlap_fraction(
                    tables[table_name][mapping["order_id"]].tolist(),
                    line_order_ids)
            # Ưu tiên độ trùng trước, rồi kích thước bảng: bảng đơn
            # thật phải nói về cùng các đơn với bảng dòng.
            score = (overlap, len(tables[table_name]))
            if best_overlap < 0 and order_table_name is None:
                order_table_name, order_mapping = table_name, mapping
                best_overlap = overlap
            elif score[0] > best_overlap or (
                    score[0] == best_overlap
                    and len(tables[table_name]) > len(tables[order_table_name])):
                order_table_name, order_mapping = table_name, mapping
                best_overlap = overlap
    order_df = tables.get(order_table_name) if order_table_name else None
    is_single_table = (
        line_table_name is not None
        and line_table_name == order_table_name
    )

    # Bước 4a: tập mã khách master, từ các bảng còn lại có mã khách.
    master_customer_keys = set()
    master_tables = []
    for table_name in sorted(tables):
        # Bảng đã đóng vai trò dòng hoặc đơn thì không tính là master
        # nữa (kể cả trường hợp một bảng vừa là dòng vừa là đơn).
        if table_name in (line_table_name, order_table_name):
            continue
        master_tables.append(table_name)
        mapping = mappings[table_name]
        if "customer_code" in mapping:
            col = mapping["customer_code"]
            for value in tables[table_name][col].tolist():
                text = _as_str(value)
                if text is not None:
                    master_customer_keys.add(text)

    # Bước 4b: tinh chỉnh cột mã khách của bảng đơn và cột kênh bán.
    order_customer_code_col = None
    order_channel_col = None
    if order_df is not None:
        order_customer_code_col = order_mapping.get("customer_code")
        if order_customer_code_col is None:
            order_customer_code_col = _detect_customer_code_column(
                order_df, order_mapping, master_customer_keys)
        order_channel_col = _detect_channel_column(order_df, order_mapping)

    # Bước 4c: tập mã hàng của bảng dòng, để tinh chỉnh cột mã master.
    line_product_codes = set()
    if line_df is not None and "product_code" in line_mapping:
        col = line_mapping["product_code"]
        for value in line_df[col].tolist():
            text = _as_str(value)
            if text is not None:
                line_product_codes.add(text)
    # Với mỗi bảng master, cột mã hàng "đúng" là cột ứng viên trùng
    # nhiều nhất với mã hàng trên bảng dòng (nếu có mã dòng để đối
    # chiếu); nếu không có mã dòng thì giữ cột map_columns đã chọn.
    master_product_code_col = {}
    master_product_name_col = {}
    master_customer_name_col = {}
    master_group_col = {}
    for table_name in master_tables:
        df = tables[table_name]
        mapping = mappings[table_name]
        if "product_code" in mapping or "product_name" in mapping:
            candidates = _candidate_columns(df, "product_code")
            chosen = mapping.get("product_code")
            if line_product_codes and candidates:
                chosen = _pick_best_overlap_column(
                    df, candidates, line_product_codes, chosen)
            master_product_code_col[table_name] = chosen
            master_product_name_col[table_name] = mapping.get("product_name")
            master_group_col[table_name] = mapping.get("product_group")
        if "customer_code" in mapping or "customer_name" in mapping:
            master_customer_name_col[table_name] = mapping.get(
                "customer_name")

    # Bước 5: dựng dict đơn từ bảng đơn (nếu có).
    # Mỗi mục: dict các trường chuẩn, giá trị None khi chưa biết.
    orders_by_id = {}
    order_has_status = False
    if order_df is not None:
        order_has_status = "order_status" in order_mapping
        order_id_col = order_mapping.get("order_id")
        # order_date ưu tiên, order_datetime là dự phòng (xem chú thích
        # ở bước chọn bảng đơn): giá trị datetime được parse_date cắt
        # về phần ngày ở bước dưới, nên dùng chung được.
        date_col = order_mapping.get("order_date") \
            or order_mapping.get("order_datetime")
        status_col = order_mapping.get("order_status")
        payment_col = order_mapping.get("payment_method")
        cust_code_col = order_customer_code_col
        cust_name_col = order_mapping.get("customer_name")
        cust_group_col = order_mapping.get("customer_group")
        # Tổng đơn: ưu tiên cột order_total đã map; nếu bảng đơn riêng
        # (không phải bảng dòng) không map được thì thử cột tổng theo
        # ý nghĩa (ví dụ 'Tổng bill'), xem _detect_order_total_column.
        total_col = order_mapping.get("order_total")
        if total_col is None and not is_single_table:
            total_col = _detect_order_total_column(order_df, order_mapping)
        paid_col = order_mapping.get("amount_paid")
        due_col = order_mapping.get("amount_due")
        # Cột năm sheet theo dòng (nếu Giai đoạn 1 có ghi).
        sheet_year_col = "_sheet_year" if "_sheet_year" in order_df.columns \
            else None
        cols = {
            "order_id": order_id_col, "date": date_col,
            "status": status_col, "payment": payment_col,
            "cust_code": cust_code_col, "cust_name": cust_name_col,
            "total": total_col, "paid": paid_col, "due": due_col,
            "channel": order_channel_col, "sheet_year": sheet_year_col,
        }
        # Lấy danh sách giá trị từng cột một lần (nhanh hơn iterrows
        # rất nhiều trên bảng hàng chục nghìn dòng).
        col_values = {}
        for key, col in cols.items():
            col_values[key] = order_df[col].tolist() \
                if col is not None and col in order_df.columns else None
        n_rows = len(order_df)
        for idx in range(n_rows):
            def _col(key, _idx=idx):
                """Lấy giá trị cột key tại dòng idx, hoặc None."""
                values = col_values[key]
                return values[_idx] if values is not None else None

            order_id = _as_str(_col("order_id"))
            if order_id is None:
                # Dòng đơn không có mã thì không gộp được vào đơn nào;
                # bỏ qua ở orders (lines vẫn giữ dòng đó nếu có).
                continue
            sheet_year = _sheet_year_of(_col("sheet_year"))
            date_value = _parse_row_date(_col("date"), sheet_year)
            status_value = _as_str(_col("status"))
            payment_value = _col("payment")
            payment_value = normalize_vn_display(payment_value) \
                if not _is_blank(payment_value) else None
            cust_code = _as_str(_col("cust_code"))
            cust_name = _as_str(_col("cust_name"))
            # Khách của đơn: mã nếu có, nếu không là tên giữ dấu.
            if cust_code is not None:
                cust_key, cust_display = cust_code, (cust_name or cust_code)
            elif cust_name is not None:
                cust_key = normalize_vn_display(cust_name)
                cust_display = cust_name
            else:
                cust_key, cust_display = None, None
            channel_value = _as_str(_col("channel"))
            entry = {
                "_order_id": order_id,
                "_date": date_value,
                "_status": status_value,
                "_status_norm": None,  # Điền sau khi biết has_status.
                "_payment": payment_value,
                "_customer_key": cust_key,
                "_customer_display": cust_display,
                "_order_total": parse_money(_col("total"))
                if not _is_blank(_col("total")) else None,
                "_amount_paid": parse_money(_col("paid"))
                if not _is_blank(_col("paid")) else None,
                "_amount_due": parse_money(_col("due"))
                if not _is_blank(_col("due")) else None,
                "_channel": channel_value,
            }
            if order_id not in orders_by_id:
                orders_by_id[order_id] = entry
            else:
                # Gộp dòng trùng mã: chỉ điền trường còn trống, không
                # ghi đè giá trị dòng đầu (dòng đầu của đơn thường
                # đầy đủ nhất do forward-fill của Giai đoạn 1).
                old = orders_by_id[order_id]
                for field_name, new_value in entry.items():
                    if field_name in ("_order_id", "_status_norm"):
                        continue
                    if old.get(field_name) is None and new_value is not None:
                        old[field_name] = new_value
        # Chuẩn hoá trạng thái cho mọi đơn sau khi đã gộp.
        for entry in orders_by_id.values():
            entry["_status_norm"] = normalize_status(
                entry["_status"], order_has_status)

    # Bước 6: dựng lines từ bảng dòng, join với dict đơn khi thiếu.
    line_rows = []
    line_has_status = "order_status" in line_mapping
    # Trạng thái của shop được coi là "có cột" khi bảng dòng HOẶC bảng
    # đơn có cột trạng thái; chỉ khi cả hai đều không có thì mới là
    # shop không ghi trạng thái (status_norm = other cho mọi dòng).
    shop_has_status = line_has_status or order_has_status
    if line_df is not None:
        line_order_col = line_mapping.get("order_id")
        line_date_col = line_mapping.get("order_date") \
            or line_mapping.get("order_datetime")
        line_status_col = line_mapping.get("order_status")
        line_payment_col = line_mapping.get("payment_method")
        line_cust_code_col = line_mapping.get("customer_code")
        line_cust_name_col = line_mapping.get("customer_name")
        line_prod_code_col = line_mapping.get("product_code")
        line_prod_name_col = line_mapping.get("product_name")
        line_group_col = line_mapping.get("product_group")
        line_qty_col = line_mapping.get("quantity")
        line_unit_col = line_mapping.get("unit")
        line_price_col = line_mapping.get("unit_price")
        line_amount_col = line_mapping.get("line_amount")
        line_cost_col = line_mapping.get("cost_price")
        line_discount_col = line_mapping.get("discount")
        line_channel_col = _detect_channel_column(line_df, line_mapping)
        has_qty_base_col = "_qty_base" in line_df.columns
        sheet_year_col = "_sheet_year" if "_sheet_year" in line_df.columns \
            else None
        col_map = {
            "order_id": line_order_col, "date": line_date_col,
            "status": line_status_col, "payment": line_payment_col,
            "cust_code": line_cust_code_col, "cust_name": line_cust_name_col,
            "prod_code": line_prod_code_col, "prod_name": line_prod_name_col,
            "group": line_group_col, "qty": line_qty_col,
            "unit": line_unit_col, "price": line_price_col,
            "amount": line_amount_col, "cost": line_cost_col,
            "discount": line_discount_col, "channel": line_channel_col,
            "sheet_year": sheet_year_col,
            "qty_base": "_qty_base" if has_qty_base_col else None,
        }
        col_values = {}
        for key, col in col_map.items():
            col_values[key] = line_df[col].tolist() \
                if col is not None and col in line_df.columns else None
        for idx in range(len(line_df)):
            def _col(key, _idx=idx):
                """Lấy giá trị cột key tại dòng idx, hoặc None."""
                values = col_values[key]
                return values[_idx] if values is not None else None

            order_id = _as_str(_col("order_id"))
            # Bản ghi đơn tương ứng (nếu join được) để điền trường
            # thiếu; dòng không có mã đơn thì không join được.
            order_entry = orders_by_id.get(order_id) \
                if order_id is not None else None
            sheet_year = _sheet_year_of(_col("sheet_year"))
            date_value = _parse_row_date(_col("date"), sheet_year)
            status_value = _as_str(_col("status"))
            payment_value = _col("payment")
            payment_value = normalize_vn_display(payment_value) \
                if not _is_blank(payment_value) else None
            cust_code = _as_str(_col("cust_code"))
            cust_name = _as_str(_col("cust_name"))
            channel_value = _as_str(_col("channel"))
            # Join: chỉ điền khi dòng trống, không ghi đè giá trị dòng.
            if order_entry is not None:
                if date_value is None:
                    date_value = order_entry["_date"]
                if status_value is None:
                    status_value = order_entry["_status"]
                if payment_value is None:
                    payment_value = order_entry["_payment"]
                if cust_code is None and cust_name is None:
                    cust_code = None
                    # Lấy lại mã/tên khách từ đơn: nếu đơn có mã thì
                    # mã đó là mã khách, nếu đơn chỉ có tên thì tên
                    # đó là tên khách (phân biệt bằng cách so sánh
                    # với display/key của đơn).
                    if order_entry["_customer_key"] is not None:
                        if order_entry["_customer_display"] \
                                == order_entry["_customer_key"]:
                            # Đơn chỉ biết một chuỗi duy nhất: không
                            # rõ là mã hay tên, giữ làm key, display
                            # cũng là chuỗi đó (giống cách dựng đơn).
                            cust_code = order_entry["_customer_key"] \
                                if order_entry["_customer_key"] \
                                and " " not in order_entry["_customer_key"] \
                                else None
                            cust_name = order_entry["_customer_display"]
                            if cust_code is None:
                                cust_name = order_entry["_customer_display"]
                        else:
                            cust_code = order_entry["_customer_key"]
                            cust_name = order_entry["_customer_display"]
                if channel_value is None:
                    channel_value = order_entry["_channel"]
            # Khách của dòng: mã nếu có, nếu không là tên giữ dấu.
            if cust_code is not None:
                cust_key, cust_display = cust_code, (cust_name or cust_code)
            elif cust_name is not None:
                cust_key = normalize_vn_display(cust_name)
                cust_display = cust_name
            else:
                cust_key, cust_display = None, None
            # Mặt hàng của dòng: mã nếu có, nếu không là tên giữ dấu.
            prod_code = _as_str(_col("prod_code"))
            prod_name = _as_str(_col("prod_name"))
            if prod_code is not None:
                prod_key, prod_display = prod_code, (prod_name or prod_code)
            elif prod_name is not None:
                prod_key = normalize_vn_display(prod_name)
                prod_display = prod_name
            else:
                prod_key, prod_display = None, None
            group_value = _as_str(_col("group"))
            qty_number, qty_unit_text = parse_quantity(_col("qty")) \
                if not _is_blank(_col("qty")) else (None, None)
            # Đơn vị bán: ưu tiên cột đơn vị (giữ dấu), nếu không có
            # thì lấy đơn vị ghi trong chuỗi số lượng (đã normalize).
            unit_value = _as_str(_col("unit"))
            if unit_value is not None:
                line_unit = normalize_vn_display(unit_value)
            elif qty_unit_text:
                line_unit = qty_unit_text
            else:
                line_unit = None
            unit_price = parse_money(_col("price")) \
                if not _is_blank(_col("price")) else None
            line_amount = parse_money(_col("amount")) \
                if not _is_blank(_col("amount")) else None
            # Tiền dòng: ưu tiên cột tiền dòng đã parse; nếu không có
            # (không có cột, hoặc ô không parse được) thì tính từ
            # số lượng nhân đơn giá, để không mất dòng chỉ vì một ô.
            if line_amount is None and qty_number is not None \
                    and unit_price is not None:
                line_amount = qty_number * unit_price
            # Số lượng quy đổi: ưu tiên cột _qty_base của Giai đoạn 1
            # theo từng dòng, KỂ CẢ khi giá trị là None (dòng không
            # tra được hệ số). Chỉ khi bảng hoàn toàn không có cột
            # này thì mới lấy _qty, vì khi đó shop không có quy đổi
            # và số lượng gốc chính là số lượng dùng được.
            if has_qty_base_col:
                qty_base = _as_float(_col("qty_base"))
            else:
                qty_base = qty_number
            line_rows.append({
                "_order_id": order_id,
                "_date": date_value,
                "_status": status_value,
                "_status_norm": normalize_status(
                    status_value, shop_has_status),
                "_payment": payment_value,
                "_customer_key": cust_key,
                "_customer_display": cust_display,
                "_product_key": prod_key,
                "_product_display": prod_display,
                "_product_group": group_value,
                "_qty": qty_number,
                "_qty_base": qty_base,
                "_unit_price": unit_price,
                "_line_amount": line_amount,
                "_cost_price": parse_money(_col("cost"))
                if not _is_blank(_col("cost")) else None,
                "_line_unit": line_unit,
                "_discount": parse_money(_col("discount"))
                if not _is_blank(_col("discount")) else None,
                "_channel": channel_value,
            })
    lines_df = _pd.DataFrame(line_rows) if line_rows else _empty_lines_frame()
    if line_rows:
        # Đảm bảo đủ cột chuẩn ngay cả khi DataFrame suy cột thiếu
        # (không xảy ra với dict đầy đủ, nhưng giữ cho an toàn).
        for col in _empty_lines_frame().columns:
            if col not in lines_df.columns:
                lines_df[col] = None

    # Bước 7: dựng orders cuối cùng.
    if order_df is not None:
        # Điền tổng đơn từ tổng tiền dòng khi bảng đơn không có tổng:
        # tổng dòng là bằng chứng duy nhất còn lại, và contract cho
        # phép dùng nó thay cho order_total.
        if line_rows:
            line_sum_by_order = {}
            for row in line_rows:
                oid = row["_order_id"]
                if oid is None or row["_line_amount"] is None:
                    continue
                line_sum_by_order[oid] = line_sum_by_order.get(oid, 0.0) \
                    + row["_line_amount"]
            for oid, entry in orders_by_id.items():
                if entry["_order_total"] is None \
                        and oid in line_sum_by_order:
                    entry["_order_total"] = line_sum_by_order[oid]
        order_rows = []
        for oid in sorted(orders_by_id):
            entry = orders_by_id[oid]
            order_rows.append({
                "_order_id": entry["_order_id"],
                "_date": entry["_date"],
                "_status": entry["_status"],
                "_status_norm": entry["_status_norm"],
                "_payment": entry["_payment"],
                "_customer_key": entry["_customer_key"],
                "_customer_display": entry["_customer_display"],
                "_order_total": entry["_order_total"],
                "_amount_paid": entry["_amount_paid"],
                "_amount_due": entry["_amount_due"],
                "_channel": entry["_channel"],
            })
        orders_df = _pd.DataFrame(order_rows) if order_rows \
            else _empty_orders_frame()
    elif line_rows:
        # Không có bảng đơn riêng: nhóm lines theo mã đơn thành orders.
        grouped = {}
        for row in line_rows:
            oid = row["_order_id"]
            if oid is None:
                continue
            if oid not in grouped:
                grouped[oid] = {
                    "_order_id": oid,
                    "_date": row["_date"],
                    "_status": row["_status"],
                    "_status_norm": row["_status_norm"],
                    "_payment": row["_payment"],
                    "_customer_key": row["_customer_key"],
                    "_customer_display": row["_customer_display"],
                    "_order_total": 0.0,
                    "_amount_paid": None,
                    "_amount_due": None,
                    "_channel": row["_channel"],
                }
            else:
                old = grouped[oid]
                # Điền trường còn trống từ dòng sau (giữ dòng đầu).
                for field_name in ("_date", "_status", "_status_norm",
                                   "_payment", "_customer_key",
                                   "_customer_display", "_channel"):
                    if old[field_name] is None \
                            and row[field_name] is not None:
                        old[field_name] = row[field_name]
            if row["_line_amount"] is not None:
                grouped[oid]["_order_total"] += row["_line_amount"]
        # Tập mã đơn có ít nhất một dòng với tiền dòng khác None, tính
        # trước một lần để không phải quét lại toàn bộ lines cho mỗi
        # đơn (tránh O(số đơn x số dòng) trên shop lớn).
        orders_with_amount = {
            row["_order_id"] for row in line_rows
            if row["_order_id"] is not None
            and row["_line_amount"] is not None
        }
        order_rows = []
        for oid in sorted(grouped):
            entry = grouped[oid]
            # Đơn chỉ có dòng mà tiền dòng toàn None thì tổng là None,
            # không phải 0 (0 sẽ làm sai công nợ và doanh thu).
            if entry["_order_total"] == 0.0 \
                    and oid not in orders_with_amount:
                entry["_order_total"] = None
            order_rows.append(entry)
        orders_df = _pd.DataFrame(order_rows) if order_rows \
            else _empty_orders_frame()
    else:
        orders_df = _empty_orders_frame()

    # Bước 8: dựng bảng thu (payments) từ các bảng còn lại.
    known_order_ids = set(orders_by_id)
    if not known_order_ids:
        for row in line_rows:
            if row["_order_id"] is not None:
                known_order_ids.add(row["_order_id"])
    payment_rows = []
    for table_name in master_tables:
        df = tables[table_name]
        mapping = mappings[table_name]
        # Bảng thu phải có ngày/giờ thu và không phải là bảng master
        # thuần (master không có ngày giao dịch theo đơn).
        date_col = mapping.get("order_date") or mapping.get("order_datetime")
        if date_col is None:
            continue
        # Cột tiền: semantic tiền nào cũng được, hoặc cột tên chứa
        # 'tien' mà mẫu parse tiền được đa số (ví dụ cột 'Tiền' đơn
        # thuần không nằm trong từ điển semantic).
        money_col = None
        for semantic in ("amount_paid", "line_amount", "order_total",
                         "return_amount"):
            if semantic in mapping:
                money_col = mapping[semantic]
                break
        if money_col is None:
            for col in df.columns:
                if str(col).startswith("_"):
                    continue
                if "tien" not in normalize_key(col):
                    continue
                samples = [v for v in df[col].tolist()[:200]
                           if not _is_blank(v)]
                if samples and sum(
                        1 for v in samples
                        if parse_money(v) is not None) / len(samples) >= 0.5:
                    money_col = col
                    break
        if money_col is None:
            continue
        # Cột mã đơn của bảng thu: cột trùng nhiều nhất với mã đơn đã
        # biết, ngưỡng 0.8 để không nhận nhầm mã phiếu thu riêng.
        ref_col = None
        ref_score = 0.8
        for col in df.columns:
            if str(col).startswith("_"):
                continue
            score = _overlap_fraction(df[col].tolist(), known_order_ids)
            if score > ref_score:
                ref_score = score
                ref_col = col
        if ref_col is None or not known_order_ids:
            continue
        sheet_year_col = "_sheet_year" if "_sheet_year" in df.columns \
            else None
        ref_values = df[ref_col].tolist()
        date_values = df[date_col].tolist()
        money_values = df[money_col].tolist()
        year_values = df[sheet_year_col].tolist() \
            if sheet_year_col is not None else [None] * len(df)
        for ref_v, date_v, money_v, year_v in zip(
                ref_values, date_values, money_values, year_values):
            oid = _as_str(ref_v)
            amount = parse_money(money_v) \
                if not _is_blank(money_v) else None
            pay_date = _parse_row_date(date_v, _sheet_year_of(year_v))
            # Thiếu mã, thiếu tiền hoặc thiếu ngày thì dòng thu không
            # dùng được cho nợ tại ngày X nên bỏ (không đoán ngày).
            if oid is None or amount is None or pay_date is None:
                continue
            payment_rows.append({
                "_order_id": oid, "_date": pay_date, "_amount": amount,
            })
    payments_df = _pd.DataFrame(
        payment_rows,
        columns=["_order_id", "_date", "_amount"]) if payment_rows \
        else _pd.DataFrame(columns=["_order_id", "_date", "_amount"])

    # Bước 9: products/customers từ lines, orders và bảng master.
    products_by_key = {}
    customers_by_key = {}

    def _add_product(key, display, code, group):
        """Thêm/gộp một mặt hàng vào dict products theo key."""
        if key is None:
            return
        entry = {"key": key, "display": display or key,
                 "code": code, "group": group}
        products_by_key[key] = _merge_master_entry(
            products_by_key.get(key), entry)

    def _add_customer(key, display, code, group):
        """Thêm/gộp một khách hàng vào dict customers theo key."""
        if key is None:
            return
        entry = {"key": key, "display": display or key,
                 "code": code, "group": group}
        customers_by_key[key] = _merge_master_entry(
            customers_by_key.get(key), entry)

    for row in line_rows:
        # Nhóm của dòng: nhóm ghi trên dòng, nếu không có thì tra từ
        # master theo key (bước master dưới đây chạy sau, nên ở đây
        # tạm để None rồi điền lại sau vòng master bằng tra cứu).
        _add_product(row["_product_key"], row["_product_display"],
                     row["_product_key"]
                     if row["_product_key"] and " " not in row["_product_key"]
                     else None,
                     row["_product_group"])
        _add_customer(row["_customer_key"], row["_customer_display"],
                      row["_customer_key"]
                      if row["_customer_key"] and " " not in row["_customer_key"]
                      else None,
                      None)
    # Khách từ orders (đơn có thể có khách mà dòng không có).
    if order_df is not None:
        for entry in orders_by_id.values():
            _add_customer(entry["_customer_key"],
                          entry["_customer_display"],
                          entry["_customer_key"]
                          if entry["_customer_key"]
                          and " " not in entry["_customer_key"] else None,
                          None)
    # Mặt hàng/khách từ bảng master.
    master_group_by_product = {}
    for table_name in master_tables:
        df = tables[table_name]
        code_col = master_product_code_col.get(table_name)
        name_col = master_product_name_col.get(table_name)
        group_col = master_group_col.get(table_name)
        if code_col is not None or name_col is not None:
            code_values = df[code_col].tolist() \
                if code_col is not None else [None] * len(df)
            name_values = df[name_col].tolist() \
                if name_col is not None else [None] * len(df)
            group_values = df[group_col].tolist() \
                if group_col is not None else [None] * len(df)
            for code_v, name_v, group_v in zip(
                    code_values, name_values, group_values):
                code = _as_str(code_v)
                name = _as_str(name_v)
                group = _as_str(group_v)
                if code is not None:
                    key, display = code, (name or code)
                elif name is not None:
                    key = normalize_vn_display(name)
                    display = name
                else:
                    continue
                _add_product(key, display, code, group)
                if group is not None:
                    master_group_by_product[key] = group
        cust_code_col = mappings[table_name].get("customer_code")
        cust_name_col = master_customer_name_col.get(table_name)
        cust_group_col = mappings[table_name].get("customer_group")
        if cust_code_col is not None or cust_name_col is not None:
            code_values = df[cust_code_col].tolist() \
                if cust_code_col is not None else [None] * len(df)
            name_values = df[cust_name_col].tolist() \
                if cust_name_col is not None else [None] * len(df)
            group_values = df[cust_group_col].tolist() \
                if cust_group_col is not None else [None] * len(df)
            for code_v, name_v, group_v in zip(
                    code_values, name_values, group_values):
                code = _as_str(code_v)
                name = _as_str(name_v)
                group = _as_str(group_v)
                if code is not None:
                    key, display = code, (name or code)
                elif name is not None:
                    key = normalize_vn_display(name)
                    display = name
                else:
                    continue
                _add_customer(key, display, code, group)
    # Điền nhóm master cho sản phẩm chỉ thấy trên dòng (chưa có nhóm).
    for key, entry in products_by_key.items():
        if entry.get("group") is None \
                and key in master_group_by_product:
            entry["group"] = master_group_by_product[key]
    # Điền ngược nhóm master vào lines: dòng bán thường không có cột
    # nhóm, nhưng bảng master có nhóm của mặt hàng đó. Không có bước
    # này thì revenue_by_group sẽ thấy toàn nhóm None và abstain oan,
    # dù shop thực sự có thông tin nhóm trong bảng master. Chỉ điền
    # khi dòng chưa có nhóm, không ghi đè nhóm ghi trên dòng.
    if master_group_by_product and len(lines_df) > 0 \
            and "_product_group" in lines_df.columns \
            and "_product_key" in lines_df.columns:
        key_list = lines_df["_product_key"].tolist()
        group_list = lines_df["_product_group"].tolist()
        new_groups = [
            group_list[idx]
            if not _is_blank(group_list[idx])
            else master_group_by_product.get(key_list[idx])
            for idx in range(len(lines_df))]
        lines_df["_product_group"] = new_groups
    products = [products_by_key[key] for key in sorted(products_by_key)]
    customers = [customers_by_key[key] for key in sorted(customers_by_key)]

    # Bước 10: khoảng ngày của shop và thông tin quy đổi.
    all_dates = []
    for row in line_rows:
        if row["_date"] is not None:
            all_dates.append(row["_date"])
    if order_df is not None:
        for entry in orders_by_id.values():
            if entry["_date"] is not None:
                all_dates.append(entry["_date"])
    date_min = min(all_dates) if all_dates else None
    date_max = max(all_dates) if all_dates else None

    base_unit_by_product = {}
    conversion_factor = {}
    conversion_products = set()
    for table_name in sorted(tables):
        df = tables[table_name]
        mapping = mappings[table_name]
        prod_col = mapping.get("product_code") or mapping.get("product_name")
        if prod_col is None or prod_col not in df.columns:
            continue
        base_col = mapping.get("base_unit")
        if base_col is not None and base_col in df.columns:
            for prod_v, base_v in zip(df[prod_col].tolist(),
                                      df[base_col].tolist()):
                key = _as_str(prod_v)
                base = normalize_key(base_v) \
                    if not _is_blank(base_v) else ""
                if key and base and key not in base_unit_by_product:
                    # Key mặt hàng ở đây dùng chuỗi thô (mã) hoặc tên;
                    # để solver đối chiếu được với _product_key của
                    # lines, chuẩn hoá key tên về dạng normalize_key
                    # khi key chứa khoảng trắng (tên), giữ nguyên mã.
                    norm_key = key if " " not in key else normalize_key(key)
                    base_unit_by_product[norm_key] = base
        factor_col = mapping.get("conversion_factor")
        if factor_col is None or factor_col not in df.columns:
            continue
        from_col = mapping.get("from_unit") or mapping.get("unit")
        if from_col is None or from_col not in df.columns:
            continue
        for prod_v, from_v, factor_v in zip(
                df[prod_col].tolist(), df[from_col].tolist(),
                df[factor_col].tolist()):
            key = _as_str(prod_v)
            from_unit = normalize_key(from_v) \
                if not _is_blank(from_v) else ""
            factor = _as_float(factor_v)
            if key and from_unit and factor is not None and factor != 0:
                norm_key = key if " " not in key else normalize_key(key)
                conversion_factor[(norm_key, from_unit)] = factor
                conversion_products.add(norm_key)

    return ShopView(
        business_id=business_id,
        profile=profile,
        lines=lines_df,
        orders=orders_df,
        date_min=date_min,
        date_max=date_max,
        products=products,
        customers=customers,
        payments=payments_df,
        base_unit_by_product=base_unit_by_product,
        conversion_factor=conversion_factor,
        conversion_products=conversion_products,
    )

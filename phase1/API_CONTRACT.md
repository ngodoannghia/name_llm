# Phase 1 — API CONTRACT (bắt buộc cho mọi module)

Mục đích: 4 agent làm song song nhưng code phải chạy chung được. Mọi module phải tuân thủ
đúng tên file, tên hàm, kiểu trả về dưới đây. Nếu cần thêm hàm, được phép thêm, nhưng
KHÔNG được đổi chữ ký của các hàm đã liệt kê.

Ngôn ngữ comment/docstring: TIẾNG VIỆT cho mọi module, class, hàm public.
Không sửa dữ liệu train. Không gọi mạng/API. Chỉ dùng pandas, openpyxl, thư viện chuẩn Python.

## Thư mục

```
phase1/
  src/
    __init__.py
    text_utils.py      # Agent Ingest
    normalizers.py     # Agent Ingest
    ingest.py          # Agent Ingest
    rulebook.py        # Agent Rulebook
    evaluate.py        # Agent Evaluation
  tests/
    test_generalization.py  # Agent Tests (unittest, stdlib, chạy bằng: python3 -m unittest discover -s tests -v)
  run_phase1.py        # Coordinator viết sau khi tích hợp
  reports/
    PHASE1_REPORT.md   # Coordinator viết sau khi chạy
```

## 1. text_utils.py (Agent Ingest)

```python
def normalize_key(text) -> str:
    """Chuẩn hoá để so sánh tên cột/khái niệm: NFC, thường hoá, BỎ dấu tiếng Việt,
    thay ký tự không phải chữ/số thành khoảng trắng, gộp khoảng trắng.
    Ví dụ: 'Tên sản phẩm' -> 'ten san pham'. Trả '' nếu đầu vào rỗng/None."""

def normalize_vn_display(text) -> str:
    """Chuẩn hoá nhẹ để hiển thị/so sánh giữ dấu: NFC, thường hoá, gộp khoảng trắng.
    Dùng khi cần giữ dấu tiếng Việt (ví dụ tên sản phẩm trong đáp án)."""

def strip_diacritics(text) -> str: ...
```

## 2. normalizers.py (Agent Ingest)

```python
def parse_money(value) -> float | None:
    """Parse tiền: số, '55.000đ', '25k', '1tr440', '16,56tr', '25,000.0000',
    '1.234.567', '1,234,567', '12.5'... Trả None nếu không parse được (KHÔNG gán 0).
    Quy ước: 'k' = 1_000, 'tr'/'triệu'/'m' = 1_000_000. '1tr440' = 1_440_000."""

def parse_date(value, sheet_year: int | None = None) -> datetime.date | None:
    """Parse ngày: Excel serial (gốc 1899-12-30, trừ bug 1900), 'DD/MM/YYYY',
    'YYYY-MM-DD', '1 thg 1 2025', '1/1' (cần sheet_year), '01/02/2025'.
    Trả None nếu không parse được. Không đoán ngày khi thiếu năm và không có sheet_year."""

def parse_quantity(value) -> tuple[float | None, str | None]:
    """Parse số lượng: '1 ly' -> (1.0, 'ly'), '3 chai' -> (3.0, 'chai'),
    '2 thùng' -> (2.0, 'thung'), 5 -> (5.0, None), 'x' -> (None, None).
    Đơn vị trả về ở dạng normalize_key."""
```

## 3. ingest.py (Agent Ingest)

Dùng dataclasses, KHÔNG dùng tên cột thật của train làm khóa duy nhất.

```python
@dataclass
class ShopProfile:
    business_id: str
    files: list[str]          # tên file tương đối trong thư mục shop
    tables: list[str]         # tên bảng đã hợp nhất, ví dụ 'sheet:TenSheet' hoặc tên file
    rows_in: int
    rows_out: int
    columns_mapped: dict[str, str]    # semantic -> tên cột thật (hoặc '' nếu không map được)
    columns_unmapped: list[str]       # semantic chưa map được
    has_product_detail: bool
    has_customer_code: bool
    has_customer_name: bool
    has_cost: bool
    has_lot_expiry: bool
    has_return_table: bool
    warnings: list[str]
    parse_failures: int

@dataclass
class IngestResult:
    business_id: str
    tables: dict[str, pandas.DataFrame]  # tên bảng -> DataFrame đã chuẩn hoá (cột gốc giữ nguyên tên, thêm cột phụ nếu cần)
    profile: ShopProfile
    warnings: list[str]

COLUMN_SYNONYMS: dict[str, list[str]]
# Khóa semantic (dùng normalize_key để so sánh), ví dụ:
# 'order_id', 'order_date', 'customer_code', 'customer_name', 'product_code',
# 'product_name', 'quantity', 'unit', 'unit_price', 'line_amount', 'order_status',
# 'payment_method', 'supplier_name', 'lot_expiry', 'cost_price'
# Giá trị là danh sách từ khóa đã normalize_key (tiếng Việt không dấu + tiếng Anh thông dụng).
# Đây là từ điển Ý NGHĨA có thể mở rộng, không phải tên cột chính xác của một shop.

def detect_header_row(rows: list[list]) -> int:
    """Quét N dòng đầu (rows là các dòng thô), trả về chỉ số dòng header thật.
    Heuristic: dòng có nhiều ô chứa chữ, ít ô trống, không phải tiêu đề/kỳ báo cáo.
    Không giả định dòng 0."""

def map_columns(df: pandas.DataFrame) -> dict[str, str]:
    """Map cột theo ý nghĩa: chuẩn hoá tên cột bằng normalize_key, so với
    COLUMN_SYNONYMS, rồi kiểm chứng bằng mẫu giá trị (cột tiền phải parse_money
    được đa số mẫu, cột ngày phải parse_date được đa số mẫu). Trả dict
    semantic -> tên cột thật; semantic không map được thì không có trong dict."""

def load_shop(business_id: str, data_dir: str | Path) -> IngestResult:
    """Đọc mọi file CSV/XLSX/SQLite trong data_dir/<business_id>, hợp nhất
    sheet theo tháng và file tách (_1/_2, _2025/_2026) theo tên bảng gốc,
    forward-fill ô trống theo nhóm đơn (khi có cột order_id), khử trùng theo
    (order_id + toàn bộ dòng) nếu có order_id, quy đổi số lượng về đơn vị cơ bản
    nếu tìm được bảng quy đổi hoặc cột quy đổi sẵn (thêm cột '_qty_base' nếu làm được,
    nếu không thì ghi warning và giữ nguyên). KHÔNG crash khi gặp file lạ:
    ghi warning và bỏ qua file đó."""

def load_all_shops(train_data_dir: str | Path) -> dict[str, IngestResult]:
    """Load mọi thư mục con biz_* trong train_data_dir, trả dict business_id -> IngestResult.
    Shop nào lỗi vẫn phải có mặt trong dict với warnings (hoặc raise có ghi rõ shop nào
    nếu lỗi nghiêm trọng), để báo cáo 30 shop không bị thiếu shop."""
```

Ghi chú: `tables` giữ DataFrame gốc đã làm sạch nhẹ (bỏ dòng hoàn toàn trống).
Việc chuẩn hoá tiền/ngày/số lượng thành cột mới là việc của code dùng ingest, không bắt
buộc ingest phải ghi đè cột gốc — nhưng ingest phải cung cấp đủ thông tin để làm việc đó
(map cột + hàm parse).

## 4. rulebook.py (Agent Rulebook)

```python
@dataclass
class Rulebook:
    source_path: str
    calendar_path: str
    raw_text: str
    rules: dict[str, str]      # khóa luật -> trích dẫn/văn bản luật (nguyên văn, rút gọn)
    revenue_excludes_cancelled: bool | None
    revenue_excludes_returned: bool | None
    week_starts_monday: bool | None
    month_to_date_cut_at_as_of: bool | None
    tet_rule_text: str | None
    unparsed_notes: list[str]  # luật nào không trích chắc chắn được thì ghi rõ, KHÔNG bịa

@dataclass
class Calendar:
    rows: list[dict]  # mỗi dòng calendar.csv, khóa là tên cột gốc

def load_calendar(calendar_path: str | Path) -> Calendar: ...
def load_rulebook(rulebook_path: str | Path, calendar_path: str | Path) -> Rulebook:
    """Nạp rulebook lúc chạy từ file. Dùng regex/từ khóa trên văn bản đã chuẩn hoá
    để trích luật, KHÔNG hard-code rulebook train. Luật nào không tìm thấy bằng
    chứng trong file thì để None và ghi vào unparsed_notes."""

def tet_range(lunar_year: int, calendar: Calendar) -> tuple[datetime.date, datetime.date] | None:
    """Từ calendar.csv: ngày dương của 23 tháng Chạp năm âm (lunar_year-1) đến
    hết ngày dương của mùng 10 tháng Giêng năm âm lunar_year. Trả None nếu không
    tìm đủ hai mốc trong calendar."""

def month_range(year: int, month: int) -> tuple[datetime.date, datetime.date]: ...
def week_range(any_date: datetime.date) -> tuple[datetime.date, datetime.date]:
    """Tuần bắt đầu thứ Hai, kết thúc Chủ nhật."""

def month_to_date_range(as_of: datetime.date) -> tuple[datetime.date, datetime.date]:
    """Từ ngày 1 của tháng chứa as_of đến hết as_of."""

def previous_month_same_days_range(as_of: datetime.date) -> tuple[datetime.date, datetime.date]:
    """Tháng trước, từ ngày 1 đến ngày trùng số với as_of; nếu tháng trước không
    có ngày đó thì đến hết tháng trước."""

def last_n_days_range(n: int, as_of: datetime.date) -> tuple[datetime.date, datetime.date]:
    """N ngày liền nhau kể cả hôm nay, ngày cuối là hôm nay."""

def snapshot_up_to(day: datetime.date) -> datetime.date:
    """Ảnh chụp đến hết ngày: trả chính day (để code dùng rõ nghĩa, không cắt kỳ)."""
```

## 5. evaluate.py (Agent Evaluation)

```python
HOLDOUT_BUSINESSES: frozenset = frozenset({'biz_124', 'biz_123', 'biz_106', 'biz_105', 'biz_117'})
# Đây là cấu hình chia validation do nhiệm vụ Giai đoạn 1 quy định, KHÔNG phải logic
# trả lời câu hỏi ăn theo business_id. Không dùng HOLDOUT trong ingest/rulebook.

@dataclass
class ScoreReport:
    total: int
    correct: int
    wrong: int
    abstain: int
    pa: float
    per_type: dict[str, dict]       # type -> {'total','correct','wrong','abstain','pa'}
    per_business: dict[str, dict]   # business_id -> như trên
    holdout_pa: float | None

def load_answers(answers_path: str | Path) -> dict[str, dict]: ...
def load_submission(submission_path: str | Path) -> dict[str, dict]: ...
def normalize_string_answer(value) -> str:
    """Chuẩn hoá đáp án string theo luật chấm: NFC, thường hoá, ký tự không phải
    chữ/số/khoảng trắng thành khoảng trắng, gộp khoảng trắng. Giữ dấu tiếng Việt."""

def answers_match(reference: dict, submission: dict) -> str:
    """Trả 'correct' | 'wrong' | 'abstain'. Quy tắc: submission thiếu hoặc
    type == 'abstain' -> abstain; type khác type tham chiếu -> wrong;
    unanswerable đúng khi và chỉ khi tham chiếu cũng unanswerable;
    number khớp tuyệt đối, percent làm tròn 2 chữ số, string theo normalize_string_answer
    và alias (nếu reference có alias), date khớp tuyệt đối, boolean khớp tuyệt đối,
    list khớp từng vị trí, table khớp cột theo tên chuẩn hoá rồi theo vị trí và dòng theo tập hợp."""

def score_submission(submission: dict[str, dict], answers: dict[str, dict],
                     questions: dict[str, dict] | None = None) -> ScoreReport:
    """Chấm toàn bộ. Nếu có questions thì per_business lấy business_id từ questions,
    nếu không thì business_id = 'unknown'."""

def make_perfect_submission(answers: dict[str, dict]) -> dict[str, dict]: ...
def make_abstain_submission(answers: dict[str, dict]) -> dict[str, dict]: ...
def make_broken_submission(answers: dict[str, dict]) -> dict[str, dict]:
    """Ít nhất một đáp án sai format: ví dụ number trả bằng chuỗi có dấu phẩy,
    hoặc percent trả 0.124 thay vì 12.4 — phải bị tính wrong khi chấm."""
```

## 6. tests/test_generalization.py (Agent Tests)

Dùng `unittest` (stdlib). Mỗi test phải có tên rõ ràng và assert pass/fail cụ thể,
không chỉ in ra. 5 nhóm bắt buộc:

1. `test_header_noise`: đổi tên cột thành đồng nghĩa chưa thấy trong COLUMN_SYNONYMS
   gốc (bằng cách thêm synonym mới vào từ điển lúc test? — KHÔNG, test phải chứng minh
   map_columns vẫn đúng khi header dời xuống dòng 3-4 và thêm cột nhiễu, dùng DataFrame
   tổng hợp nhỏ, không cần file thật).
2. `test_format_noise`: parse_money/parse_date/parse_quantity không đổi giá trị khi
   đổi dạng biểu diễn (tiền text mới, ngày Excel serial, số lượng text).
3. `test_synthetic_new_shop`: tạo thư mục shop giả lập nhỏ (CSV, tên cột/file khác train)
   rồi load_shop chạy được, profile có has_product_detail đúng.
4. `test_new_rulebook`: tạo rulebook giả lập khác (ví dụ đổi câu chữ về tuần/tháng)
   rồi load_rulebook phản ánh được ít nhất một luật đổi, và không trả về luật cũ
   khi luật cũ không còn trong file.
5. `test_missing_data`: shop giả lập thiếu chi tiết mặt hàng / thiếu giá vốn ->
   profile đánh dấu False tương ứng, load_shop không crash, không bịa số.

Test được phép tạo dữ liệu giả trong thư mục tạm (tempfile), KHÔNG sửa train.

# Giai đoạn 2 — API CONTRACT (bắt buộc, mọi module phải tuân thủ)

## Nguyên tắc chung

- Ngôn ngữ comment/docstring: **TIẾNG VIỆT** cho mọi module, class, hàm public
  (mục đích, đầu vào, đầu ra, vì sao, trường hợp biên, liên quan tổng quát hoá).
- Không sửa code trong `../phase1/` và không sửa dữ liệu train. Import phase1 bằng
  cách thêm `../phase1/src` vào `sys.path` trong `run_phase2.py` và trong các
  module cần nó (dùng đường dẫn tương đối từ file đang chạy, không hard-code
  đường dẫn tuyệt đối của máy).
- Không gọi mạng/API. Chạy offline, deterministic: thứ tự shop và câu hỏi cố định
  (sắp xếp theo business_id rồi id câu hỏi), không dùng ngẫu nhiên không seed.
- Không hard-code theo business_id, id câu hỏi, tên cột chính xác của train,
  schema theo ngành train, rulebook train, hay đáp án train.
- Mọi con số cuối cùng phải do code tính từ dữ liệu đã chuẩn hoá, không tự nhẩm.

## 1. shop_view.py (Agent Solver) — view thống nhất cho một shop

```python
@dataclass
class ShopView:
    business_id: str
    profile: object            # phase1 ShopProfile, giữ nguyên
    lines: pandas.DataFrame    # xem cột chuẩn dưới đây, có thể rỗng nếu shop không có dòng bán
    orders: pandas.DataFrame   # xem cột chuẩn dưới đây, có thể rỗng
    date_min: datetime.date | None   # ngày đặt sớm nhất trong dữ liệu shop (từ lines/orders)
    date_max: datetime.date | None
    products: list[dict]       # mỗi phần tử: {'key': str, 'display': str, 'code': str|None, 'group': str|None}
    customers: list[dict]      # mỗi phần tử: {'key': str, 'display': str, 'code': str|None, 'group': str|None}
    # key = mã nếu có mã, nếu không là tên đã chuẩn hoá giữ dấu (normalize_vn_display)

def build_shop_view(ingest_result, business_id: str) -> ShopView:
    """Ghép các bảng của phase1 thành view thống nhất.

    Cách ghép (không giả định tên bảng/cột):
    - Bảng dòng = bảng có product_name hoặc product_code VÀ (quantity hoặc
      line_amount). Nếu nhiều bảng dòng, lấy bảng dòng lớn nhất có product.
    - Bảng đơn = bảng có order_id VÀ order_date VÀ (order_status hoặc
      order_total). Nếu bảng dòng đã có order_date + order_status thì chính
      nó là bảng đơn luôn, không join.
    - Nếu bảng dòng thiếu ngày/trạng thái/thanh toán/khách thì join với bảng
      đơn theo GIÁ TRỊ mã đơn đã chuẩn hoá (strip, giữ nguyên chuỗi), không
      join theo tên cột. Bảng đơn có nhiều cột mã đơn khác nhau thì thử từng
      cột mã đơn cho đến khi tỉ lệ khớp cao nhất.
    - Cột chuẩn của lines (thêm tiền tố _ để không đè cột gốc):
      _order_id, _date (datetime.date|None), _status (chuỗi gốc),
      _status_norm ('completed'|'cancelled'|'returned'|'delivering'|'other'),
      _payment (chuỗi gốc, chuẩn hoá nhẹ), _customer_key, _customer_display,
      _product_key, _product_display, _product_group,
      _qty (float|None, từ quantity), _qty_base (float|None, từ _qty_base
      của phase1 nếu có, nếu không thì _qty),
      _unit_price (float|None), _line_amount (float|None: ưu tiên cột
      line_amount đã parse, nếu không có thì _qty * _unit_price),
      _cost_price (float|None nếu map được, nếu không thì None).
    - Cột chuẩn của orders (mỗi đơn một dòng, gộp từ bảng đơn hoặc từ lines):
      _order_id, _date, _status, _status_norm, _payment, _customer_key,
      _customer_display, _order_total (float|None: ưu tiên order_total, nếu
      không có thì tổng _line_amount của đơn đó), _amount_paid, _amount_due.
    - Trạng thái chuẩn hoá bằng từ khoá không dấu: chứa 'huy' -> cancelled;
      chứa 'tra' hoặc 'doi tra' hoặc 'hoan' -> returned; chứa 'giao' hoặc
      'dang giao' hoặc 'cho' -> delivering; còn lại -> completed nếu có
      trạng thái, nếu không có trạng thái thì 'other' (không đoán completed).
      Lưu ý: trạng thái 'Xong', 'đã lấy', 'Thành công', 'completed' đều là
      completed; trạng thái rỗng sau forward-fill vẫn là 'other' nếu bảng
      gốc không có cột trạng thái.
    - products/customers lấy từ lines + bảng master nếu có (bảng có product
      hoặc customer mà không phải bảng dòng/đơn), khử trùng theo key.
    """
```

## 2. parser.py (Agent Parser)

```python
@dataclass
class ParsedQuestion:
    measure: str        # một trong: 'revenue','order_count','quantity','cancel_rate',
                        # 'revenue_share','growth_percent','growth_amount','top_product',
                        # 'top_customer','top_employee','best_day','best_month','debt_total',
                        # 'debt_top','import_total','avg_price','price_on_day','continuous_growth',
                        # 'revenue_by_payment','revenue_by_group','revenue_by_channel',
                        # 'quantity_by_unit','product_revenue','unknown'
    entity_kind: str    # 'product'|'customer'|'group'|'payment'|'employee'|'channel'|'none'
    entity_key: str | None
    entity_display: str | None
    period_start: datetime.date | None
    period_end: datetime.date | None
    period_kind: str    # 'absolute'|'relative'|'snapshot'|'multi_month'|'none'
    period_months: list[tuple[int,int]]  # cho câu nhiều tháng / tăng liên tục: [(năm, tháng), ...]
    ranking_k: int | None   # None nếu không phải top-k; 1 nếu 'nhất'
    ranking_target: str     # 'product'|'customer'|'day'|'month'|'employee'|'none'
    unit: str | None        # đơn vị hỏi số lượng, đã normalize_key, ví dụ 'chai','ly','vien','kg','hop'
    threshold_money: float | None  # ngưỡng tiền cho câu 'đơn có doanh thu từ X trở lên'
    threshold_op: str | None       # 'gte'|'gt'|'lte'|'lt'|None (mặc định 'gte' khi nói 'từ X trở lên')
    answer_type: str    # 'number'|'percent'|'string'|'date'|'boolean'|'list'|'table'|'unknown'
    confidence_parse: float  # 0..1, chỉ riêng độ chắc của parse (không phải confidence cuối)
    parse_notes: list[str]   # ghi rõ vì sao không chắc, nếu có
    normalized_text: str     # câu hỏi sau chuẩn hoá không dấu + mở rộng viết tắt

def expand_abbreviations(text: str) -> str: ...
    # 'dthu'->'doanh thu', 'nhiu'/'bnhiu'/'bn'->'bao nhieu', 'dc'->'duoc',
    # 'sp'->'san pham', 'hnay'->'hom nay', 'tuan roi' giữ nguyên ý nghĩa.
    # Làm trên bản không dấu, thường hoá, nhưng KHÔNG làm mất tên sản phẩm:
    # chỉ thay từ viết tắt đứng độc lập, không thay trong tên riêng dài.

def parse_time(text_norm: str, as_of: datetime.date, calendar, rulebook) -> tuple[...]:
    # Trả (start, end, kind, months). Dùng rulebook/time resolver của phase1
    # (week_range, month_range, month_to_date_range,
    # previous_month_same_days_range, last_n_days_range, tet_range).
    # Hỗ trợ tối thiểu: 'thang M/nam Y', 'thang M nam Y', 'thang M' (không năm:
    # năm nay nếu tháng không sau as_of, nếu không thì năm trước),
    # 'quy Q/nam Y', 'nam Y', 'thang nay', 'thang truoc', 'tuan nay',
    # 'tuan truoc'/'tuan roi', 'hom nay', 'hom qua', 'N ngay gan nhat',
    # 'den het ngay D', 'tu X den Y', 'ngay D', 'tet nam Y',
    # 'tu dau nam', '3 thang lien nhau ket thuc o thang M/Y',
    # 'tu thang A den thang B'. Không parse được thì (None, None, 'none', []).

def parse_question(question_text: str, as_of: datetime.date, calendar,
                   rulebook, products: list[dict], customers: list[dict]) -> ParsedQuestion:
    """Parse 4 trục. Entity matching: chuẩn hoá tên (không dấu, thường hoá),
    tìm match dài nhất trong products/customers; tên sản phẩm trong câu có
    thể là tên rút gọn (ví dụ 'Sting dau' cho 'Sting dau 330ml') thì chấp
    nhận nếu mọi token của tên rút gọn nằm trong tên đầy đủ và độ dài
    tên rút gọn >= 2 token, nhưng confidence_parse phải giảm xuống.
    Tách tên sản phẩm TRƯỚC khi suy type: nếu câu chứa '%' nhưng '%' đó
    nằm trong tên sản phẩm đã match thì không suy ra percent.
    """
```

## 3. solver.py (Agent Solver) — tính toán từ ShopView + ParsedQuestion

```python
@dataclass
class SolveResult:
    status: str   # 'answered'|'abstain'|'unanswerable'
    entry: dict | None    # {'id':..., 'type':..., 'answer':...} hoặc
                          # {'id':..., 'type':'abstain'} / {'id':..., 'type':'unanswerable','reason':...}
    confidence: float     # confidence cuối sau gate (xem gate.py), 0..1
    error_group: str | None  # None nếu answered; nếu không thì một trong:
        # 'parser','entity','column','filter','time','unit','format','uncovered','unanswerable'
    evidence: dict        # {'measure':..., 'period':(str,str), 'rows_used':int,
                          #  'filter':str, 'group_key':str|None, ...} để truy vết

def is_revenue_order(status_norm: str, rulebook) -> bool:
    # True khi status là completed hoặc delivering (và rulebook không nói khác).
    # cancelled và returned đều False. status 'other' (không có cột trạng thái)
    # thì True với cảnh báo trong evidence (vì shop không ghi trạng thái thì
    # không có căn cứ loại đơn), nhưng confidence phải giảm.

def solve(parsed: ParsedQuestion, view: ShopView, rulebook, calendar) -> SolveResult:
    """Chỉ trả lời các measure đã bao phủ, còn lại trả abstain với
    error_group='uncovered'. Bộ lọc RIÊNG theo measure:
    - revenue/order_count/quantity/top_*: chỉ đơn revenue (xem is_revenue_order),
      ngày đặt trong kỳ, bỏ đơn huỷ/trả kể cả trả 1 phần (vì trạng thái đơn
      là trạng thái của cả đơn, không phải từng dòng).
    - cancel_rate: mẫu số là MỌI đơn đặt trong kỳ (mọi trạng thái), tử số là
      đơn huỷ; percent làm tròn 2 chữ số thập phân.
    - debt_*: đơn không huỷ (kể cả đơn trả nếu còn nợ), COD/sàn TMĐT loại khỏi
      nợ; nợ tại ngày X = tổng đơn - tiền đã trả đến hết ngày X; chỉ tính đơn
      còn nợ > 0. Nếu không có cột order_total/amount_due và không tính được
      tổng đơn thì abstain, không đoán.
    - price_on_day: đơn giá cho một đơn vị cơ bản, trước chiết khấu dòng,
      tính CẢ dòng của đơn huỷ/trả, ngày đặt = ngày D.
    - growth_percent/growth_amount: kỳ hỏi trừ kỳ so (tháng trước cùng số ngày
      cho 'thang nay', tháng trước đầy đủ cho tháng tuyệt đối, kỳ liền trước
      cho quý/năm); percent = (mới-cũ)/cũ*100, làm tròn 2 chữ số.
    - best_day/best_month: nhóm doanh thu theo ngày/tháng trong kỳ, lấy max;
      hòa thì lấy ngày/tháng sớm nhất? KHÔNG — theo rulebook: hòa thì theo mã
      tăng dần, nhưng ngày/tháng không có mã nên lấy ngày/tháng sớm nhất và
      ghi rõ trong evidence (đây là điểm cần Giai đoạn 3 rà lại nếu rulebook
      mới nói khác; test set đã loại câu hòa ở biên nên ảnh hưởng nhỏ).
    - Xếp hạng: tiêu chí giảm dần, hòa thì theo key (mã/tên) tăng dần.
    - Số tiền trả về phải là số nguyên VND (làm tròn về đồng); percent làm
      tròn 2 chữ số; list/table giữ đúng thứ tự/cột theo câu hỏi.
    """
```

## 4. gate.py (Agent Gate) — confidence, format, unanswerable

```python
def compute_confidence(parsed, view, solve_evidence: dict) -> float:
    """Công thức tường minh, không số magic không giải thích:
    bắt đầu 1.0, trừ điểm theo bằng chứng thiếu:
    - parse không chắc (parsed.confidence_parse < 1): trừ (1 - confidence_parse) * 0.4
    - entity cần mà không match được: trừ 0.5 (solver sau đó sẽ abstain)
    - cột bắt buộc thiếu: trừ 0.5
    - tỉ lệ parse lỗi của shop (profile.parse_failures) cao: trừ tối đa 0.1
    - thiếu hệ số quy đổi ở dòng cần tính số lượng: trừ 0.3
    - kỳ hỏi nằm ngoài khoảng dữ liệu một phần: trừ 0.2
    Kết quả kẹp trong [0, 1]. Ghi rõ từng khoản trừ vào evidence để báo cáo.

def decide(parsed, view, solve_result, rulebook) -> SolveResult:
    """Áp ngưỡng 0.65: confidence < 0.65 thì chuyển thành abstain
    (error_group giữ nguyên để phân tích lỗi, entry là abstain).
    Kiểm tra unanswerable TRƯỚC gate: nếu shop không có mặt hàng mà câu
    cần mặt hàng, hoặc shop không có giá vốn mà câu cần giá vốn/lãi, hoặc
    kỳ hỏi nằm HOÀN TOÀN ngoài [date_min, date_max] của shop, hoặc cột
    bắt buộc cho loại câu hoàn toàn không tồn tại trong bất kỳ bảng nào
    của shop, thì trả unanswerable (không phải abstain).
    Kiểm tra format SAU khi solver trả entry: dùng lại logic của phase1
    evaluate (answers_match chỉ dùng để chấm, ở đây chỉ kiểm tra type và
    kiểu dữ liệu: number phải là int/float không phải bool/str, percent
    phải là số, date phải đúng YYYY-MM-DD hoặc YYYY-MM, string phải là
    str không rỗng, list phải là list, table phải có columns/rows).
    Sai format thì chuyển thành abstain với error_group='format'.
    """
```

## 5. cache.py (Agent Gate) — cache ingest theo shop

```python
def cache_path(cache_dir, business_id: str) -> Path: ...
def save_shop_cache(cache_dir, business_id: str, view: ShopView) -> None:
    """Lưu lines/orders/profile dưới dạng parquet nếu có pyarrow, nếu không
    thì pickle (chỉ cache nội bộ, không phải file nộp). Key cache gồm
    business_id + hash của (tên file + kích thước + thời gian sửa đổi) của
    mọi file nguồn trong thư mục shop; nếu hash khác thì cache cũ vô hiệu.
    """
def load_shop_cache(cache_dir, business_id: str, data_dir) -> ShopView | None:
    """Trả None nếu không có cache hoặc hash nguồn đã đổi (khi đó caller
    phải ingest lại và ghi đè cache). Không bao giờ trả view cũ khi nguồn
    đã đổi.
    """
```

## 6. run_phase2.py (Agent Integration) — điểm vào duy nhất

```bash
python3 run_phase2.py [--train-dir ../train] [--cache-dir ./cache]
  [--output ./output/submission.jsonl] [--shops biz_101,biz_102] [--limit N]
```

- Đọc questions.jsonl theo thứ tự business_id rồi id (deterministic).
- Với mỗi shop (theo thứ tự): load cache, nếu miss thì load_shop phase1 +
  build_shop_view + lưu cache; rồi giải mọi câu của shop đó; giải phóng
  view trước khi sang shop khác (không giữ 30 shop cùng lúc).
- Ghi submission.jsonl tăng dần (mỗi câu ghi ngay sau khi giải xong, không
  chờ hết đợt) để nếu crash giữa chừng vẫn còn đáp án đã giải.
- Chấm bằng phase1 evaluate.score_submission, ghi reports/PHASE2_SUMMARY.json
  (PA tổng, per-type, per-business, holdout, abstain/wrong/correct, coverage)
  và reports/error_analysis.json (lỗi theo nhóm + 2–3 ví dụ id thật mỗi nhóm).
- Exit code 0 khi chạy hết và tests pass; khác 0 khi có shop crash không
  bắt được hoặc tests fail (shop lỗi riêng lẻ vẫn ghi vào báo cáo, không
  giết cả đợt chạy).

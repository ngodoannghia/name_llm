# Hợp đồng API — phase1_llm: Ingest bằng LLM (bổ sung, không thay thế phase1)

Mục đích: LLM chỉ gợi ý ở những chỗ rule-based của phase1 làm yếu
(phân loại bảng, map cột khó, đọc README, phát hiện header khó).
Mọi gợi ý đều phải kiểm chứng bằng dữ liệu thật trước khi áp dụng.
Tiền/ngày/số lượng vẫn do `phase1/src/normalizers.py` parse bằng code
xác định; forward-fill, dedup, hợp nhất vẫn do `phase1/src/ingest.py`
làm. Không sửa code trong `phase1/`, không sửa dữ liệu train.

## 1. Các file và chủ sở hữu (mỗi child chỉ ghi file của mình)

- `src/llm_ingest.py` — Child 1: backend trừu tượng, JSON Schema,
  prompt tiếng Việt, validation + repair tối đa 2 lần, fallback,
  và các hàm kiểm chứng mẫu giá trị.
- `run_phase1_llm.py` — Child 2: runner tích hợp. Import phase1 bằng
  sys.path tương đối (`parents[1]/phase1/src`), import llm_ingest,
  chạy load_shop phase1, chạy llm_ingest cho từng bảng, áp map đã
  kiểm chứng, ghi summary + báo cáo so sánh.
- `tests/test_llm_ingest.py` + `evaluation/compare_baseline.py` —
  Child 3: tests và script so sánh với
  `phase1/reports/phase1_summary.json`. Child 3 không sửa src/runner,
  chỉ đọc chúng và báo lỗi cho coordinator sửa.
- `reports/INGEST_LLM_REPORT.md` + `reports/phase1_llm_summary.json` —
  do Child 2 (runner) sinh ra, Child 3 kiểm tra lại số liệu trước
  khi coordinator chốt.

## 2. Backend (tái sử dụng mẫu phase2_llm, không phát minh chuẩn mới)

```python
class LocalModelBackend(ABC):
    def generate_json(self, prompt: str, schema: dict) -> dict: ...
class TransformersBackend(LocalModelBackend):
    def __init__(self, model_id: str | None = None): ...
    # Lazy import transformers/torch trong __init__/generate_json.
    # Thiếu gói/weights/model_id -> raise RuntimeError rõ ràng,
    # KHÔNG trả JSON giả.
DEFAULT_MODEL_ID = None  # Cố tình: chưa chọn/kiểm chứng model ≤15B.
```

Không ghi tên model/licence/ngày phát hành nào chưa kiểm chứng từ
nguồn chính thức. Không gọi API thương mại. Không có backend thật
thì mọi câu gọi LLM đều fallback rule-based, không crash.

## 3. JSON Schema (3 loại, đều là gợi ý, không phải dữ liệu)

TABLE_ROLES (enum cố định, không tự thêm):
`order`, `order_line`, `product`, `customer`, `import`,
`inventory`, `lot_expiry`, `payment`, `return`, `expense`,
`conversion`, `other`.

```json
{"table_role": "<TABLE_ROLES>", "confidence": 0.0,
 "evidence": ["tên bảng", "header", "mẫu giá trị"],
 "notes": ["ghi chú ngắn"]}
{"mapping": {"<semantic>": "<tên cột thật>"},
 "confidence": 0.0, "evidence": ["..."], "notes": ["..."]}
{"table_roles": {"<tên bảng>": "<TABLE_ROLES>"},
 "status_vocabulary": ["..."],
 "unit_notes": ["..."], "date_notes": ["..."],
 "conversion_table_location": "<tên bảng hoặc null>"}
```

- `semantic` chỉ được lấy trong 37 khóa của
  `phase1/src/ingest.py: COLUMN_SYNONYMS` (order_id, order_date,
  order_datetime, order_status, payment_method, customer_code,
  customer_name, customer_group, product_code, product_name,
  product_group, quantity, quantity_converted, unit, from_unit,
  base_unit, conversion_factor, unit_price, line_amount, cost_price,
  order_total, amount_paid, amount_due, discount, supplier_code,
  supplier_name, employee_code, employee_name, branch, lot_code,
  expiry_date, stock_quantity, import_date, delivery_date,
  payment_date, return_quantity, return_amount).
- Tên cột trong `mapping` phải tồn tại đúng trong DataFrame của bảng
  đó, nếu không thì map đó bị loại ngay, không repair bằng tên khác
  không tồn tại.

## 4. Hàm chính của src/llm_ingest.py (Child 1 phải giữ đúng tên)

```python
def build_table_prompt(table_name, headers, sample_rows, readme_text) -> str
def build_mapping_prompt(table_name, headers, sample_rows, readme_text,
                         unmapped_semantics) -> str
def build_readme_prompt(readme_text, table_names) -> str
def validate_table_classification(data: dict, table_name, headers) -> TableClassification | None
def validate_column_mapping(data: dict, df_columns, df) -> dict  # chỉ trả map đã qua kiểm chứng mẫu, xem mục 5
def validate_readme_notes(data: dict) -> ReadmeNotes | None
def classify_table(table_name, headers, sample_rows, readme_text, backend=None) -> TableClassification
def suggest_mapping(table_name, df, readme_text, unmapped_semantics, backend=None) -> dict
def summarize_readme(readme_text, table_names, backend=None) -> ReadmeNotes | None
def run_llm_ingest_for_shop(shop_result, readme_text, backend=None) -> LlmIngestResult
```

- `LlmIngestResult` là dataclass với các trường: `business_id`,
  `table_roles: dict`, `added_mappings: dict` (bảng -> semantic -> cột),
  `rejected_mappings: list` (mỗi phần tử ghi semantic, cột, lý do loại),
  `readme_notes`, `sources: dict` (bảng -> 'llm' | 'rule_fallback'),
  `fallback_count: int`, `warnings: list`.
- `sample_rows`: tối đa 10 dòng, chỉ chuyển giá trị dạng chuỗi, không
  chuyển toàn bộ DataFrame vào prompt.
- `readme_text`: cắt tối đa 4000 ký tự đầu (ghi rõ giới hạn trong code).
- Mọi hàm trên đều có docstring tiếng Việt đầy đủ.

## 5. Kiểm chứng mẫu giá trị (bắt buộc, không thỏa hiệp)

Dùng lại logic kiểm chứng của phase1 (tái sử dụng hàm public của
phase1 nếu import được, nếu không thì viết lại đúng quy tắc sau và
ghi rõ trong docstring là bản sao quy tắc phase1):

- money semantics (order_total, line_amount, unit_price, cost_price,
  amount_paid, amount_due, discount, return_amount): ít nhất 25% mẫu
  không trống parse được thành số bằng normalizers của phase1.
- date semantics (order_date, order_datetime, expiry_date,
  import_date, delivery_date, payment_date): ít nhất 25% mẫu không
  trống parse được thành ngày bằng normalizers của phase1.
- quantity semantics (quantity, quantity_converted, stock_quantity,
  return_quantity): ít nhất 25% mẫu không trống parse được thành
  số/số kèm đơn vị.
- Các semantic còn lại: kiểm chứng bằng tên (đã qua map_columns)
  hoặc để trống nếu không có mẫu; không dùng mẫu để bịa map.
- Cột toàn trống: tỉ lệ mẫu là None nên không bị loại (giống phase1).
- Map nào không qua kiểm chứng thì đưa vào `rejected_mappings` với
  lý do, không áp vào pipeline, không đoán cột khác.

Quy tắc “dữ liệu thật thắng README”: nếu README nói bảng X là đơn
hàng nhưng dữ liệu thật không có mã đơn/ngày/trạng thái tương ứng,
thì `table_role` vẫn phải theo dữ liệu thật (hoặc `other` + notes),
không được ghi đè bằng README.

## 6. Runner (Child 2) — luồng tích hợp, không sửa logic chuẩn hoá

1. Đọc `phase1/reports/phase1_summary.json` làm baseline (chỉ đọc).
2. Với mỗi shop (sắp xếp theo business_id, deterministic):
   a. Gọi `phase1.load_shop(business_id, train_data_dir)` — giữ nguyên
      IngestResult, tables, profile, rows_in/rows_out của phase1.
   b. Đọc README của shop (nếu có), gọi `run_llm_ingest_for_shop`.
   c. Với mỗi bảng, tính `map_columns(df)` rule-based trước; chỉ xét
      LLM cho các semantic còn trống trong map đó (không ghi đè map
      rule-based đã có, trừ khi LLM map bị rule-based mâu thuẫn và
      kiểm chứng mẫu cho thấy LLM đúng hơn — trường hợp này phải ghi
      vào `rejected_mappings`/`warnings` và báo cáo riêng, không âm
      thầm ghi đè).
   d. Cập nhật hồ sơ shop CHỈ từ map đã kiểm chứng: ví dụ có thêm
      product_name/product_code ở bảng giao dịch thì
      has_product_detail có thể đổi False -> True; không bao giờ đổi
      True -> False chỉ vì LLM nói vậy khi dữ liệu thật vẫn có cột đó.
   e. Không chạy lại forward-fill/dedup/hợp nhất/parse tiền-ngày:
      rows_in/rows_out phải bằng baseline, trừ khi báo cáo giải thích
      được chênh lệch (ví dụ bảng mới được nhận diện làm thay đổi
      cách đếm — phải ghi rõ, không được để chênh lệch không giải thích).
3. Ghi `reports/phase1_llm_summary.json` với: per_shop (rows_in/out,
   baseline_mapped_count, llm_added_mappings, rejected_count,
   fallback_count, sources, profile_delta, warnings), totals,
   backend thực tế (`backend_used`: 'none_fallback' | tên backend
   nếu có, `model_id`: null nếu chưa chọn), sha256 phase1 trước/sau.
4. Ghi `reports/INGEST_LLM_REPORT.md` theo mẫu của coordinator (xem
   nhiệm vụ gốc, mục 4). Runner phải có `--train-dir`, `--shops`,
   `--limit-shops` (mặc định chạy đủ 30 shop khi không giới hạn),
   `--output` cho summary, và exit code 0 chỉ khi không shop nào lỗi
   và không có map chưa kiểm chứng nào lọt vào kết quả.

## 7. Comment tiếng Việt và tính xác định

- Mọi module/class/hàm public đều có docstring tiếng Việt.
- Không dùng random không seed, không dùng thời gian hiện tại làm
  seed, thứ tự shop/bảng/cột luôn sắp xếp để kết quả tái lập.
- Không gọi mạng/API, không sửa `phase1/`, không sửa dữ liệu train.

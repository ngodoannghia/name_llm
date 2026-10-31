# Giai đoạn 1 — Báo cáo tổng kết (chạy thật, không phải số liệu đoán)

**Ngày chạy end-to-end:** 30/10/2026, bằng lệnh duy nhất
`python3 run_phase1.py` từ thư mục `phase1/` (thời gian chạy 429,9 giây,
trong đó ingest 426,62 giây).
**Dữ liệu:** train tại `../train/`, CHỈ ĐỌC, không sửa. Không gọi mạng/API.
**Kết quả tổng thể: PASS — ingest 30/30 shop không crash, khung đánh giá
3/3 trường hợp mẫu đạt kỳ vọng, tests tổng quát hoá 12/12 pass.**

Số liệu chi tiết dạng máy đọc được nằm ở `phase1_summary.json` cùng thư mục
này; báo cáo này là bản đọc được cho người, lấy số liệu trực tiếp từ file đó.

## 1. Kiến trúc đã xây (và đã chạy chung, không còn rời rạc)

```
Câu hỏi (Giai đoạn 2 mới dùng tới)
        │
        ▼
Rulebook loader (src/rulebook.py)
  - Nạp rulebook.md + calendar.csv của split đang chạy lúc chạy
  - Trích luật có cấu trúc, luật không trích được -> unparsed_notes, không bịa
  - Time resolver: tuần thứ Hai, tháng này cắt ở as_of_date, Tết tra calendar
        │
        ▼
Ingest theo từng shop (src/text_utils.py, normalizers.py, ingest.py)
  - Đọc CSV/XLSX/SQLite; hợp nhất sheet theo tháng và file tách
  - Phát hiện header thật (quét N dòng đầu, không giả định dòng 1)
  - Map cột theo ý nghĩa (chuẩn hoá tiếng Việt + từ điển đồng nghĩa
    + kiểm chứng mẫu giá trị), không hard-code tên cột của train
  - Chuẩn hoá tiền text, ngày Excel serial/text/không năm, số lượng text
  - Forward-fill ô trống theo nhóm đơn, khử trùng theo (mã đơn + toàn dòng)
  - Quy đổi về đơn vị cơ bản theo bảng quy đổi của chính shop đó
  - Lập hồ sơ shop (ShopProfile) để Giai đoạn 2 quyết định trả / unanswerable / abstain
        │
        ▼
Khung đánh giá (src/evaluate.py)
  - Chấm PA (+1 đúng, 0 abstain/thiếu, -1 sai), per-type, per-business, holdout riêng
        │
        ▼
Tests tổng quát hoá (tests/test_generalization.py, unittest)
  - Nhiễu header, nhiễu định dạng, shop ngành mới giả lập,
    rulebook mới giả lập, thiếu dữ liệu giả lập
```

Điểm vào duy nhất: `run_phase1.py`. Script này chạy từng shop một rồi
giải phóng DataFrame ngay (không giữ ~4 triệu dòng trong RAM cùng lúc),
ghi `phase1_summary.json` + `tests_output.txt` ra `reports/`, và trả
exit code 0 khi không có shop lỗi và test đều pass.

## 2. Kết quả ingest trên cả 30 shop train

- **30/30 shop ok, 0 shop lỗi (error).**
- **Tổng dòng vào: 3.997.042 → ra: 3.982.749. Chênh lệch 14.293 dòng
  là dòng trùng đã khử theo luật lỗi xuất file** (hai dòng của cùng một đơn
  giống nhau ở mọi ô thì tính một lần), không phải dòng bị mất lặng lẽ.
  Các shop khử nhiều nhất: biz_106 (3.230), biz_105 (2.222), biz_127 (2.899),
  biz_119 (1.908), biz_123 (1.208), biz_128 (1.143).
- **Tổng số bảng đã hợp nhất: 153 bảng** (từ 128 file dữ liệu, vì nhiều
  file XLSX có nhiều sheet theo tháng đã được gộp).
- **Thời gian:** nhanh nhất biz_114 (0,16s, CSV nhỏ), chậm nhất biz_106
  (47,05s) và biz_124 (44,01s) — đều là shop XLSX nhiều sheet lớn. Đây là
  chi phí một lần mỗi shop; Giai đoạn 2 nên cache ingest theo shop trước
  khi chạy đánh giá toàn bộ.
- **Holdout (5 shop):** biz_105, biz_106, biz_117, biz_123, biz_124 —
  tổng 1.063.928 dòng ra sau ingest, đều ingest ok, nên khung đánh giá
  holdout đã sẵn sàng cho Giai đoạn 2.

### 2.1. Bảng theo từng shop

Cột “Map” = số semantic có tên cột thật khác rỗng trong
`columns_mapped` (dict này luôn có 37 khóa semantic; khóa rỗng nghĩa là
chưa map được và cũng nằm trong `columns_unmapped`). Đọc “Map” cùng
“Chưa map” mới đúng: Map + Chưa map = 37.

| Shop | Dòng vào → ra | Bảng | Map / Chưa map | Có mặt hàng | Có mã khách | Có tên khách | Có giá vốn | Có lô/hạn | Có bảng trả riêng | Warning | Parse fail* |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| biz_101 | 56.501 → 56.501 | 1 | 10 / 27 | Có | Không | Có | Không | Không | Không | 0 | 0 |
| biz_102 | 86.106 → 86.106 | 2 | 12 / 25 | Có | Không | Có | Không | Không | Không | 0 | 0 |
| biz_103 | 45.611 → 45.611 | 2 | 12 / 25 | Có | Không | Có | Không | Không | Không | 0 | 0 |
| biz_104 | 62.896 → 62.896 | 2 | 12 / 25 | Có | Không | Có | Không | Không | Không | 0 | 0 |
| biz_105 | 141.748 → 139.526 | 5 | 16 / 21 | Có | Không | Có | Có | Không | Không | 4 | 96 |
| biz_106 | 289.353 → 286.123 | 9 | 20 / 17 | Có | Có | Có | Có | Không | Không | 4 | 0 |
| biz_107 | 43.605 → 43.605 | 1 | 8 / 29 | Có | Không | Không | Không | Không | Không | 0 | 0 |
| biz_108 | 113.696 → 113.696 | 7 | 21 / 16 | Có | Có | Có | Không | Không | Không | 2 | 0 |
| biz_109 | 156.626 → 156.626 | 9 | 22 / 15 | Có | Có | Có | Có | Không | Không | 2 | 0 |
| biz_110 | 29.317 → 29.317 | 1 | 8 / 29 | **Không** | Không | Có | Không | Không | Không | 0 | 0 |
| biz_111 | 239.499 → 239.499 | 5 | 16 / 21 | Có | Có | Có | Không | Không | Không | 0 | 0 |
| biz_112 | 113.375 → 113.375 | 2 | 12 / 25 | Có | Không | Không | Có | Không | Không | 0 | 0 |
| biz_113 | 32.994 → 32.994 | 1 | 6 / 31 | **Không** | Không | Không | Không | Không | Không | 0 | 0 |
| biz_114 | 26.119 → 26.119 | 1 | 7 / 30 | **Không** | Không | Có | Không | Không | Không | 0 | 0 |
| biz_115 | 55.962 → 55.962 | 4 | 14 / 23 | Có | Không | Có | Có | Không | Không | 2 | 0 |
| biz_116 | 79.117 → 79.117 | 4 | 14 / 23 | Có | Không | Có | Có | Không | Không | 2 | 0 |
| biz_117 | 66.286 → 65.791 | 7 | 12 / 25 | Có | Không | Không | Có | Không | Không | 2 | 0 |
| biz_118 | 92.169 → 92.169 | 4 | 13 / 24 | Có | Không | Có | Không | Không | Không | 2 | 0 |
| biz_119 | 172.569 → 170.661 | 10 | 18 / 19 | Có | Có | Có | Không | Có | Không | 6 | 0 |
| biz_120 | 240.702 → 240.702 | 10 | 19 / 18 | Có | Có | Có | Có | Có | Không | 4 | 0 |
| biz_121 | 172.139 → 172.139 | 7 | 19 / 18 | Có | Không | Có | Không | Có | Không | 0 | 0 |
| biz_122 | 214.306 → 214.306 | 7 | 19 / 18 | Có | Không | Có | Không | Có | Không | 0 | 0 |
| biz_123 | 268.911 → 267.703 | 17 | 35 / 2 | Có | Có | Có | Có | Có | **Có** | 2 | 0 |
| biz_124 | 305.973 → 304.785 | 13 | 24 / 13 | Có | Có | Có | Có | Có | Không | 2 | 0 |
| biz_125 | 79.052 → 79.052 | 1 | 6 / 31 | **Không** | Không | Có | Không | Không | Không | 0 | 0 |
| biz_126 | 66.429 → 66.429 | 1 | 7 / 30 | Có | Không | Không | Không | Không | Không | 2 | 0 |
| biz_127 | 198.800 → 195.901 | 5 | 18 / 19 | Có | Có | Có | Không | Không | Không | 2 | 0 |
| biz_128 | 152.259 → 151.116 | 8 | 18 / 19 | Có | Có | Có | Không | Không | Không | 4 | 0 |
| biz_129 | 222.331 → 222.331 | 5 | 15 / 22 | Có | Có | Có | Không | Không | Không | 0 | 0 |
| biz_130 | 172.591 → 172.591 | 2 | 12 / 25 | Có | Không | Không | Có | Không | Không | 0 | 0 |

\* Parse fail ở đây là số lỗi trên mẫu kiểm chứng (tối đa 100 mẫu/cột)
cộng lỗi quy đổi, không phải đếm toàn bộ mọi ô — xem giới hạn ở mục 5.

Đọc bảng này đúng cách:
- **Shop không có mặt hàng theo hồ sơ ingest:** biz_110, biz_113, biz_114,
  biz_125. Với các shop này, Giai đoạn 2 phải chuyển câu hỏi về sản phẩm
  sang unanswerable/abstain, không được bịa số. (Lưu ý: đây là kết quả
  phát hiện của ingest, không phải kiểm toán thủ công từng file; xem
  giới hạn về `columns_mapped` mức shop ở mục 5.)
- **Shop có lô/hạn dùng:** biz_119, biz_120, biz_121, biz_122, biz_123, biz_124
  — đúng nhóm nhà thuốc, phù hợp với phân tích train trước đó.
- **Chỉ biz_123 được phát hiện có bảng trả riêng** theo heuristic tên bảng.
  Các shop khác có thể thể hiện trả hàng bằng trạng thái hoặc dòng âm,
  không phải bằng bảng riêng — Giai đoạn 2 phải xử lý cả ba dạng này,
  không chỉ dựa vào cờ `has_return_table`.
- **Warning nhiều nhất:** biz_119 (6), tiếp theo biz_105, biz_106, biz_120,
  biz_128 (mỗi shop 4). Nội dung warning chủ yếu là khử trùng và bảng
  không tìm được bảng/cột quy đổi nên giữ nguyên số lượng gốc (không đoán
  hệ số) — ví dụ biz_105 còn 96 dòng không tra được hệ số quy đổi nên
  `_qty_base` để trống ở các dòng đó.

## 3. Rulebook loader + time resolver (đã kiểm chứng lại)

- `load_rulebook` trên rulebook train thật: 76 luật trong `rules`,
  `revenue_excludes_cancelled=True`, `revenue_excludes_returned=True`,
  `week_starts_monday=True`, `month_to_date_cut_at_as_of=True`,
  `unparsed_notes=[]` (cả 5 luật tối thiểu đều trích được).
- Calendar: 1.461 dòng dữ liệu (2024-01-01 → 2027-12-31).
- Tết: 2025 → (2025-01-22, 2025-02-07); 2026 → (2026-02-10, 2026-02-26);
  năm ngoài calendar (2030) → `None`, không tự suy ngày.
- Ngày biên đều đúng: tuần của thứ Sáu 2026-10-30 → (2026-10-26, 2026-11-01);
  tháng này đến 2026-10-30 → (2026-10-01, 2026-10-30); tháng trước cùng
  số ngày của 2026-03-31 → (2026-02-01, 2026-02-28); 7 ngày gần nhất đến
  2026-10-30 → (2026-10-24, 2026-10-30).
- Rulebook giả lập đổi luật: tuần Chủ nhật + tháng cả tháng + doanh thu
  gồm cả đơn huỷ/trả → cả 4 trường đều lật thành `False`, Tết → `None` +
  ghi chú; rulebook chỉ có 1 dòng về tuần thì các luật còn lại → `None`,
  không trả về luật train cũ. Đây là bằng chứng loader không hard-code
  rulebook train.

## 4. Khung đánh giá (đã kiểm chứng lại độc lập)

Chấm trên answers train thật (3.000 câu, 30 shop):

| Trường hợp mẫu | Total | Correct | Wrong | Abstain | PA | Holdout PA |
|---|---:|---:|---:|---:|---:|---:|
| Perfect (copy đáp án) | 3.000 | 3.000 | 0 | 0 | **1.0** | 1.0 |
| Toàn abstain | 3.000 | 0 | 0 | 3.000 | **0.0** | 0.0 |
| Broken (2 câu sai format có chủ đích) | 3.000 | 2.998 | 2 | 0 | **0.9987** | 0.9966 |

2 câu wrong trong trường hợp broken đúng như thiết kế:
`q_001883280e83` (number → chuỗi `"89,526,400"`) và `q_00558c1410ef`
(percent 2.08 → 0.0208). Per-type tổng = 3.000 (number 1.884, percent 578,
list 193, date 145, string 88, unanswerable 70, boolean 23, table 19).
Holdout = 592 câu hỏi (biz_105: 119, biz_106: 119, biz_117: 118,
biz_123: 118, biz_124: 118). Khung đánh giá đã sẵn sàng để Giai đoạn 2
cắm đáp án vào chấm ngay, không cần sửa evaluate.py.

## 5. Tests tổng quát hoá: 12/12 pass

Chạy `python3 -m unittest discover -s tests -v` từ `phase1/`:
**Ran 12 tests, OK, exit code 0.** Trước khi Ingest xong, 10/12 test từng
skip vì thiếu hàm; sau khi tích hợp, cả 12 đều pass thật, không còn skip.

| Nhóm test | Nội dung | Kết quả |
|---|---|---|
| Header noise (2 test) | Header dời xuống dòng 3 và dòng 4, thêm cột nhiễu | Pass |
| Format noise (5 test) | Tiền text = số thuần, ngày serial = ngày text, ngày thiếu năm cần sheet_year, số lượng text = số thuần, giá trị không parse được trả None không gán 0 | Pass |
| Shop ngành mới giả lập (1 test) | Shop CSV với tên cột/file khác train, cùng logic nghiệp vụ | Pass |
| Rulebook mới giả lập (2 test) | Đổi luật tuần/tháng/doanh thu; luật bị bỏ không được bịa lại | Pass |
| Thiếu dữ liệu giả lập (2 test) | Thiếu mặt hàng → has_product_detail=False; thiếu giá vốn → has_cost=False, không crash, không bịa số | Pass |

## 6. Comment code tiếng Việt: đã kiểm tra

Kiểm tra bằng AST trên mọi file trong `src/`: **không còn hàm public nào
thiếu docstring tiếng Việt** (`__init__.py`, `evaluate.py`, `ingest.py`,
`normalizers.py`, `rulebook.py`, `text_utils.py` đều đạt). Các agent còn
báo cáo rằng cả hàm helper lồng nhau cũng có docstring tiếng Việt.
Đây là yêu cầu rõ ràng của người dùng và đã được xác minh, không chỉ
cam kết bằng lời.

## 7. Giới hạn còn tồn tại — nói rõ, không giấu

1. **Chưa chạy `load_all_shops` giữ toàn bộ trong RAM** — `run_phase1.py`
   cố tình chạy từng shop một rồi giải phóng bộ nhớ. Hàm `load_all_shops`
   vẫn tồn tại đúng contract, nhưng Giai đoạn 2 nên dùng luồng từng shop
   + cache theo shop thay vì giữ cả 30 shop cùng lúc.
2. **`profile.columns_mapped` là mức shop** (cột thật đầu tiên tìm thấy
   khi duyệt bảng theo tên). Nếu hai bảng dùng tên cột khác nhau cho cùng
   semantic, giá trị gộp có thể không phải cột của bảng cần tính. Code cần
   chính xác theo bảng nên gọi lại `map_columns(df)` cho từng bảng
   (hàm này public, đúng contract).
3. **`parse_failures` là tín hiệu chất lượng, không phải kiểm toán tuyệt đối**
   (mẫu tối đa 100 mẫu/cột + lỗi quy đổi). Đếm hết ~4 triệu dòng mỗi lần
   load sẽ quá chậm.
4. **Quy đổi chưa hoàn hảo:** biz_105 còn 96 dòng không tra được hệ số
   (mặt hàng có trong bảng quy đổi nhưng đơn vị dòng không khớp đơn vị
   nào đã biết) → `_qty_base` để trống + warning, không đoán hệ số 1.
   Chỉ khi mặt hàng hoàn toàn không có dòng quy đổi nào thì mới áp hệ số 1
   theo luật rulebook. Nhiều bảng nhập/lô không tìm được bảng/cột quy đổi
   nên giữ nguyên số lượng gốc (có warning).
5. **Nhận diện bảng trả riêng chỉ dựa vào tên bảng** (token trả/return).
   Bảng trả đặt tên hoàn toàn khác sẽ bị bỏ sót → `has_return_table=False`
   dù có thông tin trả. Giai đoạn 2 phải xử lý trả hàng qua cả trạng thái,
   dòng âm và cột trả theo lô, không chỉ cờ này.
6. **Heuristic có ghi rõ trong code, không phải chân lý:** tiền `1.500`
   hiểu là 1500 (phân cách nghìn) chứ không phải 1.5; ngày năm 2 chữ số
   (`01/02/25`) không hỗ trợ → `None`; ngày không năm chỉ parse được khi
   tên sheet/file chứa năm.
7. **Hiệu năng XLSX:** đọc bằng openpyxl `read_only` vẫn tốn 17–47s cho
   shop nhiều sheet lớn; chấp nhận được ở Giai đoạn 1 vì là chi phí một
   lần mỗi shop, nhưng cần cache trước khi chạy đánh giá toàn bộ.
8. **Giới hạn của evaluate.py (đã ghi từ trước, nhắc lại để Giai đoạn 2
   không quên):** percent làm tròn kiểu nửa lên; dòng table so dưới dạng
   ghép đôi một-một (multiset), chặt hơn chữ “tập hợp” nếu có dòng trùng;
   alias table hiểu là mã thuộc về dòng đó; date chỉ khớp tuyệt đối dạng
   chuỗi, không tự chuẩn hoá `2025-9` thành `2025-09`.
9. **Giới hạn của rulebook.py:** bộ trích xuất chỉ nhận diện các cách viết
   phổ biến. Rulebook mới diễn đạt hoàn toàn khác có thể cho `None` —
   đó là hành vi đúng (abstain thay vì đoán), nhưng cần mở rộng mẫu từ
   khoá nếu test luật mới dùng cách viết ngoài bộ mẫu.

## 8. Giai đoạn 2 cần làm tiếp (theo đúng MASTER_PLAN mục 6)

1. **Question parser theo 4 trục:** đo cái gì × của ai/cái gì × kỳ nào ×
   dạng đáp án. Chuẩn hoá không dấu/viết tắt trước, tách tên sản phẩm
   (kể cả `%` trong tên) trước khi suy type.
2. **Sinh code tính toán trên dữ liệu đã chuẩn hoá** (không để LLM tự
   nhẩm số), kiểm tra bộ lọc đơn đúng cho từng loại câu (doanh thu /
   công nợ / giá bán / đợt giao khác nhau — không dùng một bộ lọc chung),
   đơn vị đã quy đổi, kỳ đã cắt đúng, tie-break theo mã.
3. **Confidence gate (ngưỡng vận hành đề xuất 0,65–0,70) + format validator:**
   dưới ngưỡng thì abstain; chỉ kết luận unanswerable khi có bằng chứng
   mạnh về thiếu dữ liệu (train chỉ có 2,33% unanswerable).
4. **Cache ingest theo shop** trước khi chạy đánh giá toàn bộ.
5. **Phân tích lỗi theo nhóm** (lỗi code, lỗi map schema, lỗi hiểu câu hỏi,
   lỗi luật) sau mỗi lần chạy, không sửa theo từng câu hỏi lẻ.
6. **Chỉ cân nhắc finetune sau khi có baseline** và phân tích lỗi cho thấy
   lỗi chủ yếu là do LLM — xem MASTER_PLAN mục 8. Không finetune trước
   baseline code.
7. **Đóng gói Docker** (Giai đoạn 3): offline hoàn toàn, tổng model ≤15B,
   `predict.sh` đọc `/data` ghi `/result/submission.jsonl` tăng dần +
   log từng câu, deterministic, tái lập |PA| ≤ 0,03, trong 6 giờ trên
   2 × RTX 5090. Hạn nộp Docker: 23:59 GMT+7 ngày 16/11/2026.

## 9. Cách chạy lại toàn bộ để kiểm chứng báo cáo này

```bash
cd workspace/datasets/datamate/phase1
python3 run_phase1.py
# Kỳ vọng: Ingest 30/30 ok, perfect PA=1.0, abstain PA=0.0,
# broken PA=0.9987, tests Ran 12 OK, exit code 0.
# Báo cáo JSON mới sẽ ghi đè reports/phase1_summary.json.
```

# Giai đoạn 2 DataMate — Parser → Solver → Gate (tích hợp end-to-end)

> Ngôn ngữ làm việc: tiếng Việt. Mọi module/class/hàm public trong
> Giai đoạn 2 đều có docstring tiếng Việt đầy đủ (kiểm tra bằng AST
> như Giai đoạn 1). Không finetune ở Giai đoạn 2. Không gọi mạng/API.
> Chạy offline, deterministic.

## 1. Kiến trúc

Pipeline chạy theo đúng `API_CONTRACT.md`, một shop một lượt (không
giữ 30 shop cùng lúc trong RAM):

```
questions.jsonl (sắp xếp theo business_id rồi id)
  └─► mỗi shop:
        cache hit? ── không ──► phase1 load_shop ──► build_shop_view ──► save_shop_cache
                  └── có ───► load_shop_cache
        mỗi câu hỏi của shop:
          parse_question (parser.py)
            └─► solve (solver.py, chỉ code tính, không tự nhẩm số)
                  └─► compute_confidence làm trần + decide (gate.py:
                      unanswerable trước, ngưỡng 0,65 sau, format cuối)
                        └─► runner điền id thật, ghi submission.jsonl TĂNG DẦN
        giải phóng view, sang shop khác
  └─► phase1 evaluate.score_submission ──► reports/PHASE2_SUMMARY.json
                                            reports/error_analysis.json
```

| Tầng | File | Trách nhiệm (một dòng) |
|---|---|---|
| Điểm vào | `run_phase2.py` | Đọc câu hỏi, nạp/cache view theo shop, giải từng câu, ghi tăng dần, chấm, ghi báo cáo. |
| Parser | `src/parser.py` | Câu hỏi tiếng Việt → `ParsedQuestion` 4 trục (measure, entity, kỳ, dạng đáp án), khớp tên rút gọn, che tên trước khi suy type (dấu `%` trong tên thuốc không thành percent). |
| View | `src/shop_view.py` | Ghép bảng phase1 thành `ShopView` với cột chuẩn tiền tố `_`, join theo giá trị mã đơn. |
| Solver | `src/solver.py` | `ParsedQuestion` + `ShopView` → `SolveResult` bằng code; bộ lọc RIÊNG theo measure (xem §4). |
| Gate | `src/gate.py` | `compute_confidence` (công thức trừ điểm tường minh), `decide` (ngưỡng `GATE_THRESHOLD = 0,65`), `validate_entry_format`. |
| Cache | `src/cache.py` | Cache view theo shop, key = business_id + hash (tên file + kích thước + mtime) của mọi file nguồn. |

### Điểm thay thế parser bằng LLM local ≤ 15B trong tương lai

Parser hiện tại là **template/rule-based** (từ khoá + khớp token +
hàm thời gian của phase1). Đây là điểm thay thế có chủ đích:

- **Hợp đồng thay thế:** bất kỳ parser nào (template hay LLM local)
  cũng phải trả về đúng `ParsedQuestion` — cùng các trường, cùng quy
  ước (`period_kind='snapshot'` thì `period_start=None`;
  `ranking_metric` cho xếp hạng; `confidence_parse` thấp khi không
  chắc, kèm `parse_notes`). Chỉ cần thay hàm `parse_question` (và giữ
  nguyên `expand_abbreviations`/`parse_time` làm dự phòng), không sửa
  solver/gate/runner.
- **Solver/gate không phụ thuộc LLM:** solver chỉ đọc `ParsedQuestion`
  và cột chuẩn của `ShopView`; gate chỉ đọc thuộc tính bằng
  duck-typing. Vì vậy đổi parser không làm đổi một dòng luật tính,
  luật ngưỡng hay luật format nào — đáp án vẫn do code tính, vẫn
  abstain khi không chắc.
- **Khi nào mới thay:** chỉ khi phân tích lỗi cho thấy lỗi template
  parser chiếm đa số (xem `reports/PHASE2_REPORT.md` và
  MASTER_PLAN mục 8 — khi đó mới cân nhắc LoRA, vẫn sau code baseline).
  LLM local phải chạy offline trong Docker, ≤ 15B, và phải bị gate
  chặn giống như parser template (confidence thấp → abstain).

## 2. Cách chạy

Từ thư mục `phase2/`:

```bash
# Toàn bộ 3.000 câu train (lần đầu ingest lâu, lần sau dùng cache)
python3 run_phase2.py --train-dir ../train --cache-dir ./cache \
  --output ./output/submission.jsonl

# Chỉ một vài shop, hoặc giới hạn số câu (để smoke test)
python3 run_phase2.py --train-dir ../train --cache-dir ./cache \
  --output ./output/submission.jsonl --shops biz_101,biz_102
python3 run_phase2.py --train-dir ../train --cache-dir ./cache \
  --output ./output/submission.jsonl --limit 100

# Kiểm thử tích hợp (29 test, phải pass thật, không skip giả)
python3 -m unittest discover -s tests -v
```

Các đường dẫn mặc định trong lệnh trên là tương đối từ thư mục
`phase2/` (script tự giải theo vị trí file, không theo thư mục làm
việc hiện tại). Kết quả:

- `output/submission.jsonl` — ghi tăng dần, mỗi dòng một entry
  `{"id", "type", "answer"?}` hoặc `{"id", "type": "abstain"}` /
  `{"id", "type": "unanswerable", "reason"?}`.
- `reports/PHASE2_SUMMARY.json` — PA tổng, per-type, per-business,
  holdout, correct/wrong/abstain, coverage, thời gian từng shop,
  cache hit/miss, shop lỗi.
- `reports/error_analysis.json` — lỗi theo nhóm + ví dụ id thật,
  câu sai theo measure và theo shop.
- `reports/PHASE2_REPORT.md` — báo cáo đầy đủ (xem §5).

**Lưu ý cache (bắt buộc):** `save_shop_cache` cần thêm tham số
`data_dir` để tính hash nguồn. `run_phase2.py` luôn truyền
`data_dir=<train-dir>`; nếu không truyền, cache sẽ lưu với
`source_hash=None` và lần nạp sau luôn miss một cách an toàn (thà
ingest lại còn hơn trả view cũ khi nguồn đã đổi). Đổi nội dung nguồn
(đổi tên/kích thước/mtime file) thì cache cũ tự vô hiệu.

**Lưu ý exit code:** script trả 0 khi chạy hết mọi shop đã chọn
(shop lỗi riêng lẻ đã được bắt, ghi abstain cho mọi câu của shop đó
và ghi vào báo cáo thì không giết đợt chạy). Exit khác 0 chỉ khi lỗi
tầng chạy chung không bắt được (không đọc được questions, rulebook
hay calendar). "Tests pass" được kiểm chứng riêng bằng lệnh unittest
ở trên; báo cáo cuối chỉ được coi là hoàn tất khi cả hai đều xanh.

## 3. Sửa lỗi tích hợp đã thực hiện (ghi rõ, không giấu)

1. **Solver xếp top_product theo doanh thu (SAI rulebook) → đã sửa.**
   Solver cũ xếp mọi `top_*` theo doanh thu vì `ParsedQuestion`
   không có trường chỉ tiêu. Nay:
   - `parser.py`: thêm trường `ranking_metric` vào `ParsedQuestion`
     (mặc định theo measure: `top_product` → `'quantity'`,
     `top_customer` → `'revenue'`, `best_day`/`best_month` →
     `'revenue'`, `debt_top` → `'debt'`; câu "hàng lời/lãi nhất" →
     `'profit'` nếu parser nhận diện được cụm lời/lãi).
   - `parser.py`: `_detect_ranking` chưa gán target cho câu "hàng nào
     lời nhất" (chỉ có từ "hang" chung chung) → bổ sung: có cụm
     lời/lãi và có xếp hạng mà chưa có target thì target là
     `'product'`.
   - `solver.py` (`_solve_top`): đọc `ranking_metric`; `quantity`
     thì cộng `_qty_base` (luật thiếu hệ số giống `_quantity_total`:
     mặt hàng có bảng quy đổi mà thiếu `_qty_base` thì abstain
     `unit`, không đoán hệ số 1); `profit` thì cộng
     `line_amount - qty * cost_price` (giả định giá vốn theo đơn vị
     bán của dòng — xem §5, Giai đoạn 3 cần rà lại); `revenue` giữ
     nguyên như cũ. `ParsedQuestion` cũ không có trường này thì dự
     phòng product → `quantity`, customer → `revenue`.
   - Đã chạy lại toàn bộ test parser/solver sau sửa đổi (25/25 pass).
2. **Solver trả entry `id=None` → runner điền id thật.** `ParsedQuestion`
   không mang id nên solver không được bịa id; `run_phase2.py`
   (`solve_one_question`) điền id thật SAU khi gate quyết định xong,
   và test `test_solve_one_question_fills_real_id` khóa hành vi này.
3. **Snapshot nợ (`period_start=None`, `period_end=X`) → đã kiểm chứng.**
   Solver `_solve_debt` dùng `period_end` làm ngày snapshot (mọi đơn
   đặt đến hết ngày đó); test `test_solve_one_question_snapshot_debt`
   và `test_debt_snapshot_excludes_cancelled_and_cod` khóa hành vi
   này, không tự gán `start = end`.
4. **Import chéo parser/solver/gate → đã kiểm chứng thực tế.**
   Solver import `ParsedQuestion` từ `parser`; gate dùng duck-typing
   (không import cứng); `run_phase2.py` nạp cả ba từ cùng thư mục
   `src/` và chạy chung trong test runner — không giả định.
5. **Gate chỉ dùng confidence của solver → runner lấy min làm trần.**
   `decide` chỉ tự tính `compute_confidence` khi solver chưa gán
   confidence; nếu giữ nguyên, câu parse mơ hồ nhưng solver tự tin
   1.0 sẽ lọt ngưỡng 0,65 oan. Runner therefore tính
   `compute_confidence(parsed, view, evidence)` và lấy
   `min(confidence_solver, confidence_gate)` trước khi gọi `decide`
   (hai công thức trừ các khoản khác nhau nên lấy min là trần an
   toàn, không cộng dồn hai lần cùng một khoản).
6. **Parser khớp nhầm thực thể do "bao nhiu" → đã sửa.** "bao nhiu"
   mở rộng thành "bao bao nhieu" tạo đoạn "bao bao" khớp nhầm với
   mọi thực thể có tên chứa "bao". Nay đoạn tên rút gọn phải gồm các
   token khác nhau (xem `reports/PHASE2_REPORT.md` §7.6).
7. **Shop_view mất toàn bộ ngày ở shop chỉ có cột "Thời gian" →
   đã sửa.** Phase1 map "Thời gian" thành `order_datetime` nhưng
   shop_view chỉ đọc `order_date`; nay cả 4 chỗ đọc ngày đều chấp
   nhận `order_datetime` làm dự phòng, cứu 6/10 shop từng 0 câu đúng
   (xem `reports/PHASE2_REPORT.md` §7.7).
8. **Hiệu năng solver → đã sửa.** Trích lines/orders một lần mỗi shop
   (`_cached_records` gắn vào view) thay vì mỗi câu hỏi, và
   `_is_blank` kết luận ngay với chuỗi/số (giữ nguyên ngữ nghĩa).
   Biz_101 từ ~40 giây còn ~4 giây, đáp án trước/sau sửa đổi giống
   hệt nhau (xem `reports/PHASE2_REPORT.md` §7.5).

## 4. Bộ lọc theo measure (nhắc lại để không dùng nhầm một bộ lọc chung)

- Doanh thu / số đơn / số lượng / top_* / best_* / tăng trưởng:
  chỉ đơn revenue (`completed`/`delivering`; `other` — shop không có
  cột trạng thái — vẫn tính kèm cảnh báo và trừ confidence), ngày
  đặt trong kỳ, bỏ đơn huỷ/trả kể cả trả một phần.
- Tỉ lệ huỷ: mẫu là MỌI đơn trong kỳ, tử là đơn huỷ.
- Công nợ: đơn không huỷ (kể cả đơn trả nếu còn nợ), loại COD/sàn
  TMĐT, nợ tại ngày X = tổng đơn − tiền trả đến hết ngày X (ưu tiên
  bảng thu theo ngày, nếu không có thì dùng ảnh chụp cuối kỳ), chỉ
  đơn còn nợ > 0.
- Giá ngày: tính CẢ dòng của đơn huỷ/trả, ngày đặt đúng bằng ngày D,
  đơn giá cho một đơn vị cơ bản, trước chiết khấu dòng.
- Xếp hạng: chỉ tiêu giảm dần, hoà thì key tăng dần; riêng
  best_day/best_month hoà thì lấy ngày/tháng sớm nhất (ghi rõ trong
  evidence; test set đã loại câu hoà ở biên).

## 5. Giới hạn còn tồn tại (xem chi tiết trong PHASE2_REPORT.md)

- Parser template chưa bao phủ: nhập hàng chi tiết, lãi/lời gộp tổng
  (không phải xếp hạng), giá trung bình mơ hồ, câu hai số (nợ thêm /
  trả được), câu hỏi theo nhân viên, và các cách diễn đạt lạ ngoài
  từ khoá đã liệt kê → các câu này abstain (`parser`/`uncovered`)
  thay vì đoán.
- Một số shop yếu hơn hẳn (xem báo cáo theo shop) — thường do shop
  không có dòng bán (chỉ hoá đơn tổng) nên mọi câu sản phẩm đều
  unanswerable/abstain, hoặc do tên sản phẩm rút gọn mơ hồ.
- Giả định giá vốn theo đơn vị bán của dòng trong xếp hạng profit
  chưa được rulebook mới xác nhận; nếu rulebook mới định nghĩa giá
  vốn theo đơn vị cơ bản thì phải đổi công thức profit.
- Tốc độ: solver trích toàn bộ lines/orders thành list cho MỖI câu
  hỏi (O(n) mỗi câu) nên shop lớn chạy chậm; cache chỉ tiết kiệm
  phần ingest, không tiết kiệm phần giải. Giai đoạn 3 nên cache
  `lines`/`orders` đã trích theo shop trong một lượt chạy.

## 6. Giai đoạn 3 cần làm

1. Đóng gói Docker offline: `predict.sh` đọc đúng split được mount
   (rulebook + calendar của split đó, không hard-code luật train),
   model LLM local ≤ 15B nếu thay parser (xem §1), không gọi mạng.
2. Tái lập: chạy lại trên train cho |PA| ≤ 0,03 so với báo cáo này
   (cùng code, cùng cache key theo hash nguồn).
3. Rà lại các điểm đã ghi "cần Giai đoạn 3 rà lại": hoà best_day/
   best_month, giả định giá vốn theo đơn vị bán, luật tăng trưởng cho
   kỳ quý/năm, và câu hoà ở biên nếu rulebook mới nói khác.
4. Chỉ cân nhắc LoRA cho parser nếu lỗi template parser chiếm đa số
   (MASTER_PLAN mục 8); vẫn giữ solver/gate bằng code và ngưỡng
   abstain — không finetune để nhớ đáp án train.
5. Hạn nộp: **23:59 GMT+7, 16/11/2026**.

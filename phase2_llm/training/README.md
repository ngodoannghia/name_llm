# LoRA Data Pipeline — Parser DataMate (phase2_llm/training)

Pipeline này tạo dữ liệu trace đã kiểm chứng, huấn luyện adapter LoRA
cho parser, và đánh giá LoRA theo cổng quyết định — **không** thay
solver bằng code, **không** để model tự nhẩm số, **không** sửa
`phase2/`, dữ liệu train hay đáp án train. Mọi file trong thư mục này
đều thuộc agent LoRA Data Pipeline; không ghi ra ngoài các file được
giao.

## 1. Cổng quyết định (MASTER_PLAN mục 8 + API_CONTRACT mục 2, 3)

LoRA chỉ được giữ khi **tất cả** các điều kiện sau cùng đúng:

1. Chỉ dùng trace đã kiểm chứng: câu hỏi → template parser phase2 →
   solver phase2 chạy trên `ShopView` → đáp án solver khớp đáp án train
   theo `answers_match` của phase1 thì mới ghi `verified=True`.
   Đáp án train **không bao giờ** là đầu vào của `ParsedQuestion`;
   nó chỉ xuất hiện sau khi solver đã chạy, ở bước đối chiếu.
2. Trộn dữ liệu tổng quát hoá: biến thể không dấu, viết tắt
   (`dthu`, `nhiu`, `dc`...) giữ nguyên parse đã kiểm chứng, và ví dụ
   tiêu cực cho abstain/unanswerable (thực thể không tồn tại, kỳ ngoài
   dữ liệu) với `parse_notes` rõ ràng, không gán đáp án bịa.
3. Đánh giá trên holdout theo shop, không đánh giá ngẫu nhiên theo dòng.
   Baseline lấy từ `phase2/reports/PHASE2_SUMMARY.json`:
   - PA tổng baseline: **−0,212** (581 đúng / 1.217 sai / 1.202 abstain
     trên 3.000 câu).
   - PA holdout baseline: **−0,3564** trên 592 câu của 5 shop holdout:
     `biz_124`, `biz_123`, `biz_106`, `biz_105`, `biz_117`.
   - Công thức PA: `(đúng − sai) / tổng`; abstain tính 0.
4. Kết luận **KEEP** chỉ khi: PA holdout tăng so với baseline **và**
   PA tổng không giảm quá 0,05 **và** submission không toàn abstain.
   Mọi trường hợp còn lại là **REJECT**. Tăng PA chỉ trên train mà
   holdout không tăng thì vẫn là REJECT.

## 2. Các file trong thư mục này

| File | Việc |
|---|---|
| `generate_verified_traces.py` | Chạy template parser phase2 (import qua `sys.path`, không copy-sửa), chạy solver, đối chiếu đáp án train; chỉ giữ trace solver trả đúng. Tạo biến thể tổng quát hoá và ví dụ tiêu cực. Ghi JSONL, không ghi đáp án train vào dòng nào. |
| `train_lora.py` | Train LoRA (không full finetune) theo `config.json`. Có chế độ `--dry-run` kiểm tra dataset/schema/config và ước lượng số bước mà không cần weights, không cần torch/transformers/peft. |
| `evaluate_lora.py` | Chấm một submission bằng `answers_match` của phase1 (dự phòng bằng logic xấp xỉ nội bộ nếu phase1 không nạp được, và ghi rõ phương pháp), tính PA tổng + PA holdout, áp cổng KEEP/REJECT. |
| `config.json` | Cấu hình train + cổng quyết định. `base_model` đang là `null` (chưa chọn — xem mục 4). |
| `data/verified_traces_sample.jsonl` | Mẫu nhỏ đã kiểm chứng thật (xem mục 5). |

Định dạng một dòng JSONL:

```json
{
  "question": "câu hỏi (gốc hoặc biến thể)",
  "normalized_question": "câu hỏi sau chuẩn hoá của parser",
  "as_of_date": "2026-08-24",
  "business_id": "biz_101",
  "parsed_question_json": { "measure": "...", "entity_kind": "...",
    "entity_key": null, "entity_display": null,
    "period_start": "2025-10-01", "period_end": "2025-12-31",
    "period_kind": "absolute", "period_months": [],
    "ranking_k": null, "ranking_target": "none",
    "ranking_metric": null, "unit": null,
    "threshold_money": null, "threshold_op": null,
    "answer_type": "number", "confidence_parse": 1.0,
    "parse_notes": [], "normalized_text": "..." },
  "verified": true,
  "verification_method": "solver_output_matches_train_answer",
  "source": "template_parser_plus_solver",
  "generalization_tag": "none"
}
```

Nhãn `verified` nói đúng sự thật, không được sửa tay:

- `verified=True`, `verification_method="solver_output_matches_train_answer"`:
  solver đã chạy và đáp án khớp đáp án train.
- `verified=False`, `verification_method="inherited_from_solver_verified_source_not_rerun"`:
  biến thể không dấu/viết tắt kế thừa parse đã kiểm chứng, solver
  **chưa** chạy lại trên câu biến thể.
- `verified=False`, `verification_method="negative_example_no_solver_run_no_answer"`:
  ví dụ tiêu cực tổng hợp, không gán đáp án, solver không chạy.
- `verified=False`, `verification_method="candidate_not_verified"`:
  chỉ xuất hiện khi chạy generator với `--keep-unverified-candidates`
  (mặc định tắt; file mẫu không chứa loại này).

## 3. Cách chạy dry-run (đã chạy thật, xem mục 5)

Từ thư mục `training/`:

```bash
# Tạo lại file mẫu (40 câu của biz_101, dùng cache phase2 nếu có):
python3 generate_verified_traces.py --shops biz_101 --limit 40

# Tạo trace toàn bộ train (chậm hơn nhiều vì nạp view từng shop):
python3 generate_verified_traces.py --limit 0

# Kiểm tra khô train LoRA (không cần weights, không tải gì):
python3 train_lora.py --dry-run

# Minh hoạ cổng đánh giá bằng submission phase2 (KHÔNG phải kết quả LoRA):
python3 evaluate_lora.py --dry-run

# Chấm submission thật của pipeline LoRA (khi đã có):
python3 evaluate_lora.py --submission ./submission_lora.jsonl \
    --output ./lora_eval_report.json
```

Ghi chú về cache: generator ưu tiên dùng cache `phase2/cache/`
(nếu hash nguồn khớp, theo thiết kế của `cache.py`); nếu cache miss
thì ingest lại từ dữ liệu gốc bằng phase1 rồi dựng `ShopView`.
Dữ liệu train chỉ đọc, không sửa.

## 4. TRUNG THỰC — trạng thái thật của lần này

**Chưa train thật weights nào.** LoRA trong lần này là
**pipeline + dataset mẫu + script đánh giá**, không phải weights đã
train xong. Bằng chứng:

- Môi trường hiện tại không có `torch`, `transformers`, `peft`
  (dry-run kiểm tra và báo cả ba là `false`), không có cache
  Hugging Face, và không được download weights nặng tùy tiện.
- `config.json` để `base_model: null` và `target_modules: null`:
  **chưa chọn model**. Yêu cầu bắt buộc khi chọn: model ≤ 15B tham
  số, weights public trước 01/10/2026, licence cho phép dùng, đã kiểm
  chứng từ nguồn chính thức. **Không bịa tên model hay licence nào**
  trong lần này; mọi tên model/licence chưa kiểm chứng đều không
  xuất hiện trong config hay script.
- Chạy `train_lora.py` không có `--dry-run` bị chặn ngay với mã thoát
  2 và thông báo "Chưa chọn base_model" — cố tình, không phải lỗi.
- **Không có số PA nào của LoRA** trong lần này, vì chưa có model
  LoRA nào để chạy. Mọi số PA trong README này đều là baseline
  phase2 (mục 1) hoặc kết quả minh hoạ cổng bằng submission phase2
  (mục 5), và đều được ghi nhãn rõ ràng như vậy.

## 5. Kết quả thật đã chạy trong lần này

### 5.1. File mẫu `data/verified_traces_sample.jsonl`

Lệnh: `generate_verified_traces.py --shops biz_101 --limit 40`
(xét 40 câu của `biz_101`, theo thứ tự id, dùng cache phase2).

| Chỉ số | Giá trị |
|---|---|
| Câu đã xét / candidate đã parse | 40 / 40 |
| Candidate lỗi schema | 0 |
| Solver trả lời (không abstain) | 27 |
| Solver trả đúng → giữ lại, `verified=True` thật | **26** |
| Solver trả sai → loại bỏ | 1 |
| Solver abstain → loại bỏ | 13 |
| Biến thể tổng quát hoá (không dấu / viết tắt) | 35 (19 không dấu, 16 viết tắt) |
| Ví dụ tiêu cực (thực thể vắng mặt / kỳ ngoài dữ liệu) | 2 (1 + 1) |
| Tổng số dòng trong file mẫu | **63** (26 verified=True, 37 verified=False) |
| Shop bị bỏ qua do nạp view thất bại | 0 |
| Dòng nào trong file chứa trường đáp án train | 0 (schema không có trường đáp án) |

Phương pháp kiểm chứng của 26 dòng `verified=True`:
template parser phase2 → solver phase2 trên `ShopView` của `biz_101`
(nạp từ cache phase2) → `answers_match` của phase1 trả `"correct"`.
1 câu solver trả sai và 13 câu solver abstain đã bị loại, không ghi
vào file.

### 5.2. Dry-run `train_lora.py --dry-run` (pass)

- Dataset: 63 dòng, 26 verified=True, 37 verified=False, 0 lỗi schema.
- Config: chỉ có 1 vấn đề là `base_model` chưa chọn (xem mục 4).
- Ước lượng bước trên file mẫu: 4 bước/epoch, 12 bước cho 3 epoch
  (batch 8 × tích lũy 2). Con số này chỉ có ý nghĩa minh hoạ trên
  file mẫu nhỏ, không phải kế hoạch train thật.
- Thư viện nặng: `torch`, `transformers`, `peft` đều `false`.
- `trained_real_weights: false`, `ready_for_real: false`.

### 5.3. Dry-run `evaluate_lora.py --dry-run` (minh hoạ cổng, REJECT)

Chấm submission phase2 (`phase2/output/submission.jsonl`) bằng
`answers_match` của phase1, nhãn báo cáo ghi rõ
`phase2_baseline_demo_NOT_lora`:

- Tổng: 3.000 câu, 581 đúng / 1.217 sai / 1.202 abstain, PA tổng **−0,212**.
- Holdout: 592 câu, 54 đúng / 265 sai, PA holdout **−0,3564**.
- Kết luận cổng: **REJECT** (PA holdout bằng baseline, không tăng).
  Đây là kết quả kỳ vọng của minh hoạ: submission phase2 không thể
  "thắng" chính baseline của nó, và minh hoạ chứng minh cổng hoạt
  động đúng trước khi có submission LoRA thật.

### 5.4. Kiểm tra comment tiếng Việt bằng AST

Đã chạy script kiểm tra AST trên cả ba file Python: mọi module, class
và hàm public đều có docstring chứa ký tự tiếng Việt — **pass,
không có trường hợp thiếu**.

## 6. Những gì còn thiếu để train thật

1. **Chọn base model** ≤ 15B, weights public trước 01/10/2026,
   licence cho phép dùng, kiểm chứng từ nguồn chính thức; ghi tên +
   licence + số tham số vào `config.json` (thay `null`), và điền
   `target_modules` đúng kiến trúc model đó.
2. **Cài môi trường train**: torch/transformers/peft (và CUDA tương
   thích nếu train trên GPU) trên máy có đủ VRAM/RAM; kiểm tra
   `train_lora.py --dry-run` báo đủ thư viện.
3. **Tạo dataset đầy đủ**: chạy generator trên toàn bộ train
   (`--limit 0`), không chỉ mẫu `biz_101`, rồi cập nhật
   `training_data` trong config; kiểm tra tỉ lệ verified và độ phủ
   theo shop/type trước khi train (mẫu hiện tại chỉ có 1 shop nên
   chưa đại diện cho holdout khó).
4. **Hoàn thiện bước nạp model/tokenizer** trong `train_lora.py`
   (hiện bị chặn tường minh ở cuối hàm `train` vì phụ thuộc kiến trúc
   model đã chọn), kèm mask token prompt khỏi loss; kiểm tra trên
   một batch nhỏ trước khi chạy toàn bộ.
5. **Chạy train, sinh submission bằng pipeline có parser LoRA,
   chấm bằng `evaluate_lora.py`** trên toàn bộ train, và chỉ giữ LoRA
   khi cổng ở mục 1 trả **KEEP** (PA holdout tăng, PA tổng không giảm
   quá 0,05). Nếu REJECT thì bỏ adapter, không đưa vào Docker.
6. **Không đưa LoRA vào Docker** cho đến khi cổng KEEP thỏa và đã có
   kiểm tra tái lập; cho đến lúc đó, pipeline phase2 hiện tại vẫn là
   baseline được giữ nguyên.

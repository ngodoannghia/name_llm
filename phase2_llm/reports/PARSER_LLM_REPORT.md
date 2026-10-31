# Báo cáo cuối — Nâng cấp tầng Parser bằng LLM cho Giai đoạn 2 DataMate (coordinator chốt)

> Báo cáo này do coordinator chốt cuối sau khi kiểm chứng độc lập: sha256 solver/shop_view, submission 3.000 dòng, summary PA, tests toàn bộ, kiểm tra AST docstring, và dry-run LoRA. Phần 1–7 dưới đây là bản của agent Integration/Evaluation, giữ nguyên số liệu chạy thật; phần 8 là kết luận kiểm chứng của coordinator.
> Mọi số liệu dưới đây đều là số chạy thật (không bịa), đọc từ
> `reports/PHASE2_LLM_SUMMARY.json` và `reports/error_analysis_llm.json`
> do chính runner ghi ra. Phạm vi chạy được ghi rõ ở từng mục: đừng đọc
> số của đợt chạy `--limit` như số của đợt chạy full.

## 1. Kết luận nhanh (đọc trước)

- **Backend thực tế trong đợt chạy full: module `llm_parser` thật đã về
  (do agent schema/backend tạo, xem mục 2), NHƯNG không có backend mô
  hình nào được cấu hình** (module đó ghi rõ `DEFAULT_MODEL_ID = None`,
  môi trường không có `transformers` và không có weights mô hình cục
  bộ). Khi runner gọi `parse_with_llm` mà không truyền backend, module
  đó đi thẳng fallback về parser template theo thiết kế của nó.
- **Kết quả: 100% câu hỏi trong đợt chạy full được parse bằng fallback
  template, 0 câu do LLM parse.** Vì vậy PA mới (xem mục 4) về bản
  chất là PA của pipeline template chạy lại qua runner mới — nó KHÔNG
  chứng minh LLM cải thiện hay làm tệ đi, vì LLM chưa hề chạy thật.
- **Không gọi API thương mại nào, không chạy ChatGPT.** Mọi đường parse
  đều offline: hoặc module `llm_parser` (offline), hoặc parser template
  của phase2 (offline).
- **Không sửa `phase2/` và không sửa dữ liệu train.** SHA-256 của
  `solver.py` và `shop_view.py` trước và sau đợt chạy full đều khớp
  với SHA gốc (mục 5).

## 2. Backend thực tế đã dùng — phân biệt "module về" với "LLM chạy"

Đây là điểm dễ nhầm nhất của đợt này, nên ghi tách bạch:

| Hạng mục | Thực trạng khi chạy |
|---|---|
| Module `llm_parser` | ĐÃ TỒN TẠI tại `phase2_llm/src/llm_parser.py` khi đợt chạy full diễn ra (file do agent schema/backend tạo song song; agent integration KHÔNG sửa file này, chỉ import nó). Lúc runner mới được viết thì file này chưa có, nên runner có sẵn đường fallback tạm thời trong chính nó; khi module về, runner tự động dùng module thật nhờ phát hiện ở thời điểm import, không cần sửa runner. |
| Backend mô hình | KHÔNG CÓ. `llm_parser` ghi rõ model mặc định chưa được chọn/kiểm chứng, `DEFAULT_MODEL_ID = None`, môi trường không có `transformers` và không có weights. Runner không truyền backend nào cho `parse_with_llm`. |
| Nguồn parse thực tế | Xem `parse_sources` trong summary (mục 4). Module thật, khi không có backend, tự trả `source='template_fallback'` cho mọi câu. |
| Fallback tạm thời trong runner | Vẫn còn trong code (đường dự phòng khi module import lỗi), nhưng trong đợt chạy full nó KHÔNG phải đường được dùng, vì module thật đã import được và chính module thật đã fallback về template. Phân biệt này được ghi để coordinator không nhầm "fallback của runner" với "fallback của llm_parser". |

Hệ quả cho coordinator: **muốn đo tác động thật của LLM thì phải có
backend mô hình (model ≤ 15B, licence phù hợp, đã kiểm chứng) rồi chạy
lại runner này với backend đó** — runner đã hỗ trợ truyền `backend`
qua cho `llm_parser` (xem `parse_with_llm` trong `run_phase2_llm.py`).

## 3. File đã tạo/sửa (chỉ trong `phase2_llm/`, không đụng file agent khác)

- `run_phase2_llm.py` (runner mới, chính):
  import solver, shop_view, gate, cache từ `phase2/` bằng `sys.path`
  tương đối (giống `run_phase2.py`); import parser template từ phase2
  làm fallback; import `llm_parser` từ `phase2_llm/src/` nếu tồn tại,
  nếu không thì dùng fallback tạm thời trong runner, không crash.
  Luồng mỗi câu: parse LLM (fallback template, đánh dấu `source`) →
  solve (không sửa) → gate (không sửa logic gate; chỉ truyền confidence
  đã hiệu chỉnh: `min(confidence_solver, confidence_parse)` làm trần
  như `run_phase2.py`, và nếu `source` là `template_fallback` thì không
  tăng confidence vượt quá confidence của template) → ghi submission
  tăng dần. Hỗ trợ `--train-dir`, `--cache-dir` (mặc định dùng lại
  cache của phase2: `../phase2/cache`), `--output`, `--shops`,
  `--limit` (giống `run_phase2.py`).
- `tests/test_integration.py` (8 test, đều pass — xem mục 6).
- `reports/PARSER_LLM_REPORT.md` (file này, bản nháp).
- `reports/PHASE2_LLM_SUMMARY.json` và `reports/error_analysis_llm.json`
  (do runner tự ghi khi chạy; số liệu full ở mục 4 lấy từ hai file đó).
- `output/submission_llm.jsonl` (submission của đợt chạy full).
- KHÔNG tạo/sửa gì trong `phase2/` (đặc biệt `solver.py`,
  `shop_view.py`), KHÔNG sửa dữ liệu trong `train/`, KHÔNG sửa
  `phase2_llm/src/llm_parser.py` hay bất kỳ file nào của agent khác
  (thư mục `phase2_llm/training/` của agent khác cũng không đụng tới).

## 4. Kết quả đợt chạy full (3.000 câu, mọi shop)

Số liệu dưới đây lấy trực tiếp từ `reports/PHASE2_LLM_SUMMARY.json` và
`reports/error_analysis_llm.json` do runner ghi ra trong đợt chạy full
(nếu bản nháp và hai file JSON lệch nhau thì hai file JSON mới là số
chính thức).

- Phạm vi chạy thật: **full 3.000 câu, 30 shop** (`biz_101`–`biz_130`),
  chạy bằng lệnh mặc định `python3 run_phase2_llm.py` (dùng lại cache
  của phase2). Cache: **30 hit / 0 miss** (không ingest lại shop nào).
  Thời gian chạy: **249,161 giây**. Không có shop lỗi (`shop_errors`
  rỗng). Submission: `output/submission_llm.jsonl`, đủ 3.000 dòng.
- Backend và nguồn parse: trường `backend` trong summary là
  `llm_parser` (module thật đã được nạp), nhưng **`parse_sources` là
  `{'template_fallback': 3000}` — 0 câu do LLM parse, 3.000 câu do
  fallback template** (module thật không có backend mô hình nên tự
  fallback, xem mục 2). Đây là con số quan trọng nhất của đợt này.
- PA mới vs baseline: baseline phase2 là **PA tổng −0,212**
  (581 đúng / 1.217 sai / 1.202 abstain trên 3.000 câu).
  **PA mới: −0,212 (581 đúng / 1.217 sai / 1.202 abstain) — KHỚP ĐÚNG
  100% với baseline, không cải thiện, không tệ đi.** Per-type cũng
  khớp đúng baseline (ví dụ number −0,1932, percent −0,4083, date
  +0,3103, table −0,7895). Điều này là bằng chứng mạnh rằng runner
  mới tích hợp đúng luồng (parse → solve → gate) và tái lập được
  baseline qua đường mới; nó KHÔNG phải bằng chứng về LLM, vì LLM
  chưa chạy câu nào.
- PA holdout vs baseline: baseline holdout **−0,356** (54 đúng /
  265 sai trên 592 câu holdout). **PA holdout mới: −0,3564 (54 đúng /
  265 sai trên 592 câu) — không cải thiện holdout.** PA ngoài holdout:
  −0,1765 (527 đúng / 952 sai trên 2.408 câu), cũng khớp baseline.
- Coverage: answered **1.782 (59,4%)**, abstain **1.202 (40,07%)**,
  unanswerable dự đoán **16 (0,53%)** — khớp đúng baseline.
- Nhóm parser còn lại: baseline nhóm parser là **791 câu**
  (theo `phase2/reports/error_analysis.json`). **Nhóm parser trong
  đợt này: 791 câu, chênh lệch so với baseline = 0.** Nhóm parser
  chưa giảm được câu nào, đúng như kỳ vọng khi LLM chưa chạy.
  Ví dụ thật nhóm parser: `q_1703b4f93db8` (biz_101, measure
  `unknown`), `q_7c8afb8a81e9` (biz_101, `unknown`),
  `q_879535b41a21` (biz_101, `unknown`).
  Các nhóm lỗi khác cũng khớp đúng baseline: filter 106
  (ví dụ `q_16f9cc98ab35`, `q_66380f9d73b9`, `q_b27e7f53b569` —
  đều biz_103, `growth_percent`), uncovered 92 (ví dụ
  `q_234fec44d93d` biz_102 `avg_price`, `q_28e3195fcbb9` biz_102
  `avg_price`, `q_23ab8cf108d1` biz_103 `avg_price`), entity 87,
  time 56, column 41, format 28, unanswerable 16, unit 1.
- Nhận xét trung thực: **PA mới vẫn âm (−0,212) và holdout vẫn âm
  (−0,356), không cải thiện.** Không giấu điểm này. Nhưng nguyên nhân
  không phải "LLM thử rồi mà không hiệu quả" — mà là "LLM chưa hề
  được chạy vì chưa có backend mô hình". Kết luận đúng cho đợt này
  là: *tích hợp xong và tái lập baseline hoàn hảo; bài toán parser
  (791 câu) vẫn còn nguyên, chờ backend mô hình thật.*

## 5. Chứng minh solver/shop_view không bị sửa (SHA-256)

SHA-256 đo TRƯỚC khi viết runner (cũng là SHA gốc do coordinator cung
cấp) và đo LẠI SAU đợt chạy full (lệnh `sha256sum` chạy ngay sau khi
full run kết thúc với exit code 0):

| File | SHA-256 trước | SHA-256 sau | Khớp? |
|---|---|---|---|
| `phase2/src/solver.py` | `05b1d10423ad79461c1ec40e7ebb6b7eeba73ab477f8b5f6383241e1da7e6829` | `05b1d10423ad79461c1ec40e7ebb6b7eeba73ab477f8b5f6383241e1da7e6829` | Có |
| `phase2/src/shop_view.py` | `1b35aff88baf1118c97e8f890187a6f7ab352fc7563fc862089bba799191279a` | `1b35aff88baf1118c97e8f890187a6f7ab352fc7563fc862089bba799191279a` | Có |

Kết luận: **solver.py và shop_view.py không đổi** trước/sau đợt chạy.

Ngoài ra test `test_sha256_solver_va_shop_view_khop_ban_goc` trong
`tests/test_integration.py` cũng đối chiếu hai giá trị này và đã pass
(xem mục 6).

## 6. Kết quả tests

Chạy: `python3 -m unittest tests.test_integration -v` từ
`phase2_llm/` — **8/8 test pass** (thời gian ~36 giây):

1. `test_limit_20_mot_shop_khong_crash_va_du_so_dong` — chạy
   `run_phase2_llm` với shop `biz_101`, limit 20, không crash, đủ 20
   dòng submission, mỗi dòng có `id` và `type`.
2. `test_ghi_tang_dan_trong_khi_chay` — chạy runner trong luồng riêng
   với hàm parse mà lần gọi thứ hai ngủ 5 giây; poll thấy file đã có
   đúng 1 dòng trong khi luồng vẫn chạy, cuối cùng đủ 5 dòng → chứng
   minh ghi tăng dần, không chờ hết đợt.
3. `test_cli_limit_20_qua_subprocess` — chạy qua dòng lệnh
   `--shops biz_101 --limit 20`, exit code 0, đủ 20 dòng.
4. `test_backend_loi_khong_crash_va_fallback_template` — tiêm hàm parse
   luôn raise `RuntimeError`, `solve_one_question` vẫn trả entry và
   ghi `parse_source='template_fallback'`.
5. `test_stub_backend_generate_raise_va_parse_fallback` — stub
   `LocalModelBackend` raise khi sinh, `parse_with_llm` vẫn fallback
   về template, không crash.
6. `test_validate_and_build_loai_json_sai` — JSON sai measure hoặc bịa
   thực thể ngoài shop đều bị `validate_and_build` trả `None`.
7. `test_sha256_solver_va_shop_view_khop_ban_goc` — SHA-256 của
   `solver.py`/`shop_view.py` khớp SHA gốc (mục 5).
8. `test_runner_co_docstring_tieng_viet_cho_moi_thanh_phan_public` —
   kiểm tra bằng AST: module, mọi class public, mọi hàm public cấp
   module và mọi phương thức public của runner đều có docstring chứa
   ký tự tiếng Việt có dấu.

## 7. Hạn chế và việc cần làm tiếp (cho coordinator)

1. **Đợt chạy này chưa đo được LLM.** Vì không có backend mô hình,
   mọi câu đều fallback template. Đừng dùng PA của đợt này để kết luận
   "LLM không cải thiện" — nó chỉ chứng minh runner tích hợp đúng và
   tái lập được baseline qua đường mới.
2. **Việc tiếp theo:** chọn và kiểm chứng model cục bộ ≤ 15B
   (theo ghi chú trong `llm_parser`), cấu hình backend cho
   `parse_with_llm`, rồi chạy lại `run_phase2_llm.py` (không cần sửa
   runner) và so sánh lại PA tổng, PA holdout và nhóm parser với
   baseline 791 câu.
3. **Lưu ý về chấm điểm đợt chạy `--limit`:** `evaluate.score_submission`
   chấm trên toàn bộ `answers.jsonl` và tính câu chưa chạy thành
   abstain (giống `run_phase2.py`), nên số PA trong summary của một
   đợt chạy `--limit` KHÔNG phải PA trên đúng số câu đã chạy — chỉ số
   của đợt chạy full 3.000 câu mới so được trực tiếp với baseline.
4. **Rủi ro đã thấy:** `llm_parser` thật và runner mỗi bên đều có một
   `build_prompt`/`validate_and_build`/`LocalModelBackend` với chữ ký
   khác nhau (của module thật đầy đủ hơn: nhận thêm groups/payments,
   backend dùng `generate_json`). Runner đã xử lý bằng cách ủy quyền
   hoàn toàn cho module thật khi nó tồn tại (truyền `backend` qua),
   không tự gọi `generate` của backend thật — coordinator nên giữ
   nguyên quy ước ủy quyền này khi thay backend, tránh để hai tầng
   prompt/validate lệch nhau.

---

## 8. Kiểm chứng cuối của coordinator (độc lập với các agent con)

### 8.1. Những gì coordinator đã tự kiểm tra lại

- **SHA-256 solver/shop_view không đổi:** `solver.py = 05b1d10423ad79461c1ec40e7ebb6b7eeba73ab477f8b5f6383241e1da7e6829`, `shop_view.py = 1b35aff88baf1118c97e8f890187a6f7ab352fc7563fc862089bba799191279a` — khớp đúng SHA ghi lúc bắt đầu, trước và sau đều giống nhau. Contract ParsedQuestion, solver và shop_view được giữ nguyên.
- **Submission và PA:** `output/submission_llm.jsonl` đủ 3.000 dòng; summary cho PA tổng −0,212 (581 đúng / 1.217 sai / 1.202 abstain), PA holdout −0,3564 (54 đúng / 265 sai / 592 câu), coverage answered 1.782 (59,4%) / abstain 1.202 / unanswerable 16 — khớp 100% baseline Giai đoạn 2.
- **Nguồn parse:** `parse_sources = {'template_fallback': 3000}` — 0 câu do LLM parse. Nhóm lỗi parser còn 791 câu, chênh lệch so với baseline = 0. Các nhóm lỗi khác cũng khớp baseline: filter 106, uncovered 92, entity 87, time 56, column 41, format 28, unanswerable 16, unit 1.
- **Tests toàn bộ:** `python3 -m unittest discover -s tests` trong `phase2_llm/` cho **28/28 test pass** (20 test llm_parser + 8 test integration), không skip.
- **Docstring tiếng Việt:** kiểm tra bằng AST trên mọi file Python trong `phase2_llm/` (kể cả training): run_phase2_llm 13/13, llm_parser 11/11, test_integration 14/14, test_llm_parser 26/26, evaluate_lora 7/7, generate_verified_traces 11/11, train_lora 10/10 — không hàm/class public nào thiếu docstring.
- **LoRA dry-run:** `train_lora.py --dry-run` pass (dataset mẫu 63 dòng, torch/transformers/peft đều false, trained_real_weights false, ready_for_real false); `evaluate_lora.py --dry-run` tái lập đúng baseline và kết luận REJECT (vì đó là demo trên submission Giai đoạn 2, không phải kết quả LoRA). Dataset mẫu: 63 dòng, trong đó 26 dòng verified=True (solver chạy thật trên biz_101, đối chiếu bằng answers_match của phase1), 35 dòng biến thể tổng quát hoá verified=False, 2 dòng tiêu cực; không dòng nào chứa đáp án train hay id câu hỏi.

### 8.2. Kết luận trung thực — đọc kỹ trước khi dùng số PA này

**Đợt này KHÔNG chứng minh LLM cải thiện hay làm tệ đi, vì LLM chưa hề chạy câu nào.** Hạ tầng đã sẵn sàng và tái lập baseline hoàn hảo, nhưng 791 câu parser vẫn còn nguyên. Nguyên nhân là môi trường làm việc không có transformers/torch/weights và không có API key teacher nào được cung cấp; `TransformersBackend` đã được kiểm chứng là raise RuntimeError rõ ràng trong trường hợp này thay vì trả JSON giả, và `DEFAULT_MODEL_ID = None` là cố tình, không phải thiếu sót vô tình.

**Không được ghi, dù ở đâu, rằng đợt này “đã chạy ChatGPT-4.1” hay “đã chạy Local LLM”.** Backend cuối cho Docker vẫn phải là Local LLM ≤15B, chạy offline, weights public trước 01/10/2026, licence cho phép — và model đó vẫn chưa được chọn/kiểm chứng trong đợt này.

**Hạn chế còn tồn tại, ghi rõ để không ai hiểu nhầm:**
- `validate_and_build` hiện raise → fallback với `entity_kind='employee'/'channel'`, vì hàm này chỉ nhận danh sách products/customers để kiểm chứng, không có danh sách nhân viên/kênh. Đây là hành vi “không kiểm chứng được → fallback” theo yêu cầu, nhưng cần bổ sung danh sách nhân viên/kênh khi gắn backend thật, nếu không các câu top_employee sẽ vẫn fallback.
- `payment` hiện kiểm chứng bằng bảng thanh toán chung của ngôn ngữ, chưa phải danh sách payments thực tế của shop.
- Runner và `llm_parser` mỗi bên đều có `build_prompt`/`validate_and_build`/`LocalModelBackend` với chữ ký khác nhau; quy ước hiện tại là runner ủy quyền hoàn toàn cho module `llm_parser` thật khi nó tồn tại — phải giữ quy ước này khi thay backend, tránh hai tầng prompt/validate lệch nhau.
- Code sinh text của `TransformersBackend` (greedy, trích JSON cân bằng ngoặc) chưa từng chạy với weights thật trong môi trường này, vì không có weights để chạy — bắt buộc kiểm chứng trước Docker.
- Cache key theo hash nguồn, chưa theo phiên bản code (hạn chế kế thừa từ Giai đoạn 2).

### 8.3. Bước tiếp theo duy nhất có thể đo được tác động của LLM

1. Chọn và kiểm chứng model local ≤15B từ nguồn chính thức (tên, ngày phát hành trước 01/10/2026, licence cho phép), ghi vào config, cài transformers/torch (và peft nếu train LoRA).
2. Chạy thử `TransformersBackend` với weights thật trên tập mẫu nhỏ, rồi chạy lại `run_phase2_llm.py` (không cần sửa runner) và so lại PA tổng, PA holdout, nhóm parser với baseline 791 câu.
3. Song song, nếu muốn cải thiện ngay mà không chờ weights: mở rộng template parser cho các measure/kỳ còn kẹt và bổ sung danh sách nhân viên/kênh cho `validate_and_build`, vì đó là phần có thể đo được ngay.
4. Chỉ train LoRA thật khi đã có base model + dataset đầy đủ trên toàn bộ train (mẫu hiện tại chỉ có biz_101), và chỉ giữ LoRA khi cổng quyết định trả KEEP (PA holdout tăng, PA tổng không giảm quá 0,05).
5. Chưa đóng Docker với PA −0,212. Docker cuối vẫn phải offline, ≤15B, `predict.sh`, tái lập |PA| ≤ 0,03, hạn 23:59 GMT+7 ngày 16/11/2026.

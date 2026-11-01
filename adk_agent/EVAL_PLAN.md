# Plan: Luồng đánh giá độ chính xác của submission (ADK QA)

> Phạm vi: CHỈ luồng đánh giá. Không bao gồm đóng gói Docker, không bao
> gồm việc sinh câu trả lời (workflow QA là hệ thống được đánh giá, plan
> này chỉ định nghĩa cách đo nó). Plan này được viết theo yêu cầu tập
> trung vào độ chính xác của submission trước khi làm bước khác.

## 1. Mục tiêu

Mỗi lần QA cho ra một `submission.jsonl`, luồng đánh giá phải trả lời
được 4 câu hỏi, không chỉ một con số PA:

1. **PA là bao nhiêu?** — tổng thể, theo shop, theo type, và riêng trên
   holdout (tín hiệu tổng quát hoá).
2. **Sai ở đâu?** — danh sách từng câu sai kèm nguyên nhân (xem §5).
3. **Sửa cái nào trước?** — xếp hạng nguyên nhân theo số câu sai và
   "headroom" (PA sẽ tăng bao nhiêu nếu sửa được nguyên nhân đó).
4. **Có thụt lùi không?** — so với lần chạy trước, câu nào đổi từ đúng
   thành sai (hồi quy), câu nào được sửa.

## 2. Nguyên tắc bất biến

- **Không viết lại bộ chấm đã kiểm chứng.** `phase1/src/evaluate.py`
  đã được kiểm chứng: submission hoàn hảo PA = 1.0, toàn abstain
  PA = 0.0, submission hỏng format PA = 0.9987. Luồng mới chỉ là
  lớp bọc (wrapper) quanh `score_submission` + `answers_match`, không
  sửa logic chấm. Mọi phân tích thêm đều xây trên verdict
  correct/wrong/abstain mà nó trả ra.
- **Đáp án tham chiếu không bao giờ đi vào inference.** QA chỉ thấy câu
  hỏi + schema + rulebook + calendar. Việc nối submission với
  `answers.jsonl` chỉ xảy ra SAU khi inference xong, trong bước đánh
  giá. Log chạy của QA không được chứa đáp án tham chiếu.
- **Chia theo shop, không chia theo câu hỏi.** 118 câu hỏi lặp nguyên
  văn giữa các shop nên mọi thống kê, mọi tập pilot/holdout đều chia
  theo `business_id`. Holdout cố định: biz_105, biz_106, biz_117,
  biz_123, biz_124 (592 câu).
- **Holdout là tín hiệu, không phải mục tiêu tune.** Báo cáo luôn tách
  `holdout_pa` riêng; không được sửa prompt/code để tăng riêng
  holdout rồi coi đó là tiến bộ.
- **Báo cáo luôn kèm số đếm, không chỉ PA.** Pilot nhỏ (ví dụ biz_123
  có 118 câu) thì PA rất nhiễu: 1 câu đổi tương đương ~0.0085 PA.
  Mọi con số PA đều đi kèm correct/wrong/abstain.
- **Sai format = sai.** Theo luật cuộc thi, type không khớp hoặc giá
  trị không đúng định dạng của type đều là wrong, không có điểm từng
  phần. Luồng đánh giá giữ đúng luật này, không tự nới lỏng (ví dụ
  không tự chấp nhận chuỗi số cho type number).

## 3. Đầu vào và đầu ra

Đầu vào của một lần đánh giá:

| File | Nguồn | Vai trò |
| --- | --- | --- |
| `submission.jsonl` | QA (mỗi dòng: id, type, answer) | Thứ được chấm |
| `answers.jsonl` | train (local, không có ở public/private test) | Tham chiếu |
| `questions.jsonl` | train | Lấy `business_id`, `as_of_date`, text câu hỏi |
| `qa_run_log.json` | QA (xem §4) | Chẩn đoán nguyên nhân |

Đầu ra của một lần đánh giá (một thư mục `eval_runs/<run_id>/`):

- `verdicts.jsonl`: một dòng mỗi câu hỏi — id, shop, type tham chiếu,
  verdict, đáp án nộp, đáp án tham chiếu, nguyên nhân (xem §5).
- `report.json`: PA tổng, per_type, per_business, holdout_pa, thống kê
  nguyên nhân, headroom, chi phí/thời gian chạy.
- `report.md`: cùng nội dung, dạng đọc được, để dán vào chat/commit.
- `history.jsonl` (chung, append mỗi lần chạy): run_id, thời gian,
  PA tổng, PA holdout, số câu đúng/sai/abstain — để vẽ tiến trình và
  phát hiện hồi quy.

## 4. Hợp đồng log từng câu hỏi của QA (phải có trước khi QA chạy)

Luồng đánh giá chỉ chẩn đoán được nguyên nhân nếu QA ghi đủ thông tin.
Mỗi bản ghi câu hỏi trong `qa_run_log.json` phải có:

- `id`, `business_id`, `as_of_date`, `question` (text gốc).
- `sql_attempts`: danh sách các lần thử, mỗi lần gồm `sql`,
  `validator_error` (chuỗi rỗng nếu qua), `executed` (true/false).
- `final_sql`: SQL cuối cùng đã chạy thành công (rỗng nếu không có).
- `final_answer`: bản ghi đáp án đã nộp (type + answer).
- `abstain_reason`: một trong `none`, `sql_loop_exhausted`,
  `format_invalid`, `confidence_low`, `unanswerable_detected`,
  `schema_uncertain`, `other` (kèm ghi chú tự do khi là `other`).
- `confidence`: số trong [0, 1] nếu QA có gate (để phân tích hiệu
  chuẩn ở §6.5).
- `model_calls`: số lần gọi model cho câu này (để tính chi phí).

Log này KHÔNG chứa đáp án tham chiếu, KHÔNG chứa dữ liệu dòng bán
hàng (chỉ SQL và đáp án cuối — giống nguyên tắc của `log_openai.json`
trước đây).

## 5. Các tầng đánh giá (thứ tự xây dựng)

### Tầng 0 — Kiểm tra submission trước khi chấm

- Mỗi dòng parse được JSON, có đủ `id`, `type`, `answer` (trừ abstain
  và unanswerable theo đúng format submission).
- `type` phải thuộc tập hợp lệ; `id` không trùng; câu hỏi thiếu trong
  submission được tính là abstain (không crash).
- Thống kê độ phủ: số câu đã nộp / tổng số câu theo từng shop.
- Tầng này chỉ báo lỗi, không tự sửa submission.

### Tầng 1 — Chấm điểm (dùng lại phase1, không sửa)

- Gọi `score_submission` với submission + answers + questions.
- Lấy ra: PA tổng, correct/wrong/abstain, per_type, per_business,
  holdout_pa.
- Kiểm tra sanity mỗi lần chạy: bộ chấm vẫn cho PA = 1.0 trên
  submission hoàn hảo và 0.0 trên toàn abstain (test đã có của
  phase1, chạy lại như một bước của luồng, không phải niềm tin).

### Tầng 2 — Danh sách câu sai + câu abstain có thể cứu

- Từ `verdicts.jsonl`, trích 2 danh sách để xem bằng mắt:
  - Câu wrong: kèm đáp án nộp vs tham chiếu, SQL cuối, lỗi validator.
  - Câu abstain: kèm `abstain_reason` — nhóm này là "tiềm năng":
    mỗi câu abstain mà sửa được thành đúng sẽ tăng PA thêm 1/total,
    trong khi mỗi câu wrong sửa thành đúng tăng 2/total (bớt 1 điểm
    trừ, thêm 1 điểm cộng). Vì vậy thứ tự ưu tiên sửa là: wrong do
    nguyên nhân sửa được trước, rồi mới tới abstain.
- Mỗi danh sách giới hạn hiển thị (ví dụ 50 câu đầu theo shop) nhưng
  file đầy đủ luôn được lưu.

### Tầng 3 — Phân rã theo chiều

Ngoài per_type và per_business đã có, báo cáo thêm:

- Theo `abstain_reason` (chỉ cho nhóm abstain).
- Theo answerable vs unanswerable (tham chiếu là unanswerable thì
  nộp unanswerable mới là đúng; nộp abstain ở đây là bỏ lỡ, nộp đáp
  án khác là sai).
- Theo nhóm câu hỏi suy ra từ text (ví dụ nhóm chứa "doanh thu",
  "công nợ", "bán chạy", "tồn kho", "giá ngày") — phân nhóm bằng
  từ khoá đơn giản trên text câu hỏi, chỉ để định hướng xem, không
  dùng để tune theo nhóm.
- Theo kỳ hỏi: tuyệt đối (ngày/tháng cụ thể) vs tương đối ("tháng
  này", "tuần trước", "quý trước") — vì rulebook xử lý kỳ tương đối
  là nguồn sai đã biết từ phase2.

### Tầng 4 — Phân loại nguyên nhân sai (error taxonomy)

Mỗi câu wrong được gán đúng một nguyên nhân chính, theo thứ tự ưu
tiên sau (câu nào vi phạm nhiều lỗi thì lấy lỗi đầu tiên gặp trong
luồng chạy):

1. `sql_rejected`: hết 3 lần thử mà SQL vẫn bị validator bác (xem
   `sql_attempts` — lỗi cuối cùng là gì: sai bảng/cột, sai cú pháp,
   vi phạm allow-list, timeout).
2. `sql_wrong_result`: SQL chạy được nhưng kết quả sai — chia tiếp
   nếu đọc được từ SQL: sai bảng/join (ví dụ cộng cả bảng lô),
   thiếu lọc trạng thái (đơn huỷ/đơn trả), sai kỳ, sai đơn vị.
3. `format_wrong`: đáp án đúng số nhưng sai type/format (ví dụ trả
   chuỗi số cho type number, sai định dạng ngày, list thiếu cột).
4. `rulebook_trap`: đáp án sai vì vi phạm một quy tắc trong rulebook
   (doanh thu tính cả đơn huỷ, công nợ tính cả COD, giá lấy giá hiện
   tại thay vì giá theo ngày...). Nhóm này cần đối chiếu thủ công
   với rulebook khi xem danh sách câu sai, không tự gán hoàn toàn
   bằng code.
5. `gate_wrong`: QA đáng lẽ abstain nhưng đã nộp (confidence cao mà
   sai) — phát hiện qua `confidence` cao + verdict wrong.
6. `unknown`: không xếp được vào nhóm trên — nhóm này phải nhỏ dần
   theo thời gian; nếu nó lớn thì taxonomy thiếu.

Báo cáo xếp hạng các nguyên nhân theo số câu sai và headroom tương
ứng (số câu × 2/total PA nếu sửa hết). Đây là đầu ra quan trọng nhất
cho việc quyết định sửa cái gì tiếp theo.

### Tầng 5 — So sánh với lần chạy trước (hồi quy)

- Với mỗi run mới, so sánh `verdicts.jsonl` với run trước đó theo
  từng id câu hỏi, đếm 4 nhóm chuyển đổi: đúng→sai (hồi quy, báo
  động), sai→đúng (tiến bộ), abstain→đúng/sai, đúng/sai→abstain.
- Báo cáo PA delta tổng, delta theo từng shop và từng type. Một lần
  chạy chỉ được coi là "tiến bộ" khi PA tổng tăng VÀ không có hồi
  quy lớn ở shop/type nào (ngưỡng báo động: một shop mất hơn 5 câu
  đúng so với lần trước thì phải xem lại trước khi nhận kết quả).
- So sánh với 2 baseline đã có (không phải để khoe, mà để biết mình
  đang đứng ở đâu): template phase2 (PA −0.212 trên 3.000 câu) và
  phase2_llm 3 shop (157 đúng / 90 sai / 31 abstain). Lưu ý: sau khi
  bỏ fallback template, PA không còn so 1-1 được với baseline cũ
  (những câu template từng "cứu" giờ thành abstain), nên báo cáo
  phải nêu rõ điểm này khi so sánh.

### Tầng 6 — Hiệu chuẩn gate và chi phí

- **Hiệu chuẩn (calibration):** vẽ confidence của QA so với tỉ lệ
  đúng thực tế theo từng khoảng confidence. Bài học từ phase2: solver
  trả confidence 1.0 cho phần lớn câu sai, nên siết gate một mình
  không sửa được PA. Chỉ khi đường hiệu chuẩn cho thấy confidence
  cao đi kèm tỉ lệ đúng cao thì mới nên điều chỉnh ngưỡng gate
  (hiện ~0.65) dựa trên dữ liệu này, và mọi lần đổi ngưỡng đều phải
  đo lại PA trên cùng một tập câu.
- **Chi phí:** tổng số model call, trung bình call/câu, thời gian
  chạy theo shop. Pilot biz_123 (118 câu) phải báo cáo được 3 số này
  để ước tính chi phí chạy 3.000 câu và chạy 30 shop ingest.

## 6. Thứ tự triển khai (không cần QA xong mới làm được)

1. **Eval runner + tầng 0–1–2:** bọc `score_submission`, sinh
   `verdicts.jsonl` + `report.json/md`. Kiểm chứng ngay bằng 2
   submission đã có: submission template phase2 (3.000 câu) và
   submission phase2_llm 3 shop (278 câu) — phải tái tạo đúng PA
   đã biết (−0.212 và 0.241 trên tập chạy).
2. **Tầng 3–4:** phân rã theo chiều + taxonomy; với submission cũ,
   taxonomy sẽ thiếu dữ liệu SQL (vì log cũ không có `sql_attempts`)
   nên các câu đó rơi vào `unknown` — đó là bằng chứng vì sao hợp
   đồng log §4 phải có trước khi QA chạy.
3. **Tầng 5–6:** lịch sử chạy + hồi quy + hiệu chuẩn; kiểm chứng
   bằng cách chạy eval 2 lần trên 2 submission cũ và đọc được delta
   có ý nghĩa.
4. **Nối QA:** khi workflow QA chạy pilot biz_123, nó chỉ cần ghi
   log đúng §4 là luồng đánh giá chạy được ngay, không sửa lại
   evaluator.

## 7. Tiêu chí nghiệm thu của luồng đánh giá

- Chạy lại test sanity của phase1: perfect PA = 1.0, abstain = 0.0.
- Tái tạo đúng PA của 2 submission cũ (sai số 0).
- Một lần chạy pilot biz_123 cho ra đủ 4 đầu ra §3, danh sách câu
  sai truy vết được tới SQL và lỗi validator, và báo cáo xếp hạng
  nguyên nhân kèm headroom.
- Hai lần chạy liên tiếp cho ra được báo cáo hồi quy câu-level
  (không chỉ delta PA tổng).
- Không có bước nào trong luồng đọc đáp án tham chiếu trước hoặc
  trong lúc inference; scorer chỉ chạy sau khi submission đã chốt.

## 8. Việc KHÔNG làm trong plan này

- Không đóng gói Docker, không chọn model offline cuối (đó là bước
  sau, khi QA + đánh giá đã ổn).
- Không finetune/LoRA (chỉ xét sau khi taxonomy cho thấy lỗi nằm ở
  khả năng hiểu của model, theo gate MASTER_PLAN §8).
- Không sửa `phase1/src/evaluate.py` — mọi mở rộng nằm ở wrapper
  mới, để bộ chấm đã kiểm chứng giữ nguyên.
- Không dùng holdout để tune prompt/code; holdout chỉ để báo cáo.

# Plan: Luồng hoàn thiện sau khi ingest xong

> Phạm vi: toàn bộ luồng làm việc SAU khi ingest cho ra DB + schema,
> cho tới khi submission đạt độ chính xác chấp nhận được. Chưa bao gồm
> đóng gói Docker và chưa bao gồm chọn model offline cuối — đó là bước
> sau, khi luồng này đã ổn.

## 0. Điểm xuất phát: thế nào là "ingest xong"

Một shop được coi là ingest xong khi có đủ 3 thứ và qua 3 kiểm tra:

- 1 file SQLite mở được, bảng/cột đúng dữ liệu đã làm sạch.
- 1 file schema JSON, tập bảng trong schema khớp đúng tập bảng trong DB.
- 1 log ingest không có shop FAILED.

Lưu ý: "ingest xong" không có nghĩa là ingest không bao giờ sửa nữa.
Lỗi ingest chỉ lộ ra khi QA chạy và đánh giá chỉ ra (ví dụ schema mô
tả sai, merge sai, cột làm sạch sai). Luồng dưới đây vì vậy có vòng
quay lại sửa ingest — đó chính là phần "hoàn thiện".

## 1. QA trả lời câu hỏi (Text-to-SQL theo từng shop)

Đầu vào của QA cho một shop: DB + schema JSON của shop đó, rulebook
của split đang chạy, calendar, và các câu hỏi của shop đó. QA không
đọc lại file thô, không đọc đáp án tham chiếu.

Các bước cho mỗi câu hỏi:

1. Nhóm câu hỏi theo `business_id`; load một shop một lần, trả lời
   hết câu của shop đó rồi giải phóng, mới sang shop tiếp.
2. Prompt gồm: schema riêng của shop (bảng, cột, mô tả tiếng Việt,
   role, join_hints, trap notes, tối đa 5 mẫu mỗi cột) + rulebook +
   calendar + câu hỏi + `as_of_date`. Không ép format chung nào.
3. Vòng lặp sinh SQL: SqlGenerator (LLM) viết SQL → validator bằng
   code kiểm (chỉ SELECT, chỉ bảng/cột có trong schema, có timeout)
   → chạy thử trên SQLite → lỗi thì đưa exception lại cho agent
   viết lại, tối đa 3 lần → hết lượt thì abstain. Không template
   fallback, không đoán số.
4. Hậu xử lý bằng code: format đáp án đúng type (number, percent,
   string, date, boolean, list, table), kiểm tra format, gate
   confidence (~0.65 ban đầu), phát hiện `unanswerable` khi kỳ hỏi
   ngoài khoảng dữ liệu hoặc loại thông tin không có trong shop.
5. Ghi `submission.jsonl` và `qa_run_log.json` đúng hợp đồng log đã
   định trong `EVAL_PLAN.md` §4 (SQL từng lần thử, lỗi validator, lý
   do abstain, confidence, số model call). Không có log này thì bước
   đánh giá không chẩn đoán được nguyên nhân sai.

## 2. Đánh giá submission (xem chi tiết trong EVAL_PLAN.md)

Chạy luồng đánh giá trên submission vừa sinh ra:

- Chấm PA bằng bộ chấm đã kiểm chứng của phase1 (không viết lại).
- PA tổng, theo shop, theo type, và riêng holdout (biz_105, biz_106,
  biz_117, biz_123, biz_124) làm tín hiệu tổng quát hoá.
- Danh sách câu sai + câu abstain, truy vết được tới SQL và lỗi
  validator.
- Phân loại nguyên nhân sai và xếp hạng theo headroom (câu wrong
  sửa thành đúng tăng 2/total PA, câu abstain sửa thành đúng tăng
  1/total).
- So sánh câu-level với lần chạy trước để phát hiện hồi quy.

## 3. Phân tích lỗi và quyết định sửa ở đâu

Mỗi câu sai được quy về đúng một trong ba nơi sửa (thứ tự xem xét
theo headroom, nhóm nào nhiều câu nhất thì sửa trước):

### 3a. Lỗi ingest — quay lại sửa ingest, chạy lại ingest shop đó

Dấu hiệu: schema mô tả sai/thiếu (role sai, join_hints sai, trap
note thiếu), merge sai (bảng đáng gộp mà tách, hoặc gộp nhầm vai
trò), làm sạch sai (cột tiền không parse, ngày không chuẩn hoá,
số lượng chưa quy đổi đơn vị). Sửa code ingest hoặc skill tương
ứng, chạy lại ingest cho shop bị ảnh hưởng, rồi chạy lại QA trên
shop đó và đo lại.

Ví dụ đã biết trước sẽ gặp: `conversion_factor` hiện vẫn là TEXT
chưa format thành số, `date_range` trong schema vẫn trống — nếu QA
cần quy đổi đơn vị hoặc lọc theo khoảng ngày mà sai, đó là lỗi
ingest, không phải lỗi QA.

### 3b. Lỗi QA — sửa QA, không đụng tới ingest

Dấu hiệu: SQL sai bảng/join, thiếu lọc trạng thái (đơn huỷ, đơn
trả), sai kỳ (nhất là kỳ tương đối như "tháng này", "tuần trước"),
sai format đáp án, gate sai (confidence cao mà trả lời sai, hoặc
abstain quá nhiều câu trả lời được). Sửa prompt, validator, hoặc
ngưỡng gate — mỗi lần chỉ sửa một thứ, rồi đo lại trên cùng tập
câu để biết sửa đó có thật sự giúp không.

### 3c. Lỗi không sửa được — QA phải abstain/unanswerable cho đúng

Dấu hiệu: câu hỏi thuộc loại thông tin shop không có, hoặc kỳ hỏi
nằm ngoài khoảng dữ liệu. Với nhóm này, "sửa" đúng nghĩa là QA phải
nhận diện được và trả `unanswerable` (khi tham chiếu cũng là
unanswerable) hoặc abstain, thay vì cố trả lời rồi bị trừ điểm.
Nhóm này không được tính vào headroom sửa được.

## 4. Vòng lặp hoàn thiện (mỗi vòng chỉ sửa một nhóm, đo lại rồi mới tiếp)

```
ingest (shop pilot) → QA → đánh giá → phân loại lỗi
        → sửa 1 nhóm nguyên nhân lớn nhất
        → chạy lại QA + đánh giá trên CÙNG tập câu
        → kiểm tra hồi quy (không shop/type nào mất > 5 câu đúng)
        → đạt thì mở rộng, chưa đạt thì lặp lại
```

Nguyên tắc kỷ luật của vòng lặp:

- Một vòng chỉ sửa một nhóm nguyên nhân. Sửa nhiều thứ cùng lúc thì
  không biết cái nào giúp, cái nào hại.
- Mọi lần đo đều trên cùng tập câu pilot cho tới khi quyết định mở
  rộng; đổi tập câu giữa chừng thì PA không so được với nhau.
- Holdout chỉ để báo cáo, không dùng để tune prompt/code.
- Đáp án tham chiếu không bao giờ đi vào prompt hay log của QA.

## 5. Mở rộng dần (để không đốt chi phí API và công sửa)

- **Vòng 1 — pilot biz_123 (118 câu):** ingest thật đã xong, shop
  khó nhất (17 bảng, có bảng lô dễ cộng trùng). Mục tiêu vòng này
  là luồng chạy thông suốt đầu-cuối và taxonomy lỗi có dữ liệu thật,
  chưa cần PA cao.
- **Vòng 2 — 3 shop biz_101, biz_102, biz_103 (278 câu):** có
  baseline cũ để so (phase2_llm: 157 đúng / 90 sai / 31 abstain;
  template: 119 / 72 / 87). Mục tiêu là vượt baseline cũ và kiểm
  chứng vòng lặp sửa có hiệu quả.
- **Vòng 3 — toàn bộ 30 shop, 3.000 câu:** chỉ mở khi vòng 2 ổn.
  Lúc này mới chạy ingest thật cho 29 shop còn lại (ước tính
  ~200 request) và đánh giá full. Chạy ingest hàng loạt trước khi
  QA pilot ổn sẽ phí tiền vì yêu cầu của QA có thể bắt sửa lại
  ingest.

## 6. Tiêu chí "hoàn thiện" (đề xuất, cần chốt trước khi chạy vòng 3)

Luồng này được coi là hoàn thiện khi đồng thời:

- PA tổng dương và PA holdout dương (vượt baseline template −0.212
  một cách rõ ràng, không phải nhiễu của vài câu).
- Tỉ lệ câu wrong được kiểm soát: số câu wrong không còn lớn hơn
  nhiều so với số câu abstain, và nhóm nguyên nhân `unknown` trong
  taxonomy đã nhỏ (hầu hết câu sai đã giải thích được).
- Không có hồi quy lớn ở bất kỳ shop/type nào so với lần chạy tốt
  nhất trước đó.
- Chi phí và thời gian chạy đo được trên pilot cho phép ước tính
  việc chạy lại toàn bộ trong giới hạn chấp nhận được.

Khi đạt các tiêu chí này mới chuyển sang bước tiếp theo (đóng gói
Docker, chọn model offline cuối). Nếu chưa đạt thì vòng lặp §4 tiếp
tục, không nhảy sang Docker để "chốt" một submission chưa tốt.

## 7. Việc KHÔNG làm trong luồng này

- Không đóng gói Docker, không chọn model offline cuối.
- Không finetune/LoRA — chỉ xét sau khi taxonomy cho thấy lỗi nằm ở
  khả năng hiểu của model, theo gate MASTER_PLAN §8.
- Không sửa `phase1/src/evaluate.py` — bộ chấm giữ nguyên, mọi mở
  rộng nằm ở wrapper đánh giá mới.
- Không hard-code theo business_id, tên cột, hay rulebook của split
  train; mọi thứ QA dùng đều lấy từ schema + rulebook + calendar của
  lần chạy hiện tại.

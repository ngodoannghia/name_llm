# name_llm — DataMate (Zalo AI Challenge 2026)

Agent trả lời câu hỏi tiếng Việt trên dữ liệu shop (CSV/Excel/SQLite),
theo kiến trúc hybrid: LLM hiểu câu hỏi, code tính toán và kiểm chứng.

## Cấu trúc

- `phase1/` — Ingest + chuẩn hoá dữ liệu shop, rulebook/calendar loader,
  evaluation harness (PA), test tổng quát hoá.
- `phase2/` — Parser template 4 trục, solver xác định bằng pandas,
  confidence gate 0.65 + format validator + unanswerable detector.
- `phase2_llm/` — Parser LLM (JSON Schema, validation + repair tối đa 2,
  fallback về template), pipeline LoRA, và backend OpenAI **chỉ dùng
  cho phát triển** (`run_phase2_openai.py`, xem cảnh báo dưới).
- `phase1_llm/` — Ingest LLM (gợi ý phân loại bảng, map cột khó,
  đọc README/header khó, luôn có rule-based fallback kiểm chứng).

Mã nguồn viết bằng UTF-8, comment giải thích đầy đủ bằng tiếng Việt.
File `.py` nào cũng có dòng `# -*- coding: utf-8 -*-` ở đầu.

## Chạy end-to-end (bản phát triển OpenAI)

```bash
pip install pandas openpyxl
# Đặt các thư mục phase1/, phase2/, phase2_llm/, phase1_llm/ cạnh train/
# train/ gồm questions.jsonl, answers.jsonl, rulebook.md, calendar.csv, data/
cd phase2_llm
cp .env.example .env   # rồi điền OPENAI_API_KEY vào .env ở máy bạn, không commit file này
python3 run_phase2_openai.py --limit 20 --shops biz_101
python3 run_phase2_openai.py
```

Kết quả: `output/submission_openai.jsonl`, báo cáo ở `reports_openai/`,
log đầy đủ ở `reports_openai/run.log` (từng lần gọi API, từng lần parse,
từng câu hỏi, từng shop).

Bản template không cần key:

```bash
cd phase2
python3 run_phase2.py
```

## Cảnh báo quan trọng — bản Docker nộp cuối

Backend OpenAI trong repo này **chỉ dùng cho phát triển** để sinh trace
kiểm chứng. Bản Docker nộp cuối bắt buộc offline, model cục bộ tổng
≤ 15B, không gọi API thương mại, không tải gì khi chạy. Không đưa
`.env`, key API, hay backend OpenAI vào Docker cuối.

## Trạng thái đã biết

- Baseline template Phase 2: PA tổng −0.212, holdout −0.356
  (581 đúng / 1.217 sai / 1.202 abstain trên 3.000 câu train).
- Đừng đóng gói Docker với PA này; ưu tiên sửa parser + hiệu chỉnh
  confidence trước.

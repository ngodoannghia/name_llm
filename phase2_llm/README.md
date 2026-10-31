# Giai đoạn 2 LLM — Nâng cấp tầng Parser cho DataMate

## Mục đích

Thay thế / nâng cấp tầng parser template của Giai đoạn 2 bằng parser có
Structured Output (JSON Schema), giữ nguyên contract `ParsedQuestion`,
giữ nguyên `solver.py` và `shop_view.py` của Giai đoạn 2, trong thư mục
mới `phase2_llm/` để so sánh PA trước/sau một cách trung thực.

## Kết luận trung thực — đọc trước khi dùng số PA

Đợt này hạ tầng đã sẵn sàng và tái lập baseline hoàn hảo, nhưng **LLM
chưa hề chạy câu nào** trong đợt chạy full, vì môi trường không có
transformers/torch/weights và không có API key teacher nào được cung
cấp. Vì vậy PA mới = PA baseline (−0,212), nhóm lỗi parser vẫn 791 câu.
Đừng ghi rằng đợt này “đã chạy ChatGPT-4.1” hay “đã chạy Local LLM”.
Chi tiết đầy đủ trong `reports/PARSER_LLM_REPORT.md` mục 8.

## Cấu trúc

```
phase2_llm/
  README.md                 # File này
  run_phase2_llm.py         # Runner mới: llm_parser → solver → gate → submission tăng dần
  src/
    llm_parser.py           # JSON Schema, prompt tiếng Việt, validation + repair (tối đa 2 lần),
                            # fallback về template parser, backend LocalModel/Transformers
  training/
    generate_verified_traces.py  # Chỉ giữ trace solver kiểm chứng đúng
    train_lora.py                # Train LoRA, có --dry-run, chưa train thật weights
    evaluate_lora.py             # Cổng quyết định KEEP/REJECT theo PA holdout
    config.json                  # base_model = null (chưa chọn/kiểm chứng)
    data/verified_traces_sample.jsonl  # 63 dòng mẫu (26 verified=True)
    README.md
  tests/
    test_llm_parser.py      # 20 test
    test_integration.py     # 8 test
  output/
    submission_llm.jsonl    # Submission đợt chạy full (3.000 dòng, toàn fallback template)
  reports/
    PARSER_LLM_REPORT.md    # Báo cáo cuối (coordinator chốt)
    PHASE2_LLM_SUMMARY.json
    error_analysis_llm.json
```

## Cách chạy

```bash
cd workspace/datasets/datamate/phase2_llm
# Chạy full (dùng lại cache của phase2, không ingest lại vô ích)
python3 run_phase2_llm.py
# Chạy giới hạn để kiểm thử nhanh
python3 run_phase2_llm.py --shops biz_101 --limit 20
# Tests
python3 -m unittest discover -s tests
# LoRA dry-run (chưa train thật)
python3 training/train_lora.py --dry-run
python3 training/evaluate_lora.py --dry-run
```

## Backend

- `LocalModelBackend` là interface trừu tượng, `TransformersBackend`
  lazy import transformers/torch và raise `RuntimeError` rõ ràng khi
  thiếu gói/weights/model_id, không trả JSON giả.
- `DEFAULT_MODEL_ID = None` là cố tình: trước khi đóng Docker bắt buộc
  phải chọn model ≤15B, weights public trước 01/10/2026, licence cho
  phép, rồi chạy thử backend với weights thật.
- Backend cuối cho Docker phải là Local LLM offline, không gọi API
  thương mại. Teacher model mạnh (nếu có quyền truy cập ở ngoài môi
  trường này) chỉ được dùng ở giai đoạn phát triển để sinh candidate
  trace, và mọi trace phải qua solver kiểm chứng mới được giữ.

## Quy ước tích hợp

- Runner ủy quyền hoàn toàn cho module `llm_parser` thật khi nó tồn
  tại (truyền `backend` qua), không tự gọi `generate` của backend thật,
  để tránh hai tầng prompt/validate lệch nhau.
- Solver và shop_view của Giai đoạn 2 không được sửa; SHA-256 đã kiểm
  chứng trước/sau đều khớp (xem báo cáo mục 5 và 8).
- Khi không có backend, `parse_with_llm` fallback về template parser
  cũ và đánh dấu `source='template_fallback'`; không bao giờ đoán thực
  thể/kỳ để lấp chỗ trống.

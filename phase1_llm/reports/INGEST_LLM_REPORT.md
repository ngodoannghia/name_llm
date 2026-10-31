# Báo cáo Ingest Phase 1 bằng LLM (phase1_llm)

Báo cáo này do `run_phase1_llm.py` sinh tự động từ `phase1_llm_summary.json` của cùng đợt chạy — mọi con số dưới đây đều lấy từ summary đó, không tính lại bằng tay.

## 1. Backend thực tế đã dùng (ghi trung thực)

- **Backend thực tế: `none_fallback` — chưa có map nào do LLM gợi ý trong đợt chạy này.** Mọi bảng đều fallback rule-based (`source='rule_fallback'`), mọi map áp dụng đều là map rule-based của phase1; `llm_added_mappings` bằng 0 là kết quả ĐÚNG của đợt chạy này, không phải lỗi ghi số.
- Module `llm_ingest` đã import được nhưng không bảng nào có `source='llm'` (module tự fallback vì chưa có backend mô hình thật, `DEFAULT_MODEL_ID=None` theo contract mục 2).
- `model_id`: `None` (null nghĩa là chưa chọn/kiểm chứng model ≤15B — không ghi tên model, licence hay ngày phát hành nào chưa kiểm chứng từ nguồn chính thức, theo contract mục 2).
- Module `llm_ingest` có sẵn lúc chạy: `True`.

## 2. Kiến trúc và luồng chạy

1. `load_shop` của phase1 chạy trước cho mỗi shop — giữ nguyên `IngestResult`, `tables`, `profile`, `rows_in`/`rows_out`. Runner KHÔNG chạy lại forward-fill, dedup, hợp nhất hay parse tiền/ngày, nên `rows_in`/`rows_out` phải bằng baseline phase1.
2. Đọc README của shop (nếu có, cắt tối đa 4000 ký tự đầu) rồi gọi `run_llm_ingest_for_shop` của Child 1 (hoặc fallback rule-based toàn shop khi module/backend thiếu).
3. Với mỗi bảng, tính `map_columns(df)` rule-based TRƯỚC; chỉ xét map LLM cho các semantic còn trống. Map LLM trùng semantic hoặc trùng cột với map rule-based thì bị loại, ghi vào `rejected_mappings`/`warnings`, KHÔNG ghi đè âm thầm.
4. Runner kiểm tra lại mọi map LLM: semantic phải nằm trong 37 khóa của `COLUMN_SYNONYMS` và tên cột phải tồn tại thật trong DataFrame của bảng đó; map không qua kiểm tra thì bị loại + warning. Kiểm chứng mẫu giá trị (tiền/ngày/số lượng ≥ 25%) là trách nhiệm của `llm_ingest` theo contract mục 5.
5. Cập nhật hồ sơ shop CHỈ từ map đã kiểm chứng, đơn điệu False -> True (không bao giờ True -> False chỉ vì LLM nói vậy khi dữ liệu thật vẫn có cột đó); delta ghi rõ trong `profile_delta` của từng shop.
6. Ghi `phase1_llm_summary.json` rồi sinh báo cáo này từ summary; hash sha256 của `phase1/src/ingest.py` và `normalizers.py` được tính trước và sau đợt chạy để Child 3 đối chiếu phase1 có bị sửa hay không.

## 3. Cách chạy

```bash
cd ~/workspace/datasets/datamate/phase1_llm
python3 run_phase1_llm.py [--train-dir ../train]
  [--shops biz_101,biz_102] [--limit-shops N]
  [--output reports/phase1_llm_summary.json]
```

Exit 0 chỉ khi không shop nào lỗi, không có map chưa kiểm chứng nào lọt vào kết quả, và hash phase1 trước/sau bằng nhau. Shop lỗi riêng lẻ vẫn được ghi vào summary và đợt chạy vẫn tiếp tục shop khác, nhưng exit sẽ khác 0.

## 4. Tổng quan đợt chạy

- Shop đã chạy: 30 (ok: 30, lỗi: 0).
- Tổng bảng (shop ok): 153.
- Tổng rows_in / rows_out (shop ok): 3997042 / 3982749.
- Rows khớp baseline ở mọi shop ok: `True`.
- Tổng map LLM đã kiểm chứng được áp dụng: 0.
- Tổng map bị loại: 0.
- Tổng fallback: 153.
- Nguồn theo bảng: `{'rule_fallback': 153}`.
- Tổng thời gian: 425.49 giây.
- Hash phase1 thay đổi trong lúc chạy: `False` (ingest.py: `89a61c1111b3de0437e34385f4727dc0ce26ee58c26f497b675831947d375643`, normalizers.py: `7a62cab0575150987a714c23d2c523dd4d37e9dbdda56b7bb33b64487cdd20f8`).

## 5. So sánh theo shop với baseline phase1

| Shop | Bảng | Rows in/out | Baseline mapped | Mapped mới | LLM thêm | Bị loại | Fallback | Nguồn | Profile delta |
|---|---|---|---|---|---|---|---|---|---|
| biz_101 | 1 | 56501/56501 | 10 | 10 | 0 | 0 | 1 | rule_fallback | - |
| biz_102 | 2 | 86106/86106 | 12 | 12 | 0 | 0 | 2 | rule_fallback | - |
| biz_103 | 2 | 45611/45611 | 12 | 12 | 0 | 0 | 2 | rule_fallback | - |
| biz_104 | 2 | 62896/62896 | 12 | 12 | 0 | 0 | 2 | rule_fallback | - |
| biz_105 | 5 | 141748/139526 | 16 | 16 | 0 | 0 | 5 | rule_fallback | - |
| biz_106 | 9 | 289353/286123 | 20 | 20 | 0 | 0 | 9 | rule_fallback | - |
| biz_107 | 1 | 43605/43605 | 8 | 8 | 0 | 0 | 1 | rule_fallback | - |
| biz_108 | 7 | 113696/113696 | 21 | 21 | 0 | 0 | 7 | rule_fallback | - |
| biz_109 | 9 | 156626/156626 | 22 | 22 | 0 | 0 | 9 | rule_fallback | - |
| biz_110 | 1 | 29317/29317 | 8 | 8 | 0 | 0 | 1 | rule_fallback | - |
| biz_111 | 5 | 239499/239499 | 16 | 16 | 0 | 0 | 5 | rule_fallback | - |
| biz_112 | 2 | 113375/113375 | 12 | 12 | 0 | 0 | 2 | rule_fallback | - |
| biz_113 | 1 | 32994/32994 | 6 | 6 | 0 | 0 | 1 | rule_fallback | - |
| biz_114 | 1 | 26119/26119 | 7 | 7 | 0 | 0 | 1 | rule_fallback | - |
| biz_115 | 4 | 55962/55962 | 14 | 14 | 0 | 0 | 4 | rule_fallback | - |
| biz_116 | 4 | 79117/79117 | 14 | 14 | 0 | 0 | 4 | rule_fallback | - |
| biz_117 | 7 | 66286/65791 | 12 | 12 | 0 | 0 | 7 | rule_fallback | - |
| biz_118 | 4 | 92169/92169 | 13 | 13 | 0 | 0 | 4 | rule_fallback | - |
| biz_119 | 10 | 172569/170661 | 18 | 18 | 0 | 0 | 10 | rule_fallback | - |
| biz_120 | 10 | 240702/240702 | 19 | 19 | 0 | 0 | 10 | rule_fallback | - |
| biz_121 | 7 | 172139/172139 | 19 | 19 | 0 | 0 | 7 | rule_fallback | - |
| biz_122 | 7 | 214306/214306 | 19 | 19 | 0 | 0 | 7 | rule_fallback | - |
| biz_123 | 17 | 268911/267703 | 35 | 35 | 0 | 0 | 17 | rule_fallback | - |
| biz_124 | 13 | 305973/304785 | 24 | 24 | 0 | 0 | 13 | rule_fallback | - |
| biz_125 | 1 | 79052/79052 | 6 | 6 | 0 | 0 | 1 | rule_fallback | - |
| biz_126 | 1 | 66429/66429 | 7 | 7 | 0 | 0 | 1 | rule_fallback | - |
| biz_127 | 5 | 198800/195901 | 18 | 18 | 0 | 0 | 5 | rule_fallback | - |
| biz_128 | 8 | 152259/151116 | 18 | 18 | 0 | 0 | 8 | rule_fallback | - |
| biz_129 | 5 | 222331/222331 | 15 | 15 | 0 | 0 | 5 | rule_fallback | - |
| biz_130 | 2 | 172591/172591 | 12 | 12 | 0 | 0 | 2 | rule_fallback | - |

## 6. Nhóm shop khó — LLM cứu được map nào, còn fallback ở đâu

Bảng dưới đây chỉ TRÌNH BÀY số liệu của các nhóm shop khó, không dùng để quyết định map: 5 shop holdout của phase1 (biz_105, biz_106, biz_117, biz_123, biz_124) và 4 shop từng 0 câu đúng ở Giai đoạn 2 (biz_114, biz_117, biz_125, biz_129; biz_117 thuộc cả hai nhóm nên xuất hiện một lần).

### Holdout phase1

- `biz_105` (status: ok, bảng: 5, baseline mapped: 16, mapped mới: 16, fallback: 5, nguồn: ['rule_fallback']): LLM cứu được: chưa có map LLM nào được áp dụng; profile delta: không đổi; còn fallback ở: 5 bảng (xem `sources` trong summary để biết bảng nào).
- `biz_106` (status: ok, bảng: 9, baseline mapped: 20, mapped mới: 20, fallback: 9, nguồn: ['rule_fallback']): LLM cứu được: chưa có map LLM nào được áp dụng; profile delta: không đổi; còn fallback ở: 9 bảng (xem `sources` trong summary để biết bảng nào).
- `biz_117` (status: ok, bảng: 7, baseline mapped: 12, mapped mới: 12, fallback: 7, nguồn: ['rule_fallback']): LLM cứu được: chưa có map LLM nào được áp dụng; profile delta: không đổi; còn fallback ở: 7 bảng (xem `sources` trong summary để biết bảng nào).
- `biz_123` (status: ok, bảng: 17, baseline mapped: 35, mapped mới: 35, fallback: 17, nguồn: ['rule_fallback']): LLM cứu được: chưa có map LLM nào được áp dụng; profile delta: không đổi; còn fallback ở: 17 bảng (xem `sources` trong summary để biết bảng nào).
- `biz_124` (status: ok, bảng: 13, baseline mapped: 24, mapped mới: 24, fallback: 13, nguồn: ['rule_fallback']): LLM cứu được: chưa có map LLM nào được áp dụng; profile delta: không đổi; còn fallback ở: 13 bảng (xem `sources` trong summary để biết bảng nào).

### 0 câu đúng ở Giai đoạn 2

- `biz_114` (status: ok, bảng: 1, baseline mapped: 7, mapped mới: 7, fallback: 1, nguồn: ['rule_fallback']): LLM cứu được: chưa có map LLM nào được áp dụng; profile delta: không đổi; còn fallback ở: 1 bảng (xem `sources` trong summary để biết bảng nào).
- `biz_117` (status: ok, bảng: 7, baseline mapped: 12, mapped mới: 12, fallback: 7, nguồn: ['rule_fallback']): LLM cứu được: chưa có map LLM nào được áp dụng; profile delta: không đổi; còn fallback ở: 7 bảng (xem `sources` trong summary để biết bảng nào).
- `biz_125` (status: ok, bảng: 1, baseline mapped: 6, mapped mới: 6, fallback: 1, nguồn: ['rule_fallback']): LLM cứu được: chưa có map LLM nào được áp dụng; profile delta: không đổi; còn fallback ở: 1 bảng (xem `sources` trong summary để biết bảng nào).
- `biz_129` (status: ok, bảng: 5, baseline mapped: 15, mapped mới: 15, fallback: 5, nguồn: ['rule_fallback']): LLM cứu được: chưa có map LLM nào được áp dụng; profile delta: không đổi; còn fallback ở: 5 bảng (xem `sources` trong summary để biết bảng nào).

## 7. Giới hạn

- Runner chỉ áp dụng map LLM cho semantic còn trống trong map rule-based; map LLM mâu thuẫn với map rule-based bị loại thay vì tự chọn phe nào (theo contract mục 6c, trường hợp LLM đúng hơn cần được báo cáo riêng và xử lý ở tầng coordinator, không ghi đè âm thầm trong runner).
- Runner không kiểm chứng lại mẫu giá trị của map LLM (đó là trách nhiệm của `llm_ingest` theo contract mục 5); lớp kiểm tra của runner chỉ bảo đảm semantic hợp lệ và cột tồn tại thật trong bảng đó.
- Runner không chạy lại chuẩn hoá nên không thể tự tạo thêm dòng hay sửa dữ liệu: mọi cải thiện chỉ thể hiện ở map cột và hồ sơ shop, không thể hiện ở `rows_in`/`rows_out`.
- Khi backend là `none_fallback`, toàn bộ đợt chạy này chỉ chứng minh runner không crash và giữ đúng baseline — nó không chứng minh LLM cải thiện được map nào.

## 8. Bước tiếp theo

1. Coordinator tích hợp lại sau khi Child 1 bàn giao `src/llm_ingest.py`: chỉ cần chạy lại runner này (không sửa runner) rồi đối chiếu summary mới với summary đợt này — số `llm_added_mappings` và `profile_delta` thay đổi mới là bằng chứng LLM thật có tác dụng.
2. Chọn và kiểm chứng một model cục bộ ≤15B (tên, licence, ngày phát hành từ nguồn chính thức, trước 2026-10-01) rồi mới ghi `model_id` khác null vào summary; trước thời điểm đó, mọi báo cáo đều phải giữ `model_id: null` và `backend_used: 'none_fallback'` khi chưa có backend thật.
3. Child 3 chạy `tests/test_llm_ingest.py` và `evaluation/compare_baseline.py` để đối chiếu summary này với `phase1/reports/phase1_summary.json`, đặc biệt kiểm tra hash phase1 trước/sau và tính đơn điệu của hồ sơ shop.
4. Sau khi có LLM thật, ưu tiên đo lại đúng các nhóm shop khó ở mục 6 (holdout và nhóm 0 câu đúng ở Giai đoạn 2) trước khi kết luận LLM có cứu được map khó hay không.

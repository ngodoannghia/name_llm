# Giai đoạn 1 — DataMate: Ingest + Rulebook + Khung đánh giá

## Mục đích

Giai đoạn 1 xây nền móng cho agent DataMate, **không** trả lời hết 3.000 câu
bằng LLM. Nền móng này gồm ba thứ, và cả ba đều phải chạy được trên shop
chưa từng thấy, header chưa từng thấy, luật chưa từng thấy:

1. **Ingest + chuẩn hoá theo từng shop** — đọc CSV/XLSX/SQLite, phát hiện
   header thật, hợp nhất sheet/file tách, chuẩn hoá tiền/ngày/số lượng,
   quy đổi về đơn vị cơ bản, lập hồ sơ shop.
2. **Rulebook loader + calendar + time resolver** — nạp rulebook và lịch
   âm–dương của split đang chạy lúc chạy, không hard-code rulebook train.
3. **Khung đánh giá** — chấm submission bằng PA (+1 đúng, 0 abstain/thiếu,
   −1 sai), chia validation theo shop, holdout riêng, kiểm thử bằng
   submission mẫu.

Kiến trúc tổng thể (xem `../MASTER_PLAN.md` mục 4): **LLM hiểu, Code tính,
Code kiểm chứng**. Giai đoạn 1 là phần “Code” của kiến trúc đó. Giai đoạn 2
mới cắm question parser + sinh code tính toán vào khung này.

## Cấu trúc

```
phase1/
  README.md               # File này
  API_CONTRACT.md         # Hợp đồng API mà mọi module phải tuân thủ
  run_phase1.py           # Điểm vào duy nhất: chạy end-to-end một lệnh
  src/
    __init__.py           # Export nhẹ tầng ingest
    text_utils.py         # Chuẩn hoá tiếng Việt (có dấu/không dấu)
    normalizers.py        # Parse tiền, ngày, số lượng (text và số)
    ingest.py             # Đọc file, phát hiện header, map cột, hồ sơ shop
    rulebook.py           # Nạp rulebook, calendar, giải kỳ thời gian
    evaluate.py           # Chấm PA, per-type, per-business, holdout
  tests/
    test_generalization.py  # 5 nhóm acceptance test (unittest)
  reports/
    phase1_summary.json   # Kết quả chạy thật (sinh bởi run_phase1.py)
    tests_output.txt      # Output unittest (sinh bởi run_phase1.py)
    PHASE1_REPORT.md      # Báo cáo tổng kết Giai đoạn 1
```

## Cách chạy

Từ thư mục `phase1/`:

```bash
# Chạy đầy đủ: rulebook + ingest 30 shop + đánh giá mẫu + tests
python3 run_phase1.py

# Chỉ chạy ingest + đánh giá mẫu, bỏ qua tests
python3 run_phase1.py --skip-tests

# Chỉ định thư mục train khác
python3 run_phase1.py --train-dir /duong/dan/toi/train

# Chạy riêng tests tổng quát hoá
python3 -m unittest discover -s tests -v
```

Lưu ý: ingest 30 shop mất vài phút (shop XLSX lớn mất 17–27 giây/shop),
và script chạy từng shop một rồi giải phóng bộ nhớ ngay, nên không giữ
cả ~4 triệu dòng trong RAM cùng lúc. Exit code = 0 khi không có shop
nào lỗi và test đều pass; khác 0 khi có shop lỗi hoặc test fail.

## Quy tắc bắt buộc (không được phá vỡ khi sửa code)

- **Comment tiếng Việt đầy đủ:** mọi module, class, hàm public đều có
  docstring tiếng Việt (mục đích, đầu vào, đầu ra, vì sao, trường hợp biên).
- **Không hard-code:** không theo business_id, tên shop, tên ngành, id câu hỏi,
  tên cột chính xác của train, schema theo ngành train, hay rulebook train.
  Map cột theo ý nghĩa (chuẩn hoá + từ điển đồng nghĩa + kiểm chứng mẫu giá trị).
- **Không bịa số:** dòng không parse được thì đánh dấu nghi ngờ, không gán 0;
  luật không trích được thì ghi vào `unparsed_notes`, không bịa luật;
  cột không map được thì ghi vào `columns_unmapped`, không đoán cột theo vị trí.
- **Không sửa dữ liệu train, không gọi mạng/API, chạy offline, deterministic**
  (thứ tự shop đã sắp xếp, không dùng ngẫu nhiên không seed).

## Giới hạn đã biết (xem chi tiết trong reports/PHASE1_REPORT.md)

- `profile.columns_mapped` là mức shop (cột thật đầu tiên tìm thấy khi duyệt
  bảng theo tên); code cần chính xác theo bảng nên gọi lại `map_columns(df)`
  cho từng bảng.
- `parse_failures` là số lỗi trên mẫu kiểm chứng (tối đa 100 mẫu/cột) +
  lỗi quy đổi, không phải đếm toàn bộ mọi ô.
- Nhận diện bảng trả riêng chỉ dựa vào tên bảng chứa token trả/return;
  bảng trả đặt tên hoàn toàn khác có thể bị bỏ sót.
- Percent làm tròn kiểu nửa lên; luật chỉ nói “làm tròn 2 chữ số”.
- Dòng table so dưới dạng ghép đôi một-một (multiset), chặt hơn chữ
  “tập hợp” nếu có dòng trùng lặp.
- Bộ trích xuất rulebook chỉ nhận diện các cách viết phổ biến; rulebook mới
  diễn đạt hoàn toàn khác có thể cho `None` — đó là hành vi đúng (abstain
  thay vì đoán), nhưng cần mở rộng mẫu từ khoá nếu test luật mới dùng
  cách viết ngoài bộ mẫu.

## Giai đoạn 2 cần làm tiếp

- Question parser theo 4 trục (đo cái gì × của ai × kỳ nào × dạng đáp án).
- Sinh code tính toán trên dữ liệu đã chuẩn hoá, kiểm tra bộ lọc đơn đúng
  cho từng loại câu, đơn vị đã quy đổi, kỳ đã cắt đúng, tie-break theo mã.
- Confidence gate (ngưỡng vận hành đề xuất 0,65–0,70) + format validator.
- Cache ingest theo shop trước khi chạy đánh giá toàn bộ (tránh đọc lại
  XLSX lớn nhiều lần).

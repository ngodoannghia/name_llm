# Quy tắc làm sạch — bảng ví dụ — skill datamate-cleaning

Tài liệu tham khảo dài (L3). Bảng dưới đây minh họa cách code áp dụng
quy tắc; với shop mới, code vẫn áp dụng cùng một quy tắc cho dữ liệu
của chính shop đó.

## Parse tiền

| Ô gốc | Kết quả | Lý do |
|-------|---------|-------|
| "1.200.000" | 1200000 | Chấm là phân cách nghìn. |
| "1,200,000" | 1200000 | Phẩy là phân cách nghìn. |
| "1 200 000" | 1200000 | Khoảng trắng là phân cách nhóm, đã bỏ. |
| "1200000" | 1200000 | Không có phân cách. |
| "1.2tr" | 1200000 | Hậu tố "tr" nghĩa là triệu, nhân với 1000000. |
| "1,2tr" | 1200000 | Phẩy là thập phân trước hậu tố "tr". |
| "1.2 triệu" | 1200000 | Hậu tố "triệu" đã biết. |
| "500k" | 500000 | Hậu tố "k" đã biết, nhân với 1000 — chỉ khi cột xác nhận "k" là nghìn, nếu không thì NULL. |
| "(200000)" | -200000 | Số trong ngoặc là số âm. |
| "-200000" | -200000 | Dấu trừ đứng trước. |
| "1.200" (cột đa số dùng chấm phân cách nghìn) | 1200 | Theo mẫu đa số của cột. |
| "1.200" (cột không có đa số rõ ràng) | NULL + đếm | Mơ hồ giữa 1200 và 1,2, không đoán. |
| "abc" | NULL + đếm | Không phải số, không phải hậu tố đã biết. |
| "" (ô trống, không thuộc forward-fill) | NULL + đếm | Không điền 0, không điền giá trị dòng khác. |

## Parse ngày

| Ô gốc | Kết quả | Lý do |
|-------|---------|-------|
| "01/05/2025" | "2025-05-01" | Ngày/tháng/năm theo quy ước Việt Nam. |
| "1/5/2025" | "2025-05-01" | Cùng quy ước, không cần số 0 đứng đầu. |
| "2025-05-01" | "2025-05-01" | Năm-tháng-ngày, không mơ hồ. |
| "01-05-2025" | "2025-05-01" | Ngày-tháng-năm. |
| "1 thg 1 2025" | "2025-01-01" | Tháng viết tắt tiếng Việt. |
| "1 tháng 1 2025" | "2025-01-01" | Tháng viết đầy đủ tiếng Việt. |
| "01/05/2025 10:30" | "2025-05-01" | Chỉ lấy phần ngày, bỏ phần giờ. |
| "32/01/2025" | NULL + đếm | Ngày 32 không hợp lệ. |
| "01/13/2025" | NULL + đếm | Tháng 13 không hợp lệ. |
| "" (ô trống) | NULL + đếm | Không forward-fill ngày. |

## Số lượng và quy đổi đơn vị

| Trường hợp | Kết quả | Lý do |
|-----------|---------|-------|
| Bán 2 hộp, hệ số 1 hộp = 10 cái | 20 cái | 2 × 10, hệ số có trong bảng quy đổi. |
| Bán 2 thùng, không có hệ số cho thùng | NULL + đếm | Thiếu hệ số thì để trống, không đoán bằng 1. |
| Bán 2 cái, bảng ghi rõ cái là đơn vị cơ sở | 2 cái | Hệ số 1 đã ghi rõ, không phải đoán. |
| Bán 2 hộp, bảng có hai hệ số khác nhau cho hộp | NULL + đếm | Hệ số mâu thuẫn, không chọn bừa. |
| Số lượng "1,5" kg, đơn vị cơ sở là kg | 1,5 | Giữ thập phân thật, kiểu REAL. |
| Số lượng "abc" | NULL + đếm | Không parse được, không điền 0. |

## Forward-fill: được và không được

| Trường hợp | Kết quả | Lý do |
|-----------|---------|-------|
| Mã đơn ghi ở dòng đầu, các dòng sau cùng đơn để trống | Điền mã đơn xuống các dòng sau | Đúng phạm vi: cột mã theo khối. |
| Ô tiền trống ở dòng sau | NULL + đếm | Tiền không được forward-fill. |
| Ô ngày trống ở dòng sau | NULL + đếm | Ngày không được forward-fill. |
| Ô trạng thái trống ở dòng sau | NULL + đếm | Trạng thái không được forward-fill. |
| Ô trống ở dòng đầu tiên của bảng | NULL + đếm | Không có giá trị phía trên để điền. |

## Dedup: được và không được

| Trường hợp | Kết quả | Lý do |
|-----------|---------|-------|
| Hai dòng giống hệt nhau trên toàn bộ cột sau chuẩn hóa | Giữ một dòng, đếm một dòng đã bỏ | Trùng lặp thật. |
| Hai dòng giống nhau trừ tiền khác nhau | Giữ cả hai | Khác giá trị thì không phải trùng lặp. |
| Hai dòng giống nhau trừ ngày khác nhau | Giữ cả hai | Khác giá trị thì không phải trùng lặp. |
| Dòng tổng hợp và dòng chi tiết có cùng mã đơn | Giữ riêng | Khác vai trò, không dedup chung. |

## Thống kê làm sạch phải đếm

Với mỗi bảng, `_clean_stats` ghi riêng từng số sau, không gộp chung
thành một con số "lỗi":

- Số dòng vào, số dòng ra, số dòng trùng đã bỏ.
- Số ô NULL do tiền không parse được, do ngày không parse được, do số
  lượng không parse được, do thiếu hệ số quy đổi, do hệ số mâu thuẫn.
- Số ô đã forward-fill (chỉ cột mã/nhóm).
- Kiểu suy luận cuối cùng của từng cột.

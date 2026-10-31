# Ví dụ header khó — skill datamate-header

Tài liệu tham khảo dài (L3). Các ví dụ dưới đây chỉ minh họa cách chấm
điểm, không phải luật riêng cho một shop nào. Với shop mới, quyết định
luôn phải dựa trên bằng chứng đo được trong chính sheet đó.

## Ví dụ 1 — Tiêu đề và logo chiếm 2 dòng đầu, header ở dòng 3

Sheet có dạng (mỗi dòng là một hàng trong file):

| Dòng | Nội dung |
|------|----------|
| 1 | "CỬA HÀNG A — BẢNG BÁN HÀNG THÁNG 5" (một ô dài, các ô còn lại trống) |
| 2 | (dòng trống) |
| 3 | Mã đơn | Ngày bán | Tên hàng | Số lượng | Thành tiền |
| 4 | HD001 | 01/05/2025 | Áo thun | 2 | 200000 |
| 5 | HD002 | 02/05/2025 | Quần dài | 1 | 150000 |

- Dòng 1 bị loại ở bước 1: một ô text dài, các ô còn lại trống, không
  có nhiều ô text ngắn song song.
- Dòng 2 bị loại: dòng trống.
- Dòng 3 được chọn: tỷ lệ ô text cao, không trùng tên sau chuẩn hóa,
  các cột bên dưới đều có giá trị thật và kiểu nhất quán (mã là text,
  ngày là ngày, số lượng là số, tiền là số).
- Kết luận: dòng header là dòng 3, tên cột chuẩn hóa là
  `ma_don, ngay_ban, ten_hang, so_luong, thanh_tien`.

## Ví dụ 2 — Sheet hai tầng tiêu đề

| Dòng | Nội dung |
|------|----------|
| 1 | (dòng trống) |
| 2 | Thông tin đơn | | Thông tin hàng | | |
| 3 | Mã đơn | Ngày bán | Mã hàng | Tên hàng | Số lượng |
| 4 | HD001 | 01/05/2025 | H001 | Áo thun | 2 |

- Dòng 2 là tầng nhóm, không phải tên cột: các ô của nó trải rộng, có
  ô trống xen kẽ, và không khớp một-một với cột dữ liệu.
- Dòng 3 là tầng tên cột chính, được chọn làm header.
- Không lấy tầng trên làm header, vì như vậy sẽ mất tên cột cụ thể và
  gây trùng tên.
- Nếu một vị trí ở tầng dưới trống mà tầng trên tại vị trí đó có giá
  trị thì mới ghép hai tầng tại vị trí đó, và phải ghi rõ việc ghép
  trong lý do.

## Ví dụ 3 — Dòng tổng cuối sheet

| Dòng | Nội dung |
|------|----------|
| 1 | Mã đơn | Ngày bán | Thành tiền |
| 2 | HD001 | 01/05/2025 | 200000 |
| 3 | HD002 | 02/05/2025 | 150000 |
| 4 | Tổng cộng | | 350000 |

- Dòng 4 là dòng tổng: có từ "tổng" kèm ô số lớn, không có mã đơn, không
  có ngày.
- Dòng 4 không phải dữ liệu, không phải header, không được tính vào số
  dòng và không được dùng để suy kiểu cột.
- Nếu lấy dòng 4 làm dữ liệu thì tổng tiền sẽ bị cộng trùng; nếu lấy
  làm header thì toàn bộ bảng sẽ sai.

## Ví dụ 4 — Header không chắc: hai dòng ứng viên điểm gần nhau

| Dòng | Nội dung |
|------|----------|
| 1 | Ngày | Mặt hàng | Số lượng |
| 2 | Mã | Tên | ĐVT | Giá |
| 3 | HD001 | Áo thun | Cái | 100000 |

- Dòng 1 và dòng 2 đều có nhiều ô text ngắn, nhưng chỉ một trong hai
  khớp với cột dữ liệu bên dưới.
- Cách xử lý đúng: chấm điểm từng cột bên dưới — dòng nào có cột bên
  dưới toàn trống hoặc kiểu lộn xộn thì bị trừ điểm; nếu sau khi chấm
  mà hai dòng vẫn gần nhau thì báo cờ không chắc, nêu rõ điểm yếu của
  từng dòng, để validator chấm lại bằng mẫu.
- Cách xử lý sai: chọn bừa dòng 1 vì nó đứng trước, hoặc gộp hai dòng
  thành một header dài mà không có bằng chứng hai tầng tiêu đề.

## Ví dụ 5 — Cột toàn trống và tên cột trùng sau chuẩn hóa

Sheet có header ứng viên: "Mã đơn | Mã Đơn | Ngày | Ngày bán".
Sau chuẩn hóa, "Mã đơn" và "Mã Đơn" trùng nhau thành `ma_don`.

- Đây là điểm yếu phải báo, không được lặng lẽ bỏ một cột hay tự thêm
  hậu tố số để lấp đi.
- Nếu cột trùng là cột trang trí không có dữ liệu bên dưới thì đề xuất
  vẫn có thể giữ, nhưng phải ghi rõ cột nào trùng và cột nào trống.
- Nếu cả hai cột trùng đều có dữ liệu khác nhau thì header này không
  chắc, vì việc phân biệt hai cột cần thêm bằng chứng ngoài header.

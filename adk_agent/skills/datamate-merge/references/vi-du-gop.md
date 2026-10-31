# Ví dụ gộp sheet — skill datamate-merge

Tài liệu tham khảo dài (L3). Các ví dụ dưới đây minh họa cách áp dụng
tiêu chí gộp, không phải luật riêng cho một shop nào. Với shop mới,
quyết định luôn phải dựa trên bằng chứng đo được trong chính shop đó.

## Ví dụ 1 — Gộp được: nhiều sheet là mảnh của cùng một bảng

Một file có 8 sheet cùng vai trò bán hàng, chữ ký cột chuẩn hóa giống
nhau (mã đơn, ngày bán, mã hàng, tên hàng, số lượng, thành tiền), ID
đơn nối tiếp nhau qua các sheet, khoảng ngày nối tiếp nhau không có
khoảng trống khó hiểu, ID trùng giữa các sheet hoặc không có, hoặc là
cùng một dòng lặp lại với nội dung giống nhau.

- Kết luận: gộp 8 sheet thành một bảng logic trước khi mô tả và truy vấn.
- Bằng chứng phải ghi: chữ ký cột chung, vai trò chung, khoảng ID của
  từng sheet, khoảng ngày của từng sheet, tỷ lệ ID trùng và kết luận
  trùng là cùng nội dung.
- Sau gộp, bước làm sạch sẽ dedup các dòng lặp lại và đếm số dòng đã bỏ.

## Ví dụ 2 — Gộp được: hai sheet chia đôi theo thời gian

Hai sheet có hậu tố dạng "_1" và "_2", cùng chữ ký cột, cùng vai trò,
sheet thứ nhất chứa các đơn từ tháng 1 đến tháng 6, sheet thứ hai chứa
các đơn từ tháng 7 đến tháng 12, ID đơn không trùng nhau và nối tiếp
nhau.

- Kết luận: gộp thành một bảng logic.
- Lưu ý: hậu tố "_1"/"_2" chỉ là gợi ý, không phải bằng chứng. Bằng
  chứng là chữ ký cột, vai trò, ID và ngày liên tục đã đo được.
- Nếu hai sheet này mà ID trùng nhau với nội dung mâu thuẫn thì dù tên
  có dạng chia đôi cũng không gộp (xem ví dụ 5).

## Ví dụ 3 — Không gộp: cùng file nhưng khác vai trò

Một file có các sheet: đơn hàng, chi tiết đơn, sản phẩm, khách hàng.

- Dù bốn sheet nằm chung một file, vai trò của chúng khác nhau nên giữ
  riêng từng bảng.
- Gộp chúng lại sẽ trộn đơn hàng với dòng chi tiết, trộn mã khách với
  mã hàng, và mọi truy vấn sau đó đều sai.
- Cách xử lý sai thường gặp: thấy các sheet cùng file thì gộp thành một
  bảng "dữ liệu chung". Đây là lỗi bị cấm tuyệt đối trong skill này.

## Ví dụ 4 — Không gộp mặc định: chi tiết và chi tiết lô

Hai bảng: một bảng chi tiết đơn, một bảng chi tiết lô. Tên bảng gần
giống nhau, nhiều cột giống nhau, nhưng bảng lô có thêm cột mã lô và
số dòng lớn hơn vì một dòng chi tiết tách thành nhiều dòng lô.

- Kết luận mặc định: **không gộp**.
- Nếu gộp, doanh thu tính từ bảng gộp sẽ bằng doanh thu chi tiết cộng
  thêm doanh thu lô, tức đếm trùng.
- Chỉ gộp khi so sánh toàn bộ schema chứng minh được hai bảng thực chất
  là một bảng bị tách đôi: cùng toàn bộ cột, không có cột lô phân biệt,
  ID và ngày liên tục, không có quan hệ tách lô. Trường hợp đó phải ghi
  rõ toàn bộ bằng chứng so sánh, không được ghi chung chung "tên giống
  nhau nên gộp".

## Ví dụ 5 — Không gộp: ID overlap mâu thuẫn

Hai sheet cùng chữ ký cột, cùng vai trò, khoảng ngày chồng lấn nhau.
Kiểm tra ID trùng thấy: cùng một mã đơn xuất hiện ở cả hai sheet, nhưng
ở sheet này thành tiền là 200000, ở sheet kia là 350000, ngày bán cũng
khác nhau.

- Kết luận: **không gộp**. Đây là mâu thuẫn, không phải bản sao.
- Giữ riêng hai bảng, ghi rõ mã đơn mâu thuẫn làm bằng chứng.
- Cách xử lý sai: gộp rồi để bước dedup tự bỏ một trong hai dòng, vì
  dedup chỉ bỏ dòng giống hệt nhau, không được quyền chọn bỏ một sự thật
  trong hai sự thật mâu thuẫn.

## Ví dụ 6 — Gộp được sau khi trừ dòng trùng cùng nội dung

Hai sheet cùng chữ ký cột, cùng vai trò, có một số ID trùng nhau. Với
mỗi ID trùng, toàn bộ các cột còn lại giống hệt nhau (cùng ngày, cùng
mã hàng, cùng số lượng, cùng tiền).

- Kết luận: gộp được, vì trùng này là bản sao, không phải mâu thuẫn.
- Sau gộp, bước làm sạch dedup các dòng giống hệt và đếm số dòng đã bỏ
  vào thống kê làm sạch.
- Nếu chỉ một phần ID trùng là cùng nội dung, phần còn lại mâu thuẫn,
  thì toàn bộ cặp này vẫn không gộp cho đến khi phần mâu thuẫn được
  làm rõ.

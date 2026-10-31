---
name: datamate-schema
description: "Viết file schema một file cho một shop DataMate từ DB tạm đã kiểm chứng: bảng và cột kèm tên gốc và tên SQL, kiểu, mô tả tiếng Việt, role_hint trong enum cố định, mẫu tối đa 5 giá trị có thật, distinct liệt kê đầy đủ chỉ khi không quá 50, join_hints có bằng chứng đo được và ghi chú bẫy của shop, không chứa dữ liệu bán hàng."
---

# Skill: datamate-schema

## Tài nguyên của skill này — đọc kỹ trước khi load

- Tài nguyên duy nhất của skill này là `references/vi-du-schema.md`
  (một ví dụ schema đã hoàn thiện). Ngoài file đó, skill này KHÔNG có
  thêm file mẫu, template hay assets nào khác.
- Chỉ load đúng file đã nêu tên ở trên. KHÔNG đoán tên file khác
  (ví dụ không thử `assets/template.json`, `assets/notes.json` hay bất
  kỳ đường dẫn nào chưa được nêu tên), vì các file đó không tồn tại và
  mỗi lần load lỗi chỉ tốn thêm một lượt gọi model mà không thêm thông tin.
- Nếu load tài nguyên lỗi, bỏ qua tài nguyên đó và tiếp tục chỉ với nội
  dung SKILL.md này — không thử lại bằng đường dẫn khác, không dừng
  workflow vì thiếu ví dụ.

## Khi nào dùng skill này

- Dùng ở node SchemaAgent trong workflow ingest, sau khi DB tạm đã ghi
  xong và trước khi publish.
- Đầu vào là: danh sách bảng đã gộp, bảng map tên gốc sang tên SQL,
  mẫu và distinct đo được từ DB tạm, README của shop (đã cắt ngắn), và
  các ghi chú bẫy đã phát hiện ở các bước trước.
- Skill này chỉ mô tả cấu trúc, không tính toán số liệu bán hàng, không
  làm sạch dữ liệu, không gộp bảng, không đổi header.

## Cấu trúc file schema — 1 file/shop

File: `sqlite/schemas/<business_id>.json`. Đây là hợp đồng duy nhất
giữa ingest và workflow trả lời câu hỏi: workflow trả lời về sau chỉ
được dùng bảng và cột có trong file này.

```json
{
  "business_id": "biz_123",
  "source_files": ["pos.sqlite"],
  "generated_at": "2026-11-01T00:00:00+07:00",
  "model": "tên model local đã dùng (chỉ ghi tên, không ghi key)",
  "tables": [
    {
      "table_id": "don_hang",
      "original_names": ["don_hang"],
      "source_sheets": ["don_hang"],
      "sqlite_table": "don_hang",
      "row_count": 33630,
      "description_vi": "Bảng đơn hàng: mỗi dòng là một đơn.",
      "columns": [
        {
          "original_name": "MaHoaDon",
          "sqlite_name": "ma_hoa_don",
          "type": "TEXT",
          "description_vi": "Mã đơn hàng, khóa nối sang chi tiết đơn.",
          "role_hint": "order_id",
          "sample_values": ["HD001", "HD002"],
          "distinct_count": 33630,
          "distinct_values": null,
          "nullable_rate": 0.0
        },
        {
          "original_name": "TinhTrang",
          "sqlite_name": "tinh_trang",
          "type": "TEXT",
          "description_vi": "Trạng thái đơn.",
          "role_hint": "status",
          "sample_values": ["Hoàn thành", "Đã hủy"],
          "distinct_count": 4,
          "distinct_values": ["Hoàn thành", "Đã hủy", "Trả hàng", "Đang giao"],
          "nullable_rate": 0.0
        }
      ],
      "date_range": {"min": "2025-01-01", "max": "2026-06-28"}
    }
  ],
  "join_hints": [
    {
      "from_table": "chi_tiet_don",
      "from_column": "ma_hoa_don",
      "to_table": "don_hang",
      "to_column": "ma_hoa_don",
      "evidence": "tên cột chung + 33630/33630 mã đơn khớp nhau"
    }
  ],
  "notes": [
    "Không cộng dồn chi_tiet_don_lo để tính doanh thu (bảng tách lô, sẽ đếm trùng).",
    "Giá tại một ngày lấy từ lich_su_gia, không lấy giá hiện tại trong san_pham."
  ]
}
```

## Làm theo từng bước

### Bước 1 — Liệt kê đủ bảng và cột, khớp với DB tạm

1. Mọi bảng trong DB tạm đều phải có mặt trong schema, và mọi bảng
   trong schema đều phải có trong DB tạm. Thiếu một bên là lỗi.
2. Mọi cột trong DB tạm đều phải có mặt trong schema, và ngược lại.
   Không thêm cột không có trong DB, không bỏ cột có trong DB.
3. `row_count` là số dòng đo được trong DB tạm, không phải số dòng ước
   lượng từ file gốc hay từ mẫu.
4. `date_range` (nếu bảng có cột ngày) là giá trị nhỏ nhất và lớn nhất
   đo được trong DB, dạng TEXT ISO. Bảng không có cột ngày thì không
   bịa khoảng ngày.

### Bước 2 — Viết mô tả bằng tiếng Việt, nêu vai trò của cột

1. `description_vi` của bảng nêu rõ mỗi dòng là gì (một đơn, một dòng
   chi tiết, một sản phẩm, một lần thanh toán...).
2. `description_vi` của cột nêu rõ vai trò: mã đơn, mã hàng, mã khách,
   ngày, tiền, số lượng, trạng thái, thanh toán, hay vai trò khác.
3. Mô tả dựa trên tên cột, mẫu và distinct đo được, không dựa trên
   phỏng đoán từ tên bảng. Cột nào vai trò không rõ thì ghi rõ không
   rõ trong mô tả và đặt `role_hint` là `unknown` (xem bước 3).

### Bước 3 — role_hint chỉ trong enum cố định

`role_hint` chỉ được dùng một trong các giá trị sau, không tự bịa role
mới:

`order_id`, `product_id`, `customer_id`, `date`, `money`, `quantity`,
`status`, `payment`, `unknown`.

- `order_id`: mã định danh đơn hàng, dùng để nối đơn với chi tiết.
- `product_id`: mã định danh sản phẩm/hàng hóa.
- `customer_id`: mã định danh khách hàng.
- `date`: cột ngày (đã chuẩn hóa về TEXT ISO).
- `money`: cột tiền (INTEGER VND).
- `quantity`: cột số lượng (đã quy đổi hoặc đã ghi rõ đơn vị).
- `status`: cột trạng thái (đơn, giao hàng, trả hàng...).
- `payment`: cột phương thức hoặc thông tin thanh toán.
- `unknown`: vai trò không xác định được từ bằng chứng — dùng giá trị
  này thay vì đoán một role cụ thể.

Một cột chỉ có một `role_hint`. Cột vừa giống mã vừa giống text thì
chọn theo bằng chứng mạnh hơn (mẫu, distinct, tên cột, bảng nối), và
nếu vẫn không phân biệt được thì dùng `unknown`.

### Bước 4 — sample_values: tối đa 5 giá trị, phải có thật trong DB

1. Mỗi cột có tối đa 5 giá trị mẫu trong `sample_values`.
2. Mọi giá trị mẫu **phải có thật trong DB tạm**. Validator sẽ kiểm
   bằng truy vấn; giá trị mẫu bịa, dù chỉ một giá trị, cũng làm schema
   bị bác và phải viết lại.
3. Chọn mẫu đại diện cho cột: với cột trạng thái thì mẫu nên bao phủ
   các trạng thái thường gặp; với cột mã thì mẫu là các mã thật; với
   cột tiền thì mẫu là các số tiền thật đã parse.
4. Không lấy mẫu từ file gốc chưa làm sạch, không lấy mẫu từ README,
   không lấy mẫu từ trí nhớ về shop khác. Mẫu chỉ lấy từ DB tạm.

### Bước 5 — distinct: liệt kê đầy đủ chỉ khi không quá 50

1. `distinct_count` là số giá trị phân biệt đo được bằng
   COUNT(DISTINCT) trên DB tạm, phải khớp đúng với số đo được.
2. `distinct_values` chỉ liệt kê đầy đủ khi `distinct_count <= 50`.
   Khi đó liệt kê toàn bộ giá trị phân biệt, không bỏ sót, không thêm.
3. Khi `distinct_count > 50` thì `distinct_values` là `null`, chỉ giữ
   `distinct_count` và `sample_values`. Không liệt kê một phần rồi giả
   vờ là đầy đủ.
4. Quy tắc này đặc biệt quan trọng với cột trạng thái: trạng thái
   thường có ít giá trị nên liệt kê đầy đủ giúp workflow trả lời biết
   hết các trạng thái có thể lọc; cột mã có hàng nghìn giá trị thì
   không liệt kê đầy đủ.

### Bước 6 — join_hints: phải có bằng chứng đo được

1. Mỗi join_hint nêu rõ bảng nguồn, cột nguồn, bảng đích, cột đích, và
   `evidence` là bằng chứng đo được.
2. Bằng chứng đo được nghĩa là: tên cột chung **cộng với** tỷ lệ ID
   khớp đo được, ví dụ "tên cột chung + 33630/33630 mã đơn khớp nhau",
   hoặc "tên cột chung + 95% mã hàng trong chi tiết có mặt trong bảng
   sản phẩm".
3. Không ghi join chỉ vì tên cột giống nhau mà không đo tỷ lệ khớp.
   Tên cột giống nhau nhưng ID không khớp thì không phải join thật.
4. Không ghi join giữa bảng tổng hợp và bảng chi tiết để cộng dồn, vì
   đó là quan hệ gây đếm trùng, không phải quan hệ nối để truy vấn.
5. Join nào không đo được tỷ lệ khớp thì không ghi vào file; ghi chú
   nghi ngờ (nếu cần) vào `notes`, không đưa vào `join_hints`.

### Bước 7 — notes: ghi các bẫy của shop

`notes` ghi các bẫy đã phát hiện trong shop, bằng tiếng Việt, để
workflow trả lời về sau tránh sai:

- Bảng nào là bảng tách lô, không được cộng dồn chung với bảng chi tiết
  (nguy cơ đếm trùng).
- Giá tại một ngày lấy từ bảng lịch sử giá hay từ bảng sản phẩm.
- Cột nào có nhiều NULL do không parse được, cột nào đã forward-fill.
- Đơn đã hủy hay đã trả hàng nằm ở bảng nào, trạng thái nào thì tính
  hay không tính (ghi đúng trạng thái đo được trong shop này, không
  chép luật của shop khác).
- Bất kỳ điểm lạ nào khác mà người đọc schema cần biết trước khi viết
  SQL.

Notes chỉ mô tả bẫy và cách tránh, không chứa số liệu bán hàng.

### Bước 8 — Kiểm tra lần cuối trước khi xuất

1. File parse được bằng JSON, đúng cấu trúc trên, không thiếu field
   bắt buộc, không có field lạ không giải thích được.
2. `type` của mỗi cột khớp với kiểu suy luận của bước làm sạch (tiền là
   INTEGER, ngày là TEXT ISO, mã là TEXT).
3. `sqlite_name` là identifier SQL hợp lệ, khớp với tên cột thật trong
   DB tạm; `original_name` là tên gốc trong file nguồn.
4. `model` chỉ ghi tên model, không ghi key, không ghi thông tin xác
   thực nào.

## Cấm làm gì

- **Cấm chứa dữ liệu bán hàng trong file schema.** File schema chỉ mô
  tả cấu trúc: không có doanh thu, không có số lượng bán theo dòng,
  không có tổng tiền theo ngày/tháng, không có bảng tổng hợp số liệu.
  Mẫu và distinct là giá trị ví dụ của cột, không phải số liệu tổng hợp.
- Cấm bịa bảng, cột, giá trị mẫu, distinct, role, join, khoảng ngày,
  số dòng — mọi giá trị đều phải đo được từ DB tạm.
- Cấm dùng `role_hint` ngoài enum cố định ở bước 3.
- Cấm liệt kê đầy đủ `distinct_values` khi `distinct_count > 50`.
- Cấm ghi `sample_values` quá 5 giá trị.
- Cấm ghi join không có bằng chứng đo được.
- Cấm ghi key hay thông tin xác thực vào trường `model` hay bất kỳ
  trường nào khác.
- Cấm mô tả schema của shop này bằng cấu trúc của shop khác, cấm
  hard-code theo business_id hay tên cột của split train.

## Output kỳ vọng

Một ShopSchema JSON đúng cấu trúc ở trên cho shop, gồm:

- Thông tin chung: `business_id`, `source_files`, `generated_at`,
  `model` (chỉ tên).
- `tables`: đủ bảng và cột khớp với DB tạm, mỗi cột có tên gốc, tên
  SQL, kiểu, mô tả tiếng Việt, `role_hint` trong enum, mẫu tối đa 5
  giá trị thật, distinct đúng quy tắc 50, tỷ lệ NULL đo được.
- `join_hints`: mỗi join có bằng chứng đo được.
- `notes`: các bẫy của shop, không chứa dữ liệu bán hàng.

Validator code sẽ kiểm: JSON đúng schema, mọi bảng/cột khớp DB tạm theo
cả hai chiều, mọi sample và distinct đều có thật trong DB (kiểm bằng
truy vấn), distinct liệt kê đầy đủ thì số lượng khớp với COUNT(DISTINCT)
đo được. Sai thì lỗi validator kèm giá trị thật đo được được đẩy lại
cho SchemaAgent viết lại; hết số lần viết lại mà vẫn sai thì shop
FAILED — schema sai thì workflow trả lời sẽ sinh SQL sai, không được
xuất schema xấu.

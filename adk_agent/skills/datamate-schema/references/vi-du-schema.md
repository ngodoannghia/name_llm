# Ví dụ schema — skill datamate-schema

Tài liệu tham khảo dài (L3). Ví dụ dưới đây là minh họa cấu trúc, các
con số là ví dụ minh họa, không phải số liệu thật của một shop nào. File
schema thật luôn phải dùng số đo được từ DB tạm của chính shop đó.

## Ví dụ đúng — một shop có hai bảng

```json
{
  "business_id": "biz_vi_du",
  "source_files": ["ban_hang.xlsx"],
  "generated_at": "2026-11-01T00:00:00+07:00",
  "model": "ten-model-local",
  "tables": [
    {
      "table_id": "don_hang",
      "original_names": ["DonHang"],
      "source_sheets": ["DonHang"],
      "sqlite_table": "don_hang",
      "row_count": 1000,
      "description_vi": "Bảng đơn hàng: mỗi dòng là một đơn.",
      "columns": [
        {
          "original_name": "MaDon",
          "sqlite_name": "ma_don",
          "type": "TEXT",
          "description_vi": "Mã đơn hàng, khóa nối sang chi tiết đơn.",
          "role_hint": "order_id",
          "sample_values": ["HD001", "HD002", "HD003"],
          "distinct_count": 1000,
          "distinct_values": null,
          "nullable_rate": 0.0
        },
        {
          "original_name": "NgayBan",
          "sqlite_name": "ngay_ban",
          "type": "TEXT",
          "description_vi": "Ngày bán, đã chuẩn hóa về dạng ISO.",
          "role_hint": "date",
          "sample_values": ["2025-05-01", "2025-05-02"],
          "distinct_count": 60,
          "distinct_values": null,
          "nullable_rate": 0.0
        },
        {
          "original_name": "TrangThai",
          "sqlite_name": "trang_thai",
          "type": "TEXT",
          "description_vi": "Trạng thái đơn.",
          "role_hint": "status",
          "sample_values": ["Hoàn thành", "Đã hủy"],
          "distinct_count": 3,
          "distinct_values": ["Hoàn thành", "Đã hủy", "Đang giao"],
          "nullable_rate": 0.0
        }
      ],
      "date_range": {"min": "2025-05-01", "max": "2025-06-30"}
    },
    {
      "table_id": "chi_tiet_don",
      "original_names": ["ChiTiet"],
      "source_sheets": ["ChiTiet"],
      "sqlite_table": "chi_tiet_don",
      "row_count": 2500,
      "description_vi": "Bảng chi tiết đơn: mỗi dòng là một mặt hàng trong một đơn.",
      "columns": [
        {
          "original_name": "MaDon",
          "sqlite_name": "ma_don",
          "type": "TEXT",
          "description_vi": "Mã đơn hàng, khóa nối sang bảng đơn hàng.",
          "role_hint": "order_id",
          "sample_values": ["HD001", "HD002"],
          "distinct_count": 1000,
          "distinct_values": null,
          "nullable_rate": 0.0
        },
        {
          "original_name": "ThanhTien",
          "sqlite_name": "thanh_tien",
          "type": "INTEGER",
          "description_vi": "Thành tiền của dòng, đơn vị VND.",
          "role_hint": "money",
          "sample_values": [200000, 150000],
          "distinct_count": 800,
          "distinct_values": null,
          "nullable_rate": 0.01
        }
      ],
      "date_range": null
    }
  ],
  "join_hints": [
    {
      "from_table": "chi_tiet_don",
      "from_column": "ma_don",
      "to_table": "don_hang",
      "to_column": "ma_don",
      "evidence": "tên cột chung + 1000/1000 mã đơn khớp nhau"
    }
  ],
  "notes": [
    "Bảng chi tiết lô (nếu có) là bảng tách lô, không cộng dồn chung với chi tiết đơn.",
    "Cột trạng thái chỉ có 3 giá trị, lọc trạng thái chỉ dùng 3 giá trị này."
  ]
}
```

Điểm đúng trong ví dụ trên:

- `distinct_values` liệt kê đầy đủ cho cột trạng thái vì
  `distinct_count` là 3 (nhỏ hơn hoặc bằng 50); cột mã có 1000 giá trị
  thì để `null`.
- `sample_values` mỗi cột không quá 5 giá trị, và trong file thật thì
  mọi giá trị đó đều phải có thật trong DB.
- Join có bằng chứng đo được "1000/1000 mã đơn khớp nhau", không chỉ
  ghi "tên cột giống nhau".
- `notes` ghi bẫy (bảng tách lô), không chứa doanh thu hay số lượng bán.

## Ví dụ sai — những lỗi validator sẽ bác

| Lỗi | Vì sao sai |
|-----|-----------|
| `role_hint` là "order_code" | Ngoài enum cố định, phải dùng `order_id` hoặc `unknown`. |
| `sample_values` có 8 giá trị | Vượt tối đa 5 giá trị. |
| `sample_values` chứa mã không có trong DB | Mẫu bịa, validator kiểm bằng truy vấn sẽ bác. |
| `distinct_count` là 2000 nhưng `distinct_values` liệt kê 2000 giá trị | Chỉ liệt kê đầy đủ khi không quá 50, trường hợp này phải để `null`. |
| `distinct_count` là 4 nhưng `distinct_values` chỉ có 3 giá trị | Liệt kê đầy đủ thì số lượng phải khớp với COUNT(DISTINCT) đo được. |
| Join chỉ ghi evidence "tên cột giống nhau" | Thiếu tỷ lệ ID khớp đo được, không phải bằng chứng. |
| Schema có bảng không có trong DB tạm | Mọi bảng trong schema phải có trong DB và ngược lại. |
| Schema ghi "doanh thu tháng 5 là 120000000" | File schema không chứa dữ liệu bán hàng. |
| Trường `model` ghi kèm key | Chỉ ghi tên model, không ghi thông tin xác thực. |

## Ghi chú bẫy hay gặp — viết thế nào cho đúng

- Sai: "Đừng đếm trùng." (chung chung, không nói bảng nào, không nói
  vì sao).
- Đúng: "Không cộng dồn chi_tiet_don_lo để tính doanh thu (bảng tách
  lô, sẽ đếm trùng)."
- Sai: "Giá lấy từ bảng giá." (không nói bảng nào, không nói khi nào).
- Đúng: "Giá tại một ngày lấy từ lich_su_gia, không lấy giá hiện tại
  trong san_pham."
- Sai: "Có nhiều trạng thái." (không liệt kê, workflow trả lời không
  dùng được).
- Đúng: liệt kê đầy đủ các trạng thái trong `distinct_values` của cột
  trạng thái (khi không quá 50 giá trị) và ghi chú thêm trong `notes`
  nếu một trạng thái có nghĩa đặc biệt trong shop này.

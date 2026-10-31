---
name: datamate-merge
description: "Quyết định gộp các sheet thành một bảng logic trong ingest DataMate theo bằng chứng đo được: cùng chữ ký cột chuẩn hóa, cùng vai trò, ID và ngày liên tục, không trùng ID vô lý; cấm gộp sheet khác vai trò chỉ vì cùng file và cấm gộp chi tiết với chi tiết lô khi chưa so sánh toàn bộ schema."
---

# Skill: datamate-merge

## Tài nguyên của skill này — đọc kỹ trước khi load

- Tài nguyên duy nhất của skill này là `references/vi-du-gop.md`.
  Ngoài file đó, skill này KHÔNG có thêm file mẫu, template hay assets
  nào khác.
- Chỉ load đúng file đã nêu tên ở trên. KHÔNG đoán tên file khác, vì
  các file đó không tồn tại và mỗi lần load lỗi chỉ tốn thêm một lượt
  gọi model mà không thêm thông tin.
- Nếu load tài nguyên lỗi, bỏ qua tài nguyên đó và tiếp tục chỉ với nội
  dung SKILL.md này — không thử lại bằng đường dẫn khác.

## Khi nào dùng skill này

- Dùng ở node MergeAgent trong workflow ingest, sau khi header của các
  bảng đã chốt.
- Đầu vào là danh sách các bảng đã quét (mỗi bảng có header chốt, mẫu,
  thống kê: số dòng, distinct ID, khoảng ngày) và README của shop nếu có.
- Skill này chỉ quyết định **nhóm gộp và bảng giữ riêng**, không làm
  sạch dữ liệu, không đổi header, không viết schema.

## Nguyên tắc chung

- Gộp sheet là một quyết định có bằng chứng, không phải quyết định làm
  gọn file. Gộp sai gây đếm trùng, tách sai chỉ gây bất tiện khi truy
  vấn. Khi bằng chứng không đủ mạnh thì giữ riêng an toàn hơn gộp sai.
- Mọi nhóm gộp đều phải có bằng chứng đo được đi kèm. Không có bằng
  chứng thì không gộp.
- Quyết định cho shop mới phải từ dữ liệu của chính shop đó, không suy
  từ shop khác, không hard-code theo business_id hay tên sheet của
  split train.

## Làm theo từng bước

### Bước 1 — Tính chữ ký cột chuẩn hóa cho mỗi bảng

1. Với mỗi bảng, lấy header chốt, chuẩn hóa từng tên cột: viết thường,
   bỏ dấu tiếng Việt, bỏ ký tự thừa, thay khoảng trắng bằng gạch dưới.
2. Chữ ký cột của bảng là tập hợp các tên cột đã chuẩn hóa đó.
3. Hai bảng chỉ được xét gộp khi chữ ký cột của chúng giống nhau, hoặc
   khác nhau chỉ ở các cột phụ đã giải thích được (ví dụ một bảng có
   thêm cột ghi chú trống). Khác nhau ở cột khóa (mã đơn, mã hàng, mã
   khách) hay cột tiền/số lượng thì không gộp.
4. Nếu thứ tự cột khác nhau nhưng tập tên cột giống nhau thì vẫn có thể
   gộp, nhưng khi gộp phải đối chiếu theo tên cột, không đối chiếu theo
   vị trí cột.

### Bước 2 — Xác định vai trò của mỗi bảng

Xác định vai trò từ header, mẫu và thống kê, ví dụ: đơn hàng, chi tiết
đơn, thanh toán, khách hàng, sản phẩm, quy đổi đơn vị, nhập hàng, tồn
kho lô, trả hàng.

- Chỉ các bảng cùng vai trò mới được xét gộp.
- Bảng khác vai trò thì giữ riêng, dù chữ ký cột có giống nhau và dù
  chúng nằm trong cùng một file (xem phần cấm).
- Vai trò không chắc thì ghi rõ không chắc và giữ riêng, không đoán vai
  trò để gộp cho xong.

### Bước 3 — Kiểm tra ID và ngày liên tục

Với mỗi cặp bảng cùng chữ ký cột và cùng vai trò, kiểm tra:

1. **ID liên tục hoặc không chồng chéo vô lý:** các ID (mã đơn, mã
   dòng, mã hàng) của hai bảng hoặc rời nhau, hoặc nối tiếp nhau, hoặc
   trùng nhau nhưng là cùng một dòng lặp lại (xem bước 4). ID của hai
   bảng không được nhảy cóc một cách không giải thích được mà vẫn gộp
   bừa.
2. **Ngày liên tục:** khoảng ngày của hai bảng hoặc nối tiếp nhau, hoặc
   chồng lấn nhau trong cùng một kỳ, không phải hai kỳ rời rạc không
   liên quan mà vẫn gộp thành một bảng.
3. **Số dòng hợp lý:** tổng số dòng sau gộp phải bằng tổng số dòng các
   bảng thành phần trừ đi số dòng trùng đã xác định ở bước 4. Số dòng
   tăng vọt không giải thích được là dấu hiệu gộp sai.

### Bước 4 — Kiểm tra trùng ID (ID overlap)

1. Tính tập ID chung của hai bảng và tỷ lệ trùng.
2. **Trùng nhưng cùng nội dung:** cùng một ID, các cột còn lại giống
   nhau — đó là bản sao, gộp được và bước làm sạch sẽ dedup, có đếm.
3. **Trùng nhưng nội dung mâu thuẫn:** cùng một ID nhưng ngày, tiền,
   số lượng hay trạng thái khác nhau — đây là mâu thuẫn, **không gộp**.
   Giữ riêng hai bảng, ghi rõ ID mâu thuẫn làm bằng chứng, để bước sau
   xử lý riêng thay vì tạo ra một bảng chứa hai sự thật khác nhau cho
   cùng một ID.
4. **Trùng ID vô lý:** một ID xuất hiện với tần suất bất thường (ví dụ
   cùng một mã đơn lặp lại hàng trăm lần với nội dung khác nhau) thì
   không gộp cho đến khi hiểu rõ vì sao trùng.

### Bước 5 — Trường hợp đặc biệt: chi tiết và chi tiết lô

- Bảng chi tiết đơn (ChiTiet) và bảng chi tiết lô (ChiTietLo) là hai
  vai trò khác nhau về bản chất: một dòng chi tiết có thể tách thành
  nhiều dòng lô. Gộp hai bảng này sẽ đếm trùng doanh thu và số lượng.
- **Cấm gộp ChiTiet với ChiTietLo khi chưa so sánh toàn bộ schema.**
  So sánh toàn bộ nghĩa là: toàn bộ tên cột và kiểu cột, số dòng, tập
  ID, cột lô (mã lô, ngày nhập lô) có mặt ở bảng nào, và tổng tiền/số
  lượng của hai bảng có quan hệ tách lô hay không.
- Mặc định là **không gộp** cặp này. Chỉ gộp khi so sánh toàn bộ schema
  chứng minh được hai bảng thực chất là một bảng bị tách đôi (cùng cột,
  cùng vai trò, ID/ngày liên tục, không có cột lô phân biệt), và phải
  ghi rõ bằng chứng so sánh đó.
- Tương tự, bất kỳ cặp bảng nào mà một bên có thêm cột phân tách chi
  tiết (lô, đợt, kho) thì đều phải so sánh toàn bộ schema trước khi
  gộp, không được gộp chỉ vì tên bảng giống nhau.

### Bước 6 — Chốt nhóm gộp và bảng giữ riêng

1. Nhóm các bảng thành từng nhóm gộp, mỗi nhóm thỏa đủ: cùng chữ ký cột,
   cùng vai trò, ID/ngày liên tục, ID overlap không mâu thuẫn.
2. Mọi bảng không vào được nhóm nào thì giữ riêng, kèm lý do giữ riêng.
3. Ghi rõ cho mỗi nhóm: bằng chứng chữ ký cột, bằng chứng vai trò,
   khoảng ID, khoảng ngày, tỷ lệ ID trùng và kết luận trùng (cùng nội
   dung hay mâu thuẫn).

## Cấm làm gì

- **Cấm gộp sheet khác vai trò chỉ vì cùng file.** Các sheet trong cùng
  một file XLSX có thể là đơn hàng, chi tiết đơn, sản phẩm, khách hàng
  — khác vai trò thì giữ riêng, dù chúng nằm chung file.
- **Cấm gộp ChiTiet với ChiTietLo khi chưa so sánh toàn bộ schema.**
  Nguy cơ đếm trùng là lý do cấm tuyệt đối này, không có ngoại lệ theo
  cảm giác "tên giống nhau".
- **Cấm gộp khi ID overlap mâu thuẫn.** Cùng ID nhưng nội dung khác
  nhau thì không gộp, dù chữ ký cột và vai trò giống nhau.
- Cấm gộp chỉ vì tên sheet giống nhau, tên file giống nhau, hay số dòng
  gần nhau. Tên và số dòng không phải bằng chứng gộp.
- Cấm gộp bảng của hai shop khác nhau, cấm gộp bảng đã là bảng tổng hợp
  với bảng chi tiết (cộng dồn sẽ đếm trùng).
- Cấm bịa bằng chứng: tỷ lệ ID trùng, khoảng ngày, chữ ký cột đều phải
  là số đo được từ mẫu và thống kê, không được ước lượng bằng lời.

## Output kỳ vọng

Một MergeProposal cho shop, gồm:

- `nhóm gộp`: danh sách các nhóm, mỗi nhóm liệt kê các bảng thành phần.
- `bằng chứng gộp`: cho mỗi nhóm — chữ ký cột chuẩn hóa, vai trò, khoảng
  ID, khoảng ngày, tỷ lệ ID trùng và kết luận trùng.
- `bảng giữ riêng`: danh sách các bảng không gộp, kèm lý do giữ riêng
  cho từng bảng (khác vai trò, khác chữ ký cột, ID overlap mâu thuẫn,
  chưa so sánh đủ schema cho cặp chi tiết/lô, hay vai trò không chắc).

Validator code sẽ kiểm từng nhóm theo đúng các tiêu chí trên (chữ ký
cột, vai trò, ID overlap, ngày liên tục, so sánh toàn bộ schema cho
trường hợp lot-detail). Nhóm nào vi phạm thì bị bác và đẩy lại cho
MergeAgent viết lại; hết số lần viết lại cho phép mà vẫn sai thì giữ
mỗi sheet là một bảng riêng (an toàn hơn gộp sai gây đếm trùng) và ghi
rõ vào log.

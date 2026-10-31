---
name: datamate-header
description: "Xác định dòng header thật trong sheet bẩn của shop DataMate: bỏ dòng tiêu đề, logo và dòng trống, xử lý header không ở dòng 1, sheet hai tầng tiêu đề và dòng tổng cuối sheet, chấm điểm header và nhận biết khi header không chắc chắn."
---

# Skill: datamate-header

## Tài nguyên của skill này — đọc kỹ trước khi load

- Tài nguyên duy nhất của skill này là `references/vi-du-header-kho.md`.
  Ngoài file đó, skill này KHÔNG có thêm file mẫu, template hay assets
  nào khác.
- Chỉ load đúng file đã nêu tên ở trên. KHÔNG đoán tên file khác, vì
  các file đó không tồn tại và mỗi lần load lỗi chỉ tốn thêm một lượt
  gọi model mà không thêm thông tin.
- Nếu load tài nguyên lỗi, bỏ qua tài nguyên đó và tiếp tục chỉ với nội
  dung SKILL.md này — không thử lại bằng đường dẫn khác.

## Khi nào dùng skill này

- Dùng ở node HeaderAgent trong workflow ingest, cho các bảng đã được quét
  nhưng bị gắn cờ không chắc chắn về header (ambiguous).
- Bảng nào đã chắc chắn từ bước quét thì đi thẳng qua, không cần dùng
  skill này.
- Skill này chỉ quyết định **dòng header và tên cột**, không làm sạch dữ
  liệu, không tính toán số liệu, không gộp bảng, không viết schema.

## Đầu vào được xem xét

- Tối đa 15 dòng đầu của sheet (giống bước quét đã kiểm chứng), kèm mẫu
  gồm 10 dòng đầu sau header ứng viên và 10 dòng ngẫu nhiên có seed cố
  định ở giữa/cuối sheet để bắt cột thưa.
- Tên sheet, tên file và đoạn README liên quan (nếu có) chỉ dùng để tham
  khảo vai trò, không dùng để bịa tên cột.
- Không bao giờ xem toàn bộ sheet, không xem toàn bộ dữ liệu bán hàng.

## Làm theo từng bước

### Bước 1 — Loại bỏ những dòng không phải header

Loại bỏ, theo thứ tự từ trên xuống, các dòng sau:

1. Dòng trống hoàn toàn, hoặc chỉ có một ô có giá trị.
2. Dòng tiêu đề, tên shop, logo, địa chỉ, số điện thoại: thường là một
   ô text dài trải ngang, các ô còn lại trống, không có nhiều ô text
   ngắn song song nhau.
3. Dòng ghi chú, dòng ngày in, dòng "bảng kê", "danh sách" đứng một mình
   mà không có các tên cột đi kèm ngay sau.
4. Dòng tổng ở cuối sheet (xem bước 4) không bao giờ là header.

Một dòng chỉ được coi là header ứng viên khi nó có nhiều ô text ngắn
đứng cạnh nhau, mỗi ô giống tên một trường dữ liệu hơn là một câu.

### Bước 2 — Tìm dòng header thật, kể cả khi nó không ở dòng 1

1. Quét các dòng còn lại trong 15 dòng đầu, không mặc định dòng đầu
   tiên khác trống là header.
2. Với mỗi dòng ứng viên, chấm điểm theo bước 3 và chọn dòng có điểm
   cao nhất.
3. Nếu hai dòng liên tiếp đều giống header (sheet hai tầng tiêu đề),
   xem bước 5 trước khi chọn.

### Bước 3 — Chấm điểm một dòng header ứng viên

Chấm điểm dựa trên bằng chứng đo được, không dựa trên cảm giác:

1. **Tỷ lệ ô text:** dòng header tốt có đa số ô là text ngắn, không
   phải số, không phải ngày, không phải ô trống. Ô trống nhiều, hoặc
   toàn số, thì trừ điểm mạnh.
2. **Không trùng lặp:** sau khi chuẩn hóa (viết thường, bỏ dấu, bỏ ký
   tự thừa, thay khoảng trắng bằng gạch dưới), các tên cột trong dòng
   không được trùng nhau. Dòng có tên cột trùng nhau sau chuẩn hóa thì
   trừ điểm mạnh, vì nó sẽ gây xung đột tên cột SQL.
3. **Cột bên dưới có giá trị thật:** mỗi cột dưới dòng ứng viên phải có
   giá trị thật trong các dòng mẫu, không phải toàn trống. Một cột toàn
   trống trong toàn bộ mẫu là dấu hiệu dòng ứng viên sai, hoặc cột đó
   là cột trang trí.
4. **Kiểu dữ liệu cột bên dưới nhất quán:** các giá trị dưới một cột
   có xu hướng cùng một họ kiểu (ngày, tiền, số lượng, mã, text trạng
   thái). Một cột mà giá trị dưới nó lộn xộn không theo họ nào thì dòng
   ứng viên đáng ngờ.
5. **Không phải dòng dữ liệu:** dòng ứng viên có ô là ngày, tiền, số
   lượng cụ thể của một đơn hàng thì đó là dòng dữ liệu, không phải
   header, dù nó đứng ở đầu sheet.
6. **Không phải dòng tổng:** dòng có từ "tổng", "total", "cộng" kèm các
   ô số lớn thì không phải header (xem bước 4).

Dòng được chọn là dòng có điểm cao nhất **và** vượt qua các kiểm tra
cứng ở bước 6. Nếu không có dòng nào vượt qua, kết luận header không
chắc theo bước 7, không cố chọn một dòng cho xong.

### Bước 4 — Dòng tổng cuối sheet

- Dòng cuối sheet chứa từ "tổng", "total", "tổng cộng" kèm các ô số là
  dòng tổng, không phải dữ liệu, không phải header.
- Dòng tổng không được tính vào mẫu dữ liệu, không được dùng để suy
  kiểu cột, không được đưa vào số dòng của bảng.
- Nếu dòng tổng nằm ngay sau header mà không có dòng dữ liệu nào ở
  giữa, đó là dấu hiệu sheet trống hoặc sheet chỉ có tiêu đề — báo
  không chắc thay vì bịa header.

### Bước 5 — Sheet hai tầng tiêu đề

- Khi hai dòng liên tiếp đều giống header (tầng trên là nhóm cột như
  "thông tin đơn", "thông tin hàng", tầng dưới là tên cột cụ thể):
  tầng dưới là tên cột chính, tầng trên chỉ là nhóm, không phải tên cột.
- Tên cột chuẩn hóa lấy từ tầng dưới. Chỉ khi tầng dưới có ô trống mà
  tầng trên tại cùng vị trí có giá trị thì mới ghép tầng trên + tầng
  dưới thành một tên, và phải ghi rõ việc ghép này trong lý do.
- Không được lấy tầng trên làm header và bỏ tầng dưới, vì như vậy sẽ
  mất tên cột cụ thể và gây trùng tên cột.
- Không được gộp hai tầng thành một tên dài một cách tùy tiện cho mọi
  cột khi tầng dưới đã đủ nghĩa.

### Bước 6 — Chuẩn hóa tên cột

- Tên cột chuẩn hóa: viết thường, bỏ dấu tiếng Việt, bỏ ký tự thừa
  (ngoặc, dấu hai chấm, khoảng trắng thừa), thay khoảng trắng bằng
  gạch dưới.
- Giữ đúng thứ tự cột như trong sheet. Không sắp xếp lại, không bỏ cột
  chỉ vì tên cột lạ, không đổi tên cột thành tên mình đoán.
- Nếu sau chuẩn hóa mà hai cột trùng tên, đó là lỗi cần báo, không
  được tự ý thêm hậu tố số để lấp đi mà không ghi rõ trong lý do.

### Bước 7 — Khi nào thì header không chắc

Báo header **không chắc** (ambiguous) khi gặp bất kỳ trường hợp nào sau:

- Hai dòng ứng viên có điểm gần nhau, không có dòng nào vượt trội rõ rệt.
- Dòng ứng viên tốt nhất vẫn có cột toàn trống, hoặc tên cột trùng nhau
  sau chuẩn hóa, hoặc kiểu dữ liệu cột bên dưới lộn xộn.
- Sheet có ít dòng mẫu quá (ví dụ chỉ có header mà không có dòng dữ
  liệu) nên không kiểm chứng được cột bên dưới.
- Header có thể nằm ngoài 15 dòng đầu đã quét, hoặc sheet dùng cột gộp
  (merged cell) khiến tên cột không nằm đúng vị trí cột dữ liệu.

Khi không chắc: vẫn đề xuất dòng tốt nhất kèm điểm và lý do, nhưng ghi
rõ cờ không chắc và ghi rõ điểm yếu (cột nào trống, dòng nào cạnh
tranh, trùng lặp ở đâu). Code validator sẽ chấm lại bằng scoring và
mẫu; nếu vẫn sai sau số lần viết lại cho phép thì workflow quay về
luật scoring đã kiểm chứng bằng mẫu và ghi rõ "rule fallback" vào log,
không dừng cả shop vì một bảng, và tuyệt đối không bịa header.

## Cấm làm gì

- Cấm lấy dòng đầu tiên khác trống làm header một cách mặc định.
- Cấm lấy dòng tiêu đề, logo, ghi chú làm header.
- Cấm lấy dòng tổng cuối sheet làm header hoặc làm dữ liệu.
- Cấm bịa tên cột không có trong sheet, cấm bỏ cột trùng tên một cách
  lặng lẽ, cấm đổi thứ tự cột.
- Cấm làm sạch dữ liệu, cấm tính tổng, cấm suy ra vai trò bảng hay
  quyết định gộp sheet — đó là việc của các bước khác.
- Cấm dùng business_id, tên cột hay rulebook của split train để đoán
  header của shop mới. Mọi quyết định phải từ bằng chứng trong chính
  sheet này.

## Output kỳ vọng

Một HeaderProposal cho mỗi bảng không chắc, gồm:

- `dòng header`: số dòng header trong sheet gốc (đếm từ 1, tính cả
  dòng tiêu đề và dòng trống phía trên).
- `tên cột chuẩn hóa`: danh sách tên cột theo đúng thứ tự trong sheet.
- `lý do`: điểm mạnh của dòng được chọn, các dòng cạnh tranh đã loại
  và vì sao loại, các điểm yếu còn lại (nếu có).
- `cờ không chắc`: có hay không, kèm điểm yếu cụ thể khi có.

Validator code sẽ chấm lại đề xuất bằng scoring và kiểm tra mẫu trước
khi header được chốt. Đề xuất không qua kiểm chứng thì không được ghi
vào state làm header chốt.

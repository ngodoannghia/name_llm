---
name: datamate-cleaning
description: "Quy tắc làm sạch dữ liệu shop DataMate cho code làm sạch hàng loạt: parse tiền nhiều kiểu phân cách, parse ngày nhiều định dạng text, quy đổi đơn vị chỉ khi có hệ số, ô không parse được thành NULL và đếm, forward-fill có giới hạn, dedup và suy luận kiểu cột."
---

# Skill: datamate-cleaning

## Tài nguyên của skill này — đọc kỹ trước khi load

- Tài nguyên duy nhất của skill này là `references/quy-tac-lam-sach.md`.
  Ngoài file đó, skill này KHÔNG có thêm file mẫu, template hay assets
  nào khác.
- Chỉ load đúng file đã nêu tên ở trên. KHÔNG đoán tên file khác, vì
  các file đó không tồn tại và mỗi lần load lỗi chỉ tốn thêm một lượt
  gọi model mà không thêm thông tin.
- Nếu load tài nguyên lỗi, bỏ qua tài nguyên đó và tiếp tục chỉ với nội
  dung SKILL.md này — không thử lại bằng đường dẫn khác.

## Ghi rõ trước tiên: skill này là tài liệu quy tắc cho code, không phải việc của LLM từng dòng

- Skill này mô tả quy tắc mà **code làm sạch** (node clean_tables, chạy
  bằng pandas và bộ parse tiền/ngày/số lượng) áp dụng cho toàn bộ dữ
  liệu của shop.
- **LLM không làm sạch từng dòng, không sửa từng ô, không tính lại từng
  giá trị.** LLM chỉ đọc skill này để hiểu quy tắc khi xem mẫu và mô tả
  schema, còn việc làm sạch hàng loạt luôn do code thực hiện.
- Mọi quy tắc dưới đây đều áp dụng thống nhất cho mọi dòng trong bảng,
  không áp dụng riêng lẻ theo cảm giác cho từng ô.
- Code thực thi nằm ở node làm sạch, không nằm trong skill này. Skill
  này không chứa script thực thi.

## Khi nào dùng skill này

- Dùng làm tài liệu tham khảo cho node clean_tables, sau khi các bảng
  đã gộp xong và trước khi ghi DB tạm.
- Dùng để hiểu vì sao một ô trở thành NULL, vì sao một số lượng được
  quy đổi hay bị để trống, và vì sao kiểu cột được suy luận như vậy.
- Không dùng skill này để đoán đáp án câu hỏi, không dùng để sửa số
  liệu cho khớp với một kết quả mong muốn.

## Nguyên tắc chung: không đoán, không bịa

1. Ô không parse được thì thành NULL và được đếm, không điền giá trị
   bịa, không điền 0, không điền giá trị trung bình, không copy giá trị
   của dòng khác (trừ forward-fill đúng phạm vi ở mục 5).
2. Thiếu hệ số quy đổi thì để trống và đếm, không đoán hệ số bằng 1
   (xem mục 4).
3. Mọi số lượng ô NULL, dòng trùng đã bỏ, dòng vào/ra đều được đếm vào
   thống kê làm sạch (`_clean_stats`) để bước kiểm chứng soi được.
4. Shop có tỷ lệ NULL bất thường thì được cảnh báo trong báo cáo ingest,
   không được giấu bằng cách điền giá trị.

## 1. Parse tiền — về INTEGER VND

Quy tắc parse một ô tiền:

1. Bỏ khoảng trắng, ký hiệu tiền tệ ("VND", "đ", "₫") và khoảng trắng
   phân cách nhóm ("1 200 000" thành "1200000").
2. Nhận biết dấu phân cách nghìn theo mẫu của cột:
   - "1.200.000" (chấm phân cách nghìn) thành 1200000.
   - "1,200,000" (phẩy phân cách nghìn) thành 1200000.
   - "1200000" (không có phân cách) thành 1200000.
   - Khi một ô vừa có chấm vừa có phẩy, ký tự phân cách cuối cùng là
     phân cách thập phân, các ký tự trước đó là phân cách nghìn; vì tiền
     VND là số nguyên nên phần thập phân (nếu có) được làm tròn half-up
     về số nguyên.
3. Hậu tố đơn vị: "1.2tr", "1,2tr", "1.2 triệu" thành 1200000 (tr/triệu
   nghĩa là nhân với 1000000); "1.2k", "1.2 nghìn" thành 1200 (k/nghìn
   nghĩa là nhân với 1000). Hậu tố chỉ áp dụng khi nó đứng ngay sau số
   và là hậu tố đã biết; hậu tố lạ thì ô đó thành NULL, không đoán.
4. Số âm: dấu trừ đứng trước số, hoặc số trong ngoặc đơn, đều là số âm.
5. Trường hợp mơ hồ không phân giải được thì thành NULL và đếm:
   - "1.200" mà cột không có mẫu đa số rõ ràng để biết đó là 1200 hay
     1,2 — dùng mẫu đa số của cột để quyết định, nếu cột cũng không có
     đa số rõ ràng thì thành NULL.
   - Ô chứa chữ không phải hậu tố đã biết, ô chứa nhiều số không rõ số
     nào là tiền, ô trống mà không thuộc phạm vi forward-fill.
6. Kiểu suy luận: cột tiền sau khi parse thành INTEGER VND. Không giữ
   tiền ở dạng text, không giữ tiền ở dạng số thực có phần thập phân.

## 2. Parse ngày — về TEXT ISO

Quy tắc parse một ô ngày:

1. Ngày sau khi parse được lưu dạng TEXT ISO "YYYY-MM-DD". Không lưu
   ngày ở dạng text gốc nhiều định dạng lẫn lộn trong cùng một cột.
2. Các định dạng text được chấp nhận:
   - "01/05/2025", "1/5/2025" (ngày/tháng/năm, theo quy ước Việt Nam:
     số đầu là ngày, số giữa là tháng).
   - "2025-05-01" (năm-tháng-ngày, không mơ hồ).
   - "01-05-2025", "1-5-2025" (ngày-tháng-năm).
   - "1 thg 1 2025", "1 tháng 1 2025", "ngày 1 tháng 1 năm 2025" (tháng
     viết bằng chữ tiếng Việt).
   - Ngày giờ kèm giờ ("01/05/2025 10:30") thì chỉ lấy phần ngày, bỏ
     phần giờ, và ghi rõ việc này trong thống kê.
3. Quy ước ngày/tháng: với định dạng gạch chéo và gạch ngang, số đầu là
   ngày theo quy ước Việt Nam. Nếu số giữa lớn hơn 12 thì đó là bằng
   chứng số đầu là tháng, nhưng trường hợp này phải được cột xác nhận
   bằng mẫu đa số; nếu cột không có mẫu đa số rõ ràng thì các ô mơ hồ
   (cả hai số đều nhỏ hơn hoặc bằng 12) vẫn theo quy ước ngày/tháng
   Việt Nam, không tự đổi sang tháng/ngày.
4. Ngày không hợp lệ thì thành NULL và đếm: ngày 32, tháng 13, năm
   thiếu số, chuỗi không chứa ngày, ô trống ngoài phạm vi forward-fill.
5. Số serial ngày của Excel chỉ được đổi thành ngày khi bối cảnh cột
   chứng minh được cột đó dùng serial (đa số giá trị là số trong khoảng
   ngày hợp lý và header/mẫu cho thấy đó là cột ngày); nếu không chứng
   minh được thì thành NULL, không đoán.

## 3. Số lượng — parse số, giữ nguyên nghĩa

1. Số lượng parse từ text có phân cách nghìn giống tiền, nhưng không có
   hậu tố tiền tệ và không nhân với 1000000 vì hậu tố "tr".
2. Số lượng giữ dấu thập phân khi dữ liệu gốc có thập phân (ví dụ cân
   theo kg có thể là 1,5). Kiểu suy luận: INTEGER nếu toàn bộ cột sau
   parse đều là số nguyên, REAL nếu cột có giá trị thập phân thật.
3. Ô số lượng không parse được thì thành NULL và đếm, không điền 0
   (0 nghĩa là không bán, khác với không biết số lượng).

## 4. Quy đổi đơn vị — chỉ quy đổi khi có hệ số

1. Mỗi sản phẩm có thể bán theo nhiều đơn vị (cái, hộp, thùng, kg...).
   Số lượng gốc chỉ được đổi sang số lượng cơ sở khi có hệ số quy đổi
   cho đúng sản phẩm đó và đúng đơn vị đó trong bảng quy đổi của shop.
2. Công thức: số lượng cơ sở = số lượng gốc × hệ số. Hệ số phải là số
   dương, có nguồn từ bảng quy đổi; hệ số bằng 0, âm, hoặc không phải
   số thì không dùng, ô đó thành NULL và đếm.
3. **Không có hệ số thì để trống và đếm, không đoán bằng 1.** Đoán bằng
   1 nghĩa là giả vờ đơn vị bán đã là đơn vị cơ sở, và sẽ làm sai mọi
   tổng số lượng sau đó. Đây là quy tắc cấm tuyệt đối.
4. Ngoại lệ duy nhất không phải đoán: khi bảng quy đổi hoặc bảng sản
   phẩm ghi rõ đơn vị đó chính là đơn vị cơ sở của sản phẩm thì hệ số 1
   là hệ số đã ghi rõ, không phải hệ số đoán. Trường hợp này phải có
   bằng chứng ghi rõ, không suy từ tên đơn vị giống nhau.
5. Nếu một sản phẩm có nhiều hệ số cho cùng một đơn vị (mâu thuẫn) thì
   các ô liên quan thành NULL và đếm, không chọn bừa một hệ số.
6. Số lượng sau quy đổi mà đơn vị gốc đã là đơn vị cơ sở thì giữ nguyên
   giá trị, không nhân thêm lần nữa.

## 5. Forward-fill — chỉ trong phạm vi hẹp

1. Forward-fill nghĩa là ô trống được điền bằng giá trị của dòng ngay
   trên trong cùng một cột.
2. Forward-fill **chỉ** áp dụng cho cột mã/nhóm mà dữ liệu gốc ghi tắt
   theo khối: ví dụ mã đơn chỉ ghi ở dòng đầu của đơn nhiều dòng, các
   dòng sau để trống nhưng vẫn thuộc cùng một đơn. Dấu hiệu là cột có
   các khối giá trị lặp lại, ô trống nằm giữa các dòng cùng khối.
3. Forward-fill **không** áp dụng cho tiền, ngày, số lượng, trạng thái,
   giá, hay bất kỳ cột giá trị nào. Ô tiền trống không được điền bằng
   tiền của dòng trên; ô ngày trống không được điền bằng ngày dòng trên.
4. Forward-fill không vượt qua ranh giới bảng, không vượt qua dòng
   trống phân cách hai khối, không điền khi không có giá trị phía trên.
5. Mọi ô đã forward-fill đều được đếm riêng trong thống kê làm sạch để
   bước kiểm chứng biết có bao nhiêu ô đã điền theo cách này.

## 6. Dedup — bỏ dòng trùng giống hệt, giữ lại dòng mâu thuẫn

1. Sau khi gộp và làm sạch, các dòng giống hệt nhau trên toàn bộ cột
   (sau khi đã chuẩn hóa tiền/ngày/số lượng) được coi là trùng lặp và
   chỉ giữ một dòng, số dòng đã bỏ được đếm — NHƯNG chỉ khi bảng có cột
   mã đơn (ngữ nghĩa order_id). Rulebook §8 chỉ coi "2 dòng giống hệt
   của cùng 1 đơn" là lỗi export; bảng không có mã đơn thì hai dòng
   giống hệt nhau không chứng minh được là cùng một đơn (có thể là hai
   lần bán riêng) nên phải giữ nguyên toàn bộ dòng, không dedup.
2. Các dòng gần giống nhưng khác nhau ở bất kỳ cột giá trị nào (tiền,
   số lượng, trạng thái, ngày) thì **không** được dedup. Đó có thể là
   hai dòng thật khác nhau, hoặc là mâu thuẫn cần giữ lại để bước sau
   soi, không được tự ý bỏ một dòng.
3. Dedup không áp dụng cho bảng tổng hợp và bảng chi tiết gộp chung với
   nhau, vì đó là hai vai trò khác nhau (xem skill datamate-merge).

## 7. Suy luận kiểu cột

| Họ cột | Kiểu trong SQLite | Ghi chú |
|--------|-------------------|---------|
| Tiền | INTEGER (VND) | Sau parse, không giữ text hay số thực thập phân. |
| Ngày | TEXT ISO "YYYY-MM-DD" | Không giữ nhiều định dạng text lẫn lộn. |
| Số lượng | INTEGER hoặc REAL | INTEGER nếu toàn cột là số nguyên, REAL nếu có thập phân thật. |
| Mã (đơn, hàng, khách) | TEXT | Giữ nguyên, kể cả số 0 đứng đầu; không đổi mã thành số. |
| Trạng thái, thanh toán, text | TEXT | Giữ nguyên text gốc sau khi cắt khoảng trắng thừa. |

Kiểu suy luận dựa trên mẫu và thống kê toàn cột, không dựa trên một ô
duy nhất. Cột có đa số giá trị parse được theo một kiểu nhưng có một số
ô không parse được thì vẫn giữ kiểu đó, các ô lỗi thành NULL và đếm.

## Cấm làm gì

- Cấm đoán hệ số quy đổi bằng 1 khi không có hệ số.
- Cấm điền giá trị bịa cho ô không parse được (0, trung bình, giá trị
  dòng khác) ngoài forward-fill đúng phạm vi ở mục 5.
- Cấm forward-fill tiền, ngày, số lượng, trạng thái, giá.
- Cấm dedup dòng chỉ gần giống nhau, cấm dedup bảng tổng hợp với bảng
  chi tiết.
- Cấm đổi mã thành số làm mất số 0 đứng đầu, cấm đổi ngày thành nhiều
  định dạng lẫn lộn, cấm giữ tiền ở dạng text.
- Cấm LLM sửa từng dòng thay cho code; cấm code sửa số liệu cho khớp
  với một kết quả mong muốn.

## Output kỳ vọng

Sau khi code làm sạch chạy xong, mỗi bảng có:

- Các DataFrame đã sạch trong bộ nhớ tạm của node (không đưa vào state,
  state chỉ giữ thống kê và đường dẫn tạm).
- Cột TEXT nào trông như cột tiền bị map sót (ít nhất 80% mẫu parse
  được thành tiền và mẫu chứa dấu bẩn như dấu phẩy, 'đ', 'k', 'tr')
  thì được gắn cờ `suspected_money` trong manifest — chỉ cảnh báo để
  schema/QA biết, không tự format bừa khi ngữ nghĩa chưa chắc.
- Thống kê làm sạch `_clean_stats`: số dòng vào, số dòng ra, số dòng
  trùng đã bỏ, số ô NULL do không parse được (theo từng cột và từng lý
  do: tiền, ngày, số lượng, thiếu hệ số), số ô đã forward-fill.
- Kiểu suy luận cho từng cột theo bảng ở mục 7, để bước ghi DB tạm và
  bước mô tả schema dùng cùng một kiểu.

# Báo cáo Giai đoạn 2 DataMate — Tích hợp end-to-end

> Chạy offline, deterministic, không finetune, không gọi mạng/API.
> Đáp án train chỉ dùng để CHẤM và phân tích lỗi, không dùng để quyết
> định đáp án, không hard-code theo business_id / id câu hỏi / đáp án.

## 1. Tóm tắt kết quả (đợt chạy cuối, sau mọi sửa lỗi tích hợp)

| Chỉ số | Giá trị |
|---|---|
| Tổng số câu train đã chạy | 3.000 / 3.000 (đủ 30 shop, không bỏ shop nào) |
| Đúng (correct) | 581 |
| Sai (wrong) | 1.217 |
| Abstain (chấm) | 1.202 |
| **PA tổng = (đúng − sai) / tổng** | **−0,212** |
| Trả lời (answered, pipeline) | 1.782 (59,4%) |
| Abstain (pipeline) | 1.202 (40,1%) |
| Dự đoán unanswerable (pipeline) | 16 (0,5%) |
| Tỉ lệ đúng trong số câu đã trả lời | 581 / 1.798 ≈ 32,3% |
| PA holdout (biz_124, 123, 106, 105, 117) | −0,356 (54 đúng / 265 sai / 592 câu) |
| PA các shop còn lại | −0,176 (527 đúng / 952 sai / 2.408 câu) |
| Kiểm thử tích hợp | 29/29 pass (`python3 -m unittest discover -s tests`) |
| Thời gian đợt chạy cuối | 1.116 giây (~18,6 phút); cache hit 20 shop, miss 10 shop |
| Shop lỗi không kiểm soát | 0 (mọi shop đều chạy hết; `shop_errors` rỗng) |

**Đọc kết quả một cách trung thực:** PA âm nghĩa là trả lời đang làm
hại điểm so với abstain toàn bộ (PA = 0). Nguyên nhân chính không
phải là "trả lời ít" mà là "trả lời sai nhiều": trong số câu pipeline
dám trả lời, chỉ ~1/3 là đúng. Vì vậy Giai đoạn 3 phải ưu tiên tăng
độ chính xác và tăng abstain có chọn lọc (khi bằng chứng yếu) hơn là
tăng độ bao phủ bằng mọi giá. Các con số dưới đây là bằng chứng cho
nhận định đó, không phải để khoe.

**Lịch sử hai đợt chạy (để thấy sửa lỗi tích hợp có tác dụng thật):**

| Đợt | PA | Đúng | Sai | Abstain |
|---|---|---|---|---|
| Đợt 1 (trước sửa lỗi ngày + khớp nhầm thực thể) | −0,276 | 436 | 1.263 | 1.301 |
| Đợt cuối (sau sửa lỗi, xem §7) | −0,212 | 581 | 1.217 | 1.202 |

Sửa lỗi giúp +145 câu đúng, −46 câu sai, nhưng vẫn còn xa mức chấp
nhận được — phần còn lại là lỗi parser/solver theo từng loại câu
(xem §5, §6), không phải lỗi tích hợp.

## 2. Kiến trúc và cách chạy

```
questions.jsonl (sắp xếp theo business_id rồi id)
  └─► mỗi shop: cache hit? ── không ──► phase1 load_shop ──► build_shop_view ──► save_shop_cache (data_dir bắt buộc)
                  └── có ───► load_shop_cache
        mỗi câu: parse_question → solve → compute_confidence (trần) → decide
                 (unanswerable trước, ngưỡng 0,65 sau, format cuối)
                 → runner điền id thật → ghi submission.jsonl TĂNG DẦN
        giải phóng view, sang shop khác
  └─► phase1 evaluate.score_submission → reports/PHASE2_SUMMARY.json, reports/error_analysis.json
```

Cách chạy (từ thư mục `phase2/`):

```bash
python3 run_phase2.py --train-dir ../train --cache-dir ./cache \
  --output ./output/submission.jsonl
python3 run_phase2.py --train-dir ../train --cache-dir ./cache \
  --output ./output/submission.jsonl --shops biz_101,biz_102
python3 -m unittest discover -s tests -v
```

File nộp/kết quả:

- `output/submission.jsonl` — 3.000 dòng, ghi tăng dần.
- `reports/PHASE2_SUMMARY.json` — PA, per-type, per-business, holdout,
  coverage, thời gian từng shop, cache, shop lỗi.
- `reports/error_analysis.json` — lỗi theo nhóm + ví dụ id thật, câu
  sai theo measure và theo shop.
- `reports/PHASE2_REPORT.md` — chính file này.

## 3. PA theo type (type lấy từ đáp án tham chiếu)

| Type | Tổng | Đúng | Sai | Abstain | PA |
|---|---|---|---|---|---|
| number | 1.884 | 362 | 726 | 796 | −0,193 |
| percent | 578 | 63 | 299 | 216 | −0,408 |
| list | 193 | 43 | 96 | 54 | −0,275 |
| date | 145 | 81 | 36 | 28 | **+0,310** |
| string | 88 | 18 | 37 | 33 | −0,216 |
| table | 19 | 0 | 15 | 4 | −0,789 |
| boolean | 23 | 0 | 5 | 18 | −0,217 |
| unanswerable | 70 | 14 | 3 | 53 | +0,157 |

Nhận xét:

- **Bao phủ tốt nhất: date (+0,310)** — best_day/best_month là nhóm
  duy nhất có PA dương rõ rệt (81/145 đúng, chỉ 28 abstain). Đây là
  loại câu solver làm tốt nhất vì chỉ cần nhóm doanh thu theo ngày/
  tháng và lấy max.
- **Bao phủ yếu nhất: table (−0,789, 0/19 đúng)** — câu bảng
  (doanh thu theo chiều mà không nêu chiều cụ thể) gần như chưa làm
  được: 15 câu trả lời thì sai hết, 4 câu abstain. Parser thường suy
  ra table nhưng solver trả bảng sai cột/thứ tự hoặc thiếu nhóm.
- **percent (−0,408) là nhóm sai nhiều thứ hai theo tỉ trọng** — tăng
  trưởng, tỉ trọng, tỉ lệ huỷ: 299 sai / 578 câu. Phần lớn là câu
  tăng trưởng sai kỳ so sánh hoặc sai mẫu số, và câu tỉ trọng sai
  thực thể (xem §5).
- **boolean (0/23 đúng)** — câu tăng liên tục: 18 abstain, 5 trả lời
  thì sai hết. Loại câu này về thực chất chưa được bao phủ dù solver
  có nhánh continuous_growth.
- **unanswerable (+0,157 nhưng chỉ 14/70 đúng)** — pipeline mới dự
  đoán đúng 14 câu unanswerable, còn 53 câu unanswerable thật bị ghi
  thành abstain (mất điểm đúng) và 2 câu bị ghi unanswerable oan
  (thành wrong; tổng số câu pipeline dự đoán unanswerable là 16).
  Gate phát hiện unanswerable còn quá dè dặt: điều kiện "shop không
  có mặt hàng / kỳ hoàn toàn ngoài dữ liệu / cột bắt buộc hoàn toàn
  không tồn tại" hiếm khi đủ bằng chứng mạnh.

## 4. PA theo từng shop và holdout

### 4.1. Từng shop

| Shop | Tổng | Đúng | Sai | Abstain | PA |
|---|---|---|---|---|---|
| biz_101 | 40 | 26 | 1 | 13 | +0,625 |
| biz_102 | 119 | 55 | 38 | 26 | +0,143 |
| biz_103 | 119 | 38 | 33 | 48 | +0,042 |
| biz_104 | 119 | 23 | 47 | 49 | −0,202 |
| biz_105 (holdout) | 119 | 18 | 53 | 48 | −0,294 |
| biz_106 (holdout) | 119 | 13 | 53 | 53 | −0,336 |
| biz_107 | 40 | 6 | 14 | 20 | −0,200 |
| biz_108 | 119 | 22 | 53 | 44 | −0,261 |
| biz_109 | 118 | 22 | 54 | 42 | −0,271 |
| biz_110 | 40 | 9 | 26 | 5 | −0,425 |
| biz_111 | 118 | 20 | 62 | 36 | −0,356 |
| biz_112 | 118 | 31 | 36 | 51 | −0,042 |
| biz_113 | 40 | 9 | 14 | 17 | −0,125 |
| biz_114 | 40 | 0 | 16 | 24 | −0,400 |
| biz_115 | 118 | 34 | 34 | 50 | 0,000 |
| biz_116 | 118 | 37 | 44 | 37 | −0,059 |
| biz_117 (holdout) | 118 | 0 | 41 | 77 | −0,347 |
| biz_118 | 118 | 16 | 48 | 54 | −0,271 |
| biz_119 | 118 | 19 | 42 | 57 | −0,195 |
| biz_120 | 118 | 22 | 36 | 60 | −0,119 |
| biz_121 | 118 | 14 | 53 | 51 | −0,331 |
| biz_122 | 118 | 22 | 50 | 46 | −0,237 |
| biz_123 (holdout) | 118 | 18 | 57 | 43 | −0,331 |
| biz_124 (holdout) | 118 | 5 | 61 | 52 | −0,475 |
| biz_125 | 40 | 0 | 15 | 25 | −0,375 |
| biz_126 | 40 | 26 | 6 | 8 | +0,500 |
| biz_127 | 118 | 36 | 51 | 31 | −0,127 |
| biz_128 | 118 | 21 | 54 | 43 | −0,280 |
| biz_129 | 118 | 0 | 58 | 60 | −0,492 |
| biz_130 | 118 | 19 | 67 | 32 | −0,407 |

### 4.2. Holdout so với các shop còn lại

| Nhóm | Tổng | Đúng | Sai | PA |
|---|---|---|---|---|
| Holdout (biz_124, 123, 106, 105, 117) | 592 | 54 | 265 | −0,356 |
| Các shop còn lại | 2.408 | 527 | 952 | −0,176 |

Holdout tệ hơn hẳn (−0,356 so với −0,176), nhưng không phải vì
"holdout khó hơn một cách bí ẩn": 2/5 shop holdout (biz_106, biz_117)
nằm trong nhóm shop từng mất ngày hoặc thiếu bảng bán (xem §6), và
biz_124 là shop holdout yếu nhất (−0,475, chỉ 5/118 đúng) dù đã có đủ
ngày — tức lỗi ở đó là lỗi hiểu/tính theo loại câu, không phải lỗi
dữ liệu. Khoảng cách holdout − non-holdout là tín hiệu tổng quát
hoá kém thật, cần Giai đoạn 3 xử lý bằng parser tốt hơn chứ không
phải bằng cách nhớ shop.

### 4.3. Shop tốt nhất / yếu nhất

- **Tốt nhất: biz_101 (+0,625, 26 đúng / 1 sai / 13 abstain)** và
  **biz_126 (+0,500, 26 đúng / 6 sai / 8 abstain)** — hai shop nhỏ
  (40 câu), dữ liệu gọn, tên sản phẩm/khách ít mơ hồ. Đây là mức
  pipeline đạt được khi dữ liệu sạch và câu hỏi nằm trong từ khoá
  đã bao phủ.
- **Yếu nhất: biz_129 (−0,492, 0 đúng / 58 sai / 60 abstain)**,
  **biz_124 (−0,475)**, **biz_130 (−0,407)**, **biz_114 (−0,400)**,
  **biz_125 (−0,375)**. Trong đó biz_114, biz_117, biz_125, biz_129
  vẫn 0 câu đúng sau mọi sửa lỗi — nguyên nhân ở §6 (thiếu bảng
  bán dùng được, không phải do gate hay ngưỡng).

## 5. Phân tích lỗi theo nhóm (abstain + unanswerable dự đoán)

Nhóm lấy từ `error_group` cuối của pipeline (solver → gate). Tổng
nhóm = 1.218 câu không trả lời được (1.202 abstain + 16 unanswerable
dự đoán). Mỗi nhóm kèm 2–3 ví dụ id thật (id đầy đủ trong
`error_analysis.json`).

| Nhóm | Số lượng | Ý nghĩa và ví dụ id thật |
|---|---|---|
| parser | 791 | Parser không nhận diện được measure/kỳ (measure='unknown'). Ví dụ: `q_1703b4f93db8` (biz_101), `q_7c8afb8a81e9` (biz_101), `q_879535b41a21` (biz_101). Đây là nhóm lớn nhất, chiếm ~65% số câu không trả lời — bằng chứng trực tiếp cho việc parser template là nút thắt (xem §8 về LoRA). |
| filter | 106 | Có dữ liệu nhưng lọc ra rỗng hoặc không xác định được (điển hình là growth_percent khi kỳ so sánh không có dòng). Ví dụ: `q_16f9cc98ab35` (biz_103), `q_66380f9d73b9` (biz_103), `q_b27e7f53b569` (biz_103). |
| uncovered | 92 | Measure đã biết tên nhưng solver cố tình chưa bao phủ (chủ yếu avg_price, top_employee, import_total). Ví dụ: `q_234fec44d93d` (biz_102), `q_28e3195fcbb9` (biz_102), `q_23ab8cf108d1` (biz_103). |
| entity | 87 | Thực thể hỏi không tồn tại trong shop (khớp tên thất bại hoặc tên nhóm/thanh toán không có). Ví dụ: `q_75d48f1bfc6d` (biz_103), `q_9f82b2f25fd6` (biz_103), `q_6c1ca5ce7bd4` (biz_105). |
| time | 56 | Không xác định được kỳ (kỳ hỏi sai định dạng hoặc solver cần ngày đơn lẻ mà kỳ không phải một ngày). Ví dụ: `q_2b36065a8adc` (biz_103), `q_3cbc76c5b4a8` (biz_103), `q_4bf0ba4d3872` (biz_103). |
| column | 41 | Thiếu cột/dữ liệu bắt buộc cho loại câu (điển hình quantity_by_unit ở shop không có đơn vị). Ví dụ: `q_41c360c34398` (biz_104), `q_4bfce7dd9238` (biz_104), `q_a01143657c29` (biz_104). |
| format | 28 | Solver trả đáp án nhưng sai format so với type câu hỏi (chủ yếu quantity_by_unit: bảng/list không đúng dạng). Ví dụ: `q_c881d843a8fb` (biz_104), `q_02ed16421456` (biz_105), `q_25e68617a9f7` (biz_106). |
| unanswerable | 16 | Gate kết luận unanswerable (shop không có mặt hàng, kỳ hoàn toàn ngoài dữ liệu, cột bắt buộc vắng hẳn). Ví dụ: `q_46e6ca519588` (biz_103), `q_5e75fcb3f8ce` (biz_103), `q_cfea850ecaa2` (biz_103). |
| unit | 1 | Thiếu hệ số quy đổi (chỉ 1 câu). Ví dụ: `q_ffcb6868973c` (biz_121). Nhóm này nhỏ vì đa số dòng đã có `_qty_base` sẵn từ phase1. |

### Câu đã trả lời nhưng sai, theo measure đã parse

| Measure | Số câu sai | Ví dụ id thật |
|---|---|---|
| revenue | 616 | `q_5049522ff12c` (biz_101), `q_1a70054e2f85` (biz_102), `q_20d5aeb823b2` (biz_102) |
| order_count | 208 | `q_1bd200cd8993` (biz_102), `q_3a520aec0ca1` (biz_102), `q_3be73af1a105` (biz_102) |
| growth_percent | 125 | `q_5029b166aa34` (biz_102), `q_b276ba56b8b1` (biz_102), `q_63501211fe59` (biz_103) |
| debt_total | 79 | `q_073aee886aef` (biz_106), `q_1f99d93acad3` (biz_106), `q_23cea7790c35` (biz_106) |
| top_customer | 69 | `q_13db1a0a7ea9` (biz_102), `q_151d139d77ca` (biz_102), `q_20229203ac89` (biz_102) |
| revenue_share | 28 | `q_8710e092dd39` (biz_108), `q_1c41ad190bbd` (biz_111), `q_3692d7766dba` (biz_111) |
| best_day | 24 | `q_56ec0e6fa497` (biz_104), `q_e2d23212a893` (biz_104), `q_e658a5d9646a` (biz_104) |
| top_product | 16 | `q_019ec94d9799` (biz_106), `q_4192ef63e140` (biz_106), `q_bfa382e47ca0` (biz_106) |
| debt_top | 15 | `q_153f4e995558` (biz_106), `q_3a2bccc332db` (biz_106), `q_420556897d22` (biz_106) |
| best_month | 12 | `q_0a0038f69d4e` (biz_103), `q_9045737fcddf` (biz_103), `q_81d6b5a181f4` (biz_106) |
| cancel_rate | 10 | `q_0f4e7cdb3ce5` (biz_112), `q_f276ab43b4cc` (biz_116), `q_06ea706b571c` (biz_122) |
| quantity | 7 | `q_0a8ec3050fe7` (biz_109), `q_16029963548e` (biz_112), `q_462895903b95` (biz_112) |
| revenue_by_group | 5 | `q_bc83ba885777` (biz_108), `q_6b5bc4607001` (biz_118), `q_ed3ef76aa90c` (biz_118) |
| continuous_growth | 2 | `q_313dfc683558` (biz_109), `q_6d6f14c7978d` (biz_118) |
| growth_amount | 1 | `q_d4b5c60c1305` (biz_122) |

Đọc bảng này cùng §3: `revenue` sai nhiều nhất theo số tuyệt đối
(616) vì nó là measure phổ biến nhất, nhưng tỉ lệ sai cao nhất lại ở
`percent`-type (tăng trưởng/tỉ trọng) và `table`. Hai dạng sai khác
nhau và cần hai cách sửa khác nhau: revenue/order_count sai chủ yếu
do khớp thực thể/kỳ sai (ví dụ câu tổng chung bị hiểu thành câu của
một khách — đã sửa một phần ở §7.6, nhưng vẫn còn các dạng khớp nhầm
khác), còn growth/percent sai chủ yếu do kỳ so sánh và mẫu số chưa
đúng luật rulebook cho từng dạng kỳ (tháng tuyệt đối vs tháng này vs
quý/năm).

## 6. Giới hạn còn tồn tại

1. **Parser template là nút thắt lớn nhất.** 791 câu (26,4% tổng số)
   abstain vì parser không hiểu measure/kỳ. Các dạng câu chưa bao phủ
   hoặc bao phủ kém: nhập hàng chi tiết (`import_total`, ngoài câu
   tổng giá trị phiếu nhập), lãi/lời gộp tổng (không phải xếp hạng),
   giá trung bình mơ hồ (`avg_price` — thuộc nhóm uncovered gồm
   `avg_price` / `top_employee` / `import_total` theo định nghĩa của
   solver), câu hai số (ví dụ "nợ thêm / trả được" — parser chỉ trả
   một measure nên không thể đúng cả hai số),
   câu hỏi theo nhân viên, câu hỏi theo ca/khung giờ ("ca chiều"),
   câu hỏi theo nhóm khách ("khách nhóm thân thiết" — parser nhận
   nhóm hàng chứ chưa nhận nhóm khách từ master), và các cách diễn
   đạt lạ ngoài từ khoá (ví dụ "bữa nay", "e" + câu không dấu nặng).
2. **4 shop vẫn 0 câu đúng vì thiếu bảng bán dùng được** (không phải
   do gate hay ngưỡng):
   - **biz_114**: chỉ có một bảng `ban_hang` mà cột số lượng/tiền
     ('Sluong', 'Tổng') và mã đơn ('Mã GD') không map được thành
     semantic phase1 → view rỗng (0 lines, 0 orders).
   - **biz_125**: bảng `ban_hang` có số lượng/tiền và ngày bán nhưng
     tên hàng ('Hàng bán') và mã đơn không map được → view rỗng.
   - **biz_117**: bảng bán thật (`SoBanHang`) chỉ ở cấp đơn (có ngày,
     có tiền, không có mặt hàng) và mã phiếu không map thành order_id;
     bảng duy nhất có mặt hàng + số lượng lại là bảng NHẬP
     (`SoNhapHang`, 1.134 dòng) nên shop_view chọn nhầm bảng nhập
     làm "bảng dòng" → mọi câu bán đều sai/abstain.
   - **biz_129**: bảng dòng (`chi_tiet_don`, 145.479 dòng) có mặt hàng
     + số lượng nhưng mã chứng từ ('Số CT') không map thành order_id;
     bảng đơn (`don_hang`) có ngày bán nhưng mã phiếu không map thành
     order_id → dòng mất ngày, đơn rỗng.
   
   Điểm chung: **thiếu synonym mã đơn / tên hàng ở tầng map cột**,
   và shop_view chưa có cơ chế phát hiện mã đơn theo giá trị cho các
   cột tên lạ (nó đã có cơ chế tương tự cho mã khách và tổng đơn, xem
   `_detect_customer_code_column` / `_detect_order_total_column`).
   Đây là việc của Giai đoạn 3 (xem §8), không phải việc sửa một shop
   cụ thể — mọi cách sửa đều phải theo giá trị và tên theo ý nghĩa,
   không hard-code tên cột của 4 shop này.
3. **Công nợ vẫn yếu (79 câu debt_total sai).** Nguyên nhân chính đã
   biết: khi shop không có bảng thu theo ngày, solver dùng ảnh chụp
   cuối kỳ (`amount_due` / tổng − đã trả) cho nợ "tại ngày X" — tiền
   trả sau ngày X cũng nằm trong ảnh chụp nên nợ bị tính thấp; và
   một số shop ghi tiền trả ở bảng Sổ quỹ với cột mã đơn tên lạ nên
   bảng thu không được nhận diện (shop_view chỉ nhận bảng thu khi
   ≥80% giá trị trùng mã đơn đã biết).
4. **Giả định chưa được rulebook mới xác nhận.** (a) Xếp hạng profit:
   giá vốn `_cost_price` được coi là theo đơn vị bán của dòng (cùng
   đơn vị với `_qty`); nếu rulebook mới định nghĩa giá vốn theo đơn
   vị cơ bản thì công thức profit phải đổi thành
   `line_amount − qty_base × cost_price`. (b) Hoà best_day/best_month
   lấy ngày/tháng sớm nhất (contract ghi rõ điểm này cần rà lại).
   (c) Câu "ngày nào doanh thu cao nhất" không nêu đối tượng được
   parser mặc định là ngày (theo `_detect_ranking`).
5. **Gate chưa đủ sức chặn câu sai.** Solver trả confidence 1,0 cho
   đa số câu sai (vì sai do khớp thực thể/kỳ, không phải do thiếu
   dữ liệu), nên ngưỡng 0,65 gần như không chặn được gì ngoài câu
   parse mơ hồ. Muốn PA dương thì Giai đoạn 3 cần thêm tín hiệu chặn:
   ví dụ abstain khi thực thể khớp rút gọn mơ hồ (nhiều ứng viên),
   khi kỳ suy năm, hoặc khi đáp án nhạy cảm với một lựa chọn khớp
   (tính cả hai cách, khác nhau thì abstain).
6. **Tốc độ.** Đợt chạy cuối mất ~18,6 phút cho 3.000 câu, trong đó
   phần lớn là ingest các shop lớn (đợt cuối: biz_106: 195 giây,
   biz_109: 107 giây, biz_120: 99 giây, biz_130: 95 giây) và solver
   vẫn duyệt list dòng nhiều lần mỗi câu. Cache chỉ tiết kiệm phần
   ingest.
   Giai đoạn 3 nên cache cả bản ghi lines/orders đã trích trong một
   lượt chạy (đã làm một phần ở §7.5) và cân nhắc trích theo cột
   vector hoá thay vì duyệt dict từng dòng.

## 7. Sửa lỗi tích hợp đã thực hiện (ghi rõ mọi sửa đổi)

Không sửa code trong `../phase1/` và không sửa dữ liệu train. Mọi sửa
đổi dưới đây đều nằm trong `phase2/` và đều có test khóa lại.

1. **Solver xếp top_product theo doanh thu (SAI rulebook) → đã sửa**
   (bắt buộc theo bàn giao). `parser.py`: thêm trường
   `ranking_metric` vào `ParsedQuestion` — `top_product` → `'quantity'`
   (số lượng quy đổi), `top_customer`/`best_day`/`best_month` →
   `'revenue'`, `debt_top` → `'debt'`, câu lời/lãi nhất → `'profit'`
   khi nhận diện được cụm lời/lãi; đồng thời `_detect_ranking` chưa
   gán target cho "hàng nào lời nhất" thì nay gán `'product'`.
   `solver.py` (`_solve_top`): đọc `ranking_metric`; `quantity` cộng
   `_qty_base` (luật thiếu hệ số giống `_quantity_total`, thiếu
   `_qty_base` mà có bảng quy đổi thì abstain `unit`); `profit` cộng
   `line_amount − qty × cost_price`; `revenue` giữ nguyên.
   `ParsedQuestion` cũ không có trường thì dự phòng product →
   `quantity`, customer → `revenue`.
2. **Solver trả entry `id=None` → runner điền id thật.**
   `run_phase2.py` (`solve_one_question`) điền id thật SAU khi gate
   quyết định xong; test `test_solve_one_question_fills_real_id`.
3. **Snapshot nợ (`period_start=None`) → đã kiểm chứng, không sửa
   solver.** `_solve_debt` dùng `period_end` làm ngày snapshot;
   test snapshot nợ qua runner và qua solver đều pass.
4. **Import chéo parser/solver/gate → đã kiểm chứng thực tế.**
   Solver import `ParsedQuestion` từ `parser`; gate duck-typing;
   runner nạp cả ba từ cùng `src/` và chạy chung trong test runner.
5. **Hiệu năng solver (lỗi tích hợp, không phải lỗi logic).**
   Trước đây `_solve_impl` trích lại toàn bộ lines/orders cho MỖI câu
   hỏi (mỗi ô đi qua `pandas.isna`), khiến shop 40 câu mất ~40 giây
   và toàn bộ 3.000 câu không thể xong trong thời gian hợp lý.
   `solver.py`: thêm `_cached_records` — trích một lần rồi gắn vào
   chính view (`_solver_records_cache`, tên có tiền tố `_` nên
   `cache.py` không đưa vào `extras`); đồng thời `_is_blank` kết luận
   ngay với chuỗi/số (giữ nguyên ngữ nghĩa: `isna` của chuỗi luôn
   False, của số hữu hạn luôn False) thay vì đi qua `pandas.isna`
   mọi ô. Kết quả: biz_101 từ ~40 giây còn ~4 giây, đáp án smoke
   trước/sau sửa đổi giống hệt nhau (đã kiểm chứng bằng diff file).
6. **Parser khớp nhầm thực thể do "bao nhiu" (lỗi thật, phát hiện khi
   phân tích đợt chạy 1).** "bao nhiu" mở rộng thành "bao bao nhieu"
   (từ "bao" gốc + từ "bao" do "nhiu" mở rộng thành) tạo đoạn "bao
   bao" dài 2 token, khớp nhầm với mọi khách/sản phẩm có tên chứa
   "bao" (ví dụ "Cao Anh Bảo") — câu tổng chung bị tính thành câu
   của một khách cụ thể. `parser.py` (`_match_one_entity`): đoạn tên
   rút gọn nay phải gồm các token KHÁC NHAU; tên đầy đủ có token lặp
   thật vẫn khớp bằng nhánh tên đầy đủ. Test
   `test_parse_question_bao_nhiu_no_false_entity` khóa lại; test tên
   rút gọn bình thường ("sting dau") vẫn pass.
7. **Shop_view mất toàn bộ ngày ở shop chỉ có cột "Thời gian" (lỗi
   thật, nguyên nhân 10 shop 0 câu đúng ở đợt 1).** Phase1 map "Thời
   gian" thành `order_datetime`, không phải `order_date`, nhưng
   shop_view chỉ đọc `order_date` ở 4 chỗ (chọn bảng đơn, chọn bảng
   dòng làm bảng đơn, dựng orders, dựng lines). `shop_view.py`: cả 4
   chỗ nay chấp nhận `order_datetime` làm dự phòng cho `order_date`
   (giá trị datetime được `parse_date` cắt về phần ngày). Kết quả:
   6/10 shop được cứu (biz_106, 108, 109, 119, 120, 128 có ngày đầy đủ
   và có câu đúng; xem §6 cho 4 shop còn lại do nguyên nhân khác).
   Test `test_order_datetime_used_as_order_date` và
   `test_order_datetime_joined_from_separate_order_table` khóa lại.
8. **Gate chỉ dùng confidence của solver → runner lấy min làm trần.**
   `decide` chỉ tự tính `compute_confidence` khi solver chưa gán
   confidence; nếu giữ nguyên, câu parse mơ hồ nhưng solver tự tin
   1,0 sẽ lọt ngưỡng 0,65 oan. Runner tính `compute_confidence` và
   lấy `min(confidence_solver, confidence_gate)` trước khi gọi
   `decide` (hai công thức trừ các khoản khác nhau nên lấy min là
   trần an toàn, không cộng dồn cùng một khoản hai lần).
9. **Cache bắt buộc truyền `data_dir`.** `run_phase2.py` luôn truyền
   `data_dir=<train-dir>` cho cả `load_shop_cache` và
   `save_shop_cache`; không truyền thì cache lưu với
   `source_hash=None` và lần sau luôn miss an toàn. Sau sửa lỗi
   shop_view (§7.7), cache của 10 shop mất ngày đã được xóa tay để
   ép ingest lại bằng code mới (cache key theo hash nguồn, không
   theo phiên bản code, nên đổi code không tự vô hiệu cache cũ —
   đây là điểm Giai đoạn 3 nên cải thiện bằng cách đưa hash code
   vào key cache hoặc version cache).

## 8. Giai đoạn 3 cần làm

1. **Đóng gói Docker offline:** `predict.sh` đọc đúng split được
   mount (rulebook + calendar của split đó, không hard-code luật
   train), không gọi mạng, model LLM local ≤ 15B nếu thay parser.
   **Hạn nộp: 23:59 GMT+7, 16/11/2026.**
2. **Tái lập:** chạy lại trên train cho |PA| ≤ 0,03 so với báo cáo
   này (cùng code, cùng cache key theo hash nguồn; lưu ý §7.9 về
   version cache khi đổi code).
3. **Parser là ưu tiên số một.** Lỗi template parser chiếm đa số
   trong số câu không trả lời được (791/1.218 ≈ 65%), nên theo
   MASTER_PLAN mục 8, Giai đoạn sau **mới được cân nhắc** LoRA cho
   parser (vẫn sau code baseline này, vẫn giữ solver/gate bằng code
   và ngưỡng abstain; không finetune để nhớ đáp án train). Điểm thay
   thế đã chuẩn bị sẵn: parser mới chỉ cần trả đúng `ParsedQuestion`
   (xem README.md §1), solver/gate không phụ thuộc LLM.
4. **Sửa map cột theo giá trị cho 4 shop còn 0 câu đúng (§6.2):**
   phát hiện mã đơn theo độ trùng giá trị cho các cột tên lạ
   ('Số CT', 'Số phiếu', 'Mã GD'), phát hiện tên hàng cho 'Hàng bán',
   và phân biệt bảng bán với bảng nhập (bảng nhập không được chọn
   làm bảng dòng). Mọi cách sửa đều phải theo giá trị/tên theo ý
   nghĩa, không hard-code tên cột của 4 shop này.
5. **Tăng độ chính xác trước khi tăng bao phủ:** thêm tín hiệu
   abstain khi khớp thực thể mơ hồ, khi kỳ suy năm, khi đáp án nhạy
   cảm với lựa chọn khớp; rà lại luật kỳ so sánh cho growth
   (tháng tuyệt đối / tháng này / quý / năm) và mẫu số cho percent;
   sửa format bảng (table hiện 0/19 đúng) và câu boolean tăng liên
   tục (0/23 đúng); cải thiện phát hiện unanswerable (hiện chỉ đúng
   14/70) mà không tăng unanswerable oan.
6. **Rà lại các giả định đã ghi:** giá vốn theo đơn vị bán hay đơn vị
   cơ bản (§6.4a), hoà best_day/best_month (§6.4b), và luật tăng
   trưởng cho kỳ quý/năm nếu rulebook mới nói khác.
7. **Hiệu năng:** cache bản ghi lines/orders đã trích trong một lượt
   chạy cho mọi shop (đã làm một phần ở §7.5), và đưa version code
   vào key cache để đổi code tự vô hiệu cache cũ.

## 9. Ghi chú về finetune

**Giai đoạn 2 KHÔNG finetune.** Mọi kết quả trên đều từ code baseline
(parser template + solver code + gate ngưỡng). Chỉ khi — và chỉ khi —
phân tích lỗi cho thấy lỗi template parser chiếm đa số thì Giai đoạn
sau mới cân nhắc LoRA theo MASTER_PLAN mục 8. Điều kiện đó hiện đã
thỏa về mặt số liệu (parser là nhóm lỗi lớn nhất, §5), nhưng quyết
định có làm LoRA hay không vẫn thuộc về Giai đoạn 3 sau khi cân nhắc
chi phí Docker offline ≤ 15B và rủi ro tổng quát hoá kém; báo cáo này
không tự ý mở một đợt finetune nào.

# -*- coding: utf-8 -*-
"""So sánh summary ingest LLM với baseline phase1 (chỉ đọc, không sửa dữ liệu).

Mục đích của script này
-----------------------
Đọc ``phase1/reports/phase1_summary.json`` (baseline, 30 shop) và
``reports/phase1_llm_summary.json`` (do runner của Child 2 sinh ra),
in bảng so sánh theo từng shop và tổng hợp, kiểm tra SHA256 của
``phase1/src/ingest.py`` và ``normalizers.py`` lúc chạy, và phát hiện
map chưa kiểm chứng nào lọt vào summary LLM.

Script KHÔNG suy luận map cho shop nào, KHÔNG dùng nhóm shop khó để
suy luận gì khác ngoài việc lọc báo cáo: hai nhóm dưới đây chỉ là bộ
lọc hiển thị (theo nhiệm vụ của Child 3), không ảnh hưởng tới bất kỳ
con số so sánh nào:
- Nhóm holdout khó: biz_124, biz_123, biz_106, biz_105, biz_117.
- Nhóm 0-correct ở phase2: biz_114, biz_117, biz_125, biz_129
  (biz_117 thuộc cả hai nhóm, được ghi chú khi in).

Cách chạy (từ thư mục ``phase1_llm/``)::

    python3 evaluation/compare_baseline.py \
        --baseline ../phase1/reports/phase1_summary.json \
        --llm reports/phase1_llm_summary.json

Quy tắc exit code (ghi rõ để coordinator không hiểu lầm):
- Exit 0 chỉ khi: đọc được cả 2 file JSON, SHA256 phase1 đều PASS,
  và không phát hiện map chưa kiểm chứng nào trong summary LLM.
- Exit khác 0 khi: summary LLM chưa tồn tại hoặc đọc lỗi, baseline
  đọc lỗi, SHA256 phase1 FAIL (phase1 đã bị sửa), hoặc phát hiện map
  chưa kiểm chứng. Script không bao giờ bịa số khi file chưa có: khi
  summary LLM chưa tồn tại thì chỉ in cảnh báo rõ ràng và exit 2.

Định dạng summary LLM được chấp nhận linh hoạt (vì runner của Child 2
có thể ghi ``per_shop`` dạng list hoặc dict keyed theo business_id,
và ``llm_added_mappings`` dạng số hoặc dict bảng -> mapping):
mọi hàm chuẩn hoá dưới đây đều xử lý cả hai dạng và ghi rõ cách đếm
trong docstring, thay vì crash vì khác dạng mong đợi.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

# SHA256 của phase1 trước khi làm (theo nhiệm vụ Child 3; so lúc chạy).
# Nếu một trong hai file khác SHA này thì phase1 đã bị sửa sau baseline
# và mọi so sánh đều không còn đáng tin - script báo FAIL, không tự sửa.
EXPECTED_SHA256 = {
    "ingest.py": "89a61c1111b3de0437e34385f4727dc0ce26ee58c26f497b675831947d375643",
    "normalizers.py": "7a62cab0575150987a714c23d2c523dd4d37e9dbdda56b7bb33b64487cdd20f8",
}

# Hai nhóm shop khó CHỈ dùng làm bộ lọc báo cáo, không dùng để suy luận map.
NHOM_HOLDOUT_KHO = ["biz_124", "biz_123", "biz_106", "biz_105", "biz_117"]
NHOM_PHASE2_KHONG_DIEM = ["biz_114", "biz_117", "biz_125", "biz_129"]


def load_json(duong_dan):
    """Đọc một file JSON và trả về dict đã parse.

    Đầu vào:
        duong_dan: đường dẫn tới file JSON cần đọc.

    Đầu ra:
        Dict nội dung file.

    Ngoại lệ:
        FileNotFoundError khi file chưa tồn tại, json.JSONDecodeError
        khi file không phải JSON hợp lệ - cả hai đều được ``main``
        bắt và chuyển thành cảnh báo + exit khác 0, không bịa số.
    """
    with open(duong_dan, encoding="utf-8") as f:
        return json.load(f)


def sha256_file(duong_dan):
    """Tính SHA256 hex của một file, đọc theo từng khối để khỏi tốn RAM.

    Đầu vào:
        duong_dan: đường dẫn tới file cần băm.

    Đầu ra:
        Chuỗi hex SHA256 của toàn bộ file.
    """
    digest = hashlib.sha256()
    with open(duong_dan, "rb") as f:
        for khoi in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(khoi)
    return digest.hexdigest()


def check_phase1_sha(thu_muc_src):
    """Kiểm tra SHA256 của ingest.py và normalizers.py so với SHA kỳ vọng.

    Đầu vào:
        thu_muc_src: thư mục ``phase1/src`` cần kiểm tra.

    Đầu ra:
        Tuple ``(ket_qua, tat_ca_pass)`` trong đó ``ket_qua`` là dict
        tên file -> (sha_thuc_te hoặc None khi thiếu file, True/False),
        ``tat_ca_pass`` True chỉ khi cả hai file đều khớp SHA kỳ vọng.
        Hàm này chỉ báo cáo, không tự sửa file nào.
    """
    ket_qua = {}
    tat_ca_pass = True
    for ten_file, sha_ky_vong in sorted(EXPECTED_SHA256.items()):
        duong_dan = Path(thu_muc_src) / ten_file
        if not duong_dan.exists():
            ket_qua[ten_file] = (None, False)
            tat_ca_pass = False
            continue
        sha_thuc_te = sha256_file(duong_dan)
        khop = sha_thuc_te == sha_ky_vong
        ket_qua[ten_file] = (sha_thuc_te, khop)
        tat_ca_pass = tat_ca_pass and khop
    return ket_qua, tat_ca_pass


def normalize_per_shop(summary, khoa_mac_dinh="per_shop"):
    """Chuẩn hoá ``per_shop`` của một summary về dict business_id -> shop.

    Đầu vào:
        summary: dict summary đã parse (baseline hoặc summary LLM).
        khoa_mac_dinh: khóa chứa per_shop (mặc định "per_shop").

    Đầu ra:
        Dict ``business_id -> dict thông tin shop``. Chấp nhận cả dạng
        list các dict có khóa "business_id" và dạng dict keyed trực
        tiếp theo business_id. Shop không có business_id thì bị bỏ qua
        (và được đếm riêng ở nơi gọi nếu cần, không crash).
    """
    # Baseline phase1 lồng per_shop trong khóa "ingest".
    nguon = summary.get(khoa_mac_dinh)
    if nguon is None and isinstance(summary.get("ingest"), dict):
        nguon = summary["ingest"].get(khoa_mac_dinh)
    if isinstance(nguon, dict):
        return {str(k): (v if isinstance(v, dict) else {}) for k, v in nguon.items()}
    ket_qua = {}
    if isinstance(nguon, list):
        for shop in nguon:
            if isinstance(shop, dict) and shop.get("business_id"):
                ket_qua[str(shop["business_id"])] = shop
    return ket_qua


def count_added_mappings(gia_tri):
    """Đếm số map LLM thêm vào từ một trường llm_added_mappings.

    Đầu vào:
        gia_tri: có thể là int (đếm sẵn), dict bảng -> mapping
            (mỗi mapping là dict semantic -> cột, đếm tổng số cặp),
            dict bảng -> int (cộng các số), hoặc list (đếm phần tử).

    Đầu ra:
        Số nguyên là tổng số map thêm; 0 khi trường vắng hoặc không
        hiểu được dạng (không đoán thêm, chỉ đếm những gì đếm được).
    """
    if isinstance(gia_tri, int) and not isinstance(gia_tri, bool):
        return gia_tri
    if isinstance(gia_tri, dict):
        tong = 0
        for v in gia_tri.values():
            if isinstance(v, dict):
                tong += len(v)
            elif isinstance(v, int) and not isinstance(v, bool):
                tong += v
            elif isinstance(v, list):
                tong += len(v)
        return tong
    if isinstance(gia_tri, list):
        return len(gia_tri)
    return 0


def count_rejected(gia_tri):
    """Đếm số map bị loại từ trường rejected_count hoặc rejected_mappings.

    Đầu vào:
        gia_tri: int (đếm sẵn) hoặc list các mục rejected.

    Đầu ra:
        Số nguyên; 0 khi trường vắng hoặc không phải int/list.
    """
    if isinstance(gia_tri, int) and not isinstance(gia_tri, bool):
        return gia_tri
    if isinstance(gia_tri, list):
        return len(gia_tri)
    return 0


def profile_delta_text(gia_tri):
    """Chuẩn hoá profile_delta của một shop thành chuỗi mô tả ngắn.

    Đầu vào:
        gia_tri: có thể là dict các cờ đổi (chỉ giữ những cờ đổi
            True/khác rỗng), list các mô tả, chuỗi, hoặc bool.

    Đầu ra:
        Chuỗi mô tả; chuỗi rỗng nghĩa là profile không đổi (để hàm
        tổng hợp đếm "số shop có profile thay đổi" chính xác).
    """
    if gia_tri is None or gia_tri is False:
        return ""
    if isinstance(gia_tri, str):
        return gia_tri.strip()
    if isinstance(gia_tri, dict):
        doi = [f"{k}={v}" for k, v in sorted(gia_tri.items())
               if v not in (None, False, "", [], {})]
        return "; ".join(doi)
    if isinstance(gia_tri, list):
        return "; ".join(str(x) for x in gia_tri)
    return str(gia_tri)


def find_unverified(summary):
    """Tìm dấu hiệu map chưa kiểm chứng trong summary LLM.

    Đầu vào:
        summary: dict summary LLM đã parse.

    Đầu ra:
        List các chuỗi mô tả vị trí phát hiện. Chỉ quét các dấu hiệu
        có cấu trúc rõ ràng (không quét chuỗi văn bản tự do để tránh
        báo động giả từ từ "kiểm chứng" trong ghi chú):
        - khóa chứa "unverified" có giá trị truthy (số > 0, list/dict
          không rỗng, True);
        - mục added_mappings nào có trường "verified" là False;
        - totals có "unverified_count" > 0.
        List rỗng nghĩa là không phát hiện map chưa kiểm chứng nào.
    """
    phat_hien = []

    def _quet(obj, duong_dan):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if "unverified" in str(k).lower():
                    if isinstance(v, bool) and v:
                        phat_hien.append(f"{duong_dan}.{k}=True")
                    elif isinstance(v, int) and v > 0:
                        phat_hien.append(f"{duong_dan}.{k}={v}")
                    elif isinstance(v, (list, dict)) and v:
                        phat_hien.append(f"{duong_dan}.{k} co {len(v)} muc")
                if k == "verified" and v is False:
                    phat_hien.append(f"{duong_dan}: verified=False")
                _quet(v, f"{duong_dan}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _quet(v, f"{duong_dan}[{i}]")

    _quet(summary, "summary")
    return phat_hien


def baseline_mapped_count(shop_baseline):
    """Đếm số semantic đã map ở baseline cho một shop.

    Đầu vào:
        shop_baseline: dict thông tin shop trong baseline phase1.

    Đầu ra:
        Số semantic có tên cột khác rỗng trong ``columns_mapped``;
        nếu không có ``columns_mapped`` thì 0.
    """
    mapped = shop_baseline.get("columns_mapped")
    if not isinstance(mapped, dict):
        return 0
    return sum(1 for v in mapped.values() if v not in (None, ""))


def compare_shops(baseline_shops, llm_shops):
    """So sánh từng shop: rows, map thêm, rejected, fallback, profile.

    Đầu vào:
        baseline_shops: dict business_id -> shop baseline.
        llm_shops: dict business_id -> shop summary LLM.

    Đầu ra:
        List các dict, mỗi dict một shop (gộp cả shop chỉ có ở một
        phía, với phía còn lại để None để bảng in ghi rõ), sắp xếp
        theo business_id để kết quả tái lập. Trường "rows_khop" False
        nghĩa là rows_in hoặc rows_out khác baseline và cần giải thích
        (theo contract mục 6: chênh lệch không giải thích là không
        được chấp nhận).
    """
    ket_qua = []
    for business_id in sorted(set(baseline_shops) | set(llm_shops)):
        base = baseline_shops.get(business_id)
        llm = llm_shops.get(business_id)
        if base is None or llm is None:
            ket_qua.append({
                "business_id": business_id,
                "base": base, "llm": llm,
                "rows_khop": False,
                "chi_tiet": "chi co o mot phia (baseline hoac LLM)",
            })
            continue
        rows_in_base = base.get("rows_in")
        rows_out_base = base.get("rows_out")
        rows_in_llm = llm.get("rows_in")
        rows_out_llm = llm.get("rows_out")
        rows_khop = (rows_in_llm == rows_in_base
                     and rows_out_llm == rows_out_base)
        # Runner Child 2 có thể ghi thêm llm_added_count (số đếm sẵn)
        # bên cạnh llm_added_mappings (dict); lấy max của hai nguồn để
        # không bỏ sót map thêm khi một trong hai nguồn ghi thiếu.
        so_map_them = count_added_mappings(
            llm.get("llm_added_mappings",
                    llm.get("added_mappings", 0)))
        dem_them_san = llm.get("llm_added_count")
        if isinstance(dem_them_san, int) and not isinstance(dem_them_san, bool):
            so_map_them = max(so_map_them, dem_them_san)
        ket_qua.append({
            "business_id": business_id,
            "base": base, "llm": llm,
            "rows_khop": rows_khop,
            "rows_in_base": rows_in_base,
            "rows_out_base": rows_out_base,
            "rows_in_llm": rows_in_llm,
            "rows_out_llm": rows_out_llm,
            "baseline_mapped_count": baseline_mapped_count(base),
            "llm_added_mappings": so_map_them,
            "rejected_count": count_rejected(
                llm.get("rejected_count",
                        llm.get("rejected_mappings", 0))),
            "fallback_count": count_rejected(llm.get("fallback_count", 0)),
            "profile_delta": profile_delta_text(
                llm.get("profile_delta", "")),
            "chi_tiet": "" if rows_khop else "ROWS KHAC BASELINE - CAN GIAI THICH",
        })
    return ket_qua


def print_table(so_sanh):
    """In bảng so sánh theo từng shop ra stdout, dạng text căn cột.

    Đầu vào:
        so_sanh: list dict từ ``compare_shops``.

    Đầu ra:
        Không trả gì; chỉ in. Cột "rows" ghi "in/out LLM vs baseline";
        shop có rows khác baseline được đánh dấu "CAN GIAI THICH" ở
        cuối dòng để coordinator nhìn là thấy ngay.
    """
    print("=" * 118)
    print("SO SANH THEO TUNG SHOP (baseline phase1 vs summary LLM)")
    print("=" * 118)
    header = (f"{'shop':<10} {'rows_in':>10} {'rows_out':>10} "
              f"{'base_map':>8} {'llm_them':>8} {'rejected':>8} "
              f"{'fallback':>8} {'profile_doi':<28} {'ghi_chu'}")
    print(header)
    print("-" * 118)
    for row in so_sanh:
        if row["base"] is None or row["llm"] is None:
            print(f"{row['business_id']:<10} {'(thieu mot phia)':<100} "
                  f"{row['chi_tiet']}")
            continue
        print(f"{row['business_id']:<10} "
              f"{str(row['rows_in_llm']):>10} {str(row['rows_out_llm']):>10} "
              f"{row['baseline_mapped_count']:>8} {row['llm_added_mappings']:>8} "
              f"{row['rejected_count']:>8} {row['fallback_count']:>8} "
              f"{row['profile_delta'][:28]:<28} {row['chi_tiet']}")
        if not row["rows_khop"]:
            print(f"{'':<10} baseline rows_in/out = "
                  f"{row['rows_in_base']}/{row['rows_out_base']}, "
                  f"LLM rows_in/out = {row['rows_in_llm']}/{row['rows_out_llm']}")
    print("-" * 118)


def print_nhom(ten_nhom, danh_sach, so_sanh_theo_id):
    """In tổng hợp riêng cho một nhóm shop khó (chỉ lọc báo cáo).

    Đầu vào:
        ten_nhom: tên nhóm để in tiêu đề.
        danh_sach: list business_id của nhóm.
        so_sanh_theo_id: dict business_id -> dict so sánh từ ``compare_shops``.

    Đầu ra:
        Không trả gì; chỉ in. Nhóm này không ảnh hưởng tới con số tổng
        hợp chung, chỉ giúp coordinator nhìn nhanh shop khó.
    """
    print(f"\n--- Nhom shop kho (chi loc bao cao, khong dung de suy luan map): {ten_nhom} ---")
    for business_id in danh_sach:
        row = so_sanh_theo_id.get(business_id)
        if row is None or row["base"] is None or row["llm"] is None:
            print(f"  {business_id}: khong co du trong ca 2 file")
            continue
        print(f"  {business_id}: llm_them={row['llm_added_mappings']}, "
              f"rejected={row['rejected_count']}, "
              f"fallback={row['fallback_count']}, "
              f"rows_khop={row['rows_khop']}, "
              f"profile_doi={row['profile_delta'] or 'khong'}")


def summarize(so_sanh, baseline_shops, llm_shops):
    """In tổng hợp chung từ bảng so sánh theo từng shop.

    Đầu vào:
        so_sanh: list dict từ ``compare_shops``.
        baseline_shops: dict shop baseline (để đếm tổng bảng).
        llm_shops: dict shop LLM (để đếm tổng shop LLM).

    Đầu ra:
        Dict các con số tổng hợp (để ``main`` dùng và để test/đọc lại
        dễ dàng), đồng thời in ra stdout.
    """
    du = [r for r in so_sanh if r["base"] is not None and r["llm"] is not None]
    tong_bang = 0
    for shop in baseline_shops.values():
        tables = shop.get("tables")
        if isinstance(tables, dict):
            tong_bang += len(tables)
    tong_hop = {
        "tong_shop_baseline": len(baseline_shops),
        "tong_shop_llm": len(llm_shops),
        "tong_shop_so_sanh_du": len(du),
        "tong_bang_baseline": tong_bang,
        "tong_map_them": sum(r["llm_added_mappings"] for r in du),
        "tong_rejected": sum(r["rejected_count"] for r in du),
        "tong_fallback": sum(r["fallback_count"] for r in du),
        "so_shop_profile_thay_doi": sum(1 for r in du if r["profile_delta"]),
        "so_shop_rows_khac_baseline": sum(1 for r in du if not r["rows_khop"]),
    }
    print("\nTONG HOP")
    print("-" * 118)
    for khoa, gia_tri in tong_hop.items():
        print(f"  {khoa}: {gia_tri}")
    if tong_hop["so_shop_rows_khac_baseline"]:
        print("  CANH BAO: co shop rows khac baseline - can giai thich "
              "trong bao cao (xem cot ghi chu o bang tren), khong duoc "
              "de chenh lech khong giai thich.")
    return tong_hop


def parse_args(argv=None):
    """Parse tham số dòng lệnh của script so sánh.

    Đầu vào:
        argv: list tham số (None nghĩa là lấy từ sys.argv).

    Đầu ra:
        Namespace với baseline, llm, phase1_src. Mặc định phase1_src
        suy từ vị trí file script (datamate/phase1/src) để chạy đúng
        cả khi cwd là phase1_llm/ hay thư mục khác.
    """
    mac_dinh_src = Path(__file__).resolve().parents[2] / "phase1" / "src"
    parser = argparse.ArgumentParser(
        description="So sanh summary ingest LLM voi baseline phase1.")
    parser.add_argument("--baseline", default="../phase1/reports/phase1_summary.json",
                        help="Duong dan baseline phase1_summary.json")
    parser.add_argument("--llm", default="reports/phase1_llm_summary.json",
                        help="Duong dan summary LLM phase1_llm_summary.json")
    parser.add_argument("--phase1-src", default=str(mac_dinh_src),
                        help="Thu muc phase1/src de kiem tra SHA256")
    return parser.parse_args(argv)


def main(argv=None):
    """Chạy toàn bộ so sánh và trả về exit code theo quy tắc đã ghi.

    Đầu vào:
        argv: tham số dòng lệnh (xem ``parse_args``).

    Đầu ra:
        int exit code: 0 chỉ khi đọc được cả 2 file, SHA256 phase1
        đều PASS và không phát hiện map chưa kiểm chứng; 2 khi summary
        LLM chưa tồn tại (cảnh báo rõ ràng, không bịa số); 1 cho các
        lỗi còn lại (baseline lỗi, SHA FAIL, có map chưa kiểm chứng).
    """
    args = parse_args(argv)

    # 1. Kiểm tra SHA256 phase1 lúc chạy (chỉ báo cáo, không tự sửa).
    print("KIEM TRA SHA256 PHASE1 (luc chay, so voi SHA truoc khi lam)")
    print("-" * 118)
    ket_qua_sha, sha_pass = check_phase1_sha(args.phase1_src)
    for ten_file in sorted(ket_qua_sha):
        sha_thuc_te, khop = ket_qua_sha[ten_file]
        if sha_thuc_te is None:
            print(f"  {ten_file}: FAIL (khong tim thay file trong {args.phase1_src})")
        else:
            print(f"  {ten_file}: {'PASS' if khop else 'FAIL'} ({sha_thuc_te})")
    if not sha_pass:
        print("  CANH BAO: SHA256 phase1 khong khop - phase1 co the da bi sua "
              "sau baseline, ket qua so sanh khong con dang tin.")

    # 2. Đọc baseline (bắt buộc phải có).
    try:
        baseline = load_json(args.baseline)
    except FileNotFoundError:
        print(f"\nLOI: khong tim thay baseline: {args.baseline}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"\nLOI: baseline khong phai JSON hop le ({args.baseline}): {exc}",
              file=sys.stderr)
        return 1

    # 3. Đọc summary LLM; chưa có thì cảnh báo rõ ràng, exit khác 0, không bịa số.
    try:
        llm_summary = load_json(args.llm)
    except FileNotFoundError:
        print(f"\nCANH BAO RO RANG: summary LLM chua ton tai: {args.llm}",
              file=sys.stderr)
        print("Runner cua Child 2 chua sinh reports/phase1_llm_summary.json - "
              "khong co so lieu LLM de so sanh, khong bia so.", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"\nLOI: summary LLM khong phai JSON hop le ({args.llm}): {exc}",
              file=sys.stderr)
        return 1

    baseline_shops = normalize_per_shop(baseline)
    llm_shops = normalize_per_shop(llm_summary)
    so_sanh = compare_shops(baseline_shops, llm_shops)
    print_table(so_sanh)
    summarize(so_sanh, baseline_shops, llm_shops)

    theo_id = {r["business_id"]: r for r in so_sanh}
    print_nhom("holdout kho (biz_124, 123, 106, 105, 117)",
               NHOM_HOLDOUT_KHO, theo_id)
    print_nhom("phase2 0-correct (biz_114, 117, 125, 129; "
               "biz_117 trung ca 2 nhom)",
               NHOM_PHASE2_KHONG_DIEM, theo_id)

    # 4. Thông tin backend của summary LLM (nếu có) - chỉ in, không suy luận.
    backend_used = llm_summary.get("backend_used")
    model_id = llm_summary.get("model_id")
    if backend_used is not None or model_id is not None:
        print(f"\nBackend LLM trong summary: backend_used={backend_used}, "
              f"model_id={model_id}")

    # 5. Phát hiện map chưa kiểm chứng.
    chua_kiem_chung = find_unverified(llm_summary)
    if chua_kiem_chung:
        print("\nLOI: phat hien map chua kiem chung trong summary LLM:")
        for muc in chua_kiem_chung:
            print(f"  - {muc}")
        return 1
    print("\nKhong phat hien map chua kiem chung nao trong summary LLM.")
    if not sha_pass:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

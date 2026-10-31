#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runner tích hợp Ingest Phase 1 bằng LLM (Child 2, xem phase1_llm/API_CONTRACT mục 6).

Mục đích của script này
-----------------------
Nối module LLM của Child 1 (``phase1_llm/src/llm_ingest.py``, khi có)
với ingest rule-based đã có của Phase 1, theo đúng luồng của contract
mà KHÔNG sửa bất kỳ file nào trong ``phase1/`` và không sửa dữ liệu
train:

1. Với mỗi shop (sắp xếp theo ``business_id``, deterministic):
   gọi ``phase1.load_shop(business_id, train_dir/data)`` — giữ nguyên
   ``IngestResult``, ``tables``, ``profile``, ``rows_in``/``rows_out``
   của Phase 1. Runner KHÔNG chạy lại forward-fill, dedup, hợp nhất
   hay parse tiền/ngày; mọi con số dòng đều lấy từ kết quả load_shop.
2. Đọc README của shop (nếu có, cắt tối đa 4000 ký tự đầu theo
   contract), gọi ``run_llm_ingest_for_shop`` của Child 1.
3. Với mỗi bảng, tính ``map_columns(df)`` rule-based TRƯỚC; chỉ xét
   map của LLM cho các semantic còn trống trong map rule-based đó.
   Map rule-based đã có KHÔNG BAO GIỜ bị ghi đè âm thầm: map LLM
   trùng semantic (hoặc trùng cột) với map rule-based thì bị loại,
   ghi vào ``rejected_mappings``/``warnings`` và báo cáo riêng.
4. Runner kiểm tra lại mọi map LLM trước khi áp dụng: semantic phải
   nằm trong 37 khóa của ``COLUMN_SYNONYMS`` và tên cột phải tồn
   tại thật trong DataFrame của bảng đó. Map nào không qua kiểm tra
   này thì bị loại + warning, KHÔNG BAO GIỜ lọt vào kết quả. Việc
   kiểm chứng mẫu giá trị (tiền/ngày/số lượng >= 25%) là trách nhiệm
   của ``llm_ingest`` theo contract mục 5; runner tin vào map đã qua
   ``validate_column_mapping`` của module đó, và chỉ áp dụng thêm
   lớp kiểm tra cấu trúc này.
5. Cập nhật hồ sơ shop CHỈ từ map đã kiểm chứng và dữ liệu thật:
   các cờ hồ sơ chỉ được đổi False -> True khi map đã kiểm chứng
   chứng minh dữ liệu thật có cột đó; không bao giờ đổi True -> False
   chỉ vì LLM nói vậy khi dữ liệu thật vẫn có cột đó. Vì map áp dụng
   luôn là phần bổ sung trên map rule-based nên tính đơn điệu này
   được bảo đảm bằng cấu trúc (xem :func:`compute_profile_delta`).
6. Ghi ``reports/phase1_llm_summary.json`` và
   ``reports/INGEST_LLM_REPORT.md`` (báo cáo do chính runner này sinh
   ra từ summary, để Child 3 đối chiếu số liệu).

Trạng thái module LLM và cách runner phát hiện module thật
----------------------------------------------------------
Việc phát hiện ``llm_ingest`` diễn ra ở thời điểm import, không cần
sửa runner khi module đó xuất hiện:

- Nếu ``phase1_llm/src/llm_ingest.py`` tồn tại và import được, runner
  dùng ``run_llm_ingest_for_shop`` thật của module đó (và truyền
  ``backend=None`` — tức không có backend mô hình thật — trừ khi
  caller truyền backend vào qua :func:`run_phase1_llm`).
- Nếu module đó chưa có hoặc import lỗi, runner KHÔNG crash mà rơi
  về fallback rule-based cho toàn bộ shop: ``added_mappings`` rỗng,
  mọi bảng có ``source='rule_fallback'``, ``fallback_count`` bằng số
  bảng của shop, và ``backend_used='none_fallback'`` trong summary.
  Đây là hành vi đúng theo contract mục 2 (không có backend thật thì
  mọi lời gọi LLM đều fallback rule-based, không crash) và theo đúng
  nhiệm vụ của Child 2: coordinator sẽ tích hợp lại sau khi Child 1
  xong, chỉ cần chạy lại runner này là có số liệu LLM thật.
- QUAN TRỌNG: chỉ riêng việc module ``llm_ingest`` tồn tại KHÔNG có
  nghĩa là map đã do LLM gợi ý. Module thật, khi không được truyền
  backend mô hình (không có weights/transformers, DEFAULT_MODEL_ID
  là None theo contract), sẽ tự rơi về rule-based với
  ``source='rule_fallback'`` theo thiết kế của nó. Vì vậy báo cáo phải
  căn cứ vào ``sources`` (số bảng từng nguồn) chứ không chỉ căn cứ
  vào việc module có tồn tại hay không. KHÔNG gọi mạng/API thương
  mại nào trong runner này; mọi đường chạy đều offline.

Cách chạy (mặc định tính tương đối từ thư mục ``phase1_llm``)::

    python3 run_phase1_llm.py [--train-dir ../train]
      [--shops biz_101,biz_102] [--limit-shops N]
      [--output reports/phase1_llm_summary.json]

Quy ước thoát: trả 0 chỉ khi không shop nào lỗi VÀ không có map chưa
kiểm chứng nào lọt vào kết quả (kiểm toán cuối mỗi shop, xem
:func:`process_shop`). Shop lỗi riêng lẻ được bắt, ghi vào summary và
làm exit khác 0 (khác với phase2_llm, vì ở đây summary theo shop là
sản phẩm chính — một shop lỗi mà vẫn exit 0 sẽ làm coordinator tưởng
đợt chạy đã hoàn tất), nhưng đợt chạy vẫn TIẾP TỤC sang shop khác
để thu đủ số liệu cho các shop còn lại. Lỗi ở tầng chạy chung (không
đọc được thư mục train, không tính được sha256 phase1) lan ra ngoài
và làm main trả exit khác 0 ngay.

Tổng quát hoá: script không hard-code business_id, tên cột train hay
luật của rulebook train. Danh sách shop được phát hiện từ thư mục
dữ liệu (hoặc từ ``--shops`` do người gọi truyền), các nhóm shop khó
trong báo cáo chỉ dùng để TRÌNH BÀY số liệu, không dùng để quyết
định map hay hồ sơ.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Nạp phase1 và phase1_llm/src bằng đường dẫn tương đối từ file này,
# không hard-code đường dẫn tuyệt đối của máy. File này nằm ở
# <datamate>/phase1_llm/run_phase1_llm.py nên phase1/src nằm ngay dưới
# thư mục cha của nó, còn llm_ingest (khi có) nằm ở
# <datamate>/phase1_llm/src/.
# ---------------------------------------------------------------------------
_PHASE1_LLM_DIR = Path(__file__).resolve().parent
_DATAMATE_DIR = _PHASE1_LLM_DIR.parent
_PHASE1_SRC = _DATAMATE_DIR / "phase1" / "src"
_LLM_SRC = _PHASE1_LLM_DIR / "src"
for _candidate in (str(_LLM_SRC), str(_PHASE1_SRC)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from ingest import COLUMN_SYNONYMS, load_shop, map_columns  # noqa: E402

#: 37 semantic hợp lệ, lấy đúng từ ``COLUMN_SYNONYMS`` của phase1
#: (contract mục 3). Runner không tự định nghĩa lại tập này để tránh
#: lệch với phase1 khi phase1 đổi từ điển.
ALLOWED_SEMANTICS = frozenset(COLUMN_SYNONYMS.keys())

#: Số ký tự tối đa của README đưa vào LLM, theo contract mục 4.
README_MAX_CHARS = 4000

#: Các cờ hồ sơ shop mà runner được phép cập nhật từ map đã kiểm
#: chứng (xem :func:`compute_profile_delta`). ``has_return_table``
#: không nằm trong danh sách này vì nó suy từ TÊN bảng trong phase1,
#: không suy từ map cột — LLM không đổi tên bảng nên cờ này luôn giữ
#: nguyên giá trị của phase1.
PROFILE_FLAGS = (
    "has_product_detail",
    "has_customer_code",
    "has_customer_name",
    "has_cost",
    "has_lot_expiry",
)

#: Nhóm shop khó dùng để TRÌNH BÀY trong báo cáo (không dùng để quyết
#: định map): 5 shop holdout của phase1, và 4 shop từng 0 câu đúng ở
#: Giai đoạn 2 theo nhiệm vụ gốc. Liệt kê ở đây để báo cáo không phải
#: hard-code lại trong hàm ghi báo cáo, và để coordinator thấy rõ
#: nguồn của từng nhóm.
HOLDOUT_SHOPS = ("biz_105", "biz_106", "biz_117", "biz_123", "biz_124")
PHASE2_ZERO_SHOPS = ("biz_114", "biz_117", "biz_125", "biz_129")

__all__ = [
    "ALLOWED_SEMANTICS",
    "README_MAX_CHARS",
    "HOLDOUT_SHOPS",
    "PHASE2_ZERO_SHOPS",
    "sha256_file",
    "phase1_hashes",
    "load_baseline",
    "resolve_data_dir",
    "discover_shops",
    "read_readme",
    "verify_llm_mappings",
    "compute_profile_delta",
    "process_shop",
    "run_phase1_llm",
    "write_report",
    "parse_args",
    "main",
]


def _try_import_real_llm_ingest():
    """Thử nạp ``run_llm_ingest_for_shop`` thật từ ``llm_ingest.py``.

    Đầu vào: không có (đọc module ``llm_ingest`` trên ``sys.path``).

    Đầu ra:
        Tuple ``(hàm, model_id, lỗi)`` trong đó hàm là
        ``run_llm_ingest_for_shop`` thật nếu module tồn tại và import
        được, ngược lại là ``None``; ``model_id`` là
        ``DEFAULT_MODEL_ID`` của module đó nếu có (thường là None theo
        contract mục 2, vì model ≤15B chưa được chọn/kiểm chứng), ngược
        lại là None; ``lỗi`` là repr của lỗi import nếu có, ngược lại
        là None. Mọi lỗi import đều được bắt, vì module đang được
        agent khác làm song song và có thể ở trạng thái dở dang;
        runner không được crash chỉ vì module đó chưa về hoặc import
        lỗi.
    """
    try:
        from llm_ingest import run_llm_ingest_for_shop as real_fn  # noqa: E402
    except Exception as exc:  # noqa: BLE001 - module chưa về thì fallback
        return None, None, repr(exc)
    model_id = None
    try:
        from llm_ingest import DEFAULT_MODEL_ID  # noqa: E402

        model_id = DEFAULT_MODEL_ID
    except Exception:
        model_id = None
    return real_fn, model_id, None


_REAL_LLM_FN, _LLM_MODEL_ID, _LLM_IMPORT_ERROR = _try_import_real_llm_ingest()

#: True khi module LLM thật đã về và import được (chưa đủ để kết luận
#: map do LLM gợi ý — xem ghi chú ở đầu file về ``sources``).
LLM_MODULE_AVAILABLE = _REAL_LLM_FN is not None


def sha256_file(path):
    """Tính sha256 của một file, trả chuỗi hex.

    Đầu vào:
        path: đường dẫn file cần băm (``str`` hoặc ``Path``).

    Đầu ra:
        Chuỗi hex sha256 của toàn bộ nội dung file, đọc theo khối để
        không nạp cả file lớn vào bộ nhớ. File không tồn tại thì raise
        ``FileNotFoundError`` (không trả hash giả), vì hash này dùng
        để Child 3 đối chiếu phase1 có bị sửa trong lúc chạy hay không.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def phase1_hashes():
    """Tính sha256 của ``phase1/src/ingest.py`` và ``normalizers.py``.

    Đầu vào: không có (đường dẫn suy từ vị trí file runner này).

    Đầu ra:
        Dict ``{'ingest.py': hex, 'normalizers.py': hex}``. Hai file
        này là toàn bộ logic chuẩn hoá mà runner dựa vào; ghi hash của
        chúng vào summary để Child 3 đối chiếu phase1 trước/sau đợt
        chạy (runner không sửa phase1 nên hash trước và sau phải bằng
        nhau — xem :func:`run_phase1_llm`).
    """
    return {
        "ingest.py": sha256_file(_PHASE1_SRC / "ingest.py"),
        "normalizers.py": sha256_file(_PHASE1_SRC / "normalizers.py"),
    }


def load_baseline():
    """Đọc ``phase1/reports/phase1_summary.json`` làm baseline (chỉ đọc).

    Đầu vào: không có (đường dẫn suy từ vị trí file runner này).

    Đầu ra:
        Dict ``business_id -> per_shop`` của baseline (lấy từ khóa
        ``ingest.per_shop``), hoặc dict rỗng khi file baseline không
        tồn tại hoặc không đọc được — khi đó caller ghi warning và
        bỏ qua so sánh rows/mapped với baseline thay vì crash, vì
        baseline chỉ dùng để SO SÁNH, không dùng để quyết định map.
    """
    baseline_path = _DATAMATE_DIR / "phase1" / "reports" / "phase1_summary.json"
    try:
        with open(baseline_path, encoding="utf-8") as handle:
            data = json.load(handle)
        per_shop = data.get("ingest", {}).get("per_shop", [])
        return {
            str(item.get("business_id")): item
            for item in per_shop
            if isinstance(item, dict) and item.get("business_id")
        }
    except Exception:
        return {}


def resolve_data_dir(train_dir):
    """Xác định thư mục chứa các thư mục shop từ ``--train-dir``.

    Đầu vào:
        train_dir: thư mục split (kỳ vọng chứa ``data/``) hoặc chính
        thư mục dữ liệu (chứa trực tiếp các thư mục ``biz_*``).

    Đầu ra:
        ``Path`` tới thư mục chứa các shop. Ưu tiên
        ``train_dir/data`` khi nó tồn tại (đúng cấu trúc split chuẩn);
        ngược lại, nếu bản thân ``train_dir`` chứa thư mục con
        ``biz_*`` thì dùng chính nó; còn lại raise ``FileNotFoundError``
        nói rõ đường dẫn, vì không có dữ liệu thì không thể chạy.
    """
    train_dir = Path(train_dir)
    data_dir = train_dir / "data"
    if data_dir.is_dir():
        return data_dir
    if train_dir.is_dir() and any(
        child.is_dir() and child.name.startswith("biz_")
        for child in train_dir.iterdir()
    ):
        return train_dir
    raise FileNotFoundError(
        f"Không tìm thấy thư mục dữ liệu shop trong '{train_dir}' "
        "(cần 'data/' hoặc trực tiếp các thư mục 'biz_*')."
    )


def discover_shops(data_dir, shops=None):
    """Phát hiện danh sách shop cần chạy, đã sắp xếp (deterministic).

    Đầu vào:
        data_dir: thư mục chứa các thư mục shop (xem
            :func:`resolve_data_dir`).
        shops: list ``business_id`` do người gọi truyền qua ``--shops``,
            hoặc ``None`` để chạy mọi shop có trong thư mục dữ liệu.

    Đầu ra:
        List ``business_id`` đã sắp xếp theo thứ tự bảng chữ cái, bỏ
        trùng và bỏ chuỗi rỗng. Khi ``shops`` được truyền thì vẫn sắp
        xếp lại (không giữ thứ tự người gọi gõ) để kết quả tái lập;
        shop không tồn tại trong thư mục dữ liệu vẫn giữ trong danh
        sách để :func:`process_shop` ghi lỗi rõ ràng thay vì lặng lẽ
        bỏ qua (bỏ qua sẽ làm summary thiếu shop mà exit vẫn 0).
    """
    if shops:
        wanted = sorted({str(shop).strip() for shop in shops if str(shop).strip()})
        return wanted
    return sorted(
        child.name
        for child in Path(data_dir).iterdir()
        if child.is_dir() and child.name.startswith("biz_")
    )


def read_readme(data_dir, business_id):
    """Đọc README của một shop, cắt tối đa 4000 ký tự đầu.

    Đầu vào:
        data_dir: thư mục chứa các thư mục shop.
        business_id: mã shop, cũng là tên thư mục shop.

    Đầu ra:
        Tuple ``(readme_text, readme_found, readme_truncated)`` trong
        đó ``readme_text`` là nội dung đã cắt (chuỗi rỗng khi không có
        README), ``readme_found`` cho biết có file README hay không,
        ``readme_truncated`` cho biết nội dung gốc có dài hơn giới hạn
        hay không. File README được tìm theo tên không phân biệt hoa
        thường (``readme.md``/``README.md``...), vì các shop đặt tên
        không thống nhất; file đọc lỗi thì coi như không có README và
        caller ghi warning thay vì crash cả shop.
    """
    shop_dir = Path(data_dir) / business_id
    readme_path = None
    if shop_dir.is_dir():
        for child in sorted(shop_dir.iterdir()):
            if child.is_file() and child.name.lower() == "readme.md":
                readme_path = child
                break
    if readme_path is None:
        return "", False, False
    try:
        text = readme_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "", False, False
    if len(text) > README_MAX_CHARS:
        return text[:README_MAX_CHARS], True, True
    return text, True, False


def verify_llm_mappings(llm_added, rule_mappings, tables):
    """Kiểm tra lại map LLM trước khi áp dụng, chỉ giữ map đã kiểm chứng.

    Đầu vào:
        llm_added: dict ``bảng -> semantic -> tên cột`` mà
            ``llm_ingest`` gợi ý (đã qua ``validate_column_mapping``
            của module đó theo contract mục 5).
        rule_mappings: dict ``bảng -> {semantic -> cột}`` rule-based
            đã tính bằng ``map_columns`` cho từng bảng.
        tables: dict ``bảng -> DataFrame`` của shop (từ
            ``load_shop``).

    Đầu ra:
        Tuple ``(applied, rejected)`` trong đó ``applied`` là dict
        ``bảng -> semantic -> cột`` chỉ chứa map đã qua kiểm tra của
        runner, còn ``rejected`` là list dict ``{table, semantic,
        column, reason}`` cho mọi map bị loại. Một map bị loại khi:
        - semantic không nằm trong 37 khóa của ``COLUMN_SYNONYMS``;
        - tên cột không tồn tại trong DataFrame của bảng đó;
        - semantic đã có trong map rule-based của bảng đó (ghi đè âm
          thầm bị cấm — kể cả khi cột LLM gợi ý khác cột rule-based,
          trường hợp mâu thuẫn này phải báo cáo riêng, không tự chọn
          phe nào; xem contract mục 6c);
        - cột đã được map rule-based gán cho một semantic khác (một
          cột chỉ mang một semantic, giống nguyên tắc gán tham lam
          của ``map_columns``).

    Vì sao runner kiểm tra lại dù ``llm_ingest`` đã kiểm chứng mẫu:
    kiểm chứng mẫu bảo đảm giá trị khớp semantic, nhưng không bảo đảm
    module LLM trả đúng bảng/cột (ví dụ nhầm tên cột giữa hai bảng) —
    lớp kiểm tra cấu trúc này là rào chắn cuối cùng trước khi map
    lọt vào hồ sơ shop, và là điều kiện để exit 0 (không map chưa
    kiểm chứng nào lọt vào kết quả).
    """
    applied = {}
    rejected = []
    for table_name in sorted(llm_added or {}):
        per_table = llm_added.get(table_name) or {}
        if not isinstance(per_table, dict):
            rejected.append({
                "table": str(table_name), "semantic": "",
                "column": "", "reason": "mapping_cua_bang_khong_phai_dict",
            })
            continue
        df = tables.get(table_name)
        df_columns = set() if df is None else {str(col) for col in df.columns}
        rule_map = rule_mappings.get(table_name) or {}
        rule_columns = set(rule_map.values())
        for semantic in sorted(per_table):
            column = per_table[semantic]
            column_text = "" if column is None else str(column)
            if semantic not in ALLOWED_SEMANTICS:
                rejected.append({
                    "table": str(table_name), "semantic": str(semantic),
                    "column": column_text,
                    "reason": "semantic_khong_nam_trong_37_khoa_COLUMN_SYNONYMS",
                })
            elif df is None:
                rejected.append({
                    "table": str(table_name), "semantic": str(semantic),
                    "column": column_text,
                    "reason": "bang_khong_ton_tai_trong_IngestResult",
                })
            elif column_text not in df_columns:
                rejected.append({
                    "table": str(table_name), "semantic": str(semantic),
                    "column": column_text,
                    "reason": "cot_khong_ton_tai_trong_DataFrame_cua_bang",
                })
            elif semantic in rule_map:
                rejected.append({
                    "table": str(table_name), "semantic": str(semantic),
                    "column": column_text,
                    "reason": "trung_semantic_voi_map_rule_based_khong_ghi_de_am_tham",
                })
            elif column_text in rule_columns:
                rejected.append({
                    "table": str(table_name), "semantic": str(semantic),
                    "column": column_text,
                    "reason": "cot_da_duoc_map_rule_based_gan_cho_semantic_khac",
                })
            else:
                applied.setdefault(str(table_name), {})[str(semantic)] = column_text
    return applied, rejected


def compute_profile_delta(profile, merged_mappings, table_names):
    """Tính hồ sơ shop mới và delta chỉ từ map đã kiểm chứng.

    Đầu vào:
        profile: ``ShopProfile`` gốc của phase1 (không sửa trực tiếp).
        merged_mappings: dict ``bảng -> {semantic -> cột}`` sau khi
            gộp map rule-based với map LLM đã kiểm chứng (xem
            :func:`process_shop`).
        table_names: tên các bảng của shop (dùng để giữ nguyên cờ
            ``has_return_table`` — cờ này suy từ tên bảng, không suy
            từ map, nên runner không tính lại từ map).

    Đầu ra:
        Tuple ``(new_columns_mapped, profile_delta)`` trong đó
        ``new_columns_mapped`` là dict 37 semantic -> cột (giữ nguyên
        giá trị phase1 cho semantic đã map, điền cột LLM đã kiểm chứng
        cho semantic còn trống, theo thứ tự bảng đã sắp xếp), còn
        ``profile_delta`` là dict chỉ chứa các trường THAY ĐỔI, mỗi
        trường là ``{'old': ..., 'new': ...}``, cộng khóa
        ``columns_mapped_added`` liệt kê ``semantic -> cột`` mới thêm.

    Bảo đảm đơn điệu (không bao giờ True -> False): mọi cờ mới đều
    tính bằng ``cờ_cũ OR cờ_tính_từ_map_mới`` — map bổ sung chỉ có
    thể bật thêm cờ, không thể tắt cờ mà dữ liệu thật của phase1 đã
    bật. ``has_product_detail`` tính đúng quy tắc phase1: có bảng vừa
    có mặt hàng (mã hoặc tên) vừa có mã đơn hoặc số lượng.
    """
    old_mapped = dict(getattr(profile, "columns_mapped", None) or {})
    new_mapped = {}
    added = {}
    for semantic in sorted(COLUMN_SYNONYMS.keys()):
        old_value = old_mapped.get(semantic, "")
        if old_value:
            new_mapped[semantic] = old_value
            continue
        found = ""
        for table_name in sorted(merged_mappings):
            column = merged_mappings[table_name].get(semantic)
            if column:
                found = column
                break
        new_mapped[semantic] = found
        if found:
            added[semantic] = found

    def _has(semantic):
        """True khi semantic có cột ở bảng nào đó trong map đã gộp."""
        return any(
            bool(mapping.get(semantic))
            for mapping in merged_mappings.values()
        )

    has_product_detail_new = False
    for mapping in merged_mappings.values():
        has_product = bool(mapping.get("product_name")) \
            or bool(mapping.get("product_code"))
        has_order_or_qty = bool(mapping.get("order_id")) \
            or bool(mapping.get("quantity"))
        if has_product and has_order_or_qty:
            has_product_detail_new = True
            break

    new_flags = {
        "has_product_detail": bool(
            getattr(profile, "has_product_detail", False)
            or has_product_detail_new
        ),
        "has_customer_code": bool(
            getattr(profile, "has_customer_code", False)
            or _has("customer_code")
        ),
        "has_customer_name": bool(
            getattr(profile, "has_customer_name", False)
            or _has("customer_name")
        ),
        "has_cost": bool(
            getattr(profile, "has_cost", False) or _has("cost_price")
        ),
        "has_lot_expiry": bool(
            getattr(profile, "has_lot_expiry", False)
            or _has("lot_code") or _has("expiry_date")
        ),
    }
    profile_delta = {}
    for flag in PROFILE_FLAGS:
        old_value = bool(getattr(profile, flag, False))
        new_value = new_flags[flag]
        if new_value != old_value:
            profile_delta[flag] = {"old": old_value, "new": new_value}
    if added:
        profile_delta["columns_mapped_added"] = added
    return new_mapped, profile_delta


def process_shop(business_id, data_dir, baseline_entry, backend=None):
    """Chạy toàn bộ luồng runner cho một shop và trả dict per_shop.

    Đầu vào:
        business_id: mã shop cần chạy.
        data_dir: thư mục chứa các thư mục shop.
        baseline_entry: dict per_shop của baseline phase1 cho shop này
            (hoặc None khi baseline không có shop đó) — chỉ dùng để
            so sánh rows/mapped, không dùng để quyết định map.
        backend: backend mô hình cục bộ tùy chọn, truyền qua cho
            ``run_llm_ingest_for_shop`` khi module LLM thật tồn tại
            (mặc định None, tức không có backend thật, theo contract
            mục 2).

    Đầu ra:
        Dict per_shop cho summary, gồm ``business_id``, ``status``
        (``'ok'`` hoặc ``'error'``), ``tables``, ``rows_in``/``rows_out``
        (lấy đúng từ ``load_shop``, không chạy lại chuẩn hoá),
        ``rows_match``/``rows_diff_reason`` (so với baseline),
        ``baseline_mapped_count``, ``new_mapped_count``,
        ``llm_added_mappings`` (dict bảng -> semantic -> cột đã kiểm
        chứng), ``llm_added_count``, ``rejected_mappings``,
        ``rejected_count``, ``fallback_count``, ``sources``,
        ``table_roles``, ``profile_delta``, ``warnings``,
        ``readme_found``/``readme_truncated`` và ``elapsed_seconds``.

    Hành vi khi lỗi: mọi ngoại lệ trong shop (load_shop lỗi, LLM lỗi,
    map cột lỗi) đều được bắt thành entry ``status='error'`` với
    ``error`` ghi rõ ngoại lệ, để đợt chạy tiếp tục shop khác; caller
    dùng ``status`` này để quyết định exit code (có shop lỗi thì exit
    khác 0). Riêng lỗi ở tầng LLM KHÔNG được giết shop: khi
    ``run_llm_ingest_for_shop`` raise thì shop rơi về fallback
    rule-based hoàn toàn (giống trường hợp module chưa có) và vẫn
    ``status='ok'`` kèm warning, vì LLM lỗi là lỗi của tầng gợi ý,
    không phải lỗi dữ liệu shop.
    """
    started = time.perf_counter()
    warnings = []
    try:
        shop_result = load_shop(business_id, data_dir)
    except Exception as exc:  # noqa: BLE001 - bắt tổng theo shop
        return {
            "business_id": business_id,
            "status": "error",
            "error": repr(exc),
            "tables": [],
            "rows_in": None,
            "rows_out": None,
            "rows_match": False,
            "rows_diff_reason": "shop_loi_nen_khong_co_rows_de_so_sanh",
            "baseline_mapped_count": None,
            "new_mapped_count": None,
            "llm_added_mappings": {},
            "llm_added_count": 0,
            "rejected_mappings": [],
            "rejected_count": 0,
            "fallback_count": 0,
            "sources": {},
            "table_roles": {},
            "profile_delta": {},
            "warnings": [f"load_shop lỗi: {exc!r}"],
            "readme_found": False,
            "readme_truncated": False,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
        }

    profile = shop_result.profile
    tables = dict(shop_result.tables or {})
    table_names = sorted(tables)

    # Baseline (chỉ đọc, chỉ để so sánh): số semantic đã map và rows.
    baseline_mapped_count = None
    baseline_rows = None
    if baseline_entry is not None:
        baseline_mapped = baseline_entry.get("columns_mapped") or {}
        baseline_mapped_count = sum(
            1 for value in baseline_mapped.values() if value
        )
        baseline_rows = (
            baseline_entry.get("rows_in"), baseline_entry.get("rows_out")
        )
    else:
        warnings.append(
            "Không có baseline phase1 cho shop này, bỏ qua so sánh "
            "rows/mapped với baseline."
        )
    profile_mapped_count = sum(
        1 for value in (getattr(profile, "columns_mapped", None) or {}).values()
        if value
    )
    if baseline_mapped_count is None:
        baseline_mapped_count = profile_mapped_count

    # README (nếu có, cắt 4000 ký tự) rồi gọi LLM của Child 1.
    readme_text, readme_found, readme_truncated = read_readme(
        data_dir, business_id
    )
    if not readme_found:
        warnings.append("Shop không có file README.md.")
    if readme_truncated:
        warnings.append(
            f"README dài hơn {README_MAX_CHARS} ký tự, chỉ lấy "
            f"{README_MAX_CHARS} ký tự đầu theo contract."
        )

    llm_added_raw = {}
    llm_rejected = []
    llm_sources = {}
    llm_table_roles = {}
    llm_fallback_count = 0
    llm_warnings = []
    llm_module_used = False
    if _REAL_LLM_FN is not None:
        llm_module_used = True
        try:
            llm_result = _REAL_LLM_FN(
                shop_result, readme_text, backend=backend
            )
            llm_added_raw = dict(getattr(llm_result, "added_mappings", None) or {})
            llm_rejected = list(getattr(llm_result, "rejected_mappings", None) or [])
            llm_sources = dict(getattr(llm_result, "sources", None) or {})
            llm_table_roles = dict(getattr(llm_result, "table_roles", None) or {})
            llm_fallback_count = int(getattr(llm_result, "fallback_count", 0) or 0)
            llm_warnings = [str(item) for item in
                            (getattr(llm_result, "warnings", None) or [])]
        except Exception as exc:  # noqa: BLE001 - LLM lỗi thì fallback
            warnings.append(
                f"run_llm_ingest_for_shop lỗi, fallback rule-based "
                f"toàn shop: {exc!r}"
            )
            llm_added_raw = {}
            llm_rejected = []
            llm_sources = {}
            llm_table_roles = {}
            llm_fallback_count = len(table_names)
            llm_module_used = False
    if not llm_module_used:
        # Module LLM chưa có (hoặc vừa lỗi): toàn bộ shop fallback
        # rule-based, không crash, không map LLM nào được áp dụng.
        if _LLM_IMPORT_ERROR is not None and _REAL_LLM_FN is None:
            warnings.append(
                "Module llm_ingest chưa tồn tại hoặc import lỗi "
                f"({_LLM_IMPORT_ERROR}), toàn shop fallback rule-based."
            )
        llm_sources = {name: "rule_fallback" for name in table_names}
        llm_fallback_count = len(table_names)

    # Map rule-based trước cho từng bảng, rồi chỉ xét LLM cho semantic
    # còn trống; runner kiểm tra lại map LLM trước khi áp dụng.
    rule_mappings = {}
    for table_name in table_names:
        try:
            rule_mappings[table_name] = dict(map_columns(tables[table_name]))
        except Exception as exc:  # noqa: BLE001 - bảng lỗi thì map rỗng
            warnings.append(
                f"Bảng '{table_name}': map_columns lỗi ({exc!r}), "
                "coi như không map được cột nào."
            )
            rule_mappings[table_name] = {}
    applied, runner_rejected = verify_llm_mappings(
        llm_added_raw, rule_mappings, tables
    )
    for item in runner_rejected:
        warnings.append(
            "Loại map LLM chưa kiểm chứng: bảng '{table}', semantic "
            "'{semantic}', cột '{column}' ({reason}).".format(**item)
        )
    # Map LLM mà module đã loại: chuẩn hoá về cùng dạng rejected để
    # báo cáo đếm được, giữ nguyên lý do của module khi có.
    rejected_all = []
    for item in llm_rejected:
        if isinstance(item, dict):
            rejected_all.append({
                "table": str(item.get("table", "")),
                "semantic": str(item.get("semantic", "")),
                "column": str(item.get("column", "")),
                "reason": str(item.get("reason", "llm_ingest_loai")),
            })
        else:
            rejected_all.append({
                "table": "", "semantic": "", "column": "",
                "reason": str(item),
            })
    rejected_all.extend(runner_rejected)

    # Kiểm toán cuối: mọi map đã áp dụng phải qua kiểm tra của runner
    # (tồn tại trong bảng, đúng semantic). Nếu kiểm toán này phát hiện
    # map lạ thì đó là lỗi của runner, phải ghi rõ và caller sẽ cho
    # exit khác 0 — không được để map chưa kiểm chứng lọt vào kết quả.
    unverified_leaked = 0
    for table_name, per_table in applied.items():
        df_columns = {str(col) for col in tables[table_name].columns}
        for semantic, column in per_table.items():
            if semantic not in ALLOWED_SEMANTICS \
                    or column not in df_columns:
                unverified_leaked += 1
                warnings.append(
                    "KIỂM TOÁN LỖI: map chưa kiểm chứng lọt vào kết "
                    f"quả: bảng '{table_name}', semantic '{semantic}', "
                    f"cột '{column}'."
                )

    merged_mappings = {}
    for table_name in table_names:
        merged = dict(rule_mappings.get(table_name) or {})
        merged.update(applied.get(table_name) or {})
        merged_mappings[table_name] = merged
    new_mapped, profile_delta = compute_profile_delta(
        profile, merged_mappings, table_names
    )
    new_mapped_count = sum(1 for value in new_mapped.values() if value)
    llm_added_count = sum(len(per_table) for per_table in applied.values())

    # Rows: lấy đúng từ load_shop, không chạy lại chuẩn hoá nên phải
    # bằng baseline; chênh lệch (nếu có) phải ghi lý do cụ thể.
    rows_in = int(getattr(profile, "rows_in", 0) or 0)
    rows_out = int(getattr(profile, "rows_out", 0) or 0)
    if baseline_rows is not None:
        rows_match = (rows_in, rows_out) == (
            baseline_rows[0], baseline_rows[1]
        )
        if rows_match:
            rows_diff_reason = None
        else:
            rows_diff_reason = (
                "rows khác baseline: baseline "
                f"(rows_in={baseline_rows[0]}, rows_out={baseline_rows[1]}) "
                f"vs lan chay nay (rows_in={rows_in}, rows_out={rows_out}); "
                "runner không chạy lại forward-fill/dedup/hợp nhất/parse "
                "tiền-ngày nên chênh lệch này phản ánh baseline được ghi "
                "từ lần chạy khác (dữ liệu hoặc code phase1 khác lúc ghi "
                "baseline), không phải do LLM; cần Child 3 đối chiếu "
                "sha256 phase1 trong summary trước khi kết luận."
            )
            warnings.append(rows_diff_reason)
    else:
        rows_match = True
        rows_diff_reason = (
            "Không có baseline để so sánh rows (baseline thiếu shop này)."
        )

    for item in llm_warnings:
        warnings.append(f"llm_ingest: {item}")
    warnings.extend(str(item) for item in (getattr(profile, "warnings", None) or []))

    return {
        "business_id": business_id,
        "status": "error" if unverified_leaked else "ok",
        "error": (
            f"{unverified_leaked} map chưa kiểm chứng lọt vào kết quả"
            if unverified_leaked else None
        ),
        "tables": table_names,
        "rows_in": rows_in,
        "rows_out": rows_out,
        "rows_match": rows_match,
        "rows_diff_reason": rows_diff_reason,
        "baseline_mapped_count": baseline_mapped_count,
        "new_mapped_count": new_mapped_count,
        "llm_added_mappings": applied,
        "llm_added_count": llm_added_count,
        "rejected_mappings": rejected_all,
        "rejected_count": len(rejected_all),
        "fallback_count": int(llm_fallback_count),
        "sources": llm_sources,
        "table_roles": llm_table_roles,
        "profile_delta": profile_delta,
        "warnings": warnings,
        "readme_found": readme_found,
        "readme_truncated": readme_truncated,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }


def run_phase1_llm(train_dir, shops=None, limit_shops=None, output_path=None,
                   reports_dir=None, backend=None):
    """Chạy runner cho các shop đã chọn và ghi summary + báo cáo.

    Đầu vào:
        train_dir: thư mục split (mặc định nên là ``../train`` tính từ
            ``phase1_llm``); xem :func:`resolve_data_dir` để biết cách
            tìm thư mục dữ liệu.
        shops: list ``business_id`` cần chạy, hoặc ``None`` để chạy
            mọi shop trong thư mục dữ liệu.
        limit_shops: chỉ chạy N shop đầu tiên theo thứ tự đã sắp
            (``None`` hoặc <= 0 thì không giới hạn).
        output_path: file summary JSON (mặc định là
            ``phase1_llm/reports/phase1_llm_summary.json``).
        reports_dir: thư mục ghi báo cáo ``INGEST_LLM_REPORT.md``
            (mặc định là ``phase1_llm/reports``).
        backend: backend mô hình cục bộ tùy chọn, truyền qua cho
            ``run_llm_ingest_for_shop`` (mặc định None).

    Đầu ra:
        Tuple ``(summary, exit_code)`` trong đó summary là dict đã ghi
        ra file JSON (gồm ``per_shop``, ``totals``, ``backend_used``,
        ``model_id``, ``phase1_sha256_before``/``phase1_sha256_after``
        và ``llm_module_available``), còn exit_code là 0 chỉ khi không
        shop nào lỗi, không có map chưa kiểm chứng nào lọt vào kết
        quả, và hash phase1 trước/sau bằng nhau (chứng tỏ runner không
        sửa phase1 trong lúc chạy); ngược lại là 1. Summary và báo
        cáo vẫn được ghi ngay cả khi exit_code là 1, để coordinator
        thấy được shop nào lỗi và vì sao.
    """
    run_started = time.perf_counter()
    train_dir = Path(train_dir)
    data_dir = resolve_data_dir(train_dir)
    shop_list = discover_shops(data_dir, shops=shops)
    if limit_shops is not None and limit_shops > 0:
        shop_list = shop_list[:limit_shops]
    baseline = load_baseline()
    if not baseline:
        baseline_note = (
            "Không đọc được phase1/reports/phase1_summary.json, "
            "so sánh baseline bị bỏ qua."
        )
    else:
        baseline_note = None

    hashes_before = phase1_hashes()
    per_shop = []
    shop_errors = []
    for business_id in shop_list:
        entry = process_shop(business_id, data_dir, baseline.get(business_id),
                             backend=backend)
        per_shop.append(entry)
        if entry["status"] != "ok":
            shop_errors.append({
                "business_id": business_id,
                "error": entry.get("error"),
            })
    hashes_after = phase1_hashes()
    hashes_changed = hashes_before != hashes_after

    ok_shops = [entry for entry in per_shop if entry["status"] == "ok"]
    total_tables = sum(len(entry["tables"]) for entry in ok_shops)
    total_added = sum(entry["llm_added_count"] for entry in ok_shops)
    total_rejected = sum(entry["rejected_count"] for entry in ok_shops)
    total_fallback = sum(entry["fallback_count"] for entry in ok_shops)
    sources_counter = {}
    for entry in ok_shops:
        for source in (entry["sources"] or {}).values():
            sources_counter[str(source)] = sources_counter.get(str(source), 0) + 1
    # Backend thực tế: 'none_fallback' khi không có backend thật hoặc
    # mọi bảng đều fallback; ngược lại ghi tên module LLM (model_id
    # vẫn null khi module chưa chọn model, theo contract mục 2).
    if sources_counter.get("llm", 0) > 0:
        backend_used = "llm_ingest"
    else:
        backend_used = "none_fallback"

    summary = {
        "shops_requested": shop_list,
        "shops_total": len(per_shop),
        "shops_ok": len(ok_shops),
        "shops_error": shop_errors,
        "per_shop": per_shop,
        "totals": {
            "shops": len(per_shop),
            "shops_ok": len(ok_shops),
            "tables": total_tables,
            "rows_in": sum(entry["rows_in"] or 0 for entry in ok_shops),
            "rows_out": sum(entry["rows_out"] or 0 for entry in ok_shops),
            "rows_match_all": all(
                entry["rows_match"] for entry in ok_shops
            ) if ok_shops else True,
            "llm_added_mappings": total_added,
            "rejected_mappings": total_rejected,
            "fallback_count": total_fallback,
            "sources": sources_counter,
            "elapsed_seconds": round(time.perf_counter() - run_started, 2),
        },
        "backend_used": backend_used,
        "model_id": _LLM_MODEL_ID,
        "llm_module_available": LLM_MODULE_AVAILABLE,
        "llm_import_error": _LLM_IMPORT_ERROR,
        "baseline_note": baseline_note,
        "phase1_sha256_before": hashes_before,
        "phase1_sha256_after": hashes_after,
        "phase1_sha256_changed": hashes_changed,
    }

    output_path = Path(output_path) if output_path is not None \
        else _PHASE1_LLM_DIR / "reports" / "phase1_llm_summary.json"
    reports_dir = Path(reports_dir) if reports_dir is not None \
        else _PHASE1_LLM_DIR / "reports"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    write_report(summary, reports_dir / "INGEST_LLM_REPORT.md")

    exit_code = 0
    if shop_errors or hashes_changed:
        exit_code = 1
    return summary, exit_code


def write_report(summary, report_path):
    """Ghi báo cáo ``INGEST_LLM_REPORT.md`` từ summary của đợt chạy.

    Đầu vào:
        summary: dict summary do :func:`run_phase1_llm` tạo (gồm
            ``per_shop``, ``totals``, ``backend_used``, ``model_id``,
            hash phase1 trước/sau).
        report_path: đường dẫn file báo cáo cần ghi.

    Đầu ra:
        Không trả giá trị; ghi file Markdown gồm kiến trúc, cách chạy,
        bảng so sánh theo shop với baseline, nhóm shop khó (holdout
        và nhóm từng 0 câu đúng ở Giai đoạn 2) với ghi chú LLM cứu
        được map nào / còn fallback ở đâu, giới hạn và bước tiếp theo.
        Mọi con số trong báo cáo đều lấy từ summary (không tính lại,
        không bịa), và backend thực tế được ghi rõ ràng, trung thực:
        khi ``backend_used`` là ``'none_fallback'`` thì báo cáo nói
        thẳng là chưa có map nào do LLM gợi ý trong đợt chạy này.
    """
    per_shop = summary.get("per_shop", [])
    totals = summary.get("totals", {})
    ok_shops = [entry for entry in per_shop if entry["status"] == "ok"]
    backend_used = summary.get("backend_used", "none_fallback")
    model_id = summary.get("model_id")
    lines = []
    lines.append("# Báo cáo Ingest Phase 1 bằng LLM (phase1_llm)")
    lines.append("")
    lines.append(
        "Báo cáo này do `run_phase1_llm.py` sinh tự động từ "
        "`phase1_llm_summary.json` của cùng đợt chạy — mọi con số dưới "
        "đây đều lấy từ summary đó, không tính lại bằng tay."
    )
    lines.append("")
    lines.append("## 1. Backend thực tế đã dùng (ghi trung thực)")
    lines.append("")
    if backend_used == "none_fallback":
        lines.append(
            "- **Backend thực tế: `none_fallback` — chưa có map nào do "
            "LLM gợi ý trong đợt chạy này.** Mọi bảng đều fallback "
            "rule-based (`source='rule_fallback'`), mọi map áp dụng đều "
            "là map rule-based của phase1; `llm_added_mappings` bằng 0 "
            "là kết quả ĐÚNG của đợt chạy này, không phải lỗi ghi số."
        )
        if not summary.get("llm_module_available"):
            lines.append(
                "- Lý do: module `phase1_llm/src/llm_ingest.py` của "
                "Child 1 chưa tồn tại (hoặc import lỗi) lúc chạy runner, "
                "nên runner rơi về fallback rule-based cho toàn bộ shop "
                "theo đúng contract mục 2 và nhiệm vụ Child 2. "
                "Coordinator cần tích hợp lại sau khi Child 1 bàn giao "
                "module, chỉ cần chạy lại runner này là có số liệu LLM "
                "thật — không cần sửa runner."
            )
        else:
            lines.append(
                "- Module `llm_ingest` đã import được nhưng không bảng "
                "nào có `source='llm'` (module tự fallback vì chưa có "
                "backend mô hình thật, `DEFAULT_MODEL_ID=None` theo "
                "contract mục 2)."
            )
    else:
        lines.append(
            f"- **Backend thực tế: `{backend_used}`** — có bảng do LLM "
            "gợi ý (xem cột `sources` trong summary)."
        )
    lines.append(
        f"- `model_id`: `{model_id}` "
        "(null nghĩa là chưa chọn/kiểm chứng model ≤15B — không ghi "
        "tên model, licence hay ngày phát hành nào chưa kiểm chứng từ "
        "nguồn chính thức, theo contract mục 2)."
    )
    lines.append(
        f"- Module `llm_ingest` có sẵn lúc chạy: "
        f"`{summary.get('llm_module_available')}`."
    )
    if summary.get("llm_import_error"):
        lines.append(
            f"- Lỗi import module LLM (nếu có): "
            f"`{summary.get('llm_import_error')}`."
        )
    lines.append("")
    lines.append("## 2. Kiến trúc và luồng chạy")
    lines.append("")
    lines.append(
        "1. `load_shop` của phase1 chạy trước cho mỗi shop — giữ nguyên "
        "`IngestResult`, `tables`, `profile`, `rows_in`/`rows_out`. "
        "Runner KHÔNG chạy lại forward-fill, dedup, hợp nhất hay parse "
        "tiền/ngày, nên `rows_in`/`rows_out` phải bằng baseline phase1."
    )
    lines.append(
        "2. Đọc README của shop (nếu có, cắt tối đa 4000 ký tự đầu) rồi "
        "gọi `run_llm_ingest_for_shop` của Child 1 (hoặc fallback "
        "rule-based toàn shop khi module/backend thiếu)."
    )
    lines.append(
        "3. Với mỗi bảng, tính `map_columns(df)` rule-based TRƯỚC; chỉ "
        "xét map LLM cho các semantic còn trống. Map LLM trùng semantic "
        "hoặc trùng cột với map rule-based thì bị loại, ghi vào "
        "`rejected_mappings`/`warnings`, KHÔNG ghi đè âm thầm."
    )
    lines.append(
        "4. Runner kiểm tra lại mọi map LLM: semantic phải nằm trong 37 "
        "khóa của `COLUMN_SYNONYMS` và tên cột phải tồn tại thật trong "
        "DataFrame của bảng đó; map không qua kiểm tra thì bị loại + "
        "warning. Kiểm chứng mẫu giá trị (tiền/ngày/số lượng ≥ 25%) là "
        "trách nhiệm của `llm_ingest` theo contract mục 5."
    )
    lines.append(
        "5. Cập nhật hồ sơ shop CHỈ từ map đã kiểm chứng, đơn điệu "
        "False -> True (không bao giờ True -> False chỉ vì LLM nói "
        "vậy khi dữ liệu thật vẫn có cột đó); delta ghi rõ trong "
        "`profile_delta` của từng shop."
    )
    lines.append(
        "6. Ghi `phase1_llm_summary.json` rồi sinh báo cáo này từ "
        "summary; hash sha256 của `phase1/src/ingest.py` và "
        "`normalizers.py` được tính trước và sau đợt chạy để Child 3 "
        "đối chiếu phase1 có bị sửa hay không."
    )
    lines.append("")
    lines.append("## 3. Cách chạy")
    lines.append("")
    lines.append("```bash")
    lines.append("cd ~/workspace/datasets/datamate/phase1_llm")
    lines.append("python3 run_phase1_llm.py [--train-dir ../train]")
    lines.append("  [--shops biz_101,biz_102] [--limit-shops N]")
    lines.append("  [--output reports/phase1_llm_summary.json]")
    lines.append("```")
    lines.append("")
    lines.append(
        "Exit 0 chỉ khi không shop nào lỗi, không có map chưa kiểm "
        "chứng nào lọt vào kết quả, và hash phase1 trước/sau bằng nhau. "
        "Shop lỗi riêng lẻ vẫn được ghi vào summary và đợt chạy vẫn "
        "tiếp tục shop khác, nhưng exit sẽ khác 0."
    )
    lines.append("")
    lines.append("## 4. Tổng quan đợt chạy")
    lines.append("")
    lines.append(f"- Shop đã chạy: {totals.get('shops', 0)} "
                 f"(ok: {totals.get('shops_ok', 0)}, "
                 f"lỗi: {len(summary.get('shops_error', []))}).")
    lines.append(f"- Tổng bảng (shop ok): {totals.get('tables', 0)}.")
    lines.append(f"- Tổng rows_in / rows_out (shop ok): "
                 f"{totals.get('rows_in', 0)} / {totals.get('rows_out', 0)}.")
    lines.append(f"- Rows khớp baseline ở mọi shop ok: "
                 f"`{totals.get('rows_match_all')}`.")
    lines.append(f"- Tổng map LLM đã kiểm chứng được áp dụng: "
                 f"{totals.get('llm_added_mappings', 0)}.")
    lines.append(f"- Tổng map bị loại: {totals.get('rejected_mappings', 0)}.")
    lines.append(f"- Tổng fallback: {totals.get('fallback_count', 0)}.")
    lines.append(f"- Nguồn theo bảng: `{totals.get('sources', {})}`.")
    lines.append(f"- Tổng thời gian: {totals.get('elapsed_seconds', 0)} giây.")
    lines.append(f"- Hash phase1 thay đổi trong lúc chạy: "
                 f"`{summary.get('phase1_sha256_changed')}` "
                 f"(ingest.py: `{summary.get('phase1_sha256_before', {}).get('ingest.py')}`, "
                 f"normalizers.py: `{summary.get('phase1_sha256_before', {}).get('normalizers.py')}`).")
    if summary.get("baseline_note"):
        lines.append(f"- Ghi chú baseline: {summary.get('baseline_note')}.")
    if summary.get("shops_error"):
        lines.append("")
        lines.append("Shop lỗi trong đợt chạy:")
        for item in summary.get("shops_error", []):
            lines.append(f"- `{item.get('business_id')}`: {item.get('error')}")
    lines.append("")
    lines.append("## 5. So sánh theo shop với baseline phase1")
    lines.append("")
    lines.append(
        "| Shop | Bảng | Rows in/out | Baseline mapped | Mapped mới | "
        "LLM thêm | Bị loại | Fallback | Nguồn | Profile delta |"
    )
    lines.append(
        "|---|---|---|---|---|---|---|---|---|---|"
    )
    for entry in per_shop:
        delta = entry.get("profile_delta") or {}
        delta_text = "; ".join(
            f"{key}: {value.get('old')}->{value.get('new')}"
            if isinstance(value, dict)
            else f"{key}: {value}"
            for key, value in sorted(delta.items())
        ) if delta else "-"
        sources_text = ",".join(
            sorted({str(source) for source in
                    (entry.get("sources") or {}).values()})
        ) or "-"
        lines.append(
            f"| {entry.get('business_id')} | {len(entry.get('tables') or [])} | "
            f"{entry.get('rows_in')}/{entry.get('rows_out')} | "
            f"{entry.get('baseline_mapped_count')} | "
            f"{entry.get('new_mapped_count')} | "
            f"{entry.get('llm_added_count')} | "
            f"{entry.get('rejected_count')} | "
            f"{entry.get('fallback_count')} | {sources_text} | "
            f"{delta_text} |"
        )
    lines.append("")
    lines.append("## 6. Nhóm shop khó — LLM cứu được map nào, còn fallback ở đâu")
    lines.append("")
    lines.append(
        "Bảng dưới đây chỉ TRÌNH BÀY số liệu của các nhóm shop khó, "
        "không dùng để quyết định map: 5 shop holdout của phase1 "
        "(biz_105, biz_106, biz_117, biz_123, biz_124) và 4 shop từng "
        "0 câu đúng ở Giai đoạn 2 (biz_114, biz_117, biz_125, biz_129; "
        "biz_117 thuộc cả hai nhóm nên xuất hiện một lần)."
    )
    lines.append("")
    by_id = {entry.get("business_id"): entry for entry in per_shop}
    hard_groups = [
        ("Holdout phase1", HOLDOUT_SHOPS),
        ("0 câu đúng ở Giai đoạn 2", PHASE2_ZERO_SHOPS),
    ]
    for group_name, group_shops in hard_groups:
        lines.append(f"### {group_name}")
        lines.append("")
        for business_id in group_shops:
            entry = by_id.get(business_id)
            if entry is None:
                lines.append(f"- `{business_id}`: không chạy trong đợt này.")
                continue
            added = entry.get("llm_added_mappings") or {}
            added_text = "; ".join(
                f"{table}: {semantic} -> {column}"
                for table in sorted(added)
                for semantic, column in sorted(added[table].items())
            ) or "chưa có map LLM nào được áp dụng"
            delta = entry.get("profile_delta") or {}
            delta_text = "; ".join(
                f"{key}: {value.get('old')}->{value.get('new')}"
                if isinstance(value, dict)
                else f"{key}: {value}"
                for key, value in sorted(delta.items())
            ) or "không đổi"
            lines.append(
                f"- `{business_id}` (status: {entry.get('status')}, "
                f"bảng: {len(entry.get('tables') or [])}, "
                f"baseline mapped: {entry.get('baseline_mapped_count')}, "
                f"mapped mới: {entry.get('new_mapped_count')}, "
                f"fallback: {entry.get('fallback_count')}, "
                f"nguồn: {sorted({str(s) for s in (entry.get('sources') or {}).values()})}): "
                f"LLM cứu được: {added_text}; "
                f"profile delta: {delta_text}; "
                f"còn fallback ở: {entry.get('fallback_count')} bảng "
                f"(xem `sources` trong summary để biết bảng nào)."
            )
        lines.append("")
    lines.append("## 7. Giới hạn")
    lines.append("")
    lines.append(
        "- Runner chỉ áp dụng map LLM cho semantic còn trống trong map "
        "rule-based; map LLM mâu thuẫn với map rule-based bị loại thay "
        "vì tự chọn phe nào (theo contract mục 6c, trường hợp LLM đúng "
        "hơn cần được báo cáo riêng và xử lý ở tầng coordinator, không "
        "ghi đè âm thầm trong runner)."
    )
    lines.append(
        "- Runner không kiểm chứng lại mẫu giá trị của map LLM (đó là "
        "trách nhiệm của `llm_ingest` theo contract mục 5); lớp kiểm "
        "tra của runner chỉ bảo đảm semantic hợp lệ và cột tồn tại "
        "thật trong bảng đó."
    )
    lines.append(
        "- Runner không chạy lại chuẩn hoá nên không thể tự tạo thêm "
        "dòng hay sửa dữ liệu: mọi cải thiện chỉ thể hiện ở map cột "
        "và hồ sơ shop, không thể hiện ở `rows_in`/`rows_out`."
    )
    lines.append(
        "- Khi backend là `none_fallback`, toàn bộ đợt chạy này chỉ "
        "chứng minh runner không crash và giữ đúng baseline — nó "
        "không chứng minh LLM cải thiện được map nào."
    )
    lines.append("")
    lines.append("## 8. Bước tiếp theo")
    lines.append("")
    lines.append(
        "1. Coordinator tích hợp lại sau khi Child 1 bàn giao "
        "`src/llm_ingest.py`: chỉ cần chạy lại runner này (không sửa "
        "runner) rồi đối chiếu summary mới với summary đợt này — số "
        "`llm_added_mappings` và `profile_delta` thay đổi mới là bằng "
        "chứng LLM thật có tác dụng."
    )
    lines.append(
        "2. Chọn và kiểm chứng một model cục bộ ≤15B (tên, licence, "
        "ngày phát hành từ nguồn chính thức, trước 2026-10-01) rồi mới "
        "ghi `model_id` khác null vào summary; trước thời điểm đó, "
        "mọi báo cáo đều phải giữ `model_id: null` và "
        "`backend_used: 'none_fallback'` khi chưa có backend thật."
    )
    lines.append(
        "3. Child 3 chạy `tests/test_llm_ingest.py` và "
        "`evaluation/compare_baseline.py` để đối chiếu summary này "
        "với `phase1/reports/phase1_summary.json`, đặc biệt kiểm tra "
        "hash phase1 trước/sau và tính đơn điệu của hồ sơ shop."
    )
    lines.append(
        "4. Sau khi có LLM thật, ưu tiên đo lại đúng các nhóm shop khó "
        "ở mục 6 (holdout và nhóm 0 câu đúng ở Giai đoạn 2) trước khi "
        "kết luận LLM có cứu được map khó hay không."
    )
    lines.append("")
    Path(report_path).write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv=None):
    """Phân tích tham số dòng lệnh theo nhiệm vụ Child 2.

    Đầu vào:
        argv: danh sách tham số (``None`` thì lấy từ ``sys.argv``).

    Đầu ra:
        Đối tượng argparse.Namespace với các trường ``train_dir``,
        ``shops`` (chuỗi gốc hoặc ``None``), ``limit_shops`` (int hoặc
        ``None``) và ``output``. Mặc định tính tương đối từ thư mục
        ``phase1_llm``: ``--train-dir ../train`` và
        ``--output reports/phase1_llm_summary.json``. ``--limit-shops
        0`` được coi là không giới hạn. Cờ ``--limit`` được chấp nhận
        như bí danh của ``--limit-shops`` để tương thích cách gọi của
        phase2_llm, nhưng ``--limit-shops`` mới là tên chính thức
        trong nhiệm vụ này.
    """
    parser = argparse.ArgumentParser(
        description="Chạy Ingest Phase 1 bằng LLM (runner tích hợp).")
    parser.add_argument(
        "--train-dir", default="../train",
        help="Thư mục split train (mặc định ../train, tính từ phase1_llm/).")
    parser.add_argument(
        "--shops", default=None,
        help="Chỉ chạy các shop này, cách nhau bằng dấu phẩy (mặc định: mọi shop).")
    parser.add_argument(
        "--limit-shops", type=int, default=None, dest="limit_shops",
        help="Chỉ chạy N shop đầu tiên theo thứ tự đã sắp (mặc định: mọi shop).")
    parser.add_argument(
        "--limit", type=int, default=None, dest="limit_alias",
        help="Bí danh của --limit-shops (tương thích phase2_llm).")
    parser.add_argument(
        "--output", default="reports/phase1_llm_summary.json",
        help="File summary JSON (mặc định reports/phase1_llm_summary.json, "
        "tính từ phase1_llm/).")
    args = parser.parse_args(argv)
    if args.limit_shops is None:
        args.limit_shops = args.limit_alias
    if args.limit_shops is not None and args.limit_shops <= 0:
        args.limit_shops = None
    return args


def main(argv=None):
    """Điểm vào dòng lệnh: phân tích tham số, chạy runner, trả exit code.

    Đầu vào:
        argv: danh sách tham số (``None`` thì lấy từ ``sys.argv``).

    Đầu ra:
        Exit code int: 0 chỉ khi không shop nào lỗi, không có map chưa
        kiểm chứng nào lọt vào kết quả, và hash phase1 trước/sau bằng
        nhau; ngược lại là 1. Đường dẫn ``--train-dir`` và ``--output``
        được hiểu tương đối từ thư mục ``phase1_llm`` (nơi đặt file
        runner này) khi người gọi truyền đường dẫn tương đối — giống
        cách phase2_llm hiểu đường dẫn tương đối từ thư mục của nó —
        để chạy từ bất kỳ thư mục làm việc nào cũng cho cùng kết quả.
    """
    args = parse_args(argv)
    train_dir = Path(args.train_dir)
    if not train_dir.is_absolute():
        train_dir = (_PHASE1_LLM_DIR / train_dir).resolve()
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = (_PHASE1_LLM_DIR / output_path).resolve()
    reports_dir = output_path.parent
    shops = None
    if args.shops:
        shops = [item.strip() for item in args.shops.split(",") if item.strip()]
    _summary, exit_code = run_phase1_llm(
        train_dir=train_dir,
        shops=shops,
        limit_shops=args.limit_shops,
        output_path=output_path,
        reports_dir=reports_dir,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

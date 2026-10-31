# -*- coding: utf-8 -*-
"""Cache ingest theo shop của Giai đoạn 2 DataMate (Zalo AI Challenge 2026).

Mục đích của module này là tránh ingest lại một shop nhiều lần: ingest
một shop train có thể đọc hàng trăm nghìn dòng Excel/CSV, trong khi một
đợt chạy Giai đoạn 2 phải giải mọi câu hỏi của shop đó rồi mới sang shop
khác. Cache lưu view đã xây của shop (lines, orders, profile, khoảng
ngày, danh sách mặt hàng/khách) vào thư mục cache do caller truyền, để
lần chạy sau chỉ cần nạp lại nếu dữ liệu nguồn không đổi.

Vì sao cache phải gắn với hash của dữ liệu nguồn, thay vì chỉ gắn với
``business_id``: dữ liệu nguồn có thể được sửa, thay file hoặc thêm file
giữa hai lần chạy (ví dụ khi test set cập nhật, hoặc khi người dùng sửa
một file shop để kiểm thử tổng quát hoá). Nếu chỉ dùng business_id làm
key, cache cũ sẽ trả view của dữ liệu cũ và mọi đáp án sau đó đều sai
mà không có dấu hiệu nào báo lỗi. Vì vậy key cache gồm business_id CỘNG
với hash của (tên file + kích thước + thời gian sửa đổi) của MỌI file
nguồn trong thư mục shop; hash khác thì :func:`load_shop_cache` trả
``None`` và caller phải ingest lại rồi ghi đè cache.

Lựa chọn định dạng lưu trữ, ghi rõ để bên gọi và người gỡ lỗi biết:

- Nếu môi trường có ``pyarrow`` thì lines/orders được lưu dạng parquet
  (nhỏ, nạp nhanh, giữ kiểu cột tốt hơn pickle cho bảng lớn), còn
  profile, khoảng ngày, danh sách mặt hàng/khách và hash nguồn được lưu
  trong một file pickle metadata đi kèm, vì các đối tượng đó không phải
  bảng và không lưu được bằng parquet.
- Nếu không có ``pyarrow`` (trường hợp của máy hiện tại) thì toàn bộ
  view được lưu bằng pickle trong một file duy nhất. Pickle ở đây chỉ
  là cache nội bộ, không phải file nộp, và chỉ được nạp lại bởi chính
  code này, nên rủi ro của pickle (chạy code lạ khi nạp file lạ) được
  giới hạn ở file do chính hệ thống ghi ra trong cache_dir của nó.
- Nếu ghi parquet thất bại giữa chừng (ví dụ bảng có kiểu cột hỗn hợp
  mà parquet không nhận), toàn bộ lần lưu sẽ rơi về pickle để không tạo
  ra một cache nửa parquet nửa pickle không nạp lại được.

An toàn và tổng quát hoá:

- Cache CHỈ ghi trong ``cache_dir`` do caller truyền, không bao giờ ghi
  vào thư mục dữ liệu train (dữ liệu train là read-only khi chạy
  Docker) và không ghi đè file nguồn.
- Không hard-code theo business_id, tên file shop hay tên cột: hash tính
  trên mọi file trong thư mục shop, bất kể tên file là gì, và view được
  lưu theo thuộc tính chung (lines/orders/profile/...), không theo
  schema của một shop train nào.
- Module này KHÔNG import cứng ``shop_view``: khi tích hợp, class
  ``ShopView`` có thể chưa tồn tại hoặc được định nghĩa ở module chung.
  Lúc lưu, hàm chỉ đọc thuộc tính của view (duck-typing); lúc nạp, hàm
  cố gắng dựng đúng ``ShopView`` từ file ``shop_view.py`` cùng thư mục
  nếu tìm thấy, nếu không thì trả một object có cùng các thuộc tính
  (``types.SimpleNamespace``) để code dùng view theo thuộc tính vẫn chạy
  được. Vì vậy module này không định nghĩa lại dataclass trùng tên
  ``ShopView`` để tránh xung đột khi tích hợp import chung.
- Mọi lỗi khi nạp cache (file hỏng, pickle không tương thích, parquet
  thiếu cột) đều được bắt và trả ``None`` thay vì crash, vì cache hỏng
  chỉ có nghĩa là "ingest lại", không phải lỗi của đợt chạy.
"""

from __future__ import annotations

import hashlib
import pickle
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

__all__ = [
    "cache_path",
    "load_shop_cache",
    "save_shop_cache",
]

#: Các trường chuẩn của ShopView theo API_CONTRACT mục 1.
#:
#: Khi lưu, các trường này được tách ra khỏi view để lưu trữ tường minh;
#: các thuộc tính khác của view (nếu module shop_view tương lai thêm
#: trường) được giữ trong khóa ``extras`` để khi nạp không bị mất.
#: Liệt kê tường minh thay vì lưu nguyên object view là để file cache
#: vẫn nạp được ngay cả khi class ShopView thay đổi thứ tự trường, và
#: để code nạp không phụ thuộc vào việc class đó có import được hay không.
_VIEW_FIELDS = (
    "business_id",
    "profile",
    "lines",
    "orders",
    "date_min",
    "date_max",
    "products",
    "customers",
)

#: Hậu tố file parquet cho bảng lines/orders khi dùng chế độ parquet.
#:
#: Hai file này nằm cùng thư mục với file pickle metadata chính (xem
#: :func:`cache_path`), có cùng tiền tố business_id đã sanitize, nên
#: việc dọn cache của một shop chỉ cần xoá các file có cùng tiền tố đó.
_LINES_PARQUET_SUFFIX = ".lines.parquet"
_ORDERS_PARQUET_SUFFIX = ".orders.parquet"

#: Hậu tố file tạm khi ghi cache, để ghi đè nguyên tử.
#:
#: Ghi vào file tạm rồi đổi tên (rename) sang tên chính giúp cache không
#: bao giờ ở trạng thái nửa vời nếu tiến trình crash giữa chừng: file
#: chính hoặc là bản cũ nguyên vẹn, hoặc là bản mới nguyên vẹn.
_TMP_SUFFIX = ".tmp"


def _sanitize_business_id(business_id: str) -> str:
    """Sanitize business_id thành tên file an toàn trong cache_dir.

    Mục đích: business_id vốn chỉ là tên thư mục shop, nhưng nếu một
    business_id lạ chứa ký tự phân tách đường dẫn (``/``) hoặc ``..``
    thì tên file cache có thể thoát khỏi cache_dir và ghi đè file ngoài
    ý muốn. Hàm này thay mọi ký tự không phải chữ, số, ``-``, ``_``,
    ``.`` bằng ``_``. Với business_id bình thường (ví dụ ``biz_101``)
    thì tên không đổi, nên không ảnh hưởng tới cache đã có.

    Trường hợp biên: business_id rỗng hoặc toàn ký tự lạ thì kết quả
    sanitize có thể rỗng hoặc toàn ``_``; bên gọi nên dùng business_id
    hợp lệ, nhưng hàm vẫn trả một chuỗi không rỗng (``"unknown"``) để
    không tạo ra tên file rỗng gây lỗi hệ thống file.
    """
    text = str(business_id) if business_id is not None else ""
    cleaned = re.sub(r"[^A-Za-z0-9_.\-]+", "_", text)
    cleaned = cleaned.strip(".")
    return cleaned if cleaned else "unknown"


def cache_path(cache_dir: Any, business_id: str) -> Path:
    """Trả đường dẫn file cache chính của một shop.

    Đầu vào: ``cache_dir`` là thư mục cache do caller truyền (chuỗi hoặc
    Path, có thể chưa tồn tại — hàm này chỉ tính đường dẫn, không tạo
    thư mục), ``business_id`` là mã shop.

    Đầu ra: ``Path(cache_dir) / "<business_id đã sanitize>.pkl"``. Đây
    là file pickle metadata chính: ở chế độ pickle toàn bộ view nằm
    trong file này; ở chế độ parquet thì file này chứa metadata, hash
    nguồn và tên các file parquet đi kèm (xem docstring module). Vì vậy
    kiểm tra sự tồn tại của file này là cách nhanh nhất để biết shop có
    cache hay không, nhưng chỉ :func:`load_shop_cache` mới kết luận
    cache còn hiệu lực (sau khi đối chiếu hash nguồn).

    Trường hợp biên: ``cache_dir`` là ``None`` thì hàm vẫn trả Path
    tương đối (dựa trên thư mục hiện tại) thay vì crash, nhưng caller
    không nên dùng đường dẫn đó — caller phải luôn truyền cache_dir
    tường minh theo contract.
    """
    base = Path(cache_dir) if cache_dir is not None else Path(".")
    return base / f"{_sanitize_business_id(business_id)}.pkl"


def _iter_source_files(shop_dir: Path) -> list[Path]:
    """Liệt kê mọi file nguồn trong thư mục shop, sắp xếp xác định.

    Duyệt đệ quy (vì một shop có thể có file trong thư mục con) và sắp
    xếp theo đường dẫn tương đối dạng POSIX, để cùng một tập file luôn
    cho cùng một thứ tự hash, không phụ thuộc thứ tự hệ thống file trả
    về (thứ tự đó có thể khác nhau giữa các lần chạy và các máy). Chỉ
    lấy file thường, bỏ qua thư mục và symlink hỏng; file ẩn cũng được
    tính vì nó vẫn là một phần của dữ liệu nguồn nếu có mặt trong thư
    mục shop.
    """
    if not shop_dir.is_dir():
        return []
    files = [path for path in shop_dir.rglob("*") if path.is_file()]
    files.sort(key=lambda p: p.relative_to(shop_dir).as_posix())
    return files


def _source_hash(shop_dir: Path) -> str | None:
    """Tính hash SHA-256 của (tên file + kích thước + mtime) mọi file nguồn.

    Mỗi file đóng góp ba thành phần, theo thứ tự đã sắp xếp: đường dẫn
    tương đối (để đổi tên file cũng làm hash đổi), kích thước byte
    (để sửa nội dung làm đổi kích thước thì hash đổi), và thời gian sửa
    đổi dạng nano giây ``st_mtime_ns`` (để sửa nội dung mà kích thước
    không đổi thì mtime vẫn đổi; dùng nano giây thay vì giây để hai lần
    sửa liên tiếp trong cùng một giây vẫn phân biệt được).

    Trả ``None`` khi thư mục shop không tồn tại hoặc không có file nào,
    để bên gọi phân biệt "không tính được hash" với một hash thật (chuỗi
    hex). Hash này là một phần của key cache, không phải checksum để
    kiểm tra tính toàn vẹn nội dung từng byte — mục đích của nó là phát
    hiện nguồn đã đổi để vô hiệu cache, và bộ ba trên đã đủ cho mục
    đích đó mà không cần đọc toàn bộ nội dung các file lớn.
    """
    files = _iter_source_files(shop_dir)
    if not files:
        return None
    hasher = hashlib.sha256()
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            # File biến mất giữa chừng khi đang liệt kê (ví dụ shop đang
            # được cập nhật): coi như không tính được hash an toàn, để
            # bên gọi ingest lại thay vì dùng cache dựa trên hash thiếu.
            return None
        relative = path.relative_to(shop_dir).as_posix()
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(str(stat.st_size).encode("ascii"))
        hasher.update(b"\x00")
        hasher.update(str(stat.st_mtime_ns).encode("ascii"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


def _resolve_shop_dir(data_dir: Any, business_id: str) -> Path | None:
    """Xác định thư mục shop từ ``data_dir`` mà caller truyền cho load.

    ``data_dir`` có thể là một trong ba dạng (tùy caller đang giữ đường
    dẫn nào): thư mục gốc của split (chứa ``data/<business_id>``), thư
    mục ``data`` (chứa ``<business_id>``), hoặc chính thư mục shop
    (chứa trực tiếp các file nguồn). Hàm thử theo thứ tự đó và trả thư
    mục đầu tiên tồn tại và chứa ít nhất một file; nếu không có ứng viên
    nào thỏa thì trả ``None`` để load trả ``None`` (không đoán thư mục).

    Vì sao cần cả ba dạng: contract của :func:`load_shop_cache` chỉ ghi
    tham số là ``data_dir`` mà không quy định nó là cấp nào, nên code
    nạp phải chịu được mọi cách gọi hợp lý của integration, thay vì bắt
    integration phải nhớ đúng một cấp.
    """
    if data_dir is None:
        return None
    base = Path(data_dir)
    candidates = [
        base / "data" / str(business_id),
        base / str(business_id),
        base,
    ]
    for candidate in candidates:
        if candidate.is_dir() and _iter_source_files(candidate):
            return candidate
    return None


def _source_dir_from_view(view: Any) -> Path | None:
    """Suy thư mục nguồn từ view nếu view có mang thông tin đó.

    ShopView theo contract không có trường thư mục nguồn, nên hàm này
    chỉ là cầu nối dự phòng: nếu module shop_view tương lai gắn thêm
    thuộc tính ``source_dir`` / ``data_dir`` / ``shop_dir`` vào view,
    hoặc profile có thuộc tính tương tự, thì save có thể tính hash mà
    không cần caller truyền thêm tham số. Nếu không có thuộc tính nào
    thì trả ``None`` và save sẽ lưu cache ở trạng thái "chưa xác minh
    được nguồn" (xem :func:`save_shop_cache`).
    """
    for attr in ("source_dir", "data_dir", "shop_dir", "source_path"):
        value = getattr(view, attr, None) if not isinstance(view, dict) else view.get(attr)
        if value:
            path = Path(value)
            if path.is_dir():
                return path
    profile = getattr(view, "profile", None) if not isinstance(view, dict) else None
    if profile is not None:
        for attr in ("source_dir", "data_dir", "shop_dir"):
            value = getattr(profile, attr, None)
            if value and Path(value).is_dir():
                return Path(value)
    return None


def _get_view_attr(view: Any, name: str) -> Any:
    """Đọc một thuộc tính của view (object hoặc dict), thiếu thì ``None``.

    Dùng duck-typing giống gate.py: không import ShopView, chỉ đọc thuộc
    tính nếu có. Điều này cho phép save làm việc với ShopView thật lẫn
    object giả trong kiểm thử.
    """
    if view is None:
        return None
    if isinstance(view, dict):
        return view.get(name)
    return getattr(view, name, None)


def _write_pickle_atomic(path: Path, payload: dict) -> None:
    """Ghi dict payload bằng pickle theo cách nguyên tử (qua file tạm).

    Ghi vào ``<tên chính>.tmp`` rồi đổi tên sang tên chính, để nếu crash
    giữa chừng thì file chính vẫn là bản cũ nguyên vẹn (xem
    :const:`_TMP_SUFFIX`). Thư mục cha được tạo nếu chưa có.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + _TMP_SUFFIX)
    with open(tmp_path, "wb") as handle:
        pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
    tmp_path.replace(path)


def _try_import_shop_view_class() -> Any | None:
    """Tìm class ShopView từ file shop_view.py cùng thư mục, nếu có.

    Thử theo thứ tự: import ``shop_view`` nếu thư mục src đã nằm trong
    ``sys.path`` (trường hợp integration chạy từ thư mục src), rồi nạp
    trực tiếp từ file ``shop_view.py`` nằm cùng thư mục với file cache.py
    này. Nếu cả hai đều thất bại (module shop_view chưa được viết), trả
    ``None`` và bên nạp sẽ dùng SimpleNamespace thay thế. Hàm không bao
    giờ raise lỗi chỉ vì module chưa tồn tại, vì cache hỏng/thiếu class
    chỉ có nghĩa là "trả view dạng duck-typing", không phải lỗi đợt chạy.
    """
    try:
        from shop_view import ShopView  # type: ignore[import-not-found]

        return ShopView
    except Exception:
        pass
    try:
        import importlib.util
        import sys

        candidate = Path(__file__).resolve().parent / "shop_view.py"
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("_datamate_shop_view", candidate)
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                sys.modules["_datamate_shop_view"] = module
                spec.loader.exec_module(module)
                return getattr(module, "ShopView", None)
    except Exception:
        return None
    return None


def _build_view(payload: dict, business_id: str) -> Any:
    """Dựng lại view từ payload đã nạp, ưu tiên đúng class ShopView.

    Nếu tìm được class ShopView thì dựng bằng các trường chuẩn của nó;
    nếu dựng thất bại (ví dụ class tương lai thêm trường bắt buộc mà
    payload cũ không có) thì rơi về SimpleNamespace với cùng các thuộc
    tính, để code dùng view theo thuộc tính vẫn chạy. Các thuộc tính phụ
    trong ``extras`` cũng được gắn lại nếu object cho phép gán.
    """
    standard = {field: payload.get(field) for field in _VIEW_FIELDS}
    standard["business_id"] = business_id
    shop_view_class = _try_import_shop_view_class()
    view: Any = None
    if shop_view_class is not None:
        try:
            view = shop_view_class(**standard)
        except Exception:
            view = None
    if view is None:
        view = SimpleNamespace(**standard)
    extras = payload.get("extras")
    if isinstance(extras, dict):
        for key, value in extras.items():
            try:
                setattr(view, key, value)
            except (AttributeError, TypeError):
                pass
    return view


def save_shop_cache(
    cache_dir: Any,
    business_id: str,
    view: Any,
    data_dir: Any = None,
) -> None:
    """Lưu view của một shop vào cache, kèm hash của dữ liệu nguồn.

    Đầu vào:

    - ``cache_dir``: thư mục cache do caller truyền; hàm tự tạo thư mục
      nếu chưa có. Cache chỉ ghi trong thư mục này, không ghi vào train.
    - ``business_id``: mã shop, dùng làm tên file cache (đã sanitize).
    - ``view``: view của shop (duck-typing: đọc các trường chuẩn
      ``profile``, ``lines``, ``orders``, ``date_min``, ``date_max``,
      ``products``, ``customers``; các thuộc tính khác được giữ trong
      ``extras`` nếu pickle được).
    - ``data_dir``: tham số TÙY CHỌN (mặc định ``None``) chỉ thư mục dữ
      liệu để tính hash nguồn. Contract mục 5 ghi chữ ký 3 tham số, nhưng
      chữ ký đó không cho save biết thư mục nguồn nằm ở đâu, nên không
      thể tính hash nguồn từ 3 tham số đó một mình. Vì vậy integration
      MUỐN cache có hiệu lực ở lần load sau PHẢI truyền ``data_dir`` vào
      đây (dạng thư mục split, thư mục ``data`` hoặc thư mục shop đều
      được, xem :func:`_resolve_shop_dir`), hoặc gắn thư mục nguồn vào
      view qua thuộc tính ``source_dir``. Nếu không có nguồn nào để tính
      hash, hàm vẫn ghi cache nhưng ghi ``source_hash = None`` và
      :func:`load_shop_cache` sẽ coi cache đó là chưa xác minh được và
      trả ``None`` (an toàn: thà ingest lại còn hơn trả view cũ khi
      nguồn đã đổi).

    Cách lưu (xem docstring module):

    - Thử import ``pyarrow``: nếu có, lưu lines/orders dạng parquet vào
      hai file đi kèm và lưu phần còn lại bằng pickle metadata. Nếu bất
      kỳ bước parquet nào lỗi, toàn bộ lần lưu rơi về pickle.
    - Nếu không có ``pyarrow``, lưu toàn bộ view bằng pickle trong một
      file duy nhất.
    - Ghi file chính theo cách nguyên tử (qua file tạm rồi rename), để
      crash giữa chừng không tạo ra cache nửa vời.

    Đầu ra: không trả gì. Lỗi ghi cache (ví dụ cache_dir không ghi được)
    được để lan ra cho caller biết, vì cache ghi thất bại là vấn đề về
    môi trường mà caller cần thấy, khác với cache nạp thất bại (chỉ cần
    ingest lại). Ngoại lệ duy nhất được nuốt là lỗi pickle của thuộc
    tính phụ trong ``extras``: thuộc tính phụ không pickle được thì bị
    bỏ qua, không làm hỏng toàn bộ lần lưu.

    Trường hợp biên: ``view`` là ``None`` thì hàm không ghi gì (trả ngay),
    vì không có view để cache; ``lines``/``orders`` không phải DataFrame
    (ví dụ list trong kiểm thử) thì chế độ parquet tự động không áp
    dụng được và hàm rơi về pickle cho các bảng đó.
    """
    if view is None:
        return
    main_path = cache_path(cache_dir, business_id)
    main_path.parent.mkdir(parents=True, exist_ok=True)

    # Tính hash nguồn: ưu tiên data_dir caller truyền, sau đó thư mục
    # nguồn gắn trong view. Không có nguồn nào thì source_hash = None
    # (cache chưa xác minh được, load sẽ trả None — xem docstring).
    shop_dir: Path | None = None
    if data_dir is not None:
        shop_dir = _resolve_shop_dir(data_dir, business_id)
    if shop_dir is None:
        shop_dir = _source_dir_from_view(view)
    source_hash = _source_hash(shop_dir) if shop_dir is not None else None

    lines = _get_view_attr(view, "lines")
    orders = _get_view_attr(view, "orders")
    extras: dict = {}
    if not isinstance(view, dict):
        try:
            attrs = vars(view)
        except TypeError:
            attrs = {}
        for key, value in attrs.items():
            if key not in _VIEW_FIELDS and not key.startswith("_"):
                try:
                    pickle.dumps(value)
                except Exception:
                    # Thuộc tính phụ không pickle được thì bỏ qua, để
                    # lần lưu chính không thất bại vì một trường phụ.
                    continue
                extras[key] = value
    elif isinstance(view, dict):
        for key, value in view.items():
            if key not in _VIEW_FIELDS:
                try:
                    pickle.dumps(value)
                except Exception:
                    continue
                extras[key] = value

    base_payload: dict = {
        "business_id": str(business_id),
        "profile": _get_view_attr(view, "profile"),
        "date_min": _get_view_attr(view, "date_min"),
        "date_max": _get_view_attr(view, "date_max"),
        "products": _get_view_attr(view, "products"),
        "customers": _get_view_attr(view, "customers"),
        "source_hash": source_hash,
        "extras": extras,
    }

    # Thử chế độ parquet: chỉ khi có pyarrow VÀ lines/orders là đối tượng
    # có phương thức to_parquet (tức DataFrame pandas thật). Mọi lỗi ở
    # chế độ này đều rơi về pickle toàn bộ (xem docstring module).
    parquet_ok = False
    try:
        import pyarrow  # noqa: F401  (chỉ kiểm tra sự tồn tại của thư viện)

        has_to_parquet = all(
            hasattr(table, "to_parquet") for table in (lines, orders)
        )
        if has_to_parquet:
            lines_name = f"{_sanitize_business_id(business_id)}{_LINES_PARQUET_SUFFIX}"
            orders_name = f"{_sanitize_business_id(business_id)}{_ORDERS_PARQUET_SUFFIX}"
            lines_path = main_path.parent / lines_name
            orders_path = main_path.parent / orders_name
            lines_tmp = lines_path.with_name(lines_path.name + _TMP_SUFFIX)
            orders_tmp = orders_path.with_name(orders_path.name + _TMP_SUFFIX)
            lines.to_parquet(lines_tmp)
            try:
                orders.to_parquet(orders_tmp)
            except Exception:
                # Orders ghi lỗi sau khi lines đã ghi tạm: xoá file tạm
                # của lines để không để lại cặp parquet lệch nhau.
                try:
                    lines_tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            lines_tmp.replace(lines_path)
            orders_tmp.replace(orders_path)
            payload = dict(base_payload)
            payload["storage"] = "parquet"
            payload["lines_file"] = lines_name
            payload["orders_file"] = orders_name
            _write_pickle_atomic(main_path, payload)
            parquet_ok = True
    except Exception:
        # Không có pyarrow, hoặc bảng không phải DataFrame, hoặc ghi
        # parquet lỗi: rơi về pickle toàn bộ, không để lại file parquet
        # dở dang nào được metadata chính trỏ tới (vì metadata chính
        # chưa được ghi ở nhánh này).
        parquet_ok = False
    if not parquet_ok:
        payload = dict(base_payload)
        payload["storage"] = "pickle"
        payload["lines"] = lines
        payload["orders"] = orders
        _write_pickle_atomic(main_path, payload)


def load_shop_cache(cache_dir: Any, business_id: str, data_dir: Any) -> Any | None:
    """Nạp view đã cache của một shop, chỉ khi dữ liệu nguồn không đổi.

    Đầu vào:

    - ``cache_dir``: thư mục cache do caller truyền (nơi
      :func:`save_shop_cache` đã ghi).
    - ``business_id``: mã shop cần nạp.
    - ``data_dir``: thư mục dữ liệu để tính lại hash nguồn (dạng thư mục
      split, thư mục ``data`` hoặc thư mục shop đều được, xem
      :func:`_resolve_shop_dir`).

    Đầu ra: view của shop (đúng class ``ShopView`` nếu module đó tìm
    thấy, nếu không là object có cùng các thuộc tính) khi TẤT CẢ điều
    kiện sau thỏa; ngược lại trả ``None`` và caller PHẢI ingest lại rồi
    ghi đè cache:

    - File cache chính tồn tại và nạp được bằng pickle.
    - ``business_id`` lưu trong cache khớp với ``business_id`` yêu cầu.
    - Cache có ``source_hash`` (không phải ``None``) và hash tính lại từ
      thư mục nguồn hiện tại bằng đúng hash đó. Hash khác, thư mục
      nguồn không tìm thấy, hoặc cache lưu mà không có hash (do lúc save
      không truyền ``data_dir``) đều cho ra ``None``.
    - Ở chế độ parquet, cả hai file parquet đi kèm đều tồn tại và nạp
      được bằng pandas; thiếu một file cũng là cache hỏng, trả ``None``.

    Vì sao không bao giờ trả view cũ khi nguồn đã đổi: trả view cũ sẽ
    làm mọi đáp án của shop đó tính trên dữ liệu cũ, sai hàng loạt mà
    không có cảnh báo nào; trong khi trả ``None`` chỉ tốn thêm một lần
    ingest. Đây là lựa chọn an toàn theo nguyên tắc "không chắc thì tính
    lại" của toàn bộ Giai đoạn 2.

    Trường hợp biên: mọi lỗi nạp (file hỏng, pickle không tương thích
    với class phase1 hiện tại, parquet thiếu cột, pandas/pyarrow không
    có) đều được bắt và trả ``None``, vì cache hỏng chỉ có nghĩa là
    ingest lại, không phải lỗi đợt chạy. Hàm không xoá file cache hỏng:
    lần save sau sẽ ghi đè nó; việc xoá ở đây có thể làm mất cache của
    một lần chạy khác đang dùng chung thư mục.
    """
    main_path = cache_path(cache_dir, business_id)
    if not main_path.is_file():
        return None
    try:
        with open(main_path, "rb") as handle:
            payload = pickle.load(handle)
    except Exception:
        # File cache hỏng hoặc pickle không tương thích: ingest lại.
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("business_id")) != str(business_id):
        return None
    stored_hash = payload.get("source_hash")
    if stored_hash is None:
        # Cache lưu mà không xác minh được nguồn (lúc save không có
        # data_dir): không thể chứng minh nguồn chưa đổi nên trả None.
        return None
    shop_dir = _resolve_shop_dir(data_dir, business_id)
    if shop_dir is None:
        return None
    current_hash = _source_hash(shop_dir)
    if current_hash is None or current_hash != stored_hash:
        # Nguồn đã đổi (thêm/bớt/đổi tên file, đổi kích thước, đổi thời
        # gian sửa đổi) hoặc không tính được hash hiện tại: cache vô hiệu.
        return None

    storage = payload.get("storage", "pickle")
    if storage == "parquet":
        try:
            import pandas as _pandas

            lines_file = payload.get("lines_file")
            orders_file = payload.get("orders_file")
            if not isinstance(lines_file, str) or not isinstance(orders_file, str):
                return None
            lines_path = main_path.parent / lines_file
            orders_path = main_path.parent / orders_file
            if not lines_path.is_file() or not orders_path.is_file():
                return None
            payload = dict(payload)
            payload["lines"] = _pandas.read_parquet(lines_path)
            payload["orders"] = _pandas.read_parquet(orders_path)
        except Exception:
            # Parquet hỏng hoặc pandas/pyarrow không có ở lần nạp này:
            # cache không dùng được, ingest lại.
            return None
    elif storage == "pickle":
        if "lines" not in payload or "orders" not in payload:
            return None
    else:
        # Giá trị storage lạ (cache từ phiên bản khác): không đoán cách nạp.
        return None
    return _build_view(payload, str(business_id))

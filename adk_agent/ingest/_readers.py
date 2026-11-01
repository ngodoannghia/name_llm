# -*- coding: utf-8 -*-
"""Đọc dữ liệu nguồn của shop thành bảng pandas + metadata quét.

Module này thuộc tầng đọc dữ liệu, dùng chung cho node quét và node làm
sạch (cả hai đều cần đọc lại file nguồn, vì DataFrame không được truyền
qua state của workflow). Chỉ module này được chạm trực tiếp vào file
nguồn; các node khác nhận bảng đã đọc qua hàm ở đây.

Giới hạn của khung lần này (ghi rõ để lần sau nâng cấp, không giấu):
- Shop có cả file SQLite lẫn file CSV/XLSX cùng lúc chưa được hỗ trợ
  đầy đủ: nhánh quét sẽ chọn SQLite khi có file SQLite, và bỏ qua file
  CSV/XLSX trong shop đó. Trong train, mỗi shop chỉ dùng một loại nguồn
  nên giới hạn này chưa ảnh hưởng pilot, nhưng phải sửa trước khi chạy
  full 30 shop nếu gặp shop hỗn hợp.
- Đọc toàn bộ bảng vào bộ nhớ khi quét (để đếm dòng và lấy mẫu ngẫu
  nhiên): chấp nhận được với shop train (lớn nhất ~65 nghìn dòng/bảng),
  chưa tối ưu cho bảng hàng triệu dòng.
"""

from __future__ import annotations

import asyncio
import csv
import random
import re
import sqlite3
from pathlib import Path

import pandas as pd

from ._config import SAMPLE_CELL_MAX_CHARS, SAMPLE_SEED
from ._models import RawTable
from ._phase1 import detect_header_row, header_score, normalize_key

# Ngưỡng "sát điểm" cho cờ ambiguous: một ô chữ tương đương 10 điểm
# trong _header_score, nên hai dòng đầu bảng lệch nhau không quá một ô
# chữ thì lựa chọn của quét là không vững — phải để HeaderAgent xem lại
# thay vì tin hẳn dòng điểm cao nhất (hòa điểm thì quét lấy dòng sớm
# nhất, mà dòng sớm nhất có thể là dòng nhãn nhóm 2 tầng). Chỉ xét các
# dòng có từ 2 ô không trống trở lên: dòng một ô luôn bị trừ nặng về
# -49 theo luật tiêu đề của _header_score nên mọi dòng một ô đều hòa
# điểm với nhau — đó là di chứng của luật trừ, không phải tín hiệu mơ
# hồ thật (bảng một cột như danh sách khách hàng sẽ bị gắn cờ oan).
HEADER_CLOSE_GAP = 10.0

# Các đuôi file được coi là dữ liệu (README và file lạ bị bỏ qua).
DATA_SUFFIXES = {".csv", ".xlsx", ".xls", ".sqlite", ".db"}
# Tên file README được nhận diện theo tên, không theo đuôi, vì README
# có thể là .md hoặc .txt.
README_NAMES = {"readme.md", "readme.txt", "readme"}


def list_data_files(shop_dir: str) -> list[Path]:
  """Liệt kê file dữ liệu trong thư mục shop, sắp xếp theo tên.

  Args:
    shop_dir: Đường dẫn thư mục shop.

  Returns:
    Danh sách Path các file dữ liệu (CSV/XLSX/SQLite), bỏ qua README,
    file ẩn và file có đuôi lạ.
  """
  root = Path(shop_dir)
  if not root.is_dir():
    return []
  files = []
  for path in sorted(root.iterdir()):
    if not path.is_file():
      continue
    if path.name.startswith("."):
      continue
    if path.name.lower() in README_NAMES:
      continue
    if path.suffix.lower() in DATA_SUFFIXES:
      files.append(path)
  return files


def read_readme_text(shop_dir: str, max_chars: int) -> str:
  """Đọc README của shop và cắt ngắn theo giới hạn ký tự.

  Args:
    shop_dir: Đường dẫn thư mục shop.
    max_chars: Số ký tự tối đa giữ lại (0 nghĩa là không lấy README).

  Returns:
    Text README đã cắt (rỗng nếu không có README hoặc max_chars <= 0).
  """
  if max_chars <= 0:
    return ""
  root = Path(shop_dir)
  for name in ("README.md", "README.txt", "readme.md", "readme.txt"):
    candidate = root / name
    if candidate.is_file():
      text = candidate.read_text(encoding="utf-8", errors="replace")
      return text[:max_chars]
  return ""


def sanitize_sqlite_name(name: str, fallback: str) -> str:
  """Sanitize tên bảng/cột thành identifier SQL hợp lệ.

  Args:
    name: Tên gốc.
    fallback: Tên dùng khi tên gốc sau sanitize thành rỗng.

  Returns:
    Tên chỉ gồm chữ thường, số và gạch dưới; bắt đầu bằng số thì thêm
    tiền tố ``t_`` (SQLite không cho identifier bắt đầu bằng số khi
    không quote).
  """
  cleaned = normalize_key(name or "")
  cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in cleaned)
  cleaned = cleaned.strip("_")
  if not cleaned:
    return fallback
  if cleaned[0].isdigit():
    cleaned = "t_" + cleaned
  return cleaned


def column_signature(columns: list[str]) -> str:
  """Tạo chữ ký cột từ danh sách tên cột.

  Chữ ký là tên cột đã chuẩn hóa (bỏ dấu, viết thường) nối bằng dấu ``|``.
  Hai bảng có cùng chữ ký thì có cùng bộ cột theo ý nghĩa tên gọi — đây
  là một trong các bằng chứng bắt buộc khi kiểm tra gộp (validator còn
  kiểm tra thêm, chữ ký chỉ là điều kiện cần, không phải đủ).

  Args:
    columns: Danh sách tên cột.

  Returns:
    Chuỗi chữ ký.
  """
  return "|".join(normalize_key(col) for col in columns)


def _clean_header_cell(value: object, index: int) -> str:
  """Làm sạch một ô header thành tên cột dùng được.

  Args:
    value: Giá trị ô header thô (có thể None hoặc rỗng).
    index: Vị trí cột (0-based), dùng để đặt tên dự phòng.

  Returns:
    Tên cột đã strip; ô trống thì thành ``cot_<số thứ tự 1-based>`` để
    không có cột nào không tên (cột không tên sẽ làm hỏng ghi SQLite).
  """
  text = "" if value is None else str(value).strip()
  if not text:
    return f"cot_{index + 1}"
  return text


def _dedupe_columns(columns: list[str]) -> list[str]:
  """Khử trùng tên cột bằng cách thêm hậu tố số cho lần xuất hiện sau.

  Args:
    columns: Danh sách tên cột có thể trùng nhau.

  Returns:
    Danh sách tên cột duy nhất, giữ nguyên thứ tự và tên lần đầu.
  """
  seen: dict[str, int] = {}
  result: list[str] = []
  for col in columns:
    if col not in seen:
      seen[col] = 1
      result.append(col)
    else:
      seen[col] += 1
      result.append(f"{col}_{seen[col]}")
  return result


def _extract_sheet_year(source_sheet: str, source_file: str) -> int | None:
  """Trích năm của bảng từ tên sheet rồi tên file.

  Rulebook §8 quy định ngày không ghi năm lấy năm của sheet chứa nó, nên
  năm phải được mang theo từ lúc đọc (về sau chỉ còn DataFrame thì
  không biết dòng nào thuộc sheet nào, nhất là sau khi gộp nhiều sheet
  tháng thành một bảng). Quy tắc chọn khi tên chứa nhiều năm: lấy năm
  4 chữ số đầu tiên (dạng 19xx hoặc 20xx) trong tên sheet; nếu tên sheet
  không có năm thì lấy năm đầu tiên trong tên file (không tính đuôi);
  không có năm ở đâu thì trả None (ngày không năm khi đó vẫn thành NULL,
  không đoán năm).

  Args:
    source_sheet: Tên sheet nguồn (rỗng với CSV/SQLite).
    source_file: Tên file nguồn (kèm đuôi).

  Returns:
    Năm dạng int, hoặc None khi không trích được năm nào.
  """
  year_pattern = re.compile(r"(?:19|20)\d{2}")
  for name in (source_sheet, Path(source_file).stem):
    match = year_pattern.search(name or "")
    if match:
      return int(match.group(0))
  return None


def _sample_rows(
    rows: list[list[str]], first: int, random_count: int, seed: int
) -> list[list[str]]:
  """Lấy mẫu dòng: N dòng đầu + M dòng ngẫu nhiên seed cố định.

  Vì sao lấy thêm dòng ngẫu nhiên: cột thưa chỉ xuất hiện ở dòng sau sẽ
  bị mẫu 10 dòng đầu bỏ sót, khiến model hiểu sai dạng cột đó. Seed cố
  định để chạy lại cho cùng mẫu (tính tái lập được).

  Args:
    rows: Toàn bộ dòng dữ liệu (đã là chuỗi).
    first: Số dòng đầu lấy.
    random_count: Số dòng ngẫu nhiên lấy thêm.
    seed: Seed cho bộ sinh số ngẫu nhiên.

  Returns:
    Danh sách dòng mẫu, mỗi ô đã cắt ngắn.
  """
  head = rows[:first]
  rest = rows[first:]
  extra: list[list[str]] = []
  if rest and random_count > 0:
    rng = random.Random(seed)
    picks = rng.sample(rest, min(random_count, len(rest)))
    extra = picks
  sampled = head + extra
  trimmed: list[list[str]] = []
  for row in sampled:
    trimmed.append([cell[:SAMPLE_CELL_MAX_CHARS] for cell in row])
  return trimmed


def _rows_to_str(rows: list[list[object]]) -> list[list[str]]:
  """Chuyển dòng thô thành dòng chuỗi (None thành chuỗi rỗng).

  Args:
    rows: Dòng thô với giá trị hỗn hợp.

  Returns:
    Dòng chuỗi, dùng cho quét và lấy mẫu.
  """
  result: list[list[str]] = []
  for row in rows:
    converted: list[str] = []
    for cell in row:
      if cell is None:
        converted.append("")
      elif isinstance(cell, float) and cell != cell:
        # NaN của pandas: coi như ô trống, không để chuỗi "nan" lọt vào mẫu.
        converted.append("")
      else:
        converted.append(str(cell))
    result.append(converted)
  return result


def _make_table_id(source_file: str, source_sheet: str, taken: set[str]) -> str:
  """Tạo id bảng duy nhất trong shop từ tên file/sheet.

  Args:
    source_file: Tên file nguồn.
    source_sheet: Tên sheet nguồn (rỗng với CSV/SQLite).
    taken: Tập id đã dùng trong shop (để tránh trùng).

  Returns:
    Id bảng đã chuẩn hóa và duy nhất.
  """
  stem = Path(source_file).stem
  base = stem if not source_sheet else f"{stem}_{source_sheet}"
  table_id = sanitize_sqlite_name(base, "bang")
  candidate = table_id
  suffix = 2
  while candidate in taken:
    candidate = f"{table_id}_{suffix}"
    suffix += 1
  taken.add(candidate)
  return candidate


def _table_from_rows(
    *,
    source_file: str,
    source_sheet: str,
    header_row: int,
    rows: list[list[object]],
    taken: set[str],
    first: int,
    random_count: int,
    seed: int,
    table_id: str | None = None,
) -> tuple[RawTable, pd.DataFrame]:
  """Tách một bảng từ các dòng thô: detect header, tạo DataFrame, lấy mẫu.

  Args:
    source_file: Tên file nguồn.
    source_sheet: Tên sheet nguồn.
    header_row: Chỉ số dòng header; -1 nghĩa là rows đã có header sẵn
      ở dòng đầu (trường hợp SQLite: dòng đầu chính là tên cột).
    rows: Toàn bộ dòng thô của bảng.
    taken: Tập id bảng đã dùng (để tạo id duy nhất).
    first: Số dòng mẫu đầu lấy.
    random_count: Số dòng mẫu ngẫu nhiên lấy thêm.
    seed: Seed cho mẫu ngẫu nhiên.
    table_id: Id bảng đã tính trước (khi tầng gọi đã cần id trước lúc
      detect để tra header_row chốt); None thì tự tạo id ở đây.

  Returns:
    Tuple (RawTable metadata, DataFrame dữ liệu với header đã chốt).
  """
  str_rows = _rows_to_str(rows)
  if header_row < 0:
    # Bảng SQLite: dòng đầu là tên cột, không cần detect header.
    header_cells = str_rows[0] if str_rows else []
    data_rows = str_rows[1:] if len(str_rows) > 1 else []
    detected = -1
  else:
    detected = header_row
    header_cells = str_rows[detected] if detected < len(str_rows) else []
    data_rows = str_rows[detected + 1 :] if detected + 1 <= len(str_rows) else []
  raw_header = [_clean_header_cell(cell, i) for i, cell in enumerate(header_cells)]
  header = _dedupe_columns(raw_header)
  # Bỏ dòng hoàn toàn trống: dòng trống không phải dữ liệu và sẽ làm
  # lệch đếm dòng cũng như dedup ở bước làm sạch.
  data_rows = [row for row in data_rows if any(cell.strip() for cell in row)]
  frame = pd.DataFrame(data_rows, columns=header) if header else pd.DataFrame()
  if not frame.empty and len(frame.columns) != len(header):
    # Phòng trường hợp dòng dữ liệu dài hơn header: pandas sẽ tự đặt tên
    # cột thừa; cắt về đúng số cột header để DataFrame khớp metadata.
    frame = frame.iloc[:, : len(header)]
    frame.columns = header
  if table_id is None:
    table_id = _make_table_id(source_file, source_sheet, taken)
  # Bảng mơ hồ khi header không ở dòng 0, hoặc header gốc có ô trống,
  # hoặc có tên cột trùng nhau, hoặc hai dòng đầu bảng sát điểm nhau:
  # cả bốn đều là dấu hiệu header chưa chắc và cần HeaderAgent xem lại
  # (bảng SQLite không qua detect nên không mơ hồ). Riêng trường hợp
  # sát điểm: detect lấy dòng sớm nhất khi hòa điểm, mà dòng sớm nhất
  # có thể là dòng nhãn nhóm 2 tầng — không có cách nào biết chắc nếu
  # không cho agent xem, nên cứ bật cờ khi khoảng cách top-1/top-2
  # không quá một ô chữ (xem HEADER_CLOSE_GAP).
  ambiguous = False
  if detected >= 0:
    original_cells = ["" if cell is None else str(cell).strip() for cell in (rows[detected] if detected < len(rows) else [])]
    scores = sorted(
        (
            header_score(row)
            for row in rows[:15]
            if sum(
                1
                for cell in row
                if cell is not None
                and (not isinstance(cell, str) or cell.strip())
            )
            >= 2
        ),
        reverse=True,
    )
    close_call = (
        len(scores) >= 2 and scores[0] - scores[1] <= HEADER_CLOSE_GAP
    )
    ambiguous = (
        detected != 0
        or any(not cell for cell in original_cells)
        or len(set(raw_header)) != len(raw_header)
        or close_call
    )
  metadata = RawTable(
      table_id=table_id,
      source_file=source_file,
      source_sheet=source_sheet,
      header=header,
      header_row=detected,
      sheet_year=_extract_sheet_year(source_sheet, source_file),
      row_count=len(frame),
      sample_rows=_sample_rows(data_rows, first, random_count, seed),
      ambiguous=ambiguous,
      column_signature=column_signature(header),
      candidate_rows=str_rows[:15],
  )
  return metadata, frame


def _read_csv_rows(path: Path) -> list[list[object]]:
  """Đọc file CSV thành các dòng thô, tự dò dấu tách và encoding.

  Args:
    path: Đường dẫn file CSV.

  Returns:
    Danh sách dòng thô (mỗi dòng là list các ô chuỗi).
  """
  raw = path.read_bytes()
  text = raw.decode("utf-8-sig", errors="replace")
  delimiter = ","
  try:
    # Sniffer cần đủ dữ liệu mẫu; file quá ngắn thì giữ dấu phẩy mặc định.
    sample = text[:8192]
    if sample.strip():
      delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
  except csv.Error:
    delimiter = ","
  rows: list[list[object]] = []
  for row in csv.reader(text.splitlines(), delimiter=delimiter):
    rows.append(list(row))
  return rows


def _read_xlsx_sheets(path: Path) -> dict[str, list[list[object]]]:
  """Đọc file XLSX thành dict tên sheet -> dòng thô.

  Args:
    path: Đường dẫn file XLSX.

  Returns:
    Dict tên sheet -> danh sách dòng thô (ô trống thành None).
  """
  frames = pd.read_excel(
      path, sheet_name=None, header=None, dtype=str, keep_default_na=False
  )
  result: dict[str, list[list[object]]] = {}
  for sheet_name, frame in frames.items():
    rows: list[list[object]] = []
    for row in frame.itertuples(index=False, name=None):
      rows.append([None if cell == "" else cell for cell in row])
    result[str(sheet_name)] = rows
  return result


def _read_sqlite_tables(path: Path) -> dict[str, list[list[object]]]:
  """Đọc file SQLite thành dict tên bảng -> dòng thô (dòng đầu là tên cột).

  Args:
    path: Đường dẫn file SQLite (mở chỉ đọc để không bao giờ sửa nguồn).

  Returns:
    Dict tên bảng -> dòng thô, trong đó dòng đầu tiên là tên các cột.
  """
  uri = f"file:{path}?mode=ro"
  connection = sqlite3.connect(uri, uri=True)
  try:
    cursor = connection.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    table_names = [row[0] for row in cursor.fetchall()]
    result: dict[str, list[list[object]]] = {}
    for table_name in table_names:
      cursor.execute(f'PRAGMA table_info("{table_name}")')
      columns = [row[1] for row in cursor.fetchall()]
      cursor.execute(f'SELECT * FROM "{table_name}"')
      data = cursor.fetchall()
      rows: list[list[object]] = [columns]
      for record in data:
        rows.append(list(record))
      result[table_name] = rows
    return result
  finally:
    connection.close()


async def read_source_tables(
    shop_dir: str,
    *,
    first: int,
    random_count: int,
    seed: int,
    only_sqlite: bool = False,
    only_sheets: bool = False,
    header_row_overrides: dict[str, int] | None = None,
) -> list[tuple[RawTable, pd.DataFrame]]:
  """Đọc toàn bộ bảng nguồn của shop (metadata + DataFrame).

  Hàm này là async và bọc phần đọc chặn (pandas/sqlite3) bằng
  ``asyncio.to_thread`` để không chặn event loop của ADK trong lúc đọc
  file lớn.

  Args:
    shop_dir: Thư mục shop.
    first: Số dòng mẫu đầu lấy cho mỗi bảng.
    random_count: Số dòng mẫu ngẫu nhiên lấy thêm.
    seed: Seed cho mẫu ngẫu nhiên.
    only_sqlite: True thì chỉ đọc file SQLite.
    only_sheets: True thì chỉ đọc file CSV/XLSX.
    header_row_overrides: Dòng header chốt theo table_id (do loop header
      ghi vào state header_rows_final) — node quét gọi không kèm
      (detect lại từ đầu), node làm sạch gọi kèm để dùng đúng dòng mà
      agent đã chốt thay vì detect lại và vô hiệu hóa quyết định của
      agent. Override ngoài khoảng dòng hoặc bằng -1 ở nhánh sheet thì
      bị bỏ qua và detect lại như thường (phòng state cũ/lạ).

  Returns:
    Danh sách tuple (RawTable, DataFrame) theo thứ tự file/sheet đã sắp xếp.
  """
  return await asyncio.to_thread(
      _read_source_tables_sync,
      shop_dir,
      first,
      random_count,
      seed,
      only_sqlite,
      only_sheets,
      header_row_overrides,
  )


def _resolve_header_row(
    table_id: str,
    rows: list[list[object]],
    header_row_overrides: dict[str, int] | None,
) -> int:
  """Chọn dòng header cho một bảng sheet: override chốt trước, detect sau.

  Args:
    table_id: Id bảng (đã tính trước để tra override).
    rows: Toàn bộ dòng thô của bảng.
    header_row_overrides: Dòng header chốt theo table_id (có thể None).

  Returns:
    Chỉ số dòng header (0-based); override hợp lệ thì dùng nó, ngược
    lại detect bằng luật scoring của phase1.
  """
  if header_row_overrides is not None:
    override = header_row_overrides.get(table_id)
    if override is not None and 0 <= override < len(rows):
      return override
  return detect_header_row(rows)


def _read_source_tables_sync(
    shop_dir: str,
    first: int,
    random_count: int,
    seed: int,
    only_sqlite: bool,
    only_sheets: bool,
    header_row_overrides: dict[str, int] | None = None,
) -> list[tuple[RawTable, pd.DataFrame]]:
  """Phần đồng bộ của :func:`read_source_tables` (chạy trong thread riêng).

  Args:
    shop_dir: Thư mục shop.
    first: Số dòng mẫu đầu lấy.
    random_count: Số dòng mẫu ngẫu nhiên lấy thêm.
    seed: Seed cho mẫu ngẫu nhiên.
    only_sqlite: Chỉ đọc file SQLite.
    only_sheets: Chỉ đọc file CSV/XLSX.
    header_row_overrides: Dòng header chốt theo table_id (xem
      :func:`read_source_tables`); nhánh SQLite không dùng override vì
      tên cột lấy thẳng từ schema DB.

  Returns:
    Danh sách tuple (RawTable, DataFrame).
  """
  taken: set[str] = set()
  results: list[tuple[RawTable, pd.DataFrame]] = []
  for path in list_data_files(shop_dir):
    suffix = path.suffix.lower()
    if suffix == ".csv" and not only_sqlite:
      rows = _read_csv_rows(path)
      # Id phải tính trước để tra override: _make_table_id đã ghi id
      # vào taken nên _table_from_rows nhận sẵn id, không tính lại.
      table_id = _make_table_id(path.name, "", taken)
      detected = _resolve_header_row(table_id, rows, header_row_overrides)
      results.append(
          _table_from_rows(
              source_file=path.name,
              source_sheet="",
              header_row=detected,
              rows=rows,
              taken=taken,
              first=first,
              random_count=random_count,
              seed=seed,
              table_id=table_id,
          )
      )
    elif suffix in (".xlsx", ".xls") and not only_sqlite:
      sheets = _read_xlsx_sheets(path)
      for sheet_name in sorted(sheets):
        rows = sheets[sheet_name]
        table_id = _make_table_id(path.name, sheet_name, taken)
        detected = _resolve_header_row(table_id, rows, header_row_overrides)
        results.append(
            _table_from_rows(
                source_file=path.name,
                source_sheet=sheet_name,
                header_row=detected,
                rows=rows,
                taken=taken,
                first=first,
                random_count=random_count,
                seed=seed,
                table_id=table_id,
            )
        )
    elif suffix in (".sqlite", ".db") and not only_sheets:
      tables = _read_sqlite_tables(path)
      for table_name in sorted(tables):
        results.append(
            _table_from_rows(
                source_file=path.name,
                source_sheet=table_name,
                header_row=-1,
                rows=tables[table_name],
                taken=taken,
                first=first,
                random_count=random_count,
                seed=seed,
            )
        )
  return results

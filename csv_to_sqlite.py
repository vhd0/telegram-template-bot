import csv
import sqlite3
import os
import sys

CSV_FILE = "rep.csv"
DB_FILE = "rep.db"
TABLE_NAME = "rep"

def sanitize_headers(headers):
    seen = {}
    new_headers = []
    indices = []
    log_lines = []
    for idx, h in enumerate(headers):
        orig = h
        h = h.strip()
        # Nếu cột không có tên thì đặt tên tự động là _colN với N là vị trí
        if not h:
            h = f"_col{idx+1}"
            log_lines.append(f"Header column {idx+1} is empty, set as '{h}'")
        orig_h = h
        i = 1
        while h in seen:
            h = f"{orig_h}_{i}"
            i += 1
        if h != orig_h:
            log_lines.append(f"Duplicate header '{orig_h}' found, renamed to '{h}'")
        seen[h] = True
        new_headers.append(h)
        indices.append(idx)
    return new_headers, indices, log_lines

def csv_to_sqlite(csv_file, db_file, table_name):
    print(f"::group::CSV to SQLite conversion")
    print(f"Input CSV: {csv_file}")
    print(f"Output DB: {db_file}")
    print(f"Table name: {table_name}")
    with open(csv_file, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        raw_headers = next(reader)
        headers, indices, header_logs = sanitize_headers(raw_headers)
        for line in header_logs:
            print(f"[header] {line}")
        rows = []
        for row_idx, row in enumerate(reader, 2):
            # Lấy chỉ các cột hợp lệ
            clean_row = [row[i] if i < len(row) else "" for i in indices]
            if row_idx <= 10:  # show preview of first 10 rows
                print(f"[row {row_idx}]: {clean_row}")
            rows.append(clean_row)
        print(f"Total rows loaded: {len(rows)}")
    if not headers:
        print("::error::No valid headers found in CSV. Please check your file.")
        raise Exception("No valid headers found in CSV. Please check your file.")
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {table_name}")
    col_defs = ', '.join(f'"{h}" TEXT' for h in headers)
    print(f"CREATE TABLE SQL: CREATE TABLE {table_name} ({col_defs})")
    cur.execute(f'CREATE TABLE {table_name} ({col_defs})')
    placeholders = ', '.join('?' for _ in headers)
    cur.executemany(
        f'INSERT INTO {table_name} VALUES ({placeholders})',
        rows
    )
    conn.commit()
    conn.close()
    print(f"::notice::Đã nạp dữ liệu từ {csv_file} vào {db_file}:{table_name} ({len(rows)} rows, {len(headers)} columns)")
    print("::endgroup::")

if __name__ == "__main__":
    if not os.path.exists(CSV_FILE):
        print(f"::error::Không tìm thấy file {CSV_FILE}")
        sys.exit(1)
    try:
        csv_to_sqlite(CSV_FILE, DB_FILE, TABLE_NAME)
    except Exception as e:
        print(f"::error::Chuyển đổi thất bại: {e}")
        sys.exit(1)

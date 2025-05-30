import csv
import sqlite3
import os

CSV_FILE = "rep.csv"
DB_FILE = "rep.db"
TABLE_NAME = "rep"

def sanitize_headers(headers):
    seen = {}
    new_headers = []
    indices = []
    for idx, h in enumerate(headers):
        h = h.strip()
        if not h:
            continue  # Bỏ qua cột không tên
        orig_h = h
        i = 1
        while h in seen:
            h = f"{orig_h}_{i}"
            i += 1
        seen[h] = True
        new_headers.append(h)
        indices.append(idx)
    return new_headers, indices

def csv_to_sqlite(csv_file, db_file, table_name):
    with open(csv_file, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        raw_headers = next(reader)
        headers, indices = sanitize_headers(raw_headers)
        rows = []
        for row in reader:
            # Lấy chỉ các cột hợp lệ
            clean_row = [row[i] if i < len(row) else "" for i in indices]
            rows.append(clean_row)
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {table_name}")
    col_defs = ', '.join(f'"{h}" TEXT' for h in headers)
    cur.execute(f'CREATE TABLE {table_name} ({col_defs})')
    placeholders = ', '.join('?' for _ in headers)
    cur.executemany(
        f'INSERT INTO {table_name} VALUES ({placeholders})',
        rows
    )
    conn.commit()
    conn.close()
    print(f"Đã nạp dữ liệu từ {csv_file} vào {db_file}:{table_name}")

if __name__ == "__main__":
    if not os.path.exists(CSV_FILE):
        print(f"Không tìm thấy file {CSV_FILE}")
    else:
        csv_to_sqlite(CSV_FILE, DB_FILE, TABLE_NAME)

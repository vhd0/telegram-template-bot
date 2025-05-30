import csv
import sqlite3
import os

CSV_FILE = "rep.csv"         # Đường dẫn file CSV đã sync từ GitHub
DB_FILE = "rep.db"           # Tên file SQLite DB sẽ tạo/cập nhật
TABLE_NAME = "rep"           # Tên bảng trong DB

def csv_to_sqlite(csv_file, db_file, table_name):
    # Đọc tiêu đề cột từ CSV
    with open(csv_file, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        headers = next(reader)
        rows = list(reader)
    
    # Tạo kết nối đến SQLite
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()

    # Tạo bảng mới (nếu tồn tại thì xóa đi tạo lại)
    cur.execute(f"DROP TABLE IF EXISTS {table_name}")
    col_defs = ', '.join(f'"{h}" TEXT' for h in headers)
    cur.execute(f'CREATE TABLE {table_name} ({col_defs})')

    # Insert dữ liệu
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

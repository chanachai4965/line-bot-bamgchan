"""
parse_data.py
แปลงข้อมูลจากไฟล์ Excel -> SQLite database
รัน: python parse_data.py <path_to_xlsx>
"""

import sys
import sqlite3
import re
import os
import pandas as pd
from pathlib import Path

XLSX_PATH = sys.argv[1] if len(sys.argv) > 1 else "ตารางผลการจับกุมประจำปีงบฯ 56-63.xlsx"
DB_PATH = "arrests.db"

SKIP_SHEETS = {'555', 'ตารางเปล่า', 'หมายจับ', 'สรุป ปี 2559'}

THAI_MONTH_NUM = {
    'ม.ค': 1, 'มกราคม': 1,
    'ก.พ': 2, 'กุมภาพันธ์': 2,
    'มี.ค': 3, 'มีนาคม': 3,
    'เม.ย': 4, 'เมษายน': 4,
    'พ.ค': 5, 'พฤษภาคม': 5,
    'มิ.ย': 6, 'มิถุนายน': 6,
    'ก.ค': 7, 'กรกฏาคม': 7, 'กรกฎาคม': 7,
    'ส.ค': 8, 'สิงหาคม': 8,
    'ก.ย': 9, 'กันยายน': 9,
    'ต.ค': 10, 'ตุลาคม': 10,
    'พ.ย': 11, 'พฤศจิกายน': 11,
    'ธ.ค': 12, 'ธันวาคม': 12,
}

THAI_MONTH_NAME = {
    1: 'มกราคม', 2: 'กุมภาพันธ์', 3: 'มีนาคม', 4: 'เมษายน',
    5: 'พฤษภาคม', 6: 'มิถุนายน', 7: 'กรกฎาคม', 8: 'สิงหาคม',
    9: 'กันยายน', 10: 'ตุลาคม', 11: 'พฤศจิกายน', 12: 'ธันวาคม',
}

def parse_year_from_sheet(sheet_name: str):
    parts = sheet_name.strip().split()
    for p in parts:
        p_clean = p.rstrip('.')
        if p_clean.isdigit() and len(p_clean) == 2:
            return 2500 + int(p_clean)
    return None

def parse_month_from_sheet(sheet_name: str):
    for abbr, num in THAI_MONTH_NUM.items():
        if abbr in sheet_name:
            return num
    return None

def clean(val) -> str:
    if val is None or (isinstance(val, float) and val != val):
        return ''
    s = str(val).strip()
    return '' if s == 'nan' else s

def create_db(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS arrests (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        sheet       TEXT,
        year_be     INTEGER,
        year_ce     INTEGER,
        month_num   INTEGER,
        month_name  TEXT,
        date_str    TEXT,
        charge      TEXT,
        name        TEXT,
        age         TEXT,
        pid         TEXT,
        evidence    TEXT,
        location    TEXT,
        team        TEXT
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_name     ON arrests(name)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_location ON arrests(location)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_year_be  ON arrests(year_be)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_month    ON arrests(month_num)')
    conn.commit()

def main():
    print(f"📂 อ่านไฟล์: {XLSX_PATH}")
    xl = pd.ExcelFile(XLSX_PATH)

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    create_db(conn)

    total = 0
    for sheet in xl.sheet_names:
        if sheet in SKIP_SHEETS:
            continue
        year_be = parse_year_from_sheet(sheet)
        if year_be is None:
            continue
        month_num = parse_month_from_sheet(sheet)
        year_ce = year_be - 543
        month_name = THAI_MONTH_NAME.get(month_num, '')

        try:
            df = pd.read_excel(xl, sheet_name=sheet, header=None)
        except Exception as e:
            print(f"  ⚠️  {sheet}: {e}")
            continue

        # Find header row
        header_row = None
        for i, row in df.iterrows():
            row_str = ' '.join([str(v) for v in row.values])
            if 'ลำดับ' in row_str and 'ชื่อ' in row_str:
                header_row = i
                break
        if header_row is None:
            continue

        ncols = df.shape[1]
        has_location = ncols >= 8

        rows = []
        for i in range(header_row + 1, len(df)):
            row = df.iloc[i]
            name = clean(row.iloc[3]) if len(row) > 3 else ''
            if not name:
                continue
            # skip header-like rows
            if 'ชื่อ' in name or 'ลำดับ' in name:
                continue

            date_str  = clean(row.iloc[1]) if len(row) > 1 else ''
            charge    = clean(row.iloc[2]) if len(row) > 2 else ''
            age       = clean(row.iloc[4]) if len(row) > 4 else ''
            pid       = clean(row.iloc[5]) if len(row) > 5 else ''
            evidence  = clean(row.iloc[6]) if len(row) > 6 else ''
            location  = clean(row.iloc[7]) if (has_location and len(row) > 7) else ''
            team      = clean(row.iloc[8]) if (has_location and len(row) > 8) else ''

            rows.append((sheet, year_be, year_ce, month_num, month_name,
                         date_str, charge, name, age, pid, evidence, location, team))

        conn.executemany('''INSERT INTO arrests
            (sheet, year_be, year_ce, month_num, month_name, date_str, charge,
             name, age, pid, evidence, location, team)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''', rows)
        conn.commit()
        total += len(rows)
        print(f"  ✅ {sheet}: {len(rows)} รายการ")

    print(f"\n🎉 นำเข้าข้อมูลสำเร็จ {total} รายการ -> {DB_PATH}")
    conn.close()

if __name__ == '__main__':
    main()

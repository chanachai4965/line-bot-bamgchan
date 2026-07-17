#!/usr/bin/env python3
"""
LINE Bot — ระบบสืบค้นผลการจับกุม สน.บางชัน
v3.0 — Google Sheets Edition (live data, Flex Messages with photo cards)
"""

import os
import re
import json
import time
import logging
import threading
from typing import Optional

from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient, Configuration, MessagingApi,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage, FlexMessage, FlexContainer,
)
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent,
    JoinEvent, MemberJoinedEvent,
    FollowEvent, LeaveEvent, UnfollowEvent,
)

import gspread
from google.oauth2.service_account import Credentials

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
log = logging.getLogger(__name__)

# ─── Flask + LINE SDK ─────────────────────────────────────────────────────────
app = Flask(__name__)

LINE_CHANNEL_SECRET       = os.environ['LINE_CHANNEL_SECRET']
LINE_CHANNEL_ACCESS_TOKEN = os.environ['LINE_CHANNEL_ACCESS_TOKEN']
SPREADSHEET_ID            = os.environ.get(
    'SPREADSHEET_ID', '1DKdVKQCBcEcm9dYbLFPHu_Fzxr1fbvyi_4q3DR8n-gg'
)
CACHE_TTL    = int(os.environ.get('CACHE_TTL', '300'))  # 5 minutes
DETAIL_LIMIT = 30

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ─── Google Sheets auth ───────────────────────────────────────────────────────
_gc = None
_gc_lock = threading.Lock()

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets.readonly',
    'https://www.googleapis.com/auth/drive.readonly',
]

def get_gc() -> gspread.Client:
    global _gc
    with _gc_lock:
        if _gc is not None:
            return _gc
        creds_json = os.environ.get('GOOGLE_CREDENTIALS_JSON')
        if creds_json:
            creds_dict = json.loads(creds_json)
        elif os.path.exists('credentials.json'):
            with open('credentials.json') as f:
                creds_dict = json.load(f)
        else:
            raise RuntimeError(
                'No Google credentials found. '
                'Set GOOGLE_CREDENTIALS_JSON env var or place credentials.json in the project folder.'
            )
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        _gc = gspread.authorize(creds)
        return _gc

# ─── Thai month / year helpers ────────────────────────────────────────────────
THAI_MONTHS = {
    'ม.ค.': 1, 'มกราคม': 1,
    'ก.พ.': 2, 'กุมภาพันธ์': 2,
    'มี.ค.': 3, 'มีนาคม': 3,
    'เม.ย.': 4, 'เมษายน': 4,
    'พ.ค.': 5, 'พฤษภาคม': 5,
    'มิ.ย.': 6, 'มิถุนายน': 6,
    'ก.ค.': 7, 'กรกฎาคม': 7,
    'ส.ค.': 8, 'สิงหาคม': 8,
    'ก.ย.': 9, 'กันยายน': 9,
    'ต.ค.': 10, 'ตุลาคม': 10,
    'พ.ย.': 11, 'พฤศจิกายน': 11,
    'ธ.ค.': 12, 'ธันวาคม': 12,
}

MONTH_NUM_TO_ABBR = {
    1: 'ม.ค.', 2: 'ก.พ.', 3: 'มี.ค.', 4: 'เม.ย.',
    5: 'พ.ค.', 6: 'มิ.ย.', 7: 'ก.ค.', 8: 'ส.ค.',
    9: 'ก.ย.', 10: 'ต.ค.', 11: 'พ.ย.', 12: 'ธ.ค.',
}

SHEET_MONTH_RE = re.compile(
    r'(ม\.ค\.|ก\.พ\.|มี\.ค\.|เม\.ย\.|พ\.ค\.|มิ\.ย\.|ก\.ค\.|'
    r'ส\.ค\.|ก\.ย\.|ต\.ค\.|พ\.ย\.|ธ\.ค\.)\.?\s*(\d{2})'
)

MONTH_INPUT_RE = re.compile(
    r'(มกราคม|กุมภาพันธ์|มีนาคม|เมษายน|พฤษภาคม|มิถุนายน|กรกฎาคม|'
    r'สิงหาคม|กันยายน|ตุลาคม|พฤศจิกายน|ธันวาคม|'
    r'ม\.ค\.|ก\.พ\.|มี\.ค\.|เม\.ย\.|พ\.ค\.|มิ\.ย\.|ก\.ค\.|'
    r'ส\.ค\.|ก\.ย\.|ต\.ค\.|พ\.ย\.|ธ\.ค\.)',
    re.UNICODE
)
YEAR_INPUT_RE = re.compile(r'\b(25[5-9]\d|26\d{2}|[5-9]\d)\b')

SKIP_SHEETS = {'555', 'ตารางเปล่า', 'สรุป', 'หมายจับ', 'Sheet1'}

DRIVE_ID_RE = re.compile(r'drive\.google\.com/file/d/([^/?&\s]+)')


def drive_url_to_image(url: str) -> Optional[str]:
    """Convert Google Drive share URL → direct image URL for LINE."""
    if not url:
        return None
    m = DRIVE_ID_RE.search(url)
    if m:
        fid = m.group(1)
        # lh3.googleusercontent.com works better than uc?export=view for LINE
        return f'https://lh3.googleusercontent.com/d/{fid}'
    return None


def parse_image_formula(val: str) -> Optional[str]:
    """Extract URL from =IMAGE("url") formula."""
    m = re.match(r'=IMAGE\(\s*"([^"]+)"', val or '', re.IGNORECASE)
    return m.group(1) if m else None


def parse_month_year(text: str):
    """Return (month_num, year_be) or (None, None)."""
    mm = MONTH_INPUT_RE.search(text)
    ym = YEAR_INPUT_RE.search(text)
    if not mm:
        return None, None
    raw = mm.group(1).replace('.', '')
    month_num = None
    for k, v in THAI_MONTHS.items():
        if raw in k.replace('.', '') or k.replace('.', '') in raw:
            month_num = v
            break
    if not month_num:
        return None, None
    year_be = None
    if ym:
        yr = int(ym.group(1))
        year_be = yr + 2500 if yr < 100 else yr
    return month_num, year_be


# ─── Column maps ──────────────────────────────────────────────────────────────
# NEW format (ก.ค.69+) — 14 columns
NEW_COL = {
    'seq': 0, 'date': 1, 'group': 2, 'charge': 3,
    'name': 4, 'nickname': 5, 'age': 6, 'pid': 7,
    'phone': 8, 'image': 9, 'evidence': 10,
    'location': 11, 'file': 12, 'note': 13,
}

# OLD format (2555–2568) — 7 columns (some sheets have location as col 7)
OLD_COL = {
    'seq': 0, 'date': 1, 'charge': 2, 'name': 3,
    'age': 4, 'pid': 5, 'evidence': 6, 'location': 7,
}


def _get(row: list, col_map: dict, key: str) -> str:
    idx = col_map.get(key, -1)
    if idx < 0 or idx >= len(row):
        return ''
    return str(row[idx]).strip()


# ─── Sheet parser ─────────────────────────────────────────────────────────────
def parse_sheet(ws: gspread.Worksheet, sheet_name: str) -> list:
    """Read one worksheet and return list of record dicts."""
    try:
        rows = ws.get_all_values()
    except Exception as e:
        log.warning(f"Cannot read sheet '{sheet_name}': {e}")
        return []

    if len(rows) < 2:
        return []

    # Detect month/year from sheet tab name
    sm = SHEET_MONTH_RE.search(sheet_name)
    month_num = 0
    year_be = 0
    month_abbr = ''
    if sm:
        abbr_raw = sm.group(1)
        yr_raw = int(sm.group(2))
        year_be = yr_raw + 2500
        # normalise abbreviation (strip dots for lookup)
        norm = abbr_raw.replace('.', '')
        for k, v in THAI_MONTHS.items():
            if norm in k.replace('.', '') or k.replace('.', '') in norm:
                month_num = v
                month_abbr = MONTH_NUM_TO_ABBR.get(v, abbr_raw)
                break

    # Find header row
    header_idx = None
    for i, row in enumerate(rows):
        joined = ' '.join(row)
        if 'ข้อหา' in joined and ('ชื่อ' in joined or 'ผู้ต้องหา' in joined):
            header_idx = i
            break
    if header_idx is None:
        return []

    header = rows[header_idx]
    is_new = any(k in ' '.join(header) for k in ('กลุ่มฐานความผิด', 'ไฟล์บันทึก', 'รูปภาพ'))

    # For new-format sheets, also fetch with FORMULA rendering to catch =IMAGE()
    formula_rows: list = []
    if is_new:
        try:
            formula_rows = ws.get_all_values(value_render_option='FORMULA')
        except Exception:
            formula_rows = rows  # fallback

    data_rows = rows[header_idx + 1:]
    records = []

    for rel_idx, row in enumerate(data_rows):
        # pad
        while len(row) < 15:
            row.append('')

        if is_new:
            # Try to get image URL: first from =IMAGE() formula in col 9
            img_url = None
            abs_idx = header_idx + 1 + rel_idx
            if formula_rows and abs_idx < len(formula_rows):
                frow = formula_rows[abs_idx]
                if len(frow) > NEW_COL['image']:
                    img_url = parse_image_formula(frow[NEW_COL['image']])
            # Fallback: use Drive link from col 12 (ไฟล์บันทึกจับกุม)
            if not img_url:
                file_url = _get(row, NEW_COL, 'file')
                img_url = drive_url_to_image(file_url)

            name = _get(row, NEW_COL, 'name')
            seq  = _get(row, NEW_COL, 'seq')

            rec = {
                'sheet':      sheet_name,
                'year_be':    year_be,
                'month_num':  month_num,
                'month_abbr': month_abbr,
                'seq':        seq,
                'date':       _get(row, NEW_COL, 'date'),
                'group':      _get(row, NEW_COL, 'group'),
                'charge':     _get(row, NEW_COL, 'charge'),
                'name':       name,
                'nickname':   _get(row, NEW_COL, 'nickname'),
                'age':        _get(row, NEW_COL, 'age'),
                'pid':        _get(row, NEW_COL, 'pid'),
                'evidence':   _get(row, NEW_COL, 'evidence'),
                'location':   _get(row, NEW_COL, 'location'),
                'note':       _get(row, NEW_COL, 'note'),
                'file_url':   _get(row, NEW_COL, 'file'),
                'image_url':  img_url,
                'new_format': True,
            }
        else:
            name = _get(row, OLD_COL, 'name')
            seq  = _get(row, OLD_COL, 'seq')

            rec = {
                'sheet':      sheet_name,
                'year_be':    year_be,
                'month_num':  month_num,
                'month_abbr': month_abbr,
                'seq':        seq,
                'date':       _get(row, OLD_COL, 'date'),
                'group':      '',
                'charge':     _get(row, OLD_COL, 'charge'),
                'name':       name,
                'nickname':   '',
                'age':        _get(row, OLD_COL, 'age'),
                'pid':        _get(row, OLD_COL, 'pid'),
                'evidence':   _get(row, OLD_COL, 'evidence'),
                'location':   _get(row, OLD_COL, 'location'),
                'note':       '',
                'file_url':   '',
                'image_url':  None,
                'new_format': False,
            }

        # Skip non-data rows
        if not rec['name'] or rec['name'] in ('ชื่อ / สกุล ผู้ต้องหา', 'ชื่อ-สกุล', 'ชื่อ', '-'):
            continue
        if rec['seq'] in ('ลำดับ', '', '-') and not rec['name']:
            continue

        records.append(rec)

    return records


# ─── Data cache ───────────────────────────────────────────────────────────────
_cache_data: list = []
_cache_ts: float  = 0.0
_cache_lock       = threading.Lock()


def fetch_all() -> list:
    """Fetch every worksheet from the spreadsheet and return combined records."""
    try:
        gc = get_gc()
        sh = gc.open_by_key(SPREADSHEET_ID)
        worksheets = sh.worksheets()
        all_records = []
        for ws in worksheets:
            name = ws.title.strip()
            if any(skip in name for skip in SKIP_SHEETS):
                continue
            recs = parse_sheet(ws, name)
            log.info(f"  Sheet '{name}': {len(recs)} records")
            all_records.extend(recs)
        log.info(f"Total records: {len(all_records)}")
        return all_records
    except Exception as e:
        log.error(f"fetch_all error: {e}", exc_info=True)
        with _cache_lock:
            return _cache_data  # return stale on error


def get_data(force: bool = False) -> list:
    global _cache_data, _cache_ts
    with _cache_lock:
        if not force and _cache_data and (time.time() - _cache_ts) < CACHE_TTL:
            return _cache_data
    fresh = fetch_all()
    with _cache_lock:
        if fresh:
            _cache_data = fresh
            _cache_ts = time.time()
    return fresh if fresh else _cache_data


# ─── Charge categorisation ────────────────────────────────────────────────────
DRUG_KW = [
    'ยาบ้า', 'ยาไอซ์', 'เสพ', 'กัญชา', 'ยาเสพ',
    'ครอบครองยา', 'จำหน่ายยา', 'ผลิตยา', 'ยาเสพติด',
]
WARRANT_KW = ['หมายจับ', 'ตามหมาย', 'หมาย จ.', 'หมาย จพ.']


def categorise(charge: str) -> str:
    c = charge
    for kw in DRUG_KW:
        if kw in c:
            return 'ยาเสพติด'
    for kw in WARRANT_KW:
        if kw in c:
            return 'หมายจับ'
    return 'คดีอื่นๆ'


# ─── Search helpers ───────────────────────────────────────────────────────────
def _sort(records: list) -> list:
    return sorted(records, key=lambda r: (-(r['year_be'] or 0), -(r['month_num'] or 0)))


def search_name(kw: str, data: list):
    kw = kw.lower()
    hits = [r for r in data if kw in r['name'].lower()]
    return _sort(hits)[:DETAIL_LIMIT], len(hits)


def search_location(kw: str, data: list):
    hits = [r for r in data if kw in r.get('location', '') and r.get('location')]
    if not hits:
        suffix = kw[-4:] if len(kw) >= 4 else kw
        hits = [r for r in data if suffix in r.get('location', '') and r.get('location')]
    return _sort(hits)[:DETAIL_LIMIT], len(hits)


def search_evidence(kw: str, data: list):
    kw = kw.lower()
    hits = [r for r in data if kw in r.get('evidence', '').lower()]
    return _sort(hits)[:DETAIL_LIMIT], len(hits)


def search_charge(kw: str, data: list):
    kw = kw.lower()
    hits = [r for r in data if kw in r.get('charge', '').lower()]
    return _sort(hits)[:DETAIL_LIMIT], len(hits)


def monthly(month_num: int, year_be: int, data: list) -> list:
    return [r for r in data if r['month_num'] == month_num and r['year_be'] == year_be]


def yearly(year_be: int, data: list) -> list:
    return [r for r in data if r['year_be'] == year_be]


def statistics(data: list):
    total = len(data)
    by_cat = {'ยาเสพติด': 0, 'หมายจับ': 0, 'คดีอื่นๆ': 0}
    by_year: dict = {}
    for r in data:
        cat = categorise(r.get('charge', ''))
        by_cat[cat] = by_cat.get(cat, 0) + 1
        yr = r.get('year_be', 0)
        if yr:
            by_year[yr] = by_year.get(yr, 0) + 1
    return total, by_cat, by_year


# ─── Flex Message builders ────────────────────────────────────────────────────
GROUP_COLOR = {
    'กลุ่ม 1': '#C62828',
    'กลุ่ม 2': '#E65100',
    'กลุ่ม 3': '#2E7D32',
    'กลุ่ม 4': '#1565C0',
    'กลุ่ม 5': '#6A1B9A',
    'จับกุมตามหมายจับ': '#FF8F00',
    'ยาเสพติด': '#C62828',
    'หมายจับ': '#FF8F00',
    'คดีอื่นๆ': '#455A64',
}


def record_color(rec: dict) -> str:
    grp = rec.get('group', '') or ''
    for k, v in GROUP_COLOR.items():
        if k in grp:
            return v
    return GROUP_COLOR.get(categorise(rec.get('charge', '')), '#455A64')


def _text(t, **kw):
    return {'type': 'text', 'text': t or '-', **kw}


def _row(label: str, value: str) -> dict:
    return {
        'type': 'box', 'layout': 'baseline', 'spacing': 'sm',
        'contents': [
            _text(label, size='xs', color='#888888', flex=2),
            _text(value or '-', size='xs', color='#333333', flex=5, wrap=True),
        ]
    }


def build_bubble(rec: dict) -> dict:
    """Build a Flex bubble card for one suspect."""
    color     = record_color(rec)
    name      = rec.get('name', '-')
    nickname  = rec.get('nickname', '')
    charge    = rec.get('charge', '-')
    grp       = rec.get('group', '')
    date      = rec.get('date', '-')
    location  = rec.get('location', '') or '-'
    evidence  = rec.get('evidence', '') or '-'
    age       = rec.get('age', '') or '-'
    image_url = rec.get('image_url')
    month_abbr = rec.get('month_abbr', '')
    year_be   = rec.get('year_be', '')
    period    = f"{month_abbr} {year_be}".strip() if month_abbr or year_be else rec.get('sheet', '')

    # Header
    header_lines = [
        _text(name, weight='bold', size='md', color='#FFFFFF', wrap=True)
    ]
    if nickname:
        header_lines.append(_text(f'ชื่อเล่น: {nickname}', size='xs', color='#FFFFFFcc'))

    # Body
    body_contents = []
    if grp:
        body_contents.append(_text(grp, size='xxs', color='#AAAAAA'))
    body_contents.append(_text(charge, weight='bold', size='sm', color=color, wrap=True))
    body_contents.append({'type': 'separator', 'margin': 'sm'})
    body_contents.append({
        'type': 'box', 'layout': 'vertical', 'margin': 'sm', 'spacing': 'xs',
        'contents': [
            _row('📅 วันที่', date),
            _row('📍 สถานที่', location),
            _row('📦 ของกลาง', evidence),
            _row('🎂 อายุ', f'{age} ปี'),
            _row('📋 เดือน/ปี', period),
        ]
    })

    bubble = {
        'type': 'bubble',
        'size': 'kilo',
        'header': {
            'type': 'box',
            'layout': 'vertical',
            'backgroundColor': color,
            'paddingAll': '13px',
            'contents': header_lines,
        },
        'body': {
            'type': 'box',
            'layout': 'vertical',
            'paddingAll': '10px',
            'spacing': 'sm',
            'contents': body_contents,
        },
    }

    if image_url:
        bubble['hero'] = {
            'type': 'image',
            'url': image_url,
            'size': 'full',
            'aspectRatio': '4:3',
            'aspectMode': 'cover',
        }

    return bubble


def build_carousel(records: list, alt: str) -> FlexMessage:
    bubbles = [build_bubble(r) for r in records[:10]]
    container = {'type': 'carousel', 'contents': bubbles} if len(bubbles) > 1 else bubbles[0]
    return FlexMessage(alt_text=alt, contents=FlexContainer.from_dict(container))


def build_summary_flex(title: str, total: int, cat: dict, records: list) -> FlexMessage:
    """Summary card with stat boxes + top-10 list."""
    drug    = cat.get('ยาเสพติด', 0)
    warrant = cat.get('หมายจับ', 0)
    other   = cat.get('คดีอื่นๆ', 0)

    list_items = []
    for i, r in enumerate(records[:10], 1):
        nm  = r.get('name', '-')
        ch  = (r.get('charge', '') or '')[:22]
        dt  = r.get('date', '')
        line = f"{i}. {nm}"
        if ch:
            line += f"\n    {ch}"
        if dt:
            line += f" ({dt})"
        list_items.append(_text(line, size='xs', color='#333333', wrap=True, margin='xs'))

    bubble = {
        'type': 'bubble',
        'header': {
            'type': 'box',
            'layout': 'vertical',
            'backgroundColor': '#1565C0',
            'paddingAll': '14px',
            'contents': [
                _text(f'🚔 {title}', weight='bold', color='#FFFFFF', size='lg'),
                _text(f'แสดง {min(len(records),10)}/{total} รายล่าสุด',
                      size='xs', color='#CCDDFFcc'),
            ]
        },
        'body': {
            'type': 'box',
            'layout': 'vertical',
            'spacing': 'md',
            'paddingAll': '12px',
            'contents': [
                # Stat row
                {
                    'type': 'box', 'layout': 'vertical',
                    'backgroundColor': '#F3F4F6', 'cornerRadius': '8px',
                    'paddingAll': '12px',
                    'contents': [
                        _text('📊 สรุปยอด', weight='bold', size='sm', color='#1565C0'),
                        {
                            'type': 'box', 'layout': 'baseline', 'margin': 'sm',
                            'contents': [
                                _text(str(total), weight='bold', size='xxl', color='#E53935'),
                                _text(' คดี/ราย', size='sm', color='#555555', margin='sm'),
                            ]
                        },
                        {
                            'type': 'box', 'layout': 'horizontal', 'margin': 'sm', 'spacing': 'md',
                            'contents': [
                                {
                                    'type': 'box', 'layout': 'vertical', 'flex': 1,
                                    'contents': [
                                        _text(str(drug), weight='bold', color='#C62828', align='center', size='lg'),
                                        _text('ยาเสพติด', size='xxs', color='#888888', align='center'),
                                    ]
                                },
                                {
                                    'type': 'box', 'layout': 'vertical', 'flex': 1,
                                    'contents': [
                                        _text(str(warrant), weight='bold', color='#FF8F00', align='center', size='lg'),
                                        _text('หมายจับ', size='xxs', color='#888888', align='center'),
                                    ]
                                },
                                {
                                    'type': 'box', 'layout': 'vertical', 'flex': 1,
                                    'contents': [
                                        _text(str(other), weight='bold', color='#455A64', align='center', size='lg'),
                                        _text('คดีอื่นๆ', size='xxs', color='#888888', align='center'),
                                    ]
                                },
                            ]
                        },
                    ]
                },
                # Separator
                {'type': 'separator'},
                # Record list
                {
                    'type': 'box', 'layout': 'vertical', 'spacing': 'none',
                    'contents': [
                        _text('รายชื่อ', weight='bold', size='sm', color='#333333'),
                    ] + list_items
                },
            ]
        }
    }

    return FlexMessage(alt_text=f'{title}: {total} ราย', contents=FlexContainer.from_dict(bubble))


# ─── Command handler ──────────────────────────────────────────────────────────
LOCATION_PREFIX_RE = re.compile(
    r'^(ชุมชน|ซอย|ถ\.|ถนน|หมู่บ้าน|สน\.|ริมคลอง|ริมถนน|ริม|แยก|'
    r'ลาน|ตลาด|ห้าง|อาคาร|บริเวณ|ปากซอย|กลางซอย|คอนโด)'
)

MONTH_YEAR_DIRECT_RE = re.compile(
    r'^(มกราคม|กุมภาพันธ์|มีนาคม|เมษายน|พฤษภาคม|มิถุนายน|กรกฎาคม|'
    r'สิงหาคม|กันยายน|ตุลาคม|พฤศจิกายน|ธันวาคม|'
    r'ม\.ค\.|ก\.พ\.|มี\.ค\.|เม\.ย\.|พ\.ค\.|มิ\.ย\.|ก\.ค\.|'
    r'ส\.ค\.|ก\.ย\.|ต\.ค\.|พ\.ย\.|ธ\.ค\.)\s*\.?\s*(25[5-9]\d|26\d{2}|[5-9]\d)\b',
    re.UNICODE
)

HELP_TEXT = (
    "🚔 คำสั่ง LINE Bot สน.บางชัน\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "🔍 bot ค้นหา <ชื่อ>\n"
    "   ค้นหาผู้ต้องหาตามชื่อ\n\n"
    "📍 bot สถานที่ <สถานที่>\n"
    "   ค้นหาตามสถานที่จับกุม\n\n"
    "📅 bot เดือน ก.ค. 69\n"
    "   สรุปผลจับกุมรายเดือน\n\n"
    "📆 bot ปี 2569\n"
    "   สรุปผลจับกุมรายปี\n\n"
    "📦 bot ของกลาง <สิ่งของ>\n"
    "   ค้นหาตามของกลาง\n\n"
    "⚖️ bot ข้อหา <ข้อหา>\n"
    "   ค้นหาตามข้อหา\n\n"
    "📊 bot สถิติ\n"
    "   ดูสถิติภาพรวมทั้งหมด\n\n"
    "🔄 bot รีเฟรช\n"
    "   โหลดข้อมูลใหม่จาก Google Sheets\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "💡 ใช้ในกลุ่ม: ต้องพิมพ์ bot นำหน้า"
)

WELCOME_TEXT = (
    "🚔 สวัสดีครับ! บอทสืบค้นผลการจับกุม\n"
    "สถานีตำรวจนครบาลบางชัน\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "📡 ข้อมูลอัปเดตสดจาก Google Sheets\n\n"
    "พิมพ์ bot ช่วยเหลือ เพื่อดูคำสั่งทั้งหมด\n"
    "หรือ bot สถิติ เพื่อดูข้อมูลภาพรวม"
)


def handle_message(text: str) -> list:
    data = get_data()
    t = text.strip()

    # ── ช่วยเหลือ / help ──
    if re.match(r'^(ช่วย|help|ช่วยเหลือ|คำสั่ง)$', t, re.IGNORECASE):
        return [TextMessage(text=HELP_TEXT)]

    # ── สถิติ ──
    if re.match(r'^สถิติ$', t):
        total, by_cat, by_year = statistics(data)
        top_years = sorted(by_year.items(), reverse=True)[:5]
        yr_lines  = '\n'.join(f"  ปี {yr}: {cnt:,} ราย" for yr, cnt in top_years)
        msg = (
            f"📊 สถิติรวมทั้งหมด\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"รวม: {total:,} ราย\n"
            f"ยาเสพติด: {by_cat.get('ยาเสพติด',0):,} ราย\n"
            f"หมายจับ: {by_cat.get('หมายจับ',0):,} ราย\n"
            f"คดีอื่นๆ: {by_cat.get('คดีอื่นๆ',0):,} ราย\n\n"
            f"📆 5 ปีล่าสุด\n{yr_lines}"
        )
        return [TextMessage(text=msg)]

    # ── รีเฟรช ──
    if re.match(r'^(รีเฟรช|refresh|โหลดใหม่)$', t, re.IGNORECASE):
        threading.Thread(target=lambda: get_data(force=True), daemon=True).start()
        return [TextMessage(text="🔄 กำลังโหลดข้อมูลใหม่จาก Google Sheets...")]

    # ── ค้นหาชื่อ ──
    m = re.match(r'^ค้นหา\s+(.+)$', t)
    if m:
        kw = m.group(1).strip()
        rows, total = search_name(kw, data)
        if not rows:
            return [TextMessage(text=f"❌ ไม่พบ '{kw}' ในระบบ")]
        summary = TextMessage(text=f"🔍 ค้นหา: {kw}\nพบทั้งหมด {total} ราย (แสดง {len(rows)} ล่าสุด)")
        carousel = build_carousel(rows[:10], f"ค้นหา: {kw}")
        return [summary, carousel]

    # ── เดือน (explicit) ──
    m = re.match(r'^เดือน\s+(.+)$', t)
    if m:
        mn, yr = parse_month_year(m.group(1))
        if mn and yr:
            rows = monthly(mn, yr, data)
            if not rows:
                return [TextMessage(text=f"❌ ไม่พบข้อมูลเดือนนั้น")]
            cat = {c: 0 for c in ('ยาเสพติด', 'หมายจับ', 'คดีอื่นๆ')}
            for r in rows:
                cat[categorise(r.get('charge', ''))] = cat.get(categorise(r.get('charge', '')), 0) + 1
            ttl = f"สรุป {MONTH_NUM_TO_ABBR.get(mn,'')} {yr}"
            return [build_summary_flex(ttl, len(rows), cat, rows)]
        return [TextMessage(text="❓ รูปแบบเดือนไม่ถูกต้อง เช่น เดือน ก.ค. 69")]

    # ── เดือน (direct input) ──
    if MONTH_YEAR_DIRECT_RE.match(t):
        mn, yr = parse_month_year(t)
        if mn and yr:
            rows = monthly(mn, yr, data)
            if not rows:
                return [TextMessage(text=f"❌ ไม่พบข้อมูลเดือนนั้น")]
            cat = {c: 0 for c in ('ยาเสพติด', 'หมายจับ', 'คดีอื่นๆ')}
            for r in rows:
                key = categorise(r.get('charge', ''))
                cat[key] = cat.get(key, 0) + 1
            ttl = f"สรุป {MONTH_NUM_TO_ABBR.get(mn,'')} {yr}"
            return [build_summary_flex(ttl, len(rows), cat, rows)]

    # ── ปี ──
    m = re.match(r'^ปี\s*(25[5-9]\d|26\d{2}|[5-9]\d)$', t)
    if m:
        yr = int(m.group(1))
        ybe = yr + 2500 if yr < 100 else yr
        rows = yearly(ybe, data)
        if not rows:
            return [TextMessage(text=f"❌ ไม่พบข้อมูลปี {ybe}")]
        cat = {c: 0 for c in ('ยาเสพติด', 'หมายจับ', 'คดีอื่นๆ')}
        for r in rows:
            key = categorise(r.get('charge', ''))
            cat[key] = cat.get(key, 0) + 1
        return [build_summary_flex(f"สรุปปี {ybe}", len(rows), cat, rows)]

    # ── สถานที่ (explicit) ──
    m = re.match(r'^สถานที่\s+(.+)$', t)
    if m:
        kw = m.group(1).strip()
        rows, total = search_location(kw, data)
        if not rows:
            return [TextMessage(text=f"❌ ไม่พบสถานที่ '{kw}'")]
        cat = {c: 0 for c in ('ยาเสพติด', 'หมายจับ', 'คดีอื่นๆ')}
        for r in rows:
            key = categorise(r.get('charge', ''))
            cat[key] = cat.get(key, 0) + 1
        return [build_summary_flex(f"📍 {kw}", total, cat, rows)]

    # ── สถานที่ (auto-detect) ──
    if LOCATION_PREFIX_RE.match(t):
        rows, total = search_location(t, data)
        if rows:
            cat = {c: 0 for c in ('ยาเสพติด', 'หมายจับ', 'คดีอื่นๆ')}
            for r in rows:
                key = categorise(r.get('charge', ''))
                cat[key] = cat.get(key, 0) + 1
            return [build_summary_flex(f"📍 {t}", total, cat, rows)]

    # ── ของกลาง ──
    m = re.match(r'^ของกลาง\s+(.+)$', t)
    if m:
        kw = m.group(1).strip()
        rows, total = search_evidence(kw, data)
        if not rows:
            return [TextMessage(text=f"❌ ไม่พบของกลาง '{kw}'")]
        summary = TextMessage(text=f"📦 ของกลาง: {kw}\nพบทั้งหมด {total} ราย (แสดง {len(rows)} ล่าสุด)")
        return [summary, build_carousel(rows[:10], f"ของกลาง: {kw}")]

    # ── ข้อหา ──
    m = re.match(r'^ข้อหา\s+(.+)$', t)
    if m:
        kw = m.group(1).strip()
        rows, total = search_charge(kw, data)
        if not rows:
            return [TextMessage(text=f"❌ ไม่พบข้อหา '{kw}'")]
        cat = {c: 0 for c in ('ยาเสพติด', 'หมายจับ', 'คดีอื่นๆ')}
        for r in rows:
            key = categorise(r.get('charge', ''))
            cat[key] = cat.get(key, 0) + 1
        return [build_summary_flex(f"⚖️ {kw}", total, cat, rows)]

    # ── ไม่รู้จักคำสั่ง ──
    return [TextMessage(text=f"❓ ไม่เข้าใจคำสั่ง '{t}'\nพิมพ์ bot ช่วยเหลือ เพื่อดูคำสั่งทั้งหมด")]


# ─── LINE reply / push ────────────────────────────────────────────────────────
def _reply(reply_token: str, messages: list) -> bool:
    if not reply_token or reply_token == '0' * 32:
        return False
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(reply_token=reply_token, messages=messages[:5])
            )
        return True
    except Exception as e:
        log.error(f"[reply] {e}")
        return False


def _push(to: str, messages: list):
    if not to:
        return
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(to=to, messages=messages[:5])
            )
    except Exception as e:
        log.error(f"[push] {e}")


# ─── LINE event handlers ──────────────────────────────────────────────────────
@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event):
    source      = event.source
    source_type = source.type
    push_to     = (getattr(source, 'group_id', None) or
                   getattr(source, 'room_id',  None) or
                   getattr(source, 'user_id',  None))
    text = event.message.text.strip()

    # In groups/rooms, require "bot" prefix
    if source_type in ('group', 'room'):
        if not re.match(r'^bot\b', text, re.IGNORECASE):
            return
        text = re.sub(r'^bot\s*', '', text, flags=re.IGNORECASE).strip()

    msgs = handle_message(text)
    if not _reply(event.reply_token, msgs) and push_to:
        _push(push_to, msgs)


@handler.add(JoinEvent)
def on_join(event):
    source = event.source
    msgs = [TextMessage(text=WELCOME_TEXT)]
    if not _reply(event.reply_token, msgs):
        target = getattr(source, 'group_id', None) or getattr(source, 'room_id', None)
        if target:
            _push(target, msgs)


@handler.add(FollowEvent)
def on_follow(event):
    source = event.source
    msgs = [TextMessage(text=WELCOME_TEXT)]
    if not _reply(event.reply_token, msgs):
        uid = getattr(source, 'user_id', None)
        if uid:
            _push(uid, msgs)


@handler.add(MemberJoinedEvent)
def on_member_join(_event):
    pass


@handler.add(LeaveEvent)
def on_leave(_event):
    log.info('[leave] Bot removed from chat')


@handler.add(UnfollowEvent)
def on_unfollow(_event):
    log.info('[unfollow] User blocked bot')


# ─── Flask routes ─────────────────────────────────────────────────────────────
@app.route('/callback', methods=['POST'])
def callback():
    sig  = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, sig)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        log.error(f'[webhook] {e}', exc_info=True)
    return 'OK', 200


@app.route('/ping')
def ping():
    return 'pong', 200


@app.route('/refresh')
def http_refresh():
    threading.Thread(target=lambda: get_data(force=True), daemon=True).start()
    return 'refreshing...', 200


@app.route('/')
def index():
    n = len(_cache_data)
    age = int(time.time() - _cache_ts) if _cache_ts else -1
    return f'LINE Bot สน.บางชัน | {n} records | cache age {age}s', 200


# ─── Startup ──────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    # Warm-up cache in background
    threading.Thread(target=get_data, daemon=True).start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

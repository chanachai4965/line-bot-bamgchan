#!/usr/bin/env python3
"""
LINE Bot — ระบบสืบค้นผลการจับกุม สน.บางชัน
v5.0 — เพิ่มฐานข้อมูลบุคลากร ชุดปฏิบัติการ และเวรวันคู่/วันคี่
ดึงข้อมูลจาก Google Apps Script Web App → cache ใน RAM → ตอบ Flex Message
"""

import os
import re
import time
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

import requests
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

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ─── Flask + LINE SDK ─────────────────────────────────────────────────────────
app = Flask(__name__)

LINE_CHANNEL_SECRET        = os.environ['LINE_CHANNEL_SECRET']
LINE_CHANNEL_ACCESS_TOKEN  = os.environ['LINE_CHANNEL_ACCESS_TOKEN']
# URL ของ Apps Script Web App (Deploy → New Deployment → Web App)
APPS_SCRIPT_URL            = os.environ['APPS_SCRIPT_URL']
# Secret key สำหรับป้องกัน Apps Script ถูกเรียกโดยคนอื่น (ต้องตรงกับ Code.gs)
APPS_SCRIPT_KEY            = os.environ.get('APPS_SCRIPT_KEY', '')
CACHE_TTL                  = int(os.environ.get('CACHE_TTL', '300'))   # วินาที
FETCH_TIMEOUT              = int(os.environ.get('FETCH_TIMEOUT', '90')) # วินาที
DETAIL_LIMIT               = 30

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler       = WebhookHandler(LINE_CHANNEL_SECRET)

# ─── Data cache ───────────────────────────────────────────────────────────────
# ห้ามเริ่ม background thread ตอน import module:
# Gunicorn อาจ import ใน master process ก่อน fork worker ทำให้ cache อยู่ผิด process
_cache_data:  list = []
_staff_data: list = []
_cache_ts:  float = 0.0
_cache_lock        = threading.RLock()
_fetch_state_lock  = threading.Lock()
_fetching:   bool  = False


def fetch_all() -> tuple[list, list]:
    """เรียก Apps Script → คืน (ข้อมูลจับกุม, ข้อมูลบุคลากร)"""
    try:
        params = {'key': APPS_SCRIPT_KEY} if APPS_SCRIPT_KEY else {}
        log.info(f'[fetch] GET {APPS_SCRIPT_URL} key={bool(APPS_SCRIPT_KEY)}')
        resp = requests.get(
            APPS_SCRIPT_URL, params=params,
            timeout=FETCH_TIMEOUT, allow_redirects=True
        )
        log.info(f'[fetch] HTTP {resp.status_code} url={resp.url[:80]}')

        ct = resp.headers.get('Content-Type', '')
        if 'html' in ct:
            log.error('[fetch] got HTML instead of JSON')
            log.error(f'[fetch] first 300 chars: {resp.text[:300]}')
            return list(_cache_data), list(_staff_data)

        resp.raise_for_status()
        payload = resp.json()

        if 'error' in payload:
            log.error(f'[fetch] Apps Script returned error: {payload["error"]}')
            return list(_cache_data), list(_staff_data)

        records = [_normalise(r) for r in payload.get('records', [])]
        staff = [_normalise_staff(r) for r in payload.get('staff', [])]

        log.info(
            f'[fetch] ✅ loaded arrests={len(records)} staff={len(staff)} '
            f'(sheets={payload.get("sheetsProcessed","?")})'
        )
        return records, staff

    except requests.exceptions.Timeout:
        log.error(f'[fetch] ⏱ timeout after {FETCH_TIMEOUT}s')
        return list(_cache_data), list(_staff_data)
    except Exception as e:
        log.error(f'[fetch] {e}', exc_info=True)
        return list(_cache_data), list(_staff_data)


def _normalise(r: dict) -> dict:
    """แปลงข้อมูลจับกุมจาก Apps Script"""
    return {
        'sheet':      r.get('sheet', ''),
        'year_be':    int(r.get('yearBe', 0) or 0),
        'month_num':  int(r.get('monthNum', 0) or 0),
        'month_abbr': r.get('monthAbbr', ''),
        'seq':        r.get('seq', ''),
        'date':       r.get('date', ''),
        'group':      r.get('group', ''),
        'charge':     r.get('charge', ''),
        'name':       r.get('name', ''),
        'nickname':   r.get('nickname', ''),
        'age':        r.get('age', ''),
        'pid':        r.get('pid', ''),
        'evidence':   r.get('evidence', ''),
        'location':   r.get('location', ''),
        'note':       r.get('note', ''),
        'image_url':  r.get('imageUrl'),
    }


def _normalise_staff(r: dict) -> dict:
    """แปลงข้อมูลบุคลากรจาก Apps Script"""
    try:
        team = int(r.get('team', 0) or 0)
    except (TypeError, ValueError):
        team = 0

    return {
        'name':       str(r.get('name', '') or '').strip(),
        'position':   str(r.get('position', '') or '').strip(),
        'phone':      str(r.get('phone', '') or '').strip(),
        'nickname':   str(r.get('nickname', '') or '').strip(),
        'image_url':  r.get('imageUrl'),
        'note':       str(r.get('note', '') or '').strip(),
        'team':       team,
    }


def _do_fetch():
    """โหลดข้อมูลทั้งหมดและอัปเดต cache ใน worker process"""
    global _cache_ts, _fetching

    with _fetch_state_lock:
        if _fetching:
            log.info('[cache] fetch already running')
            return
        _fetching = True

    try:
        fresh_records, fresh_staff = fetch_all()
        if fresh_records or fresh_staff:
            with _cache_lock:
                if fresh_records:
                    _cache_data.clear()
                    _cache_data.extend(fresh_records)
                if fresh_staff:
                    _staff_data.clear()
                    _staff_data.extend(fresh_staff)
                _cache_ts = time.time()
                arrest_count = len(_cache_data)
                staff_count = len(_staff_data)
            log.info(
                f'[cache] updated arrests={arrest_count} staff={staff_count}'
            )
        else:
            log.warning('[cache] fetch returned no data; keeping old cache')
    finally:
        with _fetch_state_lock:
            _fetching = False


def _start_fetch_background() -> bool:
    with _fetch_state_lock:
        if _fetching:
            return False

    threading.Thread(
        target=_do_fetch,
        name='apps-script-fetch',
        daemon=True
    ).start()
    return True


def get_data(force: bool = False) -> tuple[list, list]:
    """คืน (ข้อมูลจับกุม, บุคลากร) แบบ stale-while-revalidate"""
    with _cache_lock:
        records_snapshot = list(_cache_data)
        staff_snapshot = list(_staff_data)
        ts = _cache_ts

    has_cache = bool(records_snapshot or staff_snapshot)
    age = (time.time() - ts) if ts else None
    cache_expired = has_cache and age is not None and age >= CACHE_TTL

    if force:
        _do_fetch()
        with _cache_lock:
            result = (list(_cache_data), list(_staff_data))
        log.info(
            f'[cache] force arrests={len(result[0])} staff={len(result[1])}'
        )
        return result

    if has_cache:
        if cache_expired:
            started = _start_fetch_background()
            log.info(
                f'[cache] stale arrests={len(records_snapshot)} '
                f'staff={len(staff_snapshot)} age={int(age)}s '
                f'refresh_started={started}'
            )
        return records_snapshot, staff_snapshot

    started = _start_fetch_background()
    log.info(f'[cache] empty - background_started={started}')
    return [], []


# ─── Thai month helpers ───────────────────────────────────────────────────────
THAI_MONTHS = {
    'ม.ค.':1,'มกราคม':1,'ก.พ.':2,'กุมภาพันธ์':2,'มี.ค.':3,'มีนาคม':3,
    'เม.ย.':4,'เมษายน':4,'พ.ค.':5,'พฤษภาคม':5,'มิ.ย.':6,'มิถุนายน':6,
    'ก.ค.':7,'กรกฎาคม':7,'ส.ค.':8,'สิงหาคม':8,'ก.ย.':9,'กันยายน':9,
    'ต.ค.':10,'ตุลาคม':10,'พ.ย.':11,'พฤศจิกายน':11,'ธ.ค.':12,'ธันวาคม':12,
}
MONTH_NUM_TO_ABBR = {
    1:'ม.ค.',2:'ก.พ.',3:'มี.ค.',4:'เม.ย.',5:'พ.ค.',6:'มิ.ย.',
    7:'ก.ค.',8:'ส.ค.',9:'ก.ย.',10:'ต.ค.',11:'พ.ย.',12:'ธ.ค.',
}

MONTH_INPUT_RE = re.compile(
    r'(มกราคม|กุมภาพันธ์|มีนาคม|เมษายน|พฤษภาคม|มิถุนายน|กรกฎาคม|'
    r'สิงหาคม|กันยายน|ตุลาคม|พฤศจิกายน|ธันวาคม|'
    r'ม\.ค\.|ก\.พ\.|มี\.ค\.|เม\.ย\.|พ\.ค\.|มิ\.ย\.|ก\.ค\.|'
    r'ส\.ค\.|ก\.ย\.|ต\.ค\.|พ\.ย\.|ธ\.ค\.)',
    re.UNICODE
)
YEAR_INPUT_RE = re.compile(r'\b(25[5-9]\d|26\d{2}|[5-9]\d)\b')


def parse_month_year(text: str):
    """คืน (month_num, year_be) หรือ (None, None)"""
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


# ─── Charge categorisation ────────────────────────────────────────────────────
DRUG_KW    = ['ยาบ้า','ยาไอซ์','เสพ','กัญชา','ยาเสพ','ครอบครองยา','จำหน่ายยา','ผลิตยา','ยาเสพติด']
WARRANT_KW = ['หมายจับ','ตามหมาย','หมาย จ.','หมาย จพ.']


def categorise(charge: str) -> str:
    for kw in DRUG_KW:
        if kw in charge:
            return 'ยาเสพติด'
    for kw in WARRANT_KW:
        if kw in charge:
            return 'หมายจับ'
    return 'คดีอื่นๆ'


# ─── Search helpers ───────────────────────────────────────────────────────────
def _sort(records: list) -> list:
    return sorted(records, key=lambda r: (-(r['year_be']), -(r['month_num'])))


def search_name(kw: str, data: list):
    kw_l = kw.lower()
    hits  = [r for r in data if kw_l in r['name'].lower()]
    return _sort(hits)[:DETAIL_LIMIT], len(hits)


def search_location(kw: str, data: list):
    hits = [r for r in data if kw in r.get('location','') and r.get('location')]
    if not hits:
        suffix = kw[-4:] if len(kw) >= 4 else kw
        hits = [r for r in data if suffix in r.get('location','') and r.get('location')]
    return _sort(hits)[:DETAIL_LIMIT], len(hits)


def search_evidence(kw: str, data: list):
    kw_l = kw.lower()
    hits  = [r for r in data if kw_l in r.get('evidence','').lower()]
    return _sort(hits)[:DETAIL_LIMIT], len(hits)


def search_charge(kw: str, data: list):
    kw_l = kw.lower()
    hits  = [r for r in data if kw_l in r.get('charge','').lower()]
    return _sort(hits)[:DETAIL_LIMIT], len(hits)


def monthly(month_num: int, year_be: int, data: list) -> list:
    return [r for r in data if r['month_num'] == month_num and r['year_be'] == year_be]


def yearly(year_be: int, data: list) -> list:
    return [r for r in data if r['year_be'] == year_be]


def statistics(data: list):
    total   = len(data)
    by_cat  = {'ยาเสพติด': 0, 'หมายจับ': 0, 'คดีอื่นๆ': 0}
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
    'กลุ่ม 1':'#C62828','กลุ่ม 2':'#E65100','กลุ่ม 3':'#2E7D32',
    'กลุ่ม 4':'#1565C0','กลุ่ม 5':'#6A1B9A',
    'จับกุมตามหมายจับ':'#FF8F00',
    'ยาเสพติด':'#C62828','หมายจับ':'#FF8F00','คดีอื่นๆ':'#455A64',
}


def record_color(rec: dict) -> str:
    grp = rec.get('group','') or ''
    for k, v in GROUP_COLOR.items():
        if k in grp:
            return v
    return GROUP_COLOR.get(categorise(rec.get('charge','')), '#455A64')


def _t(text, **kw):
    return {'type': 'text', 'text': text or '-', **kw}


def _row(label: str, value: str) -> dict:
    return {
        'type':'box','layout':'baseline','spacing':'sm',
        'contents':[
            _t(label, size='xs', color='#888888', flex=2),
            _t(value or '-', size='xs', color='#333333', flex=5, wrap=True),
        ]
    }


def build_bubble(rec: dict) -> dict:
    color      = record_color(rec)
    name       = rec.get('name','-')
    nickname   = rec.get('nickname','')
    charge     = rec.get('charge','-')
    grp        = rec.get('group','')
    date       = rec.get('date','-')
    location   = rec.get('location','') or '-'
    evidence   = rec.get('evidence','') or '-'
    age        = rec.get('age','') or '-'
    image_url  = rec.get('image_url')
    month_abbr = rec.get('month_abbr','')
    year_be    = rec.get('year_be','')
    period     = f"{month_abbr} {year_be}".strip() or rec.get('sheet','')

    header = [_t(name, weight='bold', size='md', color='#FFFFFF', wrap=True)]
    if nickname:
        header.append(_t(f'ชื่อเล่น: {nickname}', size='xs', color='#FFFFFFcc'))

    body = []
    if grp:
        body.append(_t(grp, size='xxs', color='#AAAAAA'))
    body.append(_t(charge, weight='bold', size='sm', color=color, wrap=True))
    body.append({'type':'separator','margin':'sm'})
    body.append({
        'type':'box','layout':'vertical','margin':'sm','spacing':'xs',
        'contents':[
            _row('📅 วันที่', date),
            _row('📍 สถานที่', location),
            _row('📦 ของกลาง', evidence),
            _row('🎂 อายุ', f'{age} ปี'),
            _row('📋 เดือน/ปี', period),
        ]
    })

    bubble: dict = {
        'type':'bubble','size':'kilo',
        'header':{
            'type':'box','layout':'vertical',
            'backgroundColor':color,'paddingAll':'13px',
            'contents':header,
        },
        'body':{
            'type':'box','layout':'vertical',
            'paddingAll':'10px','spacing':'sm',
            'contents':body,
        },
    }
    if image_url:
        bubble['hero'] = {
            'type':'image','url':image_url,
            'size':'full','aspectRatio':'4:3','aspectMode':'cover',
        }
    return bubble


def build_carousel(records: list, alt: str) -> FlexMessage:
    bubbles   = [build_bubble(r) for r in records[:10]]
    container = {'type':'carousel','contents':bubbles} if len(bubbles) > 1 else bubbles[0]
    return FlexMessage(alt_text=alt, contents=FlexContainer.from_dict(container))


def build_summary_flex(title: str, total: int, cat: dict, records: list) -> FlexMessage:
    drug    = cat.get('ยาเสพติด',0)
    warrant = cat.get('หมายจับ',0)
    other   = cat.get('คดีอื่นๆ',0)

    items = []
    for i, r in enumerate(records[:10], 1):
        nm = r.get('name','-')
        ch = (r.get('charge','') or '')[:22]
        dt = r.get('date','')
        line = f"{i}. {nm}"
        if ch:  line += f"\n    {ch}"
        if dt:  line += f" ({dt})"
        items.append(_t(line, size='xs', color='#333333', wrap=True, margin='xs'))

    bubble = {
        'type':'bubble',
        'header':{
            'type':'box','layout':'vertical',
            'backgroundColor':'#1565C0','paddingAll':'14px',
            'contents':[
                _t(f'🚔 {title}', weight='bold', color='#FFFFFF', size='lg'),
                _t(f'แสดง {min(len(records),10)}/{total} รายล่าสุด',
                   size='xs', color='#CCDDFFcc'),
            ]
        },
        'body':{
            'type':'box','layout':'vertical','spacing':'md','paddingAll':'12px',
            'contents':[
                {   # stat boxes
                    'type':'box','layout':'vertical',
                    'backgroundColor':'#F3F4F6','cornerRadius':'8px','paddingAll':'12px',
                    'contents':[
                        _t('📊 สรุปยอด', weight='bold', size='sm', color='#1565C0'),
                        {
                            'type':'box','layout':'baseline','margin':'sm',
                            'contents':[
                                _t(str(total), weight='bold', size='xxl', color='#E53935'),
                                _t(' คดี/ราย', size='sm', color='#555555', margin='sm'),
                            ]
                        },
                        {
                            'type':'box','layout':'horizontal','margin':'sm','spacing':'md',
                            'contents':[
                                {'type':'box','layout':'vertical','flex':1,'contents':[
                                    _t(str(drug), weight='bold', color='#C62828', align='center', size='lg'),
                                    _t('ยาเสพติด', size='xxs', color='#888888', align='center'),
                                ]},
                                {'type':'box','layout':'vertical','flex':1,'contents':[
                                    _t(str(warrant), weight='bold', color='#FF8F00', align='center', size='lg'),
                                    _t('หมายจับ', size='xxs', color='#888888', align='center'),
                                ]},
                                {'type':'box','layout':'vertical','flex':1,'contents':[
                                    _t(str(other), weight='bold', color='#455A64', align='center', size='lg'),
                                    _t('คดีอื่นๆ', size='xxs', color='#888888', align='center'),
                                ]},
                            ]
                        },
                    ]
                },
                {'type':'separator'},
                {   # record list
                    'type':'box','layout':'vertical','spacing':'none',
                    'contents':[_t('รายชื่อ', weight='bold', size='sm', color='#333333')] + items,
                },
            ]
        }
    }
    return FlexMessage(alt_text=f'{title}: {total} ราย',
                       contents=FlexContainer.from_dict(bubble))



# ─── Staff / operation team helpers ───────────────────────────────────────────
BANGKOK_TZ = ZoneInfo("Asia/Bangkok")


def _search_key(value: str) -> str:
    """ทำข้อความให้เหมาะกับการค้นหา: ตัดช่องว่าง จุด และขีด"""
    return re.sub(r'[\\s.()\\-_/]+', '', str(value or '')).lower()


def search_staff(keyword: str, staff: list) -> list:
    """ค้นหาจากชื่อ ตำแหน่ง หรือชื่อเล่น"""
    key = _search_key(keyword)
    if not key:
        return []

    results = []
    for person in staff:
        haystacks = (
            person.get('name', ''),
            person.get('position', ''),
            person.get('nickname', ''),
        )
        if any(key in _search_key(value) for value in haystacks):
            results.append(person)
    return results


def staff_by_team(team: int, staff: list) -> list:
    return [p for p in staff if p.get('team') == team]


def parse_team_command(text: str) -> Optional[int]:
    """รองรับ ชุดปฏิบัติการ1 / ชป.ที่1 / ชป.1 / ชุด1"""
    compact = re.sub(r'\\s+', '', text)
    m = re.fullmatch(
        r'(?:ชุดปฏิบัติการ(?:ที่)?|ชป\\.(?:ที่)?|ชป(?:ที่)?|ชุด(?:ที่)?)[.]?([12])',
        compact,
        re.IGNORECASE
    )
    return int(m.group(1)) if m else None


def parse_duty_day(text: str) -> Optional[int]:
    """
    รองรับ:
    - วันที่ 18
    - เวรวันที่ 18
    - ใครเข้าเวรวันที่ 18
    - เวรวันนี้ / ใครเข้าเวรวันนี้
    - วันที่ 18/7/69 หรือ 18 ก.ค. 69 (ใช้เลขวันเท่านั้น)
    """
    compact = text.strip()

    if re.search(r'(?:เวรวันนี้|เข้าเวรวันนี้)', compact):
        return datetime.now(BANGKOK_TZ).day

    if not re.search(r'(?:วันที่|เวร|เข้าเวร)', compact):
        return None

    m = re.search(r'(?:วันที่\\s*)?(\\d{1,2})(?=\\D|$)', compact)
    if not m:
        return None

    day = int(m.group(1))
    return day if 1 <= day <= 31 else None


def build_staff_bubble(person: dict) -> dict:
    team = person.get('team', 0)
    team_text = f'ชุดปฏิบัติการที่ {team}' if team in (1, 2) else 'ฝ่ายสืบสวน'
    color = '#1565C0' if team == 1 else '#7B1FA2' if team == 2 else '#37474F'

    name = person.get('name', '-') or '-'
    position = person.get('position', '-') or '-'
    nickname = person.get('nickname', '-') or '-'
    phone = person.get('phone', '-') or '-'
    image_url = person.get('image_url')

    bubble = {
        'type': 'bubble',
        'size': 'kilo',
        'header': {
            'type': 'box',
            'layout': 'vertical',
            'backgroundColor': color,
            'paddingAll': '13px',
            'contents': [
                _t(name, weight='bold', size='md', color='#FFFFFF', wrap=True),
                _t(team_text, size='xs', color='#FFFFFFcc', margin='xs'),
            ],
        },
        'body': {
            'type': 'box',
            'layout': 'vertical',
            'paddingAll': '12px',
            'spacing': 'sm',
            'contents': [
                _row('👮 ตำแหน่ง', position),
                _row('😊 ชื่อเล่น', nickname),
                _row('📞 เบอร์โทร', phone),
            ],
        },
    }

    if image_url:
        bubble['hero'] = {
            'type': 'image',
            'url': image_url,
            'size': 'full',
            'aspectRatio': '3:4',
            'aspectMode': 'cover',
        }

    return bubble


def build_staff_carousels(people: list, title: str) -> list:
    """แสดงบุคลากรทั้งหมด เป็น carousel ละไม่เกิน 10 คน"""
    if not people:
        return [TextMessage(text=f"❌ ไม่พบข้อมูล {title}")]

    messages = [
        TextMessage(text=f"👮 {title}\\nพบทั้งหมด {len(people)} นาย")
    ]

    # LINE carousel จำกัด 10 bubbles; LINE reply จำกัด 5 messages
    for start in range(0, min(len(people), 40), 10):
        chunk = people[start:start + 10]
        container = {
            'type': 'carousel',
            'contents': [build_staff_bubble(p) for p in chunk]
        }
        messages.append(
            FlexMessage(
                alt_text=f'{title} ({start + 1}-{start + len(chunk)})',
                contents=FlexContainer.from_dict(container)
            )
        )

    return messages[:5]


# ─── Continuation text helpers ────────────────────────────────────────────────
LINE_TEXT_LIMIT = 4800  # เผื่อจากขีดจำกัดข้อความ LINE 5,000 ตัวอักษร


def _record_text_line(rec: dict, index: int) -> str:
    """แปลงข้อมูลหนึ่งรายการเป็นข้อความสั้นสำหรับรายการที่เกิน 10 ราย"""
    name = rec.get('name', '-') or '-'
    charge = rec.get('charge', '') or ''
    date = rec.get('date', '') or ''
    location = rec.get('location', '') or ''

    parts = [f"{index}. {name}"]
    if charge:
        parts.append(f"   ข้อหา: {charge}")
    if date:
        parts.append(f"   วันที่: {date}")
    if location:
        parts.append(f"   สถานที่: {location}")
    return "\n".join(parts)


def _chunk_text(header: str, blocks: list[str]) -> list[TextMessage]:
    """แบ่งข้อความยาวเป็นหลายข้อความ โดยไม่เกินขีดจำกัดของ LINE"""
    if not blocks:
        return []

    messages = []
    current = header.strip()

    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > LINE_TEXT_LIMIT and current:
            messages.append(TextMessage(text=current))
            current = block
        else:
            current = candidate

    if current:
        messages.append(TextMessage(text=current))

    return messages


def build_remaining_text_messages(records: list, start_index: int = 11,
                                  title: str = "รายชื่อเพิ่มเติม") -> list:
    """สร้างข้อความธรรมดาสำหรับข้อมูลตั้งแต่รายการที่ 11 เป็นต้นไป"""
    if not records:
        return []

    blocks = [
        _record_text_line(rec, start_index + offset)
        for offset, rec in enumerate(records)
    ]
    header = f"📋 {title}\nแสดงรายการที่ {start_index}-{start_index + len(records) - 1}"
    return _chunk_text(header, blocks)


def build_summary_messages(title: str, rows: list) -> list:
    """
    ส่ง Flex สรุป 10 รายแรก และรายการที่เหลือเป็นข้อความธรรมดา
    LINE reply ได้สูงสุด 5 messages จึงใช้ Flex 1 + ข้อความต่อเนื่องสูงสุด 4
    """
    messages = [
        build_summary_flex(title, len(rows), _cat_count(rows), rows)
    ]
    remaining = build_remaining_text_messages(
        rows[10:],
        start_index=11,
        title=f"{title} — รายการต่อจากการ์ด"
    )
    messages.extend(remaining[:4])
    return messages[:5]


def build_search_messages(prefix: str, rows: list, total: int, alt: str) -> list:
    """ผลค้นหา: ข้อความสรุป + การ์ดสูงสุด 10 + รายการที่เหลือเป็นข้อความ"""
    messages = [
        TextMessage(
            text=f"{prefix}\nพบทั้งหมด {total} ราย "
                 f"(แสดงการ์ด {min(len(rows), 10)} รายแรก)"
        ),
        build_carousel(rows[:10], alt),
    ]
    remaining = build_remaining_text_messages(
        rows[10:],
        start_index=11,
        title="ผลค้นหาเพิ่มเติม"
    )
    messages.extend(remaining[:3])
    return messages[:5]


# ─── Command router ───────────────────────────────────────────────────────────
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
    "👮 bot บุคลากร <ชื่อ/ตำแหน่ง/ชื่อเล่น>\n"
    "   ค้นหาบุคลากรฝ่ายสืบสวน\n\n"
    "👥 bot ชป.1 หรือ bot ชุด2\n"
    "   แสดงสมาชิกชุดปฏิบัติการ\n\n"
    "🗓️ bot ใครเข้าเวรวันที่ 18\n"
    "   วันคู่ = ชุด 1, วันคี่ = ชุด 2\n\n"
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
    "   ดูสถิติภาพรวม\n\n"
    "🔄 bot รีเฟรช\n"
    "   โหลดข้อมูลใหม่จาก Apps Script\n"
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


def _cat_count(rows: list) -> dict:
    c = {'ยาเสพติด':0,'หมายจับ':0,'คดีอื่นๆ':0}
    for r in rows:
        k = categorise(r.get('charge',''))
        c[k] = c.get(k,0) + 1
    return c


def handle_message(text: str) -> list:
    t = text.strip()
    log.info(f'[message] text={t!r}')
    data, staff = get_data()
    log.info(
        f'[message] cache_records={len(data)} staff_records={len(staff)}'
    )

    # ── ยังโหลดไม่เสร็จ ──
    if not data and not staff:
        return [TextMessage(text=(
            "⏳ ระบบกำลังโหลดข้อมูลจาก Google Sheets\n"
            "กรุณาลองใหม่อีกครั้งใน 1-2 นาที"
        ))]

    # ── ช่วยเหลือ ──
    if re.match(r'^(ช่วย|help|ช่วยเหลือ|คำสั่ง)$', t, re.IGNORECASE):
        return [TextMessage(text=HELP_TEXT)]

    # ── สถิติ ──
    if re.match(r'^สถิติ$', t):
        total, by_cat, by_year = statistics(data)
        top = sorted(by_year.items(), reverse=True)[:5]
        yr_txt = '\n'.join(f"  ปี {yr}: {cnt:,} ราย" for yr, cnt in top)
        msg = (
            f"📊 สถิติรวมทั้งหมด\n━━━━━━━━━━━━━━━━\n"
            f"รวม: {total:,} ราย\n"
            f"ยาเสพติด: {by_cat.get('ยาเสพติด',0):,} ราย\n"
            f"หมายจับ: {by_cat.get('หมายจับ',0):,} ราย\n"
            f"คดีอื่นๆ: {by_cat.get('คดีอื่นๆ',0):,} ราย\n\n"
            f"📆 5 ปีล่าสุด\n{yr_txt}"
        )
        return [TextMessage(text=msg)]

    # ── รีเฟรช ──
    if re.match(r'^(รีเฟรช|refresh|โหลดใหม่)$', t, re.IGNORECASE):
        threading.Thread(target=lambda: get_data(force=True), daemon=True).start()
        return [TextMessage(text="🔄 กำลังโหลดข้อมูลใหม่จาก Apps Script...")]

    # ── ชุดปฏิบัติการ ──
    team = parse_team_command(t)
    if team:
        people = staff_by_team(team, staff)
        return build_staff_carousels(
            people, f"ชุดปฏิบัติการที่ {team}"
        )

    # ── เวรวันคู่/วันคี่ ──
    duty_day = parse_duty_day(t)
    if duty_day:
        team = 1 if duty_day % 2 == 0 else 2
        people = staff_by_team(team, staff)
        parity = "วันคู่" if team == 1 else "วันคี่"
        return build_staff_carousels(
            people,
            f"เวรวันที่ {duty_day} ({parity}) — ชุดปฏิบัติการที่ {team}"
        )

    # ── ค้นหาบุคลากรแบบระบุคำสั่ง ──
    m = re.match(r'^(?:บุคลากร|เจ้าหน้าที่|ตำรวจ)\s+(.+)$', t)
    if m:
        keyword = m.group(1).strip()
        people = search_staff(keyword, staff)
        if not people:
            return [TextMessage(text=f"❌ ไม่พบบุคลากร '{keyword}'")]
        return build_staff_carousels(
            people, f"ผลค้นหาบุคลากร: {keyword}"
        )

    # ── ค้นหาชื่อ ──
    m = re.match(r'^ค้นหา\s+(.+)$', t)
    if m:
        kw = m.group(1).strip()
        rows, total = search_name(kw, data)
        if not rows:
            return [TextMessage(text=f"❌ ไม่พบ '{kw}' ในระบบ")]
        return build_search_messages(
            f"🔍 ค้นหา: {kw}", rows, total, f"ค้นหา: {kw}"
        )

    # ── เดือน (explicit) ──
    m = re.match(r'^เดือน\s+(.+)$', t)
    if m:
        mn, yr = parse_month_year(m.group(1))
        if mn and yr:
            rows = monthly(mn, yr, data)
            if not rows:
                return [TextMessage(text="❌ ไม่พบข้อมูลเดือนนั้น")]
            abbr = MONTH_NUM_TO_ABBR.get(mn, '')
            return build_summary_messages(f"สรุป {abbr} {yr}", rows)
        return [TextMessage(text="❓ รูปแบบเดือนไม่ถูกต้อง เช่น เดือน ก.ค. 69")]

    # ── เดือน/ปี พิมพ์ตรง ──
    if MONTH_YEAR_DIRECT_RE.match(t):
        mn, yr = parse_month_year(t)
        if mn and yr:
            rows = monthly(mn, yr, data)
            if not rows:
                return [TextMessage(text="❌ ไม่พบข้อมูลเดือนนั้น")]
            abbr = MONTH_NUM_TO_ABBR.get(mn, '')
            return build_summary_messages(f"สรุป {abbr} {yr}", rows)

    # ── ปี ──
    m = re.match(r'^ปี\s*(25[5-9]\d|26\d{2}|[5-9]\d)$', t)
    if m:
        yr  = int(m.group(1))
        ybe = yr + 2500 if yr < 100 else yr
        rows = yearly(ybe, data)
        if not rows:
            return [TextMessage(text=f"❌ ไม่พบข้อมูลปี {ybe}")]
        return build_summary_messages(f"สรุปปี {ybe}", rows)

    # ── สถานที่ (explicit) ──
    m = re.match(r'^สถานที่\s+(.+)$', t)
    if m:
        kw = m.group(1).strip()
        rows, total = search_location(kw, data)
        if not rows:
            return [TextMessage(text=f"❌ ไม่พบสถานที่ '{kw}'")]
        return build_summary_messages(f"📍 {kw}", rows)

    # ── สถานที่ (auto-detect prefix) ──
    if LOCATION_PREFIX_RE.match(t):
        rows, total = search_location(t, data)
        if rows:
            return build_summary_messages(f"📍 {t}", rows)

    # ── ของกลาง ──
    m = re.match(r'^ของกลาง\s+(.+)$', t)
    if m:
        kw = m.group(1).strip()
        rows, total = search_evidence(kw, data)
        if not rows:
            return [TextMessage(text=f"❌ ไม่พบของกลาง '{kw}'")]
        return build_search_messages(
            f"📦 ของกลาง: {kw}", rows, total, f"ของกลาง: {kw}"
        )

    # ── ข้อหา ──
    m = re.match(r'^ข้อหา\s+(.+)$', t)
    if m:
        kw = m.group(1).strip()
        rows, total = search_charge(kw, data)
        if not rows:
            return [TextMessage(text=f"❌ ไม่พบข้อหา '{kw}'")]
        return build_summary_messages(f"⚖️ {kw}", rows)

    # ── ค้นหาบุคลากรอัตโนมัติจากชื่อ/ตำแหน่ง/ชื่อเล่น ──
    if len(t) >= 2:
        people = search_staff(t, staff)
        if people:
            return build_staff_carousels(
                people, f"ผลค้นหาบุคลากร: {t}"
            )

    # ── fallback: ลองค้นหาชื่อ (รองรับพิมพ์ชื่อตรงๆ ไม่ต้องมีคำนำหน้า) ──
    if len(t) >= 2:
        rows, total = search_name(t, data)
        if rows:
            return build_search_messages(
                f"🔍 ค้นหา: {t}", rows, total, f"ค้นหา: {t}"
            )

    return [TextMessage(text=f"❓ ไม่พบ '{t}' ในระบบ\nพิมพ์ bot ช่วยเหลือ เพื่อดูคำสั่งทั้งหมด")]


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
        log.error(f'[reply] {e}')
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
        log.error(f'[push] {e}')


# ─── LINE event handlers ──────────────────────────────────────────────────────
@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event):
    source      = event.source
    source_type = source.type
    push_to     = (getattr(source,'group_id',None) or
                   getattr(source,'room_id',None)  or
                   getattr(source,'user_id',None))
    text = event.message.text.strip()

    if source_type in ('group','room'):
        if not re.match(r'^bot\b', text, re.IGNORECASE):
            return
        text = re.sub(r'^bot\s*','', text, flags=re.IGNORECASE).strip()

    msgs = handle_message(text)
    if not _reply(event.reply_token, msgs) and push_to:
        _push(push_to, msgs)


@handler.add(JoinEvent)
def on_join(event):
    source = event.source
    msgs   = [TextMessage(text=WELCOME_TEXT)]
    if not _reply(event.reply_token, msgs):
        target = getattr(source,'group_id',None) or getattr(source,'room_id',None)
        if target:
            _push(target, msgs)


@handler.add(FollowEvent)
def on_follow(event):
    source = event.source
    msgs   = [TextMessage(text=WELCOME_TEXT)]
    if not _reply(event.reply_token, msgs):
        uid = getattr(source,'user_id',None)
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
    # ทำงานใน worker process จริง จึงใช้เริ่ม cache loader ได้อย่างปลอดภัย
    if not _cache_data:
        _start_fetch_background()

    sig  = request.headers.get('X-Line-Signature','')
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
    threading.Thread(target=_do_fetch, daemon=True).start()
    return 'refreshing...', 200


@app.route('/')
def index():
    # Render เรียกหน้า / หลัง worker พร้อมใช้งาน จึงใช้จุดนี้ preload cache
    if not _cache_data:
        _start_fetch_background()

    with _cache_lock:
        n = len(_cache_data)
        staff_n = len(_staff_data)
        ts = _cache_ts
    age = int(time.time() - ts) if ts else -1
    return (
        f'LINE Bot สน.บางชัน v5.0 | arrests {n} | '
        f'staff {staff_n} | cache age {age}s'
    ), 200


@app.route('/debug')
def debug():
    """ทดสอบการเชื่อมต่อ Apps Script และแสดง raw response"""
    import json as _json
    try:
        params = {'key': APPS_SCRIPT_KEY} if APPS_SCRIPT_KEY else {}
        resp   = requests.get(
            APPS_SCRIPT_URL, params=params,
            timeout=FETCH_TIMEOUT, allow_redirects=True
        )
        ct      = resp.headers.get('Content-Type', '')
        preview = resp.text[:500]
        try:
            payload  = resp.json()
            n_rec    = len(payload.get('records', []))
            n_staff  = len(payload.get('staff', []))
            sheets   = payload.get('sheetsProcessed', '?')
            err_msg  = payload.get('error', None)
            status   = (
                f'OK — arrests {n_rec}, staff {n_staff}, {sheets} sheets'
            )
            if err_msg:
                status = f'ERROR: {err_msg}'
        except Exception:
            n_rec  = 0
            status = 'JSON parse failed'

        lines = [
            f'=== Debug: Apps Script ===',
            f'URL: {resp.url[:100]}',
            f'HTTP: {resp.status_code}',
            f'Content-Type: {ct}',
            f'Apps Script status: {status}',
            f'Cache: arrests {len(_cache_data)}, staff {len(_staff_data)}, '
            f'age {int(time.time()-_cache_ts) if _cache_ts else -1}s',
            f'',
            f'--- Response preview (first 500 chars) ---',
            preview,
        ]
        return '\n'.join(lines), 200, {'Content-Type': 'text/plain; charset=utf-8'}
    except Exception as e:
        return f'debug error: {e}', 500


# ─── Startup preload ──────────────────────────────────────────────────────────
# ไม่เริ่ม thread ตอน import เพราะ Gunicorn อาจ import ใน master ก่อน fork worker
# การ preload จะเริ่มจาก route / หรือ /callback ซึ่งทำงานใน worker processจริง

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

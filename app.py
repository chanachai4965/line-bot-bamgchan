#!/usr/bin/env python3
"""
LINE Bot — ระบบสืบค้นผลการจับกุม สน.บางชัน
v6.5 — เพิ่มสถานะฐานข้อมูลและค้นหาผู้ต้องหาอัตโนมัติหลังโหลดเสร็จ
ดึงข้อมูลจาก Google Apps Script Web App → cache ใน RAM → ตอบ Flex Message
"""

import os
import re
import time
import logging
import threading
from datetime import datetime, timedelta, date
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
ARREST_CACHE_TTL           = int(os.environ.get('ARREST_CACHE_TTL', '900'))
STAFF_CACHE_TTL            = int(os.environ.get('STAFF_CACHE_TTL', '300'))
ARREST_FETCH_TIMEOUT       = int(os.environ.get('ARREST_FETCH_TIMEOUT', '180'))
STAFF_FETCH_TIMEOUT        = int(os.environ.get('STAFF_FETCH_TIMEOUT', '45'))
DETAIL_LIMIT               = 30

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler       = WebhookHandler(LINE_CHANNEL_SECRET)

# ─── แยก Cache: ข้อมูลคดี / ข้อมูลบุคลากร ───────────────────────────────────
_arrest_data: list = []
_staff_data: list = []
_arrest_ts: float = 0.0
_staff_ts: float = 0.0

_arrest_lock = threading.RLock()
_staff_lock = threading.RLock()
_arrest_state_lock = threading.Lock()
_staff_state_lock = threading.Lock()
_arrest_fetching = False
_staff_fetching = False

# คำค้นผู้ต้องหาที่ส่งเข้ามาขณะฐานคดียังโหลดไม่เสร็จ
_pending_arrest_searches: list[dict] = []
_pending_lock = threading.RLock()
MAX_PENDING_SEARCHES = 30


def _request_api(mode: str, timeout: int) -> dict:
    params = {'mode': mode}
    if APPS_SCRIPT_KEY:
        params['key'] = APPS_SCRIPT_KEY
    log.info(f'[fetch:{mode}] GET Apps Script timeout={timeout}s')
    resp = requests.get(
        APPS_SCRIPT_URL,
        params=params,
        timeout=timeout,
        allow_redirects=True,
    )
    log.info(f'[fetch:{mode}] HTTP {resp.status_code}')
    if 'html' in resp.headers.get('Content-Type', '').lower():
        raise RuntimeError('Apps Script ส่ง HTML กลับมาแทน JSON')
    resp.raise_for_status()
    payload = resp.json()
    if payload.get('error'):
        raise RuntimeError(payload['error'])
    return payload


def _normalise(r: dict) -> dict:
    return {
        'sheet': r.get('sheet', ''),
        'year_be': int(r.get('yearBe', 0) or 0),
        'month_num': int(r.get('monthNum', 0) or 0),
        'month_abbr': r.get('monthAbbr', ''),
        'seq': r.get('seq', ''),
        'date': r.get('date', ''),
        'group': r.get('group', ''),
        'charge': r.get('charge', ''),
        'name': r.get('name', ''),
        'nickname': r.get('nickname', ''),
        'age': r.get('age', ''),
        'pid': r.get('pid', ''),
        'evidence': r.get('evidence', ''),
        'location': r.get('location', ''),
        'note': r.get('note', ''),
        'image_url': r.get('imageUrl'),
    }


def _normalise_staff(r: dict) -> dict:
    try:
        team = int(r.get('team', 0) or 0)
    except (TypeError, ValueError):
        team = 0
    return {
        'name': str(r.get('name', '') or '').strip(),
        'position': str(r.get('position', '') or '').strip(),
        'phone': str(r.get('phone', '') or '').strip(),
        'nickname': str(r.get('nickname', '') or '').strip(),
        'image_url': r.get('imageUrl'),
        'note': str(r.get('note', '') or '').strip(),
        'team': team,
        'controller': bool(r.get('controller', False)),
        'team_order': int(r.get('teamOrder', 999) or 999),
    }


def fetch_arrests() -> list:
    payload = _request_api('arrests', ARREST_FETCH_TIMEOUT)
    rows = [_normalise(r) for r in payload.get('records', [])]
    log.info(f'[fetch:arrests] ✅ {len(rows)} records')
    return rows


def fetch_staff() -> list:
    payload = _request_api('staff', STAFF_FETCH_TIMEOUT)
    rows = [_normalise_staff(r) for r in payload.get('staff', [])]
    log.info(f'[fetch:staff] ✅ {len(rows)} people')
    return rows


def _do_fetch_arrests():
    global _arrest_ts, _arrest_fetching
    with _arrest_state_lock:
        if _arrest_fetching:
            return
        _arrest_fetching = True
    try:
        rows = fetch_arrests()
        if rows:
            with _arrest_lock:
                _arrest_data.clear()
                _arrest_data.extend(rows)
                _arrest_ts = time.time()

            # ตอบคำค้นที่ค้างไว้โดยอัตโนมัติ หลังฐานคดีโหลดเสร็จ
            try:
                _process_pending_arrest_searches(rows)
            except Exception as e:
                log.error(f'[pending-search] {e}', exc_info=True)
    except requests.exceptions.Timeout:
        log.error(f'[fetch:arrests] timeout after {ARREST_FETCH_TIMEOUT}s')
    except Exception as e:
        log.error(f'[fetch:arrests] {e}', exc_info=True)
    finally:
        with _arrest_state_lock:
            _arrest_fetching = False


def _do_fetch_staff():
    global _staff_ts, _staff_fetching
    with _staff_state_lock:
        if _staff_fetching:
            return
        _staff_fetching = True
    try:
        rows = fetch_staff()
        if rows:
            with _staff_lock:
                _staff_data.clear()
                _staff_data.extend(rows)
                _staff_ts = time.time()
    except requests.exceptions.Timeout:
        log.error(f'[fetch:staff] timeout after {STAFF_FETCH_TIMEOUT}s')
    except Exception as e:
        log.error(f'[fetch:staff] {e}', exc_info=True)
    finally:
        with _staff_state_lock:
            _staff_fetching = False


def _start_arrest_fetch() -> bool:
    with _arrest_state_lock:
        if _arrest_fetching:
            return False
    threading.Thread(target=_do_fetch_arrests, daemon=True, name='fetch-arrests').start()
    return True


def _start_staff_fetch() -> bool:
    with _staff_state_lock:
        if _staff_fetching:
            return False
    threading.Thread(target=_do_fetch_staff, daemon=True, name='fetch-staff').start()
    return True


def get_arrests(force: bool = False) -> list:
    with _arrest_lock:
        snapshot = list(_arrest_data)
        ts = _arrest_ts
    if force:
        _do_fetch_arrests()
        with _arrest_lock:
            return list(_arrest_data)
    if snapshot:
        if ts and time.time() - ts >= ARREST_CACHE_TTL:
            _start_arrest_fetch()
        return snapshot
    _start_arrest_fetch()
    return []


def get_staff(force: bool = False, wait_if_empty: bool = False) -> list:
    with _staff_lock:
        snapshot = list(_staff_data)
        ts = _staff_ts
    if force:
        _do_fetch_staff()
        with _staff_lock:
            return list(_staff_data)
    if snapshot:
        if ts and time.time() - ts >= STAFF_CACHE_TTL:
            _start_staff_fetch()
        return snapshot
    if wait_if_empty:
        _do_fetch_staff()
        with _staff_lock:
            return list(_staff_data)
    _start_staff_fetch()
    return []


def _format_cache_age(ts: float) -> str:
    if not ts:
        return "ยังไม่เคยโหลด"
    age = max(0, int(time.time() - ts))
    if age < 60:
        return f"{age} วินาที"
    return f"{age // 60} นาที {age % 60} วินาที"


def database_status_text() -> str:
    with _arrest_lock:
        arrest_count = len(_arrest_data)
        arrest_ts = _arrest_ts
    with _staff_lock:
        staff_count = len(_staff_data)
        staff_ts = _staff_ts
    with _arrest_state_lock:
        arrest_loading = _arrest_fetching
    with _staff_state_lock:
        staff_loading = _staff_fetching
    with _pending_lock:
        pending_count = len(_pending_arrest_searches)

    arrest_state = "กำลังโหลด ⏳" if arrest_loading else (
        "พร้อม ✅" if arrest_count else "ยังไม่พร้อม ❌"
    )
    staff_state = "กำลังโหลด ⏳" if staff_loading else (
        "พร้อม ✅" if staff_count else "ยังไม่พร้อม ❌"
    )

    return (
        "📡 สถานะฐานข้อมูล\n"
        "━━━━━━━━━━━━━━━━\n"
        f"👮 บุคลากร: {staff_state}\n"
        f"   จำนวน {staff_count:,} คน\n"
        f"   อัปเดตเมื่อ {_format_cache_age(staff_ts)} ที่แล้ว\n\n"
        f"🔍 ข้อมูลคดี: {arrest_state}\n"
        f"   จำนวน {arrest_count:,} รายการ\n"
        f"   อัปเดตเมื่อ {_format_cache_age(arrest_ts)} ที่แล้ว\n\n"
        f"🕓 คำค้นรอประมวลผล: {pending_count} รายการ"
    )


def _queue_pending_arrest_search(keyword: str, push_to: Optional[str]) -> bool:
    if not keyword or not push_to:
        return False

    item = {
        "keyword": keyword.strip(),
        "push_to": push_to,
        "queued_at": time.time(),
    }

    with _pending_lock:
        # ไม่เพิ่มคำค้นซ้ำจากปลายทางเดียวกัน
        for existing in _pending_arrest_searches:
            if (
                existing.get("push_to") == push_to
                and existing.get("keyword") == item["keyword"]
            ):
                return True

        if len(_pending_arrest_searches) >= MAX_PENDING_SEARCHES:
            _pending_arrest_searches.pop(0)
        _pending_arrest_searches.append(item)

    _start_arrest_fetch()
    return True


def _process_pending_arrest_searches(data: list) -> None:
    with _pending_lock:
        pending = list(_pending_arrest_searches)
        _pending_arrest_searches.clear()

    if not pending:
        return

    log.info(f'[pending-search] processing {len(pending)} searches')

    for item in pending:
        keyword = item.get("keyword", "").strip()
        push_to = item.get("push_to")
        if not keyword or not push_to:
            continue

        rows, total = search_name(keyword, data)
        if rows:
            messages = build_search_messages(
                f"🔍 ค้นหา: {keyword}",
                rows,
                total,
                f"ค้นหา: {keyword}",
            )
        else:
            messages = [
                TextMessage(
                    text=(
                        f"❌ โหลดฐานข้อมูลคดีเสร็จแล้ว "
                        f"แต่ไม่พบ '{keyword}' ในระบบ"
                    )
                )
            ]
        _push(push_to, messages)


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
    """ค้นหาบุคลากรจากชื่อ ตำแหน่ง ชื่อเล่น หรือเบอร์โทร"""
    key = _search_key(keyword)
    phone_key = re.sub(r'\D', '', str(keyword or ''))
    if not key:
        return []

    results = []
    for person in staff:
        haystacks = (
            person.get('name', ''),
            person.get('position', ''),
            person.get('nickname', ''),
            person.get('phone', ''),
        )
        text_match = any(key in _search_key(value) for value in haystacks)
        person_phone = re.sub(r'\D', '', person.get('phone', ''))
        phone_match = bool(phone_key and len(phone_key) >= 4 and phone_key in person_phone)
        if text_match or phone_match:
            results.append(person)

    return sort_staff(results)


def _rank_weight(name: str, position: str) -> int:
    """เรียงยศ/ตำแหน่งผู้บังคับบัญชาก่อน"""
    text = f"{name} {position}".replace(" ", "")
    rules = [
        ("ผกก.", 10),
        ("รองผกก.", 20),
        ("สว.", 30),
        ("รองสว.", 40),
        ("ร.ต.อ.", 50),
        ("ร.ต.ท.", 60),
        ("ร.ต.ต.", 70),
        ("ด.ต.", 80),
        ("จ.ส.ต.", 90),
        ("ส.ต.อ.", 100),
        ("ส.ต.ท.", 110),
        ("ส.ต.ต.", 120),
    ]
    for token, weight in rules:
        if token.replace(" ", "") in text:
            return weight
    return 999


def sort_staff(people: list) -> list:
    """เรียงผู้ควบคุมชุดก่อน แล้วตามลำดับในชีต ชป."""
    return sorted(
        people,
        key=lambda p: (
            0 if p.get('controller') else 1,
            int(p.get('team_order', 999) or 999),
            _rank_weight(p.get('name', ''), p.get('position', '')),
            _search_key(p.get('name', '')),
        )
    )


def staff_by_team(team: int, staff: list) -> list:
    """สมาชิกทั้งชุด รวมผู้ควบคุมชุด"""
    return sort_staff([p for p in staff if p.get('team') == team])


def duty_staff_by_team(team: int, staff: list) -> list:
    """สมาชิกผู้เข้าเวร ไม่รวมผู้ควบคุมชุด"""
    members = staff_by_team(team, staff)
    if any(p.get('controller') for p in members):
        return [p for p in members if not p.get('controller')]
    return members[1:] if len(members) > 1 else []


def parse_team_command(text: str) -> Optional[int]:
    """รองรับ ชุดปฏิบัติการ1 / ชป.ที่1 / ชป.1 / ชุด1"""
    compact = re.sub(r'\s+', '', text.strip())
    m = re.fullmatch(
        r'(?:ชุดปฏิบัติการ(?:ที่)?|ชป\.?(?:ที่)?|ชุด(?:ที่)?)[.]?([12])',
        compact,
        re.IGNORECASE
    )
    return int(m.group(1)) if m else None


THAI_MONTH_LOOKUP = {
    'ม.ค.': 1, 'มค': 1, 'มกราคม': 1,
    'ก.พ.': 2, 'กพ': 2, 'กุมภาพันธ์': 2,
    'มี.ค.': 3, 'มีค': 3, 'มีนาคม': 3,
    'เม.ย.': 4, 'เมย': 4, 'เมษายน': 4,
    'พ.ค.': 5, 'พค': 5, 'พฤษภาคม': 5,
    'มิ.ย.': 6, 'มิย': 6, 'มิถุนายน': 6,
    'ก.ค.': 7, 'กค': 7, 'กรกฎาคม': 7,
    'ส.ค.': 8, 'สค': 8, 'สิงหาคม': 8,
    'ก.ย.': 9, 'กย': 9, 'กันยายน': 9,
    'ต.ค.': 10, 'ตค': 10, 'ตุลาคม': 10,
    'พ.ย.': 11, 'พย': 11, 'พฤศจิกายน': 11,
    'ธ.ค.': 12, 'ธค': 12, 'ธันวาคม': 12,
}
THAI_MONTH_FULL = {
    1: 'มกราคม', 2: 'กุมภาพันธ์', 3: 'มีนาคม', 4: 'เมษายน',
    5: 'พฤษภาคม', 6: 'มิถุนายน', 7: 'กรกฎาคม', 8: 'สิงหาคม',
    9: 'กันยายน', 10: 'ตุลาคม', 11: 'พฤศจิกายน', 12: 'ธันวาคม',
}
THAI_WEEKDAY = {
    0: 'วันจันทร์', 1: 'วันอังคาร', 2: 'วันพุธ', 3: 'วันพฤหัสบดี',
    4: 'วันศุกร์', 5: 'วันเสาร์', 6: 'วันอาทิตย์',
}


def _normalise_be_year(year: Optional[int], fallback_ad: int) -> int:
    if year is None:
        return fallback_ad
    if year < 100:
        year += 2500
    if year >= 2400:
        year -= 543
    return year


def parse_duty_date(text: str) -> Optional[date]:
    """
    ตรวจจับคำถามเกี่ยวกับเวรและวัน เช่น:
    เวร 20 ก.ค.69 / เวรวันที่ 20 / ใครเข้าเวร 20/7/69
    วันนี้ใครเข้าเวร / เวรพรุ่งนี้ / มะรืนใครอยู่เวร
    """
    raw = text.strip()
    compact = re.sub(r'\s+', '', raw)
    now = datetime.now(BANGKOK_TZ).date()

    duty_intent = re.search(
        r'(เวร|เข้าเวร|อยู่เวร|ปฏิบัติหน้าที่|ชุดไหนทำงาน|ใครทำงาน)',
        raw
    )
    if not duty_intent:
        return None

    if 'มะรืน' in compact:
        return now + timedelta(days=2)
    if 'พรุ่งนี้' in compact:
        return now + timedelta(days=1)
    if 'วันนี้' in compact:
        return now
    if 'เมื่อวาน' in compact:
        return now - timedelta(days=1)

    # 20/7/69, 20-7-2569, 20.7.69
    m = re.search(r'(?<!\d)(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{2,4})(?!\d)', raw)
    if m:
        day, month, year = map(int, m.groups())
        year_ad = _normalise_be_year(year, now.year)
        try:
            return date(year_ad, month, day)
        except ValueError:
            return None

    # 20 ก.ค. 69 / วันที่ 20 กรกฎาคม 2569
    month_tokens = sorted(THAI_MONTH_LOOKUP, key=len, reverse=True)
    month_pattern = '|'.join(re.escape(x) for x in month_tokens)
    m = re.search(
        rf'(?<!\d)(\d{{1,2}})\s*(?:วันที่)?\s*({month_pattern})\s*\.?\s*(\d{{2,4}})?',
        raw
    )
    if m:
        day = int(m.group(1))
        token = m.group(2).rstrip('.')
        month = THAI_MONTH_LOOKUP.get(m.group(2), THAI_MONTH_LOOKUP.get(token))
        year = int(m.group(3)) if m.group(3) else None
        year_ad = _normalise_be_year(year, now.year)
        try:
            return date(year_ad, month, day)
        except (ValueError, TypeError):
            return None

    # เวรวันที่ 20 / ใครเข้าเวรวัน 20 / เวร 20
    m = re.search(r'(?:วันที่|วัน|เวร)\s*(\d{1,2})(?!\d)', raw)
    if not m:
        m = re.search(r'เข้าเวร\s*(\d{1,2})(?!\d)', raw)
    if m:
        day = int(m.group(1))
        try:
            return date(now.year, now.month, day)
        except ValueError:
            return None

    return None


def duty_title(duty_date: date, team: int) -> str:
    be_year = duty_date.year + 543
    weekday = THAI_WEEKDAY[duty_date.weekday()]
    parity = 'วันคู่' if duty_date.day % 2 == 0 else 'วันคี่'
    return (
        f"เวร{weekday}ที่ {duty_date.day} "
        f"{THAI_MONTH_FULL[duty_date.month]} {be_year} "
        f"({parity}) — ชุดปฏิบัติการที่ {team}"
    )


def _phone_uri(phone: str) -> Optional[str]:
    digits = re.sub(r'\D', '', phone or '')
    return f"tel:{digits}" if len(digits) >= 9 else None


def build_staff_bubble(person: dict) -> dict:
    team = person.get('team', 0)
    is_controller = bool(person.get('controller'))
    if team in (1, 2):
        team_text = (
            f'ผู้ควบคุมชุดปฏิบัติการที่ {team}'
            if is_controller else f'ชุดปฏิบัติการที่ {team}'
        )
    else:
        team_text = 'ฝ่ายสืบสวน'

    if is_controller:
        duty_text = 'ผู้ควบคุมชุด — ไม่เข้าเวร'
    else:
        duty_text = 'เข้าเวรวันคู่' if team == 1 else 'เข้าเวรวันคี่' if team == 2 else '-'
    color = '#1565C0' if team == 1 else '#7B1FA2' if team == 2 else '#37474F'

    name = person.get('name', '-') or '-'
    position = person.get('position', '-') or '-'
    nickname = person.get('nickname', '-') or '-'
    phone = person.get('phone', '-') or '-'
    image_url = person.get('image_url')
    tel_uri = _phone_uri(phone)

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
                _row('📅 เวร', duty_text),
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
            'action': {
                'type': 'uri',
                'label': 'เปิดรูปภาพ',
                'uri': image_url,
            },
        }

    if tel_uri:
        bubble['footer'] = {
            'type': 'box',
            'layout': 'vertical',
            'paddingAll': '10px',
            'contents': [{
                'type': 'button',
                'style': 'primary',
                'height': 'sm',
                'action': {
                    'type': 'uri',
                    'label': f'โทร {phone}',
                    'uri': tel_uri,
                },
            }],
        }

    return bubble


def build_staff_carousels(people: list, title: str) -> list:
    """แสดงบุคลากรทั้งหมด เป็น carousel ละไม่เกิน 10 คน"""
    if not people:
        return [TextMessage(text=f"❌ ไม่พบข้อมูล {title}")]

    people = sort_staff(people)
    messages = [
        TextMessage(text=f"👮 {title}\nพบทั้งหมด {len(people)} นาย")
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


def build_main_menu() -> FlexMessage:
    bubble = {
        'type': 'bubble',
        'size': 'mega',
        'header': {
            'type': 'box',
            'layout': 'vertical',
            'backgroundColor': '#173B57',
            'paddingAll': '18px',
            'contents': [
                _t('🚔 เมนู LINE Bot สน.บางชัน',
                   color='#FFFFFF', weight='bold', size='lg'),
                _t('แตะเมนูเพื่อส่งคำสั่ง',
                   color='#FFFFFFcc', size='sm', margin='sm'),
            ],
        },
        'body': {
            'type': 'box',
            'layout': 'vertical',
            'spacing': 'sm',
            'paddingAll': '14px',
            'contents': [
                {
                    'type': 'button', 'style': 'primary',
                    'action': {'type': 'message', 'label': '🚔 เวรวันนี้', 'text': 'เวรวันนี้'}
                },
                {
                    'type': 'button', 'style': 'secondary',
                    'action': {'type': 'message', 'label': '👥 ชุดปฏิบัติการที่ 1', 'text': 'ชป.1'}
                },
                {
                    'type': 'button', 'style': 'secondary',
                    'action': {'type': 'message', 'label': '👥 ชุดปฏิบัติการที่ 2', 'text': 'ชป.2'}
                },
                {
                    'type': 'button', 'style': 'secondary',
                    'action': {'type': 'message', 'label': '📊 สถิติคดี', 'text': 'สถิติ'}
                },
                {
                    'type': 'button',
                    'action': {'type': 'message', 'label': '❓ วิธีใช้งาน', 'text': 'ช่วยเหลือ'}
                },
            ],
        },
    }
    return FlexMessage(
        alt_text='เมนู LINE Bot สน.บางชัน',
        contents=FlexContainer.from_dict(bubble)
    )


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
    "🏠 เมนู\n"
    "   เปิดเมนูหลักแบบกดเลือกได้\n\n"
    "👮 <ชื่อ/ตำแหน่ง/ชื่อเล่น/เบอร์โทร>\n"
    "   เช่น ชนะชัย, รอง ผกก., หนึ่ง, 6780\n\n"
    "👥 ชป.1 / ชป.ที่2 / ชุด1\n"
    "   แสดงสมาชิกชุดปฏิบัติการ\n\n"
    "🗓️ ค้นหาเวรได้หลายรูปแบบ\n"
    "   เวรวันนี้, ใครเข้าเวรพรุ่งนี้\n"
    "   เวร 20 ก.ค.69, เข้าเวร 20/7/69\n"
    "   วันคู่ = ชุด 1, วันคี่ = ชุด 2\n\n"
    "🔍 ค้นหา <ชื่อผู้ต้องหา>\n"
    "📍 สถานที่ <สถานที่จับกุม>\n"
    "📅 เดือน ก.ค. 69\n"
    "📆 ปี 2569\n"
    "📦 ของกลาง <สิ่งของ>\n"
    "⚖️ ข้อหา <ข้อหา>\n"
    "📊 สถิติ\n"
    "🔄 รีเฟรช\n"
    "📡 สถานะ\n"
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


def handle_message(text: str, push_to: Optional[str] = None) -> list:
    t = text.strip()
    log.info(f'[message] text={t!r}')
    # โหลดบุคลากรก่อน เพราะมีขนาดเล็กและใช้เวลาสั้น
    staff = get_staff(wait_if_empty=True)
    data = get_arrests()
    log.info(
        f'[message] arrest_records={len(data)} staff_records={len(staff)}'
    )

    # ── เมนูหลัก ──
    if re.match(r'^(เมนู|menu|หน้าหลัก|home)$', t, re.IGNORECASE):
        return [build_main_menu()]

    # ── ช่วยเหลือ ──
    if re.match(r'^(ช่วย|help|ช่วยเหลือ|คำสั่ง)$', t, re.IGNORECASE):
        return [TextMessage(text=HELP_TEXT)]

    # ── สถานะฐานข้อมูล ──
    if re.match(r'^(สถานะ|status|เช็กสถานะ|ตรวจสอบสถานะ)$', t, re.IGNORECASE):
        return [TextMessage(text=database_status_text())]

    # ── สถิติ ──
    if re.match(r'^สถิติ$', t):
        if not data:
            return [TextMessage(text="⏳ ข้อมูลคดีกำลังโหลด กรุณาลองใหม่อีกครั้งใน 1-2 นาที")]
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
        _start_staff_fetch()
        _start_arrest_fetch()
        return [TextMessage(text=(
            "🔄 เริ่มโหลดข้อมูลใหม่แล้ว\n"
            "• บุคลากรจะพร้อมก่อน\n"
            "• ข้อมูลคดีโหลดแยกในพื้นหลัง"
        ))]

    # ── ชุดปฏิบัติการ ──
    team = parse_team_command(t)
    if team:
        people = staff_by_team(team, staff)
        return build_staff_carousels(
            people, f"ชุดปฏิบัติการที่ {team}"
        )

    # ── เวรวันคู่/วันคี่: ค้นจากฐานบุคลากรก่อนเสมอ ──
    duty_date = parse_duty_date(t)
    if duty_date:
        team = 1 if duty_date.day % 2 == 0 else 2
        people = duty_staff_by_team(team, staff)
        title = duty_title(duty_date, team) + " (ไม่รวมผู้ควบคุมชุด)"
        return build_staff_carousels(
            people,
            title
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

    # ── ค้นหาบุคลากรอัตโนมัติจากชื่อ/ตำแหน่ง/ชื่อเล่น/เบอร์โทร ──
    # ทำก่อนตรวจฐานคดี เพื่อให้ค้นหาบุคลากรได้ แม้ข้อมูลคดียังโหลดไม่เสร็จ
    if len(t) >= 2:
        people = search_staff(t, staff)
        if people:
            return build_staff_carousels(
                people, f"ผลค้นหาบุคลากร: {t}"
            )

    # คำสั่งตั้งแต่ส่วนนี้ต้องใช้ฐานข้อมูลคดี
    if not data:
        # เก็บคำค้นผู้ต้องหาไว้ แล้วตอบอัตโนมัติเมื่อโหลดเสร็จ
        pending_keyword = t
        m_pending = re.match(r'^ค้นหา\s+(.+)$', t)
        if m_pending:
            pending_keyword = m_pending.group(1).strip()

        queued = False
        if len(pending_keyword) >= 2:
            queued = _queue_pending_arrest_search(pending_keyword, push_to)

        if queued:
            return [TextMessage(text=(
                "⏳ ข้อมูลคดีกำลังโหลดจาก Google Sheets\n"
                f"ผมบันทึกคำค้น '{pending_keyword}' ไว้แล้ว\n"
                "เมื่อโหลดเสร็จ บอทจะส่งผลค้นหาให้อัตโนมัติ"
            ))]

        return [TextMessage(text=(
            "⏳ ข้อมูลคดีกำลังโหลดจาก Google Sheets\n"
            "ฐานข้อมูลบุคลากรใช้งานได้แล้ว แต่ข้อมูลคดีอาจใช้เวลา 1-2 นาที\n"
            "พิมพ์ 'สถานะ' เพื่อตรวจสอบ"
        ))]

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

    msgs = handle_message(text, push_to=push_to)
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
    # บุคลากรโหลดแยกและเร็วกว่าข้อมูลคดี
    if not _staff_data:
        _start_staff_fetch()
    if not _arrest_data:
        _start_arrest_fetch()

    sig = request.headers.get('X-Line-Signature', '')
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
    _start_staff_fetch()
    _start_arrest_fetch()
    return 'refreshing staff and arrests separately...', 200


@app.route('/')
def index():
    if not _staff_data:
        _start_staff_fetch()
    if not _arrest_data:
        _start_arrest_fetch()
    with _arrest_lock:
        arrest_n = len(_arrest_data)
        arrest_age = int(time.time() - _arrest_ts) if _arrest_ts else -1
    with _staff_lock:
        staff_n = len(_staff_data)
        staff_age = int(time.time() - _staff_ts) if _staff_ts else -1
    return (
        f'LINE Bot สน.บางชัน v6.5 | arrests {arrest_n} age {arrest_age}s | '
        f'staff {staff_n} age {staff_age}s'
    ), 200


@app.route('/debug')
def debug():
    lines = ['=== LINE Bot v6 Debug ===']
    for mode, timeout in [('staff', STAFF_FETCH_TIMEOUT), ('arrests', ARREST_FETCH_TIMEOUT)]:
        try:
            payload = _request_api(mode, timeout)
            count = len(payload.get('staff', [])) if mode == 'staff' else len(payload.get('records', []))
            lines.append(f'{mode}: OK — {count} records')
        except Exception as e:
            lines.append(f'{mode}: ERROR — {e}')
    with _arrest_lock:
        lines.append(f'arrest cache: {len(_arrest_data)}')
    with _staff_lock:
        lines.append(f'staff cache: {len(_staff_data)}')
    return '\n'.join(lines), 200, {'Content-Type': 'text/plain; charset=utf-8'}


# ─── Startup preload ──────────────────────────────────────────────────────────
# ไม่เริ่ม thread ตอน import เพราะ Gunicorn อาจ import ใน master ก่อน fork worker
# การ preload จะเริ่มจาก route / หรือ /callback ซึ่งทำงานใน worker processจริง

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

"""
LINE Bot - ระบบสืบค้นผลการจับกุม สน.บางชัน  v2.0
ติดตั้ง: pip install flask line-bot-sdk
รัน:    python app.py

การเปลี่ยนแปลง v2.0:
  - กลุ่ม LINE ต้องพิมพ์ "bot " นำหน้าก่อนทุกคำสั่ง
  - ใช้ Flex Message แทน Text Message ทุก response
  - ผลลัพธ์ยาว → แสดงสรุปรวมก่อน แล้วตามด้วยรายละเอียด
  - แก้ค้นหาสถานที่ให้แสดงรายชื่อผู้ถูกจับชัดเจน
"""

import os
import re
import json
import sqlite3
import logging
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage, FlexMessage, FlexContainer
)
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent,
    JoinEvent, MemberJoinedEvent, FollowEvent,
    LeaveEvent, UnfollowEvent,
)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
LINE_CHANNEL_SECRET       = os.environ.get("LINE_CHANNEL_SECRET", "YOUR_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "YOUR_CHANNEL_ACCESS_TOKEN")
DB_PATH = os.environ.get("DB_PATH", "arrests.db")

app = Flask(__name__)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

THAI_MONTH_NAME = {
    1: 'มกราคม', 2: 'กุมภาพันธ์', 3: 'มีนาคม', 4: 'เมษายน',
    5: 'พฤษภาคม', 6: 'มิถุนายน', 7: 'กรกฎาคม', 8: 'สิงหาคม',
    9: 'กันยายน', 10: 'ตุลาคม', 11: 'พฤศจิกายน', 12: 'ธันวาคม',
}

# สีธีม
CLR_HEADER_BLUE   = "#1A73E8"
CLR_HEADER_GREEN  = "#1DB446"
CLR_HEADER_ORANGE = "#F4A020"
CLR_HEADER_RED    = "#D32F2F"
CLR_HEADER_PURPLE = "#6A1B9A"
CLR_WHITE         = "#FFFFFF"
CLR_GRAY          = "#888888"
CLR_DARK          = "#333333"
CLR_LIGHT_BG      = "#F8F8F8"


# ─── DB Helper ────────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


DETAIL_LIMIT = 30  # จำนวนรายการที่แสดงในรายละเอียด (ล่าสุดก่อน)


def search_by_name(keyword: str):
    conn = get_conn()
    total = conn.execute(
        "SELECT COUNT(*) FROM arrests WHERE name LIKE ?",
        (f"%{keyword}%",)
    ).fetchone()[0]
    rows = conn.execute(
        """SELECT * FROM arrests
           WHERE name LIKE ?
           ORDER BY year_be DESC, month_num DESC, date_str DESC
           LIMIT ?""",
        (f"%{keyword}%", DETAIL_LIMIT)
    ).fetchall()
    conn.close()
    return rows, total


def _loc_query(conn, like_kw: str):
    """query ทั้ง total และ rows ล่าสุด 30 ราย สำหรับ location"""
    total = conn.execute(
        "SELECT COUNT(*) FROM arrests WHERE location LIKE ? AND location != ''",
        (like_kw,)
    ).fetchone()[0]
    rows = conn.execute(
        """SELECT name, date_str, charge, evidence, location, year_be, month_num
           FROM arrests
           WHERE location LIKE ? AND location != ''
           ORDER BY year_be DESC, month_num DESC, date_str DESC
           LIMIT ?""",
        (like_kw, DETAIL_LIMIT)
    ).fetchall()
    return rows, total


def search_by_location(keyword: str):
    """ค้นหาสถานที่ — ค้น exact แล้ว fallback แบบ token-split, คืน (rows, total)"""
    conn = get_conn()

    # รอบ 1: exact keyword
    rows, total = _loc_query(conn, f"%{keyword}%")

    # รอบ 2: ตัด prefix (ชุมชน/ซอย/ถ. ฯลฯ)
    if not rows and len(keyword) >= 3:
        stripped = re.sub(r'^(ชุมชน|ซอย|ถ\.|ถนน|หมู่บ้าน|สน\.|ริม|แยก)\s*', '', keyword).strip()
        if stripped and stripped != keyword:
            rows, total = _loc_query(conn, f"%{stripped}%")

    # รอบ 3: suffix 3 ตัวท้าย
    if not rows and len(keyword) >= 3:
        rows, total = _loc_query(conn, f"%{keyword[-3:]}%")

    conn.close()
    return rows, total


def summary_by_month(month_num: int, year_be: int):
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM arrests
           WHERE month_num=? AND year_be=?
           ORDER BY date_str""",
        (month_num, year_be)
    ).fetchall()
    conn.close()
    return rows


def summary_by_year(year_be: int):
    conn = get_conn()
    rows = conn.execute(
        """SELECT month_num, month_name, COUNT(*) as cnt
           FROM arrests
           WHERE year_be=?
           GROUP BY month_num
           ORDER BY month_num""",
        (year_be,)
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) FROM arrests WHERE year_be=?", (year_be,)
    ).fetchone()[0]
    conn.close()
    return rows, total


def search_evidence(keyword: str, limit=30):
    conn = get_conn()
    rows = conn.execute(
        """SELECT evidence, COUNT(*) as cnt
           FROM arrests
           WHERE evidence LIKE ? AND evidence != ''
           GROUP BY evidence
           ORDER BY cnt DESC
           LIMIT ?""",
        (f"%{keyword}%", limit)
    ).fetchall()
    conn.close()
    return rows


def search_by_charge(keyword: str):
    conn = get_conn()
    total = conn.execute(
        "SELECT COUNT(*) FROM arrests WHERE charge LIKE ?",
        (f"%{keyword}%",)
    ).fetchone()[0]
    rows = conn.execute(
        """SELECT * FROM arrests
           WHERE charge LIKE ?
           ORDER BY year_be DESC, month_num DESC, date_str DESC
           LIMIT ?""",
        (f"%{keyword}%", DETAIL_LIMIT)
    ).fetchall()
    conn.close()
    return rows, total


def get_overall_stats():
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM arrests").fetchone()[0]
    year_range = conn.execute(
        "SELECT MIN(year_be), MAX(year_be) FROM arrests"
    ).fetchone()
    top_charges = conn.execute(
        """SELECT charge, COUNT(*) as cnt FROM arrests
           WHERE charge != ''
           GROUP BY charge ORDER BY cnt DESC LIMIT 10"""
    ).fetchall()
    top_locations = conn.execute(
        """SELECT location, COUNT(*) as cnt FROM arrests
           WHERE location != ''
           GROUP BY location ORDER BY cnt DESC LIMIT 8"""
    ).fetchall()
    top_evidence = conn.execute(
        """SELECT evidence, COUNT(*) as cnt FROM arrests
           WHERE evidence != ''
           GROUP BY evidence ORDER BY cnt DESC LIMIT 5"""
    ).fetchall()
    yearly = conn.execute(
        """SELECT year_be, COUNT(*) as cnt FROM arrests
           GROUP BY year_be ORDER BY year_be"""
    ).fetchall()
    conn.close()
    return total, year_range, top_charges, top_locations, top_evidence, yearly


# ─── Flex Message Builders ────────────────────────────────────────────────────

def _text(txt, **kw):
    """Helper สร้าง Flex text component"""
    obj = {"type": "text", "text": str(txt), "wrap": True}
    obj.update(kw)
    return obj


def _sep():
    return {"type": "separator", "margin": "sm"}


def _row(label, value, label_color=CLR_GRAY, value_color=CLR_DARK):
    return {
        "type": "box", "layout": "horizontal", "margin": "sm",
        "contents": [
            _text(label, size="sm", color=label_color, flex=3),
            _text(value, size="sm", color=value_color, flex=5, weight="bold"),
        ]
    }


def _bubble(header_text, header_color, body_contents, footer_text=None):
    """สร้าง Flex bubble มาตรฐาน"""
    bubble = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": header_color,
            "paddingAll": "14px",
            "contents": [
                _text(header_text, color=CLR_WHITE, weight="bold", size="lg")
            ]
        },
        "body": {
            "type": "box", "layout": "vertical",
            "spacing": "sm", "paddingAll": "14px",
            "contents": body_contents
        }
    }
    if footer_text:
        bubble["footer"] = {
            "type": "box", "layout": "vertical",
            "backgroundColor": CLR_LIGHT_BG,
            "paddingAll": "10px",
            "contents": [_text(footer_text, size="xs", color=CLR_GRAY, wrap=True)]
        }
    return bubble


def _carousel(bubbles):
    """สร้าง Flex carousel จาก list of bubbles"""
    return {"type": "carousel", "contents": bubbles[:10]}  # LINE limit 10 bubbles


def flex(alt_text: str, container: dict) -> FlexMessage:
    return FlexMessage(
        alt_text=alt_text,
        contents=FlexContainer.from_dict(container)
    )


# ─── Response Builders ────────────────────────────────────────────────────────

def build_help() -> list:
    """เมนูช่วยเหลือ"""
    body = [
        _text("🔍 ค้นหาบุคคล", weight="bold", color=CLR_HEADER_BLUE),
        _text("ค้นหา [ชื่อ/นามสกุล]", size="sm", color=CLR_DARK),
        _text("ตัวอย่าง: ค้นหา สมชาย", size="xs", color=CLR_GRAY),
        _sep(),
        _text("📍 ค้นหาสถานที่จับกุม", weight="bold", color=CLR_HEADER_BLUE),
        _text("สถานที่ [ชื่อสถานที่]", size="sm", color=CLR_DARK),
        _text("ตัวอย่าง: สถานที่ รามอินทรา", size="xs", color=CLR_GRAY),
        _sep(),
        _text("📅 สรุปรายเดือน", weight="bold", color=CLR_HEADER_BLUE),
        _text("เดือน [เดือน] [ปี พ.ศ.]", size="sm", color=CLR_DARK),
        _text("ตัวอย่าง: เดือน ม.ค. 63", size="xs", color=CLR_GRAY),
        _sep(),
        _text("📆 สรุปรายปี", weight="bold", color=CLR_HEADER_BLUE),
        _text("ปี [พ.ศ.]    ตัวอย่าง: ปี 2563", size="sm", color=CLR_DARK),
        _sep(),
        _text("🧪 ของกลาง", weight="bold", color=CLR_HEADER_BLUE),
        _text("ของกลาง [คำค้น]    ตัวอย่าง: ของกลาง ยาบ้า", size="sm", color=CLR_DARK),
        _sep(),
        _text("📋 ข้อหา", weight="bold", color=CLR_HEADER_BLUE),
        _text("ข้อหา [คำค้น]    ตัวอย่าง: ข้อหา เสพยาบ้า", size="sm", color=CLR_DARK),
        _sep(),
        _text("📊 สถิติภาพรวม", weight="bold", color=CLR_HEADER_BLUE),
        _text("พิมพ์: สถิติ", size="sm", color=CLR_DARK),
    ]
    footer = "💡 ในกลุ่ม LINE ต้องพิมพ์ \"bot\" นำหน้าทุกคำสั่ง\nเช่น: bot ค้นหา สมชาย"
    bubble = _bubble("🚔 ระบบสืบค้นการจับกุม สน.บางชัน", CLR_HEADER_BLUE, body, footer)
    return [flex("คำสั่งทั้งหมด", bubble)]


def build_not_found(msg: str) -> list:
    body = [_text(f"❌ {msg}", color=CLR_HEADER_RED, wrap=True)]
    bubble = _bubble("ไม่พบข้อมูล", CLR_HEADER_RED, body)
    return [flex("ไม่พบข้อมูล", bubble)]


def build_name_result(rows, total: int, keyword: str) -> list:
    """ค้นหาบุคคล: Flex summary (ยอดจริง) + Text รายละเอียด 30 ล่าสุด"""
    if not rows:
        return build_not_found(f"ไม่พบบุคคลที่ชื่อ '{keyword}'")

    # Group by name จาก rows ที่แสดง (30 ล่าสุด)
    by_name: dict = {}
    for r in rows:
        by_name.setdefault(r['name'], []).append(r)

    shown = len(rows)

    # ── Flex summary card ──
    body = [
        _row("คำค้นหา", keyword),
        _row("จำนวนครั้งที่จับ (ยอดจริง)", f"{total} ครั้ง"),
        _row("จำนวนบุคคล (ยอดจริง)", f"≥ {len(by_name)} คน"),
        _sep(),
        _text(f"📋 แสดง {shown} รายการล่าสุด:", weight="bold", size="sm"),
    ]
    for name, recs in by_name.items():
        body.append({
            "type": "box", "layout": "horizontal", "margin": "sm",
            "contents": [
                _text("👤", size="sm", flex=1),
                _text(name, size="sm", weight="bold", color=CLR_DARK, flex=7),
                _text(f"{len(recs)} ครั้ง", size="sm", color=CLR_HEADER_RED,
                      flex=2, align="end"),
            ]
        })

    if total > DETAIL_LIMIT:
        body.append(_sep())
        body.append(_text(
            f"⚠️ แสดง {DETAIL_LIMIT} รายการล่าสุด จากทั้งหมด {total} รายการ\n"
            "กรุณาระบุชื่อให้ชัดเจนขึ้นเพื่อดูครบทุกรายการ",
            size="xs", color=CLR_GRAY
        ))

    bubble = _bubble(f"🔍 ค้นหา: {keyword}", CLR_HEADER_BLUE, body)
    messages = [flex(f"ค้นหา {keyword}: พบ {total} ครั้ง", bubble)]

    # ── Text รายละเอียด (30 ล่าสุด เรียงจากปัจจุบัน) ──
    header = f"📋 รายละเอียด {shown} รายการล่าสุด"
    if total > DETAIL_LIMIT:
        header += f" (จากทั้งหมด {total} รายการ)"
    detail_lines = [header + "\n"]
    for name, recs in by_name.items():
        detail_lines.append(f"👤 {name}  ({len(recs)} ครั้ง ในชุดนี้)")
        for i, rec in enumerate(recs, 1):
            date   = rec['date_str'] or '-'
            charge = rec['charge'] or '-'
            ev     = rec['evidence'] or ''
            loc    = rec['location'] or ''
            detail_lines.append(f"  ครั้งที่ {i}: {date}")
            detail_lines.append(f"  ข้อหา: {charge}")
            if ev:
                detail_lines.append(f"  ของกลาง: {ev}")
            if loc:
                detail_lines.append(f"  สถานที่: {loc}")
        detail_lines.append("")

    detail_text = "\n".join(detail_lines).strip()
    while detail_text:
        chunk = detail_text[:4900]
        detail_text = detail_text[4900:]
        messages.append(TextMessage(text=chunk))
        if len(messages) >= 5:
            break

    return messages


def build_location_result(rows, total: int, keyword: str) -> list:
    """ค้นหาสถานที่: Flex summary (ยอดจริง) + Text รายชื่อ 30 ล่าสุด"""
    if not rows:
        return build_not_found(
            f"ไม่พบข้อมูลสถานที่ '{keyword}'\n"
            "หมายเหตุ: ข้อมูลสถานที่มีเฉพาะปี 2558 เป็นต้นไป"
        )

    shown = len(rows)
    unique_names = list(dict.fromkeys(r['name'] for r in rows))

    # นับข้อหาจาก rows ที่แสดง (ใช้ข้อมูล total จริงสำหรับ summary)
    charge_count: dict = {}
    for r in rows:
        c = r['charge'] or 'ไม่ระบุ'
        charge_count[c] = charge_count.get(c, 0) + 1
    top_charges = sorted(charge_count.items(), key=lambda x: -x[1])[:5]

    # ── Flex summary card ──
    body = [
        _row("สถานที่", keyword),
        _row("จำนวนครั้งที่จับ (ยอดจริง)", f"{total} ครั้ง"),
        _row("จำนวนผู้ต้องหา (ยอดจริง)", f"≥ {len(unique_names)} คน"),
        _sep(),
        _text(f"📋 ข้อหาที่พบ (จาก {shown} ล่าสุด):", weight="bold", size="sm"),
    ]
    for c, cnt in top_charges:
        body.append(_row(f"  • {c}", f"{cnt} ราย"))

    if total > DETAIL_LIMIT:
        body.append(_sep())
        body.append(_text(
            f"⚠️ แสดง {DETAIL_LIMIT} รายการล่าสุด จากทั้งหมด {total} รายการ",
            size="xs", color=CLR_GRAY
        ))

    bubble = _bubble(f"📍 สถานที่: {keyword}", CLR_HEADER_GREEN, body)
    messages = [flex(f"สถานที่ {keyword}: พบ {total} ครั้ง", bubble)]

    # ── Text รายชื่อ 30 ล่าสุด ──
    header = f"📍 รายชื่อ {shown} รายการล่าสุด ที่ {keyword}"
    if total > DETAIL_LIMIT:
        header += f"\n(ทั้งหมด {total} รายการ — แสดง {DETAIL_LIMIT} ล่าสุด)"
    lines = [header + "\n"]
    for i, r in enumerate(rows, 1):
        name   = r['name']
        date   = r['date_str'] or '-'
        charge = r['charge'] or '-'
        ev     = r['evidence'] or ''
        loc    = r['location'] or keyword
        lines.append(f"{i}. 👤 {name}")
        lines.append(f"   📅 {date}")
        lines.append(f"   ข้อหา: {charge}")
        if ev:
            lines.append(f"   ของกลาง: {ev}")
        if loc and loc != keyword:
            lines.append(f"   สถานที่: {loc}")

    detail_text = "\n".join(lines).strip()
    while detail_text:
        chunk = detail_text[:4900]
        detail_text = detail_text[4900:]
        messages.append(TextMessage(text=chunk))
        if len(messages) >= 5:
            break

    return messages


DRUG_KEYWORDS = ['ยาบ้า', 'ยาไอซ์', 'ยาเสพ', 'กัญชา', 'เสพ', 'เมทแอมเฟต',
                 'ครอบครองยา', 'จำหน่ายยา', 'พืชกระท่อม', 'ฝิ่น', 'เฮโรอีน',
                 'ครอบครองและเสพ', 'ร่วมกันครอบครองยา', 'ครอบครองยาเสพติด',
                 'เสพยาเสพ', 'ครอบครองยา']
WARRANT_KEYWORDS = ['หมายจับ', 'ตามหมาย', 'หมาย จ.', 'หมาย จพ.']


def categorize_charge(charge: str) -> str:
    """จัดหมวดข้อหา → ยาเสพติด / หมายจับ / คดีอื่นๆ"""
    if not charge or charge.strip() in ('', '-', 'nan'):
        return 'คดีอื่นๆ'
    for kw in WARRANT_KEYWORDS:
        if kw in charge:
            return 'หมายจับ'
    for kw in DRUG_KEYWORDS:
        if kw in charge:
            return 'ยาเสพติด'
    return 'คดีอื่นๆ'


def build_month_result(rows, month_num: int, year_be: int) -> list:
    """สรุปรายเดือน พร้อมแยกหมวด ยาเสพติด / หมายจับ / คดีอื่นๆ"""
    month_name = THAI_MONTH_NAME.get(month_num, str(month_num))
    if not rows:
        return build_not_found(f"ไม่พบข้อมูลเดือน{month_name} พ.ศ.{year_be}")

    total = len(rows)

    # แยกหมวดหมู่หลัก
    cat_count = {'ยาเสพติด': 0, 'หมายจับ': 0, 'คดีอื่นๆ': 0}
    charge_count: dict = {}
    for r in rows:
        c = r['charge'] or ''
        cat = categorize_charge(c)
        cat_count[cat] = cat_count.get(cat, 0) + 1
        label = c if c else 'ไม่ระบุ'
        charge_count[label] = charge_count.get(label, 0) + 1

    # ── Flex summary card ──
    body = [
        _row("เดือน", f"{month_name} พ.ศ.{year_be}"),
        _row("จำนวนผู้ต้องหารวม", f"{total} ราย"),
        _sep(),
        _text("📊 แยกตามประเภทคดี:", weight="bold", size="sm"),
        {
            "type": "box", "layout": "horizontal", "margin": "sm",
            "contents": [
                {
                    "type": "box", "layout": "vertical", "flex": 1,
                    "backgroundColor": "#FFF3E0", "cornerRadius": "8px",
                    "paddingAll": "8px",
                    "contents": [
                        _text("💊", size="xl", align="center"),
                        _text("ยาเสพติด", size="xs", align="center", color=CLR_HEADER_RED),
                        _text(str(cat_count['ยาเสพติด']), size="lg",
                              align="center", weight="bold", color=CLR_HEADER_RED),
                        _text("ราย", size="xs", align="center", color=CLR_GRAY),
                    ]
                },
                {"type": "box", "layout": "vertical", "width": "8px", "contents": []},
                {
                    "type": "box", "layout": "vertical", "flex": 1,
                    "backgroundColor": "#E3F2FD", "cornerRadius": "8px",
                    "paddingAll": "8px",
                    "contents": [
                        _text("📜", size="xl", align="center"),
                        _text("หมายจับ", size="xs", align="center", color=CLR_HEADER_BLUE),
                        _text(str(cat_count['หมายจับ']), size="lg",
                              align="center", weight="bold", color=CLR_HEADER_BLUE),
                        _text("ราย", size="xs", align="center", color=CLR_GRAY),
                    ]
                },
                {"type": "box", "layout": "vertical", "width": "8px", "contents": []},
                {
                    "type": "box", "layout": "vertical", "flex": 1,
                    "backgroundColor": "#F3E5F5", "cornerRadius": "8px",
                    "paddingAll": "8px",
                    "contents": [
                        _text("⚖️", size="xl", align="center"),
                        _text("คดีอื่นๆ", size="xs", align="center", color=CLR_HEADER_PURPLE),
                        _text(str(cat_count['คดีอื่นๆ']), size="lg",
                              align="center", weight="bold", color=CLR_HEADER_PURPLE),
                        _text("ราย", size="xs", align="center", color=CLR_GRAY),
                    ]
                },
            ]
        },
        _sep(),
        _text("📋 สรุปตามข้อหา (ทั้งหมด):", weight="bold", size="sm"),
    ]
    for c, cnt in sorted(charge_count.items(), key=lambda x: -x[1]):
        cat = categorize_charge(c)
        icon = "💊" if cat == 'ยาเสพติด' else ("📜" if cat == 'หมายจับ' else "⚖️")
        body.append(_row(f"{icon} {c}", f"{cnt} ราย"))

    bubble = _bubble(f"📅 {month_name} พ.ศ.{year_be}", CLR_HEADER_ORANGE, body)
    messages = [flex(f"สรุปเดือน{month_name} {year_be}: {total} ราย", bubble)]

    # ── Text รายชื่อ ──
    lines = [f"👤 รายชื่อผู้ต้องหา {month_name} พ.ศ.{year_be} ({total} ราย)\n"]
    for i, r in enumerate(rows, 1):
        name   = r['name']
        date   = r['date_str'] or '-'
        charge = r['charge'] or '-'
        ev     = r['evidence'] or ''
        loc    = r['location'] or ''
        line   = f"{i}. {name}  ({date})"
        if loc:
            line += f"  [{loc}]"
        lines.append(line)
        detail = f"   {charge}"
        if ev:
            detail += f"  |  ของกลาง: {ev}"
        lines.append(detail)

    detail_text = "\n".join(lines).strip()
    while detail_text:
        chunk = detail_text[:4900]
        detail_text = detail_text[4900:]
        messages.append(TextMessage(text=chunk))
        if len(messages) >= 5:
            break

    return messages


def build_year_result(rows, total: int, year_be: int) -> list:
    """สรุปรายปี"""
    if total == 0:
        return build_not_found(f"ไม่พบข้อมูลปี พ.ศ.{year_be}")

    body = [
        _row("ปี พ.ศ.", str(year_be)),
        _row("รวมทั้งปี", f"{total} ราย"),
        _sep(),
        _text("📅 รายเดือน:", weight="bold", size="sm"),
    ]
    max_cnt = max(r['cnt'] for r in rows) if rows else 1
    for r in rows:
        m_name = r['month_name'] or THAI_MONTH_NAME.get(r['month_num'], '')
        cnt = r['cnt']
        bar = "█" * int(cnt / max_cnt * 8) + "░" * (8 - int(cnt / max_cnt * 8))
        body.append({
            "type": "box", "layout": "horizontal", "margin": "xs",
            "contents": [
                _text(m_name[:3], size="xs", color=CLR_GRAY, flex=3),
                _text(bar, size="xs", color=CLR_HEADER_BLUE, flex=5),
                _text(str(cnt), size="xs", color=CLR_DARK, align="end", flex=2),
            ]
        })

    bubble = _bubble(f"📆 ปี พ.ศ.{year_be}", CLR_HEADER_PURPLE, body)
    return [flex(f"สรุปปี {year_be}: รวม {total} ราย", bubble)]


def build_evidence_result(rows, keyword: str) -> list:
    """ของกลาง"""
    if not rows:
        return build_not_found(f"ไม่พบของกลางที่มีคำว่า '{keyword}'")

    total_cases = sum(r['cnt'] for r in rows)
    body = [
        _row("คำค้นหา", keyword),
        _row("รวมทั้งหมด", f"{total_cases} ครั้ง"),
        _row("จำนวนประเภท", f"{len(rows)} ประเภท"),
        _sep(),
    ]
    for r in rows:
        body.append({
            "type": "box", "layout": "horizontal", "margin": "xs",
            "contents": [
                _text(f"• {r['evidence']}", size="sm", color=CLR_DARK, flex=7, wrap=True),
                _text(str(r['cnt']), size="sm", color=CLR_HEADER_RED,
                      align="end", flex=2, weight="bold"),
            ]
        })

    bubble = _bubble(f"🧪 ของกลาง: {keyword}", CLR_HEADER_RED, body)
    return [flex(f"ของกลาง {keyword}: {total_cases} ครั้ง", bubble)]


def build_charge_result(rows, total: int, keyword: str) -> list:
    """ข้อหา: ยอดจริง + รายละเอียด 30 ล่าสุด"""
    if not rows:
        return build_not_found(f"ไม่พบข้อหาที่มีคำว่า '{keyword}'")

    shown = len(rows)
    charge_count: dict = {}
    for r in rows:
        c = r['charge'] or 'ไม่ระบุ'
        charge_count[c] = charge_count.get(c, 0) + 1

    body = [
        _row("คำค้นหา", keyword),
        _row("จำนวนผู้ต้องหา (ยอดจริง)", f"{total} ราย"),
        _sep(),
        _text(f"📋 ข้อหาย่อย (จาก {shown} ล่าสุด):", weight="bold", size="sm"),
    ]
    for c, cnt in sorted(charge_count.items(), key=lambda x: -x[1])[:8]:
        body.append(_row(f"  • {c}", f"{cnt} ราย"))

    if total > DETAIL_LIMIT:
        body.append(_sep())
        body.append(_text(
            f"⚠️ แสดง {DETAIL_LIMIT} รายการล่าสุด จากทั้งหมด {total} รายการ",
            size="xs", color=CLR_GRAY
        ))

    bubble = _bubble(f"📋 ข้อหา: {keyword}", CLR_HEADER_ORANGE, body)
    messages = [flex(f"ข้อหา {keyword}: {total} ราย", bubble)]

    # Text รายชื่อ 30 ล่าสุด
    header = f"📋 รายชื่อข้อหา '{keyword}' — {shown} รายการล่าสุด"
    if total > DETAIL_LIMIT:
        header += f" (ทั้งหมด {total} ราย)"
    lines = [header + "\n"]
    for i, r in enumerate(rows, 1):
        date = r['date_str'] or '-'
        name = r['name']
        ev   = r['evidence'] or ''
        yr   = r['year_be']
        lines.append(f"{i}. 👤 {name}  (พ.ศ.{yr}  {date})")
        if ev:
            lines.append(f"   ของกลาง: {ev}")

    detail_text = "\n".join(lines).strip()
    while detail_text:
        chunk = detail_text[:4900]
        detail_text = detail_text[4900:]
        messages.append(TextMessage(text=chunk))
        if len(messages) >= 5:
            break

    return messages


def build_stats() -> list:
    """สถิติภาพรวม — ส่งคืนเป็น Flex carousel 3 bubbles"""
    total, yr_range, charges, locations, evidences, yearly = get_overall_stats()
    yr_min, yr_max = yr_range

    # Bubble 1: ภาพรวม + รายปี
    body1 = [
        _row("ข้อมูลช่วง", f"พ.ศ.{yr_min} - {yr_max}"),
        _row("จับกุมทั้งสิ้น", f"{total:,} ราย"),
        _sep(),
        _text("📅 รายปี:", weight="bold", size="sm"),
    ]
    for r in yearly:
        body1.append(_row(f"  พ.ศ.{r['year_be']}", f"{r['cnt']} ราย"))

    # Bubble 2: Top ข้อหา
    body2 = [_text("📋 ข้อหาที่พบบ่อย (Top 10):", weight="bold", size="sm")]
    for i, r in enumerate(charges, 1):
        body2.append({
            "type": "box", "layout": "horizontal", "margin": "xs",
            "contents": [
                _text(str(i), size="xs", color=CLR_GRAY, flex=1),
                _text(r['charge'], size="xs", color=CLR_DARK, flex=7, wrap=True),
                _text(str(r['cnt']), size="xs", color=CLR_HEADER_RED,
                      align="end", flex=2, weight="bold"),
            ]
        })

    # Bubble 3: Top สถานที่ + ของกลาง
    body3 = []
    if locations:
        body3 += [
            _text("📍 สถานที่จับกุมบ่อย (Top 8):", weight="bold", size="sm"),
        ]
        for i, r in enumerate(locations, 1):
            body3.append(_row(f"  {i}. {r['location']}", f"{r['cnt']} ครั้ง"))
        body3.append(_sep())

    if evidences:
        body3 += [
            _text("🧪 ของกลางที่พบบ่อย (Top 5):", weight="bold", size="sm"),
        ]
        for i, r in enumerate(evidences, 1):
            body3.append(_row(f"  {i}. {r['evidence']}", f"{r['cnt']} ครั้ง"))

    carousel = _carousel([
        _bubble("📊 สถิติภาพรวม สน.บางชัน", CLR_HEADER_BLUE, body1),
        _bubble("📋 Top ข้อหา", CLR_HEADER_ORANGE, body2),
        _bubble("📍🧪 สถานที่ & ของกลาง", CLR_HEADER_GREEN, body3),
    ])
    return [flex(f"สถิติสน.บางชัน: รวม {total:,} ราย", carousel)]


# ─── Thai date parser ─────────────────────────────────────────────────────────

MONTH_ABBR_MAP = {
    'ม.ค': 1, 'มกราคม': 1,
    'ก.พ': 2, 'กุมภาพันธ์': 2,
    'มี.ค': 3, 'มีนาคม': 3,
    'เม.ย': 4, 'เมษายน': 4,
    'พ.ค': 5, 'พฤษภาคม': 5,
    'มิ.ย': 6, 'มิถุนายน': 6,
    'ก.ค': 7, 'กรกฎาคม': 7, 'กรกฏาคม': 7,
    'ส.ค': 8, 'สิงหาคม': 8,
    'ก.ย': 9, 'กันยายน': 9,
    'ต.ค': 10, 'ตุลาคม': 10,
    'พ.ย': 11, 'พฤศจิกายน': 11,
    'ธ.ค': 12, 'ธันวาคม': 12,
}


def parse_month_year(text: str):
    text = text.strip()
    month_num = None
    year_be = None
    for abbr, num in sorted(MONTH_ABBR_MAP.items(), key=lambda x: -len(x[0])):
        if abbr in text:
            month_num = num
            break
    m4 = re.search(r'\b(25\d{2})\b', text)
    m2 = re.search(r'\b(\d{2})\b', text)
    if m4:
        year_be = int(m4.group(1))
    elif m2:
        year_be = 2500 + int(m2.group(1))
    return month_num, year_be


def parse_year_only(text: str):
    m4 = re.search(r'\b(25\d{2})\b', text)
    if m4:
        return int(m4.group(1))
    m2 = re.search(r'\b(\d{2})\b', text)
    if m2:
        return 2500 + int(m2.group(1))
    return None


# ─── Location prefix patterns (auto-detect) ──────────────────────────────────

# ถ้าข้อความขึ้นต้นด้วยคำเหล่านี้ ให้ถือว่าเป็นการค้นหาสถานที่ทันที
LOCATION_PREFIX_RE = re.compile(
    r'^(ชุมชน|ซอย|ถ\.|ถนน|หมู่บ้าน|สน\.|ริมคลอง|ริมถนน|ริม|แยก|ลาน|ตลาด|ห้าง|อาคาร|บริเวณ)'
)


def _is_direct_month_year(text: str):
    """
    ตรวจว่าข้อความเป็น month+year โดยตรง ไม่มี prefix เช่น
    'มิ.ย.69', 'ม.ค. 69', 'มกราคม 69', 'ม.ค.2569'
    คืน (month_num, year_be) หรือ (None, None)
    """
    month_num, year_be = parse_month_year(text)
    if not month_num or not year_be:
        return None, None
    # ตรวจว่าข้อความสั้น (ไม่ใช่ประโยคยาวที่บังเอิญมีเดือน)
    if len(text.strip()) <= 20:
        return month_num, year_be
    return None, None


# ─── Intent Router ────────────────────────────────────────────────────────────

def handle_message(text: str) -> list:
    """รับข้อความ คืนค่า list ของ LINE message objects"""
    t = text.strip()
    t_lower = t.lower()

    # ── Help ──
    if any(k in t_lower for k in ['ช่วย', 'help', '?', 'คำสั่ง', 'menu', 'เมนู']):
        return build_help()

    # ── สถิติ ──
    if any(k in t for k in ['สถิติ', 'ภาพรวม', 'รวมทั้งหมด', 'รายงาน']):
        return build_stats()

    # ── ค้นหาบุคคล (มี prefix) ──
    if re.match(r'^(ค้นหา|หา)\s', t):
        keyword = re.sub(r'^(ค้นหา|หา)\s+', '', t).strip()
        if not keyword:
            return [TextMessage(text="กรุณาระบุชื่อ เช่น: ค้นหา สมชาย")]
        rows, total = search_by_name(keyword)
        return build_name_result(rows, total, keyword)

    # ── ค้นหาสถานที่ (มี prefix: สถานที่ / จุดจับ / ที่จับ) ──
    if re.match(r'^(สถานที่|จุดจับ|ที่จับ)', t):
        keyword = re.sub(r'^(สถานที่|จุดจับ|ที่จับ)\s*', '', t).strip()
        if not keyword:
            return [TextMessage(text="กรุณาระบุสถานที่ เช่น: สถานที่ ชุมชนวิมานสุข")]
        rows, total = search_by_location(keyword)
        return build_location_result(rows, total, keyword)

    # ── ค้นหาสถานที่ (ไม่มี prefix — ขึ้นต้นด้วย ชุมชน/ซอย/ถ. ฯลฯ) ──
    if LOCATION_PREFIX_RE.match(t):
        rows, total = search_by_location(t)
        return build_location_result(rows, total, t)

    # ── สรุปเดือน (มี prefix: เดือน / สรุปเดือน) ──
    if re.match(r'^(สรุปเดือน|เดือน)\s', t):
        arg = re.sub(r'^(สรุปเดือน|เดือน)\s+', '', t).strip()
        month_num, year_be = parse_month_year(arg)
        if not month_num or not year_be:
            return [TextMessage(text="กรุณาระบุเดือนและปี\nเช่น: เดือน ม.ค. 63")]
        return build_month_result(summary_by_month(month_num, year_be), month_num, year_be)

    # ── สรุปเดือน (ไม่มี prefix — พิมพ์ตรงๆ เช่น มิ.ย.69 / มกราคม 69) ──
    direct_month, direct_year = _is_direct_month_year(t)
    if direct_month and direct_year:
        return build_month_result(
            summary_by_month(direct_month, direct_year), direct_month, direct_year
        )

    # ── สรุปปี ──
    if re.match(r'^(สรุปปี|ปี)\s', t):
        arg = re.sub(r'^(สรุปปี|ปี)\s+', '', t).strip()
        year_be = parse_year_only(arg)
        if not year_be:
            return [TextMessage(text="กรุณาระบุปี พ.ศ. เช่น: ปี 2563")]
        rows, total = summary_by_year(year_be)
        return build_year_result(rows, total, year_be)

    # ── ของกลาง ──
    if t.startswith('ของกลาง'):
        keyword = re.sub(r'^ของกลาง\s*', '', t).strip()
        return build_evidence_result(search_evidence(keyword), keyword)

    # ── ข้อหา ──
    if t.startswith('ข้อหา'):
        keyword = re.sub(r'^ข้อหา\s*', '', t).strip()
        if not keyword:
            return [TextMessage(text="กรุณาระบุข้อหา เช่น: ข้อหา เสพยาบ้า")]
        rows, total = search_by_charge(keyword)
        return build_charge_result(rows, total, keyword)

    # ── Default: ลองค้นชื่อ → ถ้าไม่เจอ ลองค้นสถานที่ ──
    if len(t) >= 2:
        name_rows, name_total = search_by_name(t)
        if name_rows:
            return build_name_result(name_rows, name_total, t)
        loc_rows, loc_total = search_by_location(t)
        if loc_rows:
            return build_location_result(loc_rows, loc_total, t)

    return [TextMessage(
        text=f"❓ ไม่เข้าใจคำสั่ง '{t}'\n\nพิมพ์ ช่วยเหลือ หรือ help เพื่อดูคำสั่งทั้งหมด"
    )]


# ─── LINE Webhook ─────────────────────────────────────────────────────────────

WELCOME_TEXT = (
    "🚔 สวัสดีครับ! บอทสืบค้นการจับกุม สน.บางชัน พร้อมให้บริการแล้ว\n\n"
    "💡 พิมพ์  bot help  เพื่อดูคำสั่งทั้งหมด\n"
    "ตัวอย่าง:\n"
    "  bot ค้นหา สมชาย\n"
    "  bot ชุมชนวิมานสุข\n"
    "  bot มิ.ย.69\n"
    "  bot สถิติ"
)


def _reply(reply_token: str, messages: list):
    """ส่ง reply พร้อม error handling"""
    if not reply_token:
        log.warning("[reply] reply_token is empty, skip")
        return
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=messages[:5]
                )
            )
    except Exception as e:
        log.error(f"[reply error] {e}")


def _push(to: str, messages: list):
    """ส่ง push message (ไม่ต้องใช้ reply_token)"""
    if not to:
        return
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(to=to, messages=messages[:5])
            )
    except Exception as e:
        log.error(f"[push error] {e}")


@app.route("/callback", methods=['POST'])
def callback():
    """
    Webhook endpoint — ต้องคืน 200 OK เสมอ
    ถ้าคืน 4xx/5xx LINE จะ retry และอาจนำ Bot ออกจากกลุ่ม
    """
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    log.info(f"[webhook] body={body[:120]}")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        log.error("[webhook] Invalid signature")
        abort(400)
    except Exception as e:
        log.error(f"[webhook] Unhandled error: {e}", exc_info=True)
    return 'OK', 200   # ← คืน 200 เสมอ แม้จะมี error


# ── Bot เข้าร่วมกลุ่ม / ห้อง ──────────────────────────────────────────────────
@handler.add(JoinEvent)
def handle_join(event: JoinEvent):
    """
    รับเหตุการณ์เมื่อ Bot ถูกเชิญเข้ากลุ่ม
    ใช้ reply_token ถ้ามี ไม่งั้น push
    """
    source = event.source
    log.info(f"[JoinEvent] type={source.type} id={getattr(source, 'group_id', None) or getattr(source, 'room_id', None)}")
    try:
        if event.reply_token and event.reply_token != '00000000000000000000000000000000':
            _reply(event.reply_token, [TextMessage(text=WELCOME_TEXT)])
        else:
            # fallback: push ไปที่กลุ่ม
            group_id = getattr(source, 'group_id', None) or getattr(source, 'room_id', None)
            if group_id:
                _push(group_id, [TextMessage(text=WELCOME_TEXT)])
    except Exception as e:
        log.error(f"[JoinEvent] error: {e}", exc_info=True)


# ── มีสมาชิกใหม่เข้ากลุ่ม ─────────────────────────────────────────────────────
@handler.add(MemberJoinedEvent)
def handle_member_joined(event: MemberJoinedEvent):
    log.info("[MemberJoinedEvent] received")


# ── ผู้ใช้ Add Bot เป็นเพื่อน ──────────────────────────────────────────────────
@handler.add(FollowEvent)
def handle_follow(event: FollowEvent):
    log.info(f"[FollowEvent] user_id={event.source.user_id}")
    welcome = (
        "🚔 สวัสดีครับ! บอทสืบค้นการจับกุม สน.บางชัน พร้อมให้บริการ\n\n"
        "💡 พิมพ์  help  เพื่อดูคำสั่งทั้งหมด\n"
        "ตัวอย่าง:\n"
        "  ค้นหา สมชาย\n"
        "  ชุมชนวิมานสุข\n"
        "  มิ.ย.69\n"
        "  สถิติ"
    )
    _reply(event.reply_token, [TextMessage(text=welcome)])


# ── Bot ออกจากกลุ่ม / Unfollow ───────────────────────────────────────────────
@handler.add(LeaveEvent)
def handle_leave(event: LeaveEvent):
    log.warning(f"[LeaveEvent] Bot was removed — source={event.source.type}")


@handler.add(UnfollowEvent)
def handle_unfollow(event: UnfollowEvent):
    log.info(f"[UnfollowEvent] user_id={event.source.user_id}")


# ── รับข้อความ ────────────────────────────────────────────────────────────────
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event: MessageEvent):
    try:
        user_text = event.message.text.strip()
        source_type = event.source.type   # 'user' | 'group' | 'room'
        log.info(f"[msg] source={source_type} text={user_text[:60]}")

        # ── กลุ่ม / ห้อง: ต้องพิมพ์ "bot" นำหน้า ──
        if source_type in ('group', 'room'):
            if not re.match(r'^bot\b', user_text, re.IGNORECASE):
                return   # เงียบ ไม่ตอบ
            user_text = re.sub(r'^bot\s*', '', user_text, flags=re.IGNORECASE).strip()
            if not user_text:
                _reply(event.reply_token, [TextMessage(text=WELCOME_TEXT)])
                return

        messages = handle_message(user_text)
        _reply(event.reply_token, messages)

    except Exception as e:
        log.error(f"[handle_text_message] {e}", exc_info=True)
        try:
            _reply(event.reply_token,
                   [TextMessage(text="❌ เกิดข้อผิดพลาด กรุณาลองใหม่อีกครั้ง")])
        except Exception:
            pass


@app.route("/health", methods=['GET'])
def health():
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM arrests").fetchone()[0]
    conn.close()
    return {"status": "ok", "total_records": total, "version": "2.0"}


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 LINE Bot v2.0 กำลังทำงานที่ port {port}")
    print(f"📂 ฐานข้อมูล: {DB_PATH}")
    print(f"💡 กลุ่ม LINE: พิมพ์ 'bot [คำสั่ง]' เพื่อเรียกใช้งาน")
    app.run(host='0.0.0.0', port=port, debug=False)

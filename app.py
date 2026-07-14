gunicorn app:app
"""
LINE Bot - ระบบสืบค้นผลการจับกุม สน.บางชัน
ติดตั้ง: pip install flask line-bot-sdk pandas openpyxl
รัน:    python app.py
"""

import os
import re
import sqlite3
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage, FlexMessage, FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# ─── Config ───────────────────────────────────────────────────────────────────
LINE_CHANNEL_SECRET      = os.environ.get("LINE_CHANNEL_SECRET", "YOUR_CHANNEL_SECRET")
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

HELP_TEXT = """🚔 ระบบสืบค้นข้อมูลการจับกุม สน.บางชัน

📌 คำสั่งที่ใช้ได้:

🔍 ค้นหาบุคคล
  ค้นหา [ชื่อ หรือ นามสกุล]
  เช่น: ค้นหา สมชาย

📍 ค้นหาสถานที่
  สถานที่ [ชื่อสถานที่]
  เช่น: สถานที่ รามอินทรา

📅 สรุปรายเดือน
  เดือน [เดือน] [ปี พ.ศ.]
  เช่น: เดือน มกราคม 2563
  หรือ: เดือน ม.ค. 63

📆 สรุปรายปี
  ปี [ปี พ.ศ.]
  เช่น: ปี 2563  หรือ  ปี 63

🧪 สรุปของกลาง
  ของกลาง [คำค้น]
  เช่น: ของกลาง ยาบ้า

📊 สถิติภาพรวม
  สถิติ

📋 ข้อหา
  ข้อหา [คำค้น]
  เช่น: ข้อหา เสพยาบ้า

❓ ช่วยเหลือ
  ช่วยเหลือ  หรือ  help"""


# ─── DB Helper ────────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def search_by_name(keyword: str, limit=20):
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM arrests
           WHERE name LIKE ?
           ORDER BY year_be, month_num, date_str
           LIMIT ?""",
        (f"%{keyword}%", limit)
    ).fetchall()
    conn.close()
    return rows


def search_by_location(keyword: str, limit=30):
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM arrests
           WHERE location LIKE ?
           ORDER BY year_be, month_num
           LIMIT ?""",
        (f"%{keyword}%", limit)
    ).fetchall()
    conn.close()
    return rows


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


def search_evidence(keyword: str, limit=50):
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


def search_by_charge(keyword: str, limit=30):
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM arrests
           WHERE charge LIKE ?
           ORDER BY year_be, month_num
           LIMIT ?""",
        (f"%{keyword}%", limit)
    ).fetchall()
    conn.close()
    return rows


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
           GROUP BY location ORDER BY cnt DESC LIMIT 10"""
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


# ─── Response Builders ────────────────────────────────────────────────────────

def fmt_name_result(rows) -> str:
    """รายละเอียดบุคคล"""
    if not rows:
        return "❌ ไม่พบข้อมูลที่ค้นหา"

    # Group by name
    by_name = {}
    for r in rows:
        n = r['name']
        if n not in by_name:
            by_name[n] = []
        by_name[n].append(r)

    lines = []
    for name, recs in by_name.items():
        lines.append(f"👤 {name}")
        lines.append(f"   จำนวนครั้งที่จับ: {len(recs)} ครั้ง")
        for rec in recs:
            date = rec['date_str'] or '-'
            charge = rec['charge'] or '-'
            ev = rec['evidence'] or '-'
            loc = rec['location'] or '-'
            lines.append(f"   📅 {date}")
            lines.append(f"      ข้อหา: {charge}")
            if ev != '-':
                lines.append(f"      ของกลาง: {ev}")
            if loc != '-':
                lines.append(f"      สถานที่: {loc}")
        lines.append("")

    total = len(rows)
    if total == 20:
        lines.append("⚠️ แสดงผลสูงสุด 20 รายการ กรุณาระบุชื่อให้ชัดเจนขึ้น")

    return "\n".join(lines).strip()


def fmt_location_result(rows, keyword: str) -> str:
    if not rows:
        return f"❌ ไม่พบบุคคลที่ถูกจับที่ '{keyword}'"

    lines = [f"📍 สถานที่: {keyword}\n🔢 พบ {len(rows)} รายการ\n"]
    for r in rows:
        date = r['date_str'] or '-'
        name = r['name']
        charge = r['charge'] or '-'
        ev = r['evidence'] or '-'
        lines.append(f"• {name}")
        lines.append(f"  📅 {date} | ข้อหา: {charge}")
        if ev != '-':
            lines.append(f"  ของกลาง: {ev}")

    if len(rows) == 30:
        lines.append("\n⚠️ แสดงผลสูงสุด 30 รายการ")
    return "\n".join(lines).strip()


def fmt_month_result(rows, month_num: int, year_be: int) -> str:
    month_name = THAI_MONTH_NAME.get(month_num, str(month_num))
    if not rows:
        return f"❌ ไม่พบข้อมูลเดือน {month_name} พ.ศ. {year_be}"

    lines = [f"📅 สรุปผลจับกุม เดือน{month_name} พ.ศ.{year_be}",
             f"📊 รวม {len(rows)} ราย\n"]

    # charge summary
    charge_count = {}
    for r in rows:
        c = r['charge'] or 'ไม่ระบุ'
        charge_count[c] = charge_count.get(c, 0) + 1

    lines.append("📋 ข้อหา:")
    for c, cnt in sorted(charge_count.items(), key=lambda x: -x[1]):
        lines.append(f"  • {c}: {cnt} ราย")

    lines.append("\n👤 รายชื่อ:")
    for r in rows:
        date = r['date_str'] or '-'
        name = r['name']
        charge = r['charge'] or '-'
        ev = r['evidence'] or ''
        loc = r['location'] or ''
        info = f"  • {name} ({date})"
        if loc:
            info += f" [{loc}]"
        lines.append(info)
        detail = f"    {charge}"
        if ev:
            detail += f" | ของกลาง: {ev}"
        lines.append(detail)

    return "\n".join(lines)


def fmt_year_result(rows, total, year_be: int) -> str:
    if total == 0:
        return f"❌ ไม่พบข้อมูลปี พ.ศ. {year_be}"

    lines = [f"📆 สรุปผลจับกุม ปี พ.ศ. {year_be}",
             f"📊 รวมทั้งปี: {total} ราย\n",
             "รายเดือน:"]
    for r in rows:
        m_name = r['month_name'] or THAI_MONTH_NAME.get(r['month_num'], str(r['month_num']))
        lines.append(f"  • {m_name}: {r['cnt']} ราย")
    return "\n".join(lines)


def fmt_evidence_result(rows, keyword: str) -> str:
    if not rows:
        return f"❌ ไม่พบของกลางที่มีคำว่า '{keyword}'"

    total = sum(r['cnt'] for r in rows)
    lines = [f"🧪 ของกลาง '{keyword}'",
             f"📊 พบ {total} รายการ ({len(rows)} ประเภท)\n"]
    for r in rows:
        lines.append(f"  • {r['evidence']}: {r['cnt']} ครั้ง")
    return "\n".join(lines)


def fmt_charge_result(rows, keyword: str) -> str:
    if not rows:
        return f"❌ ไม่พบข้อหาที่มีคำว่า '{keyword}'"

    lines = [f"📋 ข้อหา '{keyword}': {len(rows)} รายการ\n"]
    for r in rows:
        date = r['date_str'] or '-'
        name = r['name']
        ev = r['evidence'] or '-'
        yr = r['year_be']
        lines.append(f"• {name} ({date}, พ.ศ.{yr})")
        if ev != '-':
            lines.append(f"  ของกลาง: {ev}")

    if len(rows) == 30:
        lines.append("\n⚠️ แสดงผลสูงสุด 30 รายการ")
    return "\n".join(lines)


def fmt_stats() -> str:
    total, yr_range, charges, locations, evidences, yearly = get_overall_stats()
    yr_min, yr_max = yr_range

    lines = [
        f"📊 สถิติภาพรวม สน.บางชัน",
        f"🗓️ ช่วงข้อมูล: พ.ศ. {yr_min} - {yr_max}",
        f"🔢 จับกุมรวมทั้งสิ้น: {total:,} ราย\n",
        "📅 จำนวนจับกุมรายปี:",
    ]
    for r in yearly:
        lines.append(f"  พ.ศ.{r['year_be']}: {r['cnt']} ราย")

    lines.append("\n📋 ข้อหาที่พบบ่อยที่สุด (Top 10):")
    for i, r in enumerate(charges, 1):
        lines.append(f"  {i}. {r['charge']}: {r['cnt']} ราย")

    if locations:
        lines.append("\n📍 สถานที่จับกุมบ่อย (Top 10):")
        for i, r in enumerate(locations, 1):
            lines.append(f"  {i}. {r['location']}: {r['cnt']} ครั้ง")

    if evidences:
        lines.append("\n🧪 ของกลางที่พบบ่อย (Top 5):")
        for i, r in enumerate(evidences, 1):
            lines.append(f"  {i}. {r['evidence']}: {r['cnt']} ครั้ง")

    return "\n".join(lines)


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
    """แปลง 'มกราคม 2563' หรือ 'ม.ค. 63' -> (month_num, year_be)"""
    text = text.strip()

    month_num = None
    year_be = None

    # Match month
    for abbr, num in sorted(MONTH_ABBR_MAP.items(), key=lambda x: -len(x[0])):
        if abbr in text:
            month_num = num
            break

    # Match year (4 digits or 2 digits)
    m4 = re.search(r'\b(25\d{2})\b', text)
    m2 = re.search(r'\b(\d{2})\b', text)
    if m4:
        year_be = int(m4.group(1))
    elif m2:
        yr2 = int(m2.group(1))
        year_be = 2500 + yr2

    return month_num, year_be


def parse_year_only(text: str):
    m4 = re.search(r'\b(25\d{2})\b', text)
    if m4:
        return int(m4.group(1))
    m2 = re.search(r'\b(\d{2})\b', text)
    if m2:
        return 2500 + int(m2.group(1))
    return None


# ─── Intent Router ────────────────────────────────────────────────────────────

def handle_message(text: str) -> str:
    t = text.strip()
    t_lower = t.lower()

    # Help
    if any(k in t_lower for k in ['ช่วย', 'help', '?', 'คำสั่ง', 'menu', 'เมนู']):
        return HELP_TEXT

    # ค้นหาบุคคล
    if t.startswith('ค้นหา') or t.startswith('หา'):
        keyword = re.sub(r'^(ค้นหา|หา)\s*', '', t).strip()
        if not keyword:
            return "กรุณาระบุชื่อที่ต้องการค้นหา\nเช่น: ค้นหา สมชาย"
        rows = search_by_name(keyword)
        return fmt_name_result(rows)

    # ค้นหาสถานที่
    if t.startswith('สถานที่') or t.startswith('จุด') or t.startswith('ที่จับ'):
        keyword = re.sub(r'^(สถานที่|จุดจับ|ที่จับ)\s*', '', t).strip()
        if not keyword:
            return "กรุณาระบุสถานที่\nเช่น: สถานที่ รามอินทรา"
        rows = search_by_location(keyword)
        return fmt_location_result(rows, keyword)

    # สรุปเดือน
    if t.startswith('เดือน') or t.startswith('สรุปเดือน'):
        arg = re.sub(r'^(สรุปเดือน|เดือน)\s*', '', t).strip()
        month_num, year_be = parse_month_year(arg)
        if not month_num or not year_be:
            return "กรุณาระบุเดือนและปี\nเช่น: เดือน มกราคม 2563\nหรือ: เดือน ม.ค. 63"
        rows = summary_by_month(month_num, year_be)
        return fmt_month_result(rows, month_num, year_be)

    # สรุปปี
    if t.startswith('ปี') or t.startswith('สรุปปี'):
        arg = re.sub(r'^(สรุปปี|ปี)\s*', '', t).strip()
        year_be = parse_year_only(arg)
        if not year_be:
            return "กรุณาระบุปี พ.ศ.\nเช่น: ปี 2563  หรือ  ปี 63"
        rows, total = summary_by_year(year_be)
        return fmt_year_result(rows, total, year_be)

    # ของกลาง
    if t.startswith('ของกลาง'):
        keyword = re.sub(r'^ของกลาง\s*', '', t).strip()
        if not keyword:
            keyword = ''
        rows = search_evidence(keyword)
        return fmt_evidence_result(rows, keyword)

    # ข้อหา
    if t.startswith('ข้อหา'):
        keyword = re.sub(r'^ข้อหา\s*', '', t).strip()
        if not keyword:
            return "กรุณาระบุข้อหาที่ต้องการค้นหา\nเช่น: ข้อหา เสพยาบ้า"
        rows = search_by_charge(keyword)
        return fmt_charge_result(rows, keyword)

    # สถิติ
    if any(k in t for k in ['สถิติ', 'ภาพรวม', 'รวมทั้งหมด', 'รายงาน']):
        return fmt_stats()

    # Default: ลองค้นชื่อ
    if len(t) >= 2:
        rows = search_by_name(t)
        if rows:
            return f"🔍 ค้นหาชื่อ '{t}':\n\n" + fmt_name_result(rows)

    return (f"❓ ไม่เข้าใจคำสั่ง '{t}'\n\n"
            "พิมพ์ ช่วยเหลือ หรือ help เพื่อดูคำสั่งทั้งหมด")


# ─── LINE Webhook ─────────────────────────────────────────────────────────────

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event: MessageEvent):
    user_text = event.message.text
    reply = handle_message(user_text)

    # LINE message limit: 5000 chars per message
    messages = []
    while reply:
        chunk = reply[:4900]
        reply = reply[4900:]
        messages.append(TextMessage(text=chunk))
        if len(messages) >= 5:  # LINE allows max 5 messages per reply
            break

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=messages
            )
        )


@app.route("/health", methods=['GET'])
def health():
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM arrests").fetchone()[0]
    conn.close()
    return {"status": "ok", "total_records": total}


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 LINE Bot กำลังทำงานที่ port {port}")
    print(f"📂 ฐานข้อมูล: {DB_PATH}")
    app.run(host='0.0.0.0', port=port, debug=False)

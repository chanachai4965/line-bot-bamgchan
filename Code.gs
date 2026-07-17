/**
 * LINE Bot สน.บางชัน — Google Apps Script Data API
 * วิธีใช้: Deploy → New Deployment → Web App
 *          Execute as: Me, Who has access: Anyone
 *
 * ความปลอดภัย: ต้องส่ง ?key=SECRET_KEY มาด้วย
 * ค่า key ต้องตรงกับ APPS_SCRIPT_KEY ใน LINE Bot (Render)
 */

const SECRET_KEY  = 'bangchan-secret-2026';   // ← ต้องตรงกับ APPS_SCRIPT_KEY
const SKIP_SHEETS = ['555', 'ตารางเปล่า', 'สรุป', 'หมายจับ', 'Sheet1'];

/* คอลัมน์รูปแบบใหม่ (ก.ค.69+) */
const NEW_COL = {
  seq:0, date:1, group:2, charge:3, name:4, nickname:5,
  age:6, pid:7, phone:8, image:9, evidence:10, location:11, file:12, note:13
};

/* คอลัมน์รูปแบบเก่า */
const OLD_COL = {
  seq:0, date:1, charge:2, name:3, age:4, pid:5, evidence:6, location:7
};

const MONTH_MAP = {
  'ม.ค.':1,'ก.พ.':2,'มี.ค.':3,'เม.ย.':4,'พ.ค.':5,'มิ.ย.':6,
  'ก.ค.':7,'ส.ค.':8,'ก.ย.':9,'ต.ค.':10,'พ.ย.':11,'ธ.ค.':12
};

const MONTH_ABBR_LIST = ['','ม.ค.','ก.พ.','มี.ค.','เม.ย.','พ.ค.','มิ.ย.',
                         'ก.ค.','ส.ค.','ก.ย.','ต.ค.','พ.ย.','ธ.ค.'];

const SHEET_MONTH_RE = /(ม\.ค\.|ก\.พ\.|มี\.ค\.|เม\.ย\.|พ\.ค\.|มิ\.ย\.|ก\.ค\.|ส\.ค\.|ก\.ย\.|ต\.ค\.|พ\.ย\.|ธ\.ค\.)\.?\s*(\d{2})/;

// ─── Entry point ──────────────────────────────────────────────────────────────
function doGet(e) {
  try {
    // ตรวจสอบ secret key
    const key = (e && e.parameter && e.parameter.key) || '';
    if (key !== SECRET_KEY) {
      return json({ error: 'Unauthorized', hint: 'ต้องส่ง ?key=SECRET_KEY' });
    }
    return json(getAllRecords());
  } catch (err) {
    return json({ error: err.message, stack: err.stack });
  }
}

function json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

// ─── Fetch all sheets ─────────────────────────────────────────────────────────
function getAllRecords() {
  const ss      = SpreadsheetApp.getActiveSpreadsheet();
  const sheets  = ss.getSheets();
  const records = [];
  let   sheetsProcessed = 0;

  for (const ws of sheets) {
    const name = ws.getName().trim();
    // ข้ามชีทที่ไม่ใช่ข้อมูล
    if (SKIP_SHEETS.some(s => name.includes(s))) continue;
    // ข้ามชีทที่ชื่อไม่มีเดือน (เช่น ชีทสรุป, ชีทหมายจับ)
    const recs = parseSheet(ws, name);
    records.push(...recs);
    sheetsProcessed++;
  }

  return {
    records:         records,
    total:           records.length,
    sheetsProcessed: sheetsProcessed
  };
}

// ─── Parse one sheet ──────────────────────────────────────────────────────────
function parseSheet(ws, sheetName) {
  const data = ws.getDataRange().getValues();
  if (data.length < 2) return [];

  // Detect month/year from tab name
  let monthNum = 0, yearBe = 0, monthAbbr = '';
  const sm = sheetName.match(SHEET_MONTH_RE);
  if (sm) {
    yearBe    = parseInt(sm[2]) + 2500;
    monthAbbr = sm[1];
    monthNum  = MONTH_MAP[monthAbbr] || 0;
  }

  // Find data header row (ต้องมี "ข้อหา" และ "ชื่อ"/"ผู้ต้องหา")
  let headerIdx = -1;
  for (let i = 0; i < Math.min(data.length, 10); i++) {
    const joined = data[i].map(cellStr).join(' ');
    if (joined.includes('ข้อหา') &&
        (joined.includes('ชื่อ') || joined.includes('ผู้ต้องหา'))) {
      headerIdx = i;
      break;
    }
  }
  if (headerIdx < 0) return [];

  const headerText = data[headerIdx].map(cellStr).join(' ');
  const isNew = /กลุ่มฐานความผิด|ไฟล์บันทึก/.test(headerText);
  const COL   = isNew ? NEW_COL : OLD_COL;

  const records = [];

  for (let i = headerIdx + 1; i < data.length; i++) {
    const row = data[i];

    // helper: get cell value as trimmed string
    function g(key) {
      const idx = COL[key];
      if (idx === undefined || idx < 0 || idx >= row.length) return '';
      return cellStr(row[idx]);
    }

    const name = g('name');
    const seq  = g('seq');

    // ข้ามแถวหัวตาราง / แถวว่าง
    if (!name) continue;
    if (/^(ชื่อ|ชื่อ \/ สกุล|ชื่อ-สกุล|ผู้ต้องหา|-+|ลำดับ)$/.test(name)) continue;
    if (seq === 'ลำดับ') continue;
    // ข้ามแถวที่ทุกเซลล์ว่าง
    if (row.every(v => v === null || v === undefined || v === '')) continue;

    let imageUrl = null;
    if (isNew) {
      const imgVal  = g('image');
      const fileUrl = g('file');
      imageUrl = driveToImage(imgVal) || driveToImage(fileUrl)
              || (imgVal.startsWith('http') ? imgVal : null);
    }

    records.push({
      sheet:     sheetName,
      yearBe:    yearBe,
      monthNum:  monthNum,
      monthAbbr: monthAbbr,
      seq:       seq,
      date:      g('date'),
      group:     isNew ? g('group')    : '',
      charge:    g('charge'),
      name:      name,
      nickname:  isNew ? g('nickname') : '',
      age:       g('age'),
      pid:       g('pid'),
      evidence:  g('evidence'),
      location:  g('location'),
      note:      isNew ? g('note')     : '',
      imageUrl:  imageUrl
    });
  }

  return records;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

/**
 * แปลงค่าเซลล์เป็น string รองรับ Date object
 * กรณีที่เซลล์ใน Sheets ถูก format เป็น Date → getValues() คืน Date object
 */
function cellStr(v) {
  if (v === null || v === undefined || v === '') return '';
  if (v instanceof Date) {
    // แปลงเป็นรูปแบบ "7 ก.ค. 69"
    const d = v.getDate();
    const m = v.getMonth() + 1;           // 0-based → 1-based
    const y = (v.getFullYear() - 2500) % 100; // CE → BE 2-digit
    return d + ' ' + (MONTH_ABBR_LIST[m] || m) + ' ' + y;
  }
  return String(v).trim();
}

function driveToImage(url) {
  if (!url) return null;
  const m = url.match(/drive\.google\.com\/file\/d\/([^/?&\s]+)/);
  return m ? 'https://lh3.googleusercontent.com/d/' + m[1] : null;
}

/**
 * LINE Bot สน.บางชัน — Google Apps Script Data API
 * วิธีใช้: Deploy → New Deployment → Web App
 *          Execute as: Me, Who has access: Anyone
 * แล้วคัดลอก URL ไปใส่ใน APPS_SCRIPT_URL ของ LINE Bot
 *
 * ความปลอดภัย: เรียกได้เฉพาะคนที่รู้ SECRET_KEY เท่านั้น
 * ตั้งค่า key ให้ตรงกับ APPS_SCRIPT_KEY ใน LINE Bot
 */

const SECRET_KEY  = 'bangchan-secret-2026';   // ← เปลี่ยนได้ตามต้องการ
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

const SHEET_MONTH_RE = /(ม\.ค\.|ก\.พ\.|มี\.ค\.|เม\.ย\.|พ\.ค\.|มิ\.ย\.|ก\.ค\.|ส\.ค\.|ก\.ย\.|ต\.ค\.|พ\.ย\.|ธ\.ค\.)\.?\s*(\d{2})/;

// ─── Entry point ──────────────────────────────────────────────────────────────
function doGet(e) {
  try {
    // ตรวจสอบ secret key
    const key = (e.parameter && e.parameter.key) || '';
    if (key !== SECRET_KEY) {
      return ContentService
        .createTextOutput(JSON.stringify({ error: 'Unauthorized' }))
        .setMimeType(ContentService.MimeType.JSON);
    }

    const result = getAllRecords();
    return ContentService
      .createTextOutput(JSON.stringify(result))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ error: err.message }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

// ─── Fetch all sheets ─────────────────────────────────────────────────────────
function getAllRecords() {
  const ss      = SpreadsheetApp.getActiveSpreadsheet();
  const sheets  = ss.getSheets();
  const records = [];

  for (const ws of sheets) {
    const name = ws.getName().trim();
    if (SKIP_SHEETS.some(s => name.includes(s))) continue;
    const recs = parseSheet(ws, name);
    records.push(...recs);
  }

  return { records: records, total: records.length };
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

  // Find data header row (contains "ข้อหา" and "ชื่อ")
  let headerIdx = -1;
  for (let i = 0; i < data.length; i++) {
    const joined = data[i].join(' ');
    if (joined.includes('ข้อหา') && (joined.includes('ชื่อ') || joined.includes('ผู้ต้องหา'))) {
      headerIdx = i;
      break;
    }
  }
  if (headerIdx < 0) return [];

  const isNew = data[headerIdx].join(' ').match(/กลุ่มฐานความผิด|ไฟล์บันทึก/);

  const records = [];

  for (let i = headerIdx + 1; i < data.length; i++) {
    const row = data[i];

    function g(col, key) {
      const idx = col[key];
      if (idx === undefined || idx < 0 || idx >= row.length) return '';
      const v = row[idx];
      return (v !== null && v !== undefined) ? String(v).trim() : '';
    }

    const name = isNew ? g(NEW_COL, 'name') : g(OLD_COL, 'name');

    // Skip blank / header rows
    if (!name || name.match(/^(ชื่อ|ชื่อ \/ สกุล|ชื่อ-สกุล|ผู้ต้องหา|-+)$/)) continue;
    if (g(isNew ? NEW_COL : OLD_COL, 'seq') === 'ลำดับ') continue;

    let imageUrl = null;
    if (isNew) {
      // Column J (image) may have =IMAGE() result or direct URL
      const imgVal  = g(NEW_COL, 'image');
      const fileUrl = g(NEW_COL, 'file');
      imageUrl = driveToImage(imgVal) || driveToImage(fileUrl) || (imgVal.startsWith('http') ? imgVal : null);
    }

    const rec = {
      sheet:     sheetName,
      yearBe:    yearBe,
      monthNum:  monthNum,
      monthAbbr: monthAbbr,
      seq:       isNew ? g(NEW_COL,'seq')      : g(OLD_COL,'seq'),
      date:      isNew ? g(NEW_COL,'date')     : g(OLD_COL,'date'),
      group:     isNew ? g(NEW_COL,'group')    : '',
      charge:    isNew ? g(NEW_COL,'charge')   : g(OLD_COL,'charge'),
      name:      name,
      nickname:  isNew ? g(NEW_COL,'nickname') : '',
      age:       isNew ? g(NEW_COL,'age')      : g(OLD_COL,'age'),
      pid:       isNew ? g(NEW_COL,'pid')      : g(OLD_COL,'pid'),
      evidence:  isNew ? g(NEW_COL,'evidence') : g(OLD_COL,'evidence'),
      location:  isNew ? g(NEW_COL,'location') : g(OLD_COL,'location'),
      note:      isNew ? g(NEW_COL,'note')     : '',
      imageUrl:  imageUrl
    };

    records.push(rec);
  }

  return records;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
function driveToImage(url) {
  if (!url) return null;
  const m = url.match(/drive\.google\.com\/file\/d\/([^/?&\s]+)/);
  return m ? 'https://lh3.googleusercontent.com/d/' + m[1] : null;
}

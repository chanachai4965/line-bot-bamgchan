/**
 * LINE Bot สน.บางชัน — Google Apps Script Data API v6.8
 * วิธีใช้: Deploy → New Deployment → Web App
 *          Execute as: Me, Who has access: Anyone
 *
 * ความปลอดภัย: ต้องส่ง ?key=SECRET_KEY มาด้วย
 * ค่า key ต้องตรงกับ APPS_SCRIPT_KEY ใน LINE Bot (Render)
 */

const SECRET_KEY     = 'bangchan-secret-2026';            // ← ต้องตรงกับ APPS_SCRIPT_KEY
const SPREADSHEET_ID = '1DKdVKQCBcEcm9dYbLFPHu_Fzxr1fbvyi_4q3DR8n-gg'; // ชีตข้อมูลจับกุม
const STAFF_SPREADSHEET_ID = '1o4kFO5gTlu9M8Qp_jwnl_xhARd7RqrQc'; // ชีตรายชื่อสืบสวน
const SKIP_SHEETS    = ['555', 'ตารางเปล่า', 'สรุป', 'หมายจับ', 'Sheet1'];

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
    const key = (e && e.parameter && e.parameter.key) || '';
    if (key !== SECRET_KEY) {
      return json({ error: 'Unauthorized', hint: 'ต้องส่ง ?key=SECRET_KEY' });
    }

    // v6 แยก API เพื่อลด timeout
    const mode = ((e && e.parameter && e.parameter.mode) || 'arrests').toLowerCase();
    if (mode === 'staff') {
      const staff = getStaffRecords();
      return json({ mode: 'staff', staff: staff, staffTotal: staff.length });
    }
    if (mode === 'arrests') {
      return json(getArrestRecords());
    }
    if (mode === 'ping') {
      return json({ ok: true, version: '6.0' });
    }
    return json({ error: 'Unknown mode', allowed: ['staff', 'arrests', 'ping'] });
  } catch (err) {
    return json({ error: err.message, stack: err.stack });
  }
}

function json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

// ─── Notification target configuration v6.8 ─────────────────────────────────
function doPost(e) {
  try {
    const raw = (e && e.postData && e.postData.contents) || '{}';
    const body = JSON.parse(raw);
    const key = String(body.key || '');

    if (key !== SECRET_KEY) {
      return json({ error: 'Unauthorized' });
    }

    const action = String(body.action || '').toLowerCase();
    const props = PropertiesService.getScriptProperties();
    const propName = 'LINE_DUTY_NOTIFY_TARGET';

    if (action === 'set_notify_target') {
      const target = String(body.target || '').trim();
      if (!/^[CUR][A-Za-z0-9_-]{10,}$/.test(target)) {
        return json({ error: 'Invalid LINE target ID' });
      }
      props.setProperty(propName, target);
      return json({ ok: true, action: action });
    }

    if (action === 'get_notify_target') {
      return json({
        ok: true,
        action: action,
        target: props.getProperty(propName) || ''
      });
    }

    if (action === 'clear_notify_target') {
      props.deleteProperty(propName);
      return json({ ok: true, action: action });
    }

    return json({ error: 'Unknown action' });
  } catch (err) {
    return json({ error: err.message, stack: err.stack });
  }
}


// ─── Fetch all sheets ─────────────────────────────────────────────────────────
function getArrestRecords() {
  // ใช้ openById() แทน getActiveSpreadsheet()
  // เพื่อให้ทำงานได้ทั้ง standalone project และ bound project
  const ss      = SpreadsheetApp.openById(SPREADSHEET_ID);
  const sheets  = ss.getSheets();
  const records = [];
  let   sheetsProcessed = 0;
  const skipped = [];

  for (const ws of sheets) {
    const name = ws.getName().trim();
    // ข้ามชีทที่ไม่ใช่ข้อมูล
    if (SKIP_SHEETS.some(s => name.includes(s))) {
      skipped.push(name);
      continue;
    }
    const recs = parseSheet(ws, name);
    records.push(...recs);
    sheetsProcessed++;
  }

  return {
    mode:            'arrests',
    records:         records,
    total:           records.length,
    sheetsProcessed: sheetsProcessed,
    totalSheets:     sheets.length,
    skipped:         skipped.length
  };
}

// ─── Parse one sheet ──────────────────────────────────────────────────────────
function parseSheet(ws, sheetName) {
  const range     = ws.getDataRange();
  const data      = range.getValues();
  const formulas  = range.getFormulas();
  const richTexts = range.getRichTextValues();
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
      const imageIdx = COL.image;
      const fileIdx  = COL.file;

      const rawImgValue = imageIdx !== undefined ? row[imageIdx] : null;
      const rawFileValue= fileIdx  !== undefined ? row[fileIdx]  : null;

      const imgVal      = g('image');
      const fileVal     = g('file');
      const imgFormula  = imageIdx !== undefined ? (formulas[i][imageIdx] || '') : '';
      const fileFormula = fileIdx  !== undefined ? (formulas[i][fileIdx]  || '') : '';
      const imgLink     = imageIdx !== undefined ? richTextLink(richTexts[i][imageIdx]) : '';
      const fileLink    = fileIdx  !== undefined ? richTextLink(richTexts[i][fileIdx])  : '';

      // รองรับภาพที่แทรกอยู่ในเซลล์โดยตรง (Insert image in cell)
      imageUrl = cellImageUrl(rawImgValue)
              || normaliseImageUrl(imgVal)
              || normaliseImageUrl(imgLink)
              || imageUrlFromFormula(imgFormula)
              || cellImageUrl(rawFileValue)
              || normaliseImageUrl(fileVal)
              || normaliseImageUrl(fileLink)
              || imageUrlFromFormula(fileFormula);
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

// ─── Staff database ───────────────────────────────────────────────────────────
function getStaffRecords() {
  if (!STAFF_SPREADSHEET_ID ||
      STAFF_SPREADSHEET_ID === 'PUT_STAFF_GOOGLE_SHEET_ID_HERE') {
    return [];
  }

  const ss = SpreadsheetApp.openById(STAFF_SPREADSHEET_ID);
  const main = ss.getSheetByName('Sheet1') || ss.getSheets()[0];
  if (!main) throw new Error('ไม่พบชีตรายชื่อสืบสวน');

  const teamInfoByPhone = {};
  const teamMembers = [];

  for (const team of [1, 2]) {
    const ws = ss.getSheetByName('ชป.' + team);
    if (!ws) continue;

    const values = ws.getDataRange().getDisplayValues();
    let memberOrder = 0;

    // อ่านชื่อและเบอร์จากทุกแถว คนแรกเป็นผู้ควบคุมชุด
    for (let i = 0; i < values.length; i++) {
      const memberName = cellStr(values[i][1]);
      const phoneKey = phoneDigits(values[i][3]);

      if (!memberName || memberName.indexOf('ยศ ชื่อ') !== -1) continue;

      memberOrder++;
      const info = {
        team: team,
        controller: memberOrder === 1,
        teamOrder: memberOrder,
        nameKey: normalisePersonName(memberName),
        displayName: memberName,
        phoneKey: phoneKey
      };

      teamMembers.push(info);
      if (phoneKey) teamInfoByPhone[phoneKey] = info;
    }
  }

  const range = main.getDataRange();
  const values = range.getValues();
  const display = range.getDisplayValues();
  const formulas = range.getFormulas();
  const richTexts = range.getRichTextValues();
  const staff = [];

  // Sheet1: แถว 2 เป็นหัวตาราง, ข้อมูลเริ่มแถว 4
  for (let i = 3; i < values.length; i++) {
    const row = values[i];
    const shown = display[i];

    const name = cellStr(shown[1]);
    if (!name) continue;

    const phone = cellStr(shown[5]);
    const phoneKey = phoneDigits(phone);

    const rawImage = row[7];
    const imageText = cellStr(shown[7]);
    const imageFormula = formulas[i][7] || '';
    const imageLink = richTextLink(richTexts[i][7]);

    const teamInfo = teamInfoByPhone[phoneKey]
                  || findTeamInfoByName(name, teamMembers)
                  || {};

    staff.push({
      name: name,
      position: cellStr(shown[4]),
      phone: phone,
      nickname: cellStr(shown[6]),
      imageUrl: cellImageUrl(rawImage)
             || normaliseImageUrl(imageText)
             || normaliseImageUrl(imageLink)
             || imageUrlFromFormula(imageFormula),
      note: cellStr(shown[8]),
      team: teamInfo.team || 0,
      controller: Boolean(teamInfo.controller),
      teamOrder: teamInfo.teamOrder || 999
    });
  }

  return staff;
}

function normalisePersonName(value) {
  return String(value || '')
    .toLowerCase()
    .replace(/พ\.ต\.ท\.|พ\.ต\.ต\.|ร\.ต\.อ\.|ร\.ต\.ท\.|ร\.ต\.ต\.|ด\.ต\.|จ\.ส\.ต\.|ส\.ต\.อ\.|ส\.ต\.ท\.|ส\.ต\.ต\./g, '')
    .replace(/[ศษส]/g, 'ส')
    .replace(/[ฎฏ]/g, 'ด')
    .replace(/์/g, '')
    .replace(/[^ก-๙a-z0-9]/g, '');
}

function levenshteinDistance(a, b) {
  a = String(a || '');
  b = String(b || '');

  const matrix = [];
  for (let i = 0; i <= b.length; i++) matrix[i] = [i];
  for (let j = 0; j <= a.length; j++) matrix[0][j] = j;

  for (let i = 1; i <= b.length; i++) {
    for (let j = 1; j <= a.length; j++) {
      const cost = b.charAt(i - 1) === a.charAt(j - 1) ? 0 : 1;
      matrix[i][j] = Math.min(
        matrix[i - 1][j] + 1,
        matrix[i][j - 1] + 1,
        matrix[i - 1][j - 1] + cost
      );
    }
  }
  return matrix[b.length][a.length];
}

function findTeamInfoByName(name, teamMembers) {
  const key = normalisePersonName(name);
  if (!key) return null;

  let best = null;
  let bestDistance = 999;

  for (const member of teamMembers) {
    if (!member.nameKey) continue;

    if (key === member.nameKey ||
        key.indexOf(member.nameKey) !== -1 ||
        member.nameKey.indexOf(key) !== -1) {
      return member;
    }

    const distance = levenshteinDistance(key, member.nameKey);
    if (distance < bestDistance) {
      bestDistance = distance;
      best = member;
    }
  }

  return bestDistance <= 3 ? best : null;
}


function phoneDigits(value) {
  const digits = String(value || '').replace(/\D/g, '');
  // ใช้ 9 หลักท้าย ป้องกันรูปแบบ 0xx-xxx-xxxx ต่างกัน
  return digits ? digits.slice(-9) : '';
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

function cellImageUrl(value) {
  try {
    if (value &&
        value.valueType === SpreadsheetApp.ValueType.IMAGE &&
        typeof value.getContentUrl === 'function') {
      return value.getContentUrl() || null;
    }
  } catch (err) {
    console.log('cellImageUrl error: ' + err.message);
  }
  return null;
}


function richTextLink(richText) {
  if (!richText) return '';
  const direct = richText.getLinkUrl();
  if (direct) return direct;

  const runs = richText.getRuns ? richText.getRuns() : [];
  for (const run of runs) {
    const url = run.getLinkUrl();
    if (url) return url;
  }
  return '';
}

function imageUrlFromFormula(formula) {
  if (!formula) return null;

  // รองรับ =IMAGE("url") และ =HYPERLINK("url","ข้อความ")
  const m = formula.match(/=(?:IMAGE|HYPERLINK)\s*\(\s*"([^"]+)"/i);
  return m ? normaliseImageUrl(m[1]) : null;
}

function normaliseImageUrl(url) {
  if (!url) return null;
  url = String(url).trim();
  if (!url) return null;

  // Google Drive รูปแบบ /file/d/FILE_ID
  let m = url.match(/drive\.google\.com\/file\/d\/([^/?&\s]+)/i);
  if (m) return 'https://lh3.googleusercontent.com/d/' + m[1];

  // Google Drive รูปแบบ open?id=, uc?id= หรือ thumbnail?id=
  m = url.match(/[?&]id=([^&\s]+)/i);
  if (m && /drive\.google\.com|googleusercontent\.com/i.test(url)) {
    return 'https://lh3.googleusercontent.com/d/' + m[1];
  }

  // ลิงก์รูปภาพตรง หรือ lh3.googleusercontent.com
  if (/^https?:\/\//i.test(url)) return url;

  return null;
}

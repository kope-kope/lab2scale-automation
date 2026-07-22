/**
 * Lab2Scale — Leads web app (Google Apps Script).
 *
 * Lets the Lab2Scale weekly run append candidate companies to THIS sheet as
 * leads, without a Google Cloud service account (works even when your org
 * blocks service-account keys). It dedups by Company + Source URL.
 *
 * SETUP
 *   1. Open your Google Sheet → Extensions → Apps Script.
 *   2. Delete any boilerplate, paste this whole file, and set SECRET below to a
 *      long random string.
 *   3. Deploy → New deployment → gear icon → Web app.
 *        - Description: "Lab2Scale leads"
 *        - Execute as:  Me (your account)
 *        - Who has access: Anyone
 *      → Deploy → Authorize when prompted → copy the Web app URL (ends in /exec).
 *   4. Give Lab2Scale:
 *        LEADS_WEBAPP_URL    = <the /exec URL>
 *        LEADS_WEBAPP_SECRET = <the same SECRET you set below>
 *
 * To change columns later, edit HEADER + the row order in doPost.
 */

const SECRET = 'CHANGE_ME_to_a_long_random_string';
const TAB = 'Leads';
const HEADER = ['Date', 'Company', 'Sector', 'Stage', 'Why it fits',
                'Contacts', 'Source URL', 'Relevance', 'Status'];

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    if (SECRET && body.secret !== SECRET) {
      return json_({ error: 'unauthorized' });
    }

    const ss = SpreadsheetApp.getActiveSpreadsheet();
    let sh = ss.getSheetByName(TAB) || ss.insertSheet(TAB);
    if (sh.getLastRow() === 0) sh.appendRow(HEADER);

    // Dedup on the normalized COMPANY NAME only (column B), so the same
    // company surfaced from a different source URL is never re-added.
    const values = sh.getDataRange().getValues();
    const seen = {};
    for (let i = 1; i < values.length; i++) {
      const k = key_(values[i][1]);
      if (k) seen[k] = true;
    }

    const rows = [];
    (body.leads || []).forEach(function (l) {
      const k = key_(l.company);
      if (k && seen[k]) return;   // already have this company
      if (k) seen[k] = true;      // (empty names aren't deduped — always append)
      rows.push([l.date || '', l.company || '', l.sector || '', l.stage || '',
                 l.why || '', l.contacts || '', l.url || '', l.relevance || '', 'New']);
    });

    if (rows.length) {
      sh.getRange(sh.getLastRow() + 1, 1, rows.length, HEADER.length).setValues(rows);
    }
    return json_({ added: rows.length });
  } catch (err) {
    return json_({ error: String(err) });
  }
}

// Normalize a company name for dedup: lowercase, drop legal suffixes
// (Inc/LLC/Corp/Ltd/Co) and all punctuation/spaces. "Ferveret, Inc." and
// "ferveret" collapse to the same key.
function key_(company) {
  return String(company || '')
    .toLowerCase()
    .replace(/\b(inc|llc|corp|corporation|ltd|limited|co)\b\.?/g, '')
    .replace(/[^a-z0-9]+/g, '')
    .trim();
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

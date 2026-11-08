/**
 * Job Pipeline - instant moves when you edit (paste into Extensions > Apps Script).
 *
 * Interest:  Pass  -> the job moves to Passed
 *            1-5   -> stays in To review (shaded) and is copied to My applications
 *            blank -> back to To review only
 * Status / Next step / Due / Notes: copied to the job's other row, so both copies agree.
 * The hourly pipeline still adds new jobs and tidies up; this only reacts to your edits.
 */
var REVIEW = 'To review', APPS = 'My applications', PASSED = 'Passed';
var JOB_TABS = [REVIEW, APPS, PASSED];
var MIRRORED = ['Interest', 'Status', 'Next step', 'Due', 'Notes'];

function onEdit(e) {
  var sheet = e.range.getSheet();
  var tab = sheet.getName();
  if (JOB_TABS.indexOf(tab) < 0 || e.range.getRow() < 2) return;
  var header = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  var col = {};
  header.forEach(function (h, i) { col[h] = i + 1; });
  if (!col['Interest'] || !col['Company'] || !col['Role']) return;

  var lock = LockService.getDocumentLock();
  lock.waitLock(20000);
  try {
    // one edit can cover several rows (paste / fill down): handle each, bottom-up
    for (var r = e.range.getLastRow(); r >= e.range.getRow(); r--) {
      for (var c = e.range.getColumn(); c <= e.range.getLastColumn(); c++) {
        var name = header[c - 1];
        if (MIRRORED.indexOf(name) >= 0) handle(sheet, tab, r, name, col);
      }
    }
  } finally {
    lock.releaseLock();
  }
}

function handle(sheet, tab, row, name, col) {
  var key = keyOf(sheet, row, col);
  if (!key) return;
  var value = sheet.getRange(row, col[name]).getDisplayValue().trim();

  // copy this field to the job's other rows
  JOB_TABS.forEach(function (t) {
    findRows(t, key).forEach(function (loc) {
      if (loc.sheet.getName() === tab && loc.row === row) return;
      var other = colsOf(loc.sheet);
      if (other[name]) loc.sheet.getRange(loc.row, other[name]).setValue(value);
    });
  });
  if (name !== 'Interest') return;

  var pick = value.toLowerCase();
  var source = sheet.getRange(row, 1, 1, sheet.getLastColumn());
  if (pick === 'pass') {
    ensureIn(PASSED, key, source);
    removeFrom(REVIEW, key); removeFrom(APPS, key);
  } else if (/^[1-5]$/.test(pick) || pick === 'apply') {
    ensureIn(REVIEW, key, source); ensureIn(APPS, key, source);
    removeFrom(PASSED, key);
  } else if (pick === '') {
    ensureIn(REVIEW, key, source);
    removeFrom(APPS, key); removeFrom(PASSED, key);
  }
}

// --- helpers -------------------------------------------------------------
function colsOf(sheet) {
  var h = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0], out = {};
  h.forEach(function (name, i) { out[name] = i + 1; });
  return out;
}

// a job is its Company + Role + Location (the hidden ID can be left behind by a sort)
function keyOf(sheet, row, col) {
  var get = function (n) { return col[n] ? String(sheet.getRange(row, col[n]).getDisplayValue()).trim().toLowerCase() : ''; };
  var k = [get('Company'), get('Role'), get('Location')].join('|');
  return k === '||' ? null : k;
}

function findRows(tabName, key) {
  var sh = SpreadsheetApp.getActive().getSheetByName(tabName);
  if (!sh || sh.getLastRow() < 2) return [];
  var col = colsOf(sh), out = [];
  var n = sh.getLastRow() - 1;
  var vals = {};
  ['Company', 'Role', 'Location'].forEach(function (h) {
    vals[h] = col[h] ? sh.getRange(2, col[h], n, 1).getDisplayValues() : null;
  });
  for (var i = 0; i < n; i++) {
    var k = ['Company', 'Role', 'Location'].map(function (h) {
      return vals[h] ? String(vals[h][i][0]).trim().toLowerCase() : '';
    }).join('|');
    if (k === key) out.push({ sheet: sh, row: i + 2 });
  }
  return out;
}

function ensureIn(tabName, key, source) {
  if (findRows(tabName, key).length) return;
  var sh = SpreadsheetApp.getActive().getSheetByName(tabName);
  if (!sh) return;
  var target = sh.getRange(Math.max(sh.getLastRow(), 1) + 1, 1, 1, source.getNumColumns());
  source.copyTo(target);           // keeps the Role link and formats
}

function removeFrom(tabName, key) {
  findRows(tabName, key).reverse().forEach(function (loc) { loc.sheet.deleteRow(loc.row); });
}

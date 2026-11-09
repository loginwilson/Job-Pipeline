// Paste into the Sheet once: Extensions -> Apps Script (in a computer browser), then Save.
// Re-sorts Apply the moment you change a score: 5s first, Passes last.
function onEdit(e) {
  const sh = e.range.getSheet();
  if (sh.getName() !== 'Apply' || e.range.getColumn() !== 1 || e.range.getRow() < 2) return;
  const col = sh.getRange('A:A').getValues();
  let last = 1;
  while (last < col.length && col[last][0] !== '') last++;   // the list ends at the first blank row
  const n = last - 1;
  if (n < 2) return;
  const keyCol = sh.getMaxColumns();                          // a spare column, used only while sorting
  const keys = col.slice(1, last).map(r => [r[0] === 'Pass' ? -1 : (Number(r[0]) || 0)]);
  sh.getRange(2, keyCol, n, 1).setValues(keys);
  sh.getRange(2, 1, n, keyCol).sort({column: keyCol, ascending: false});
  sh.getRange(2, keyCol, n, 1).clearContent();
}

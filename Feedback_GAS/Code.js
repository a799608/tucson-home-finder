/**
 * Tucson Home Finder — feedback endpoint (JSONP).
 * Records ✗ Pass (with reason) and ♥ Interested actions from the GitHub Pages
 * site into a Google Sheet, so preferences can be tuned from real decisions.
 *
 * Storage: a spreadsheet auto-created on first write; its ID is kept in
 * ScriptProperties.SHEET_ID. Tabs: rejections, interests.
 *
 * API (all GET, JSONP via &callback=fn, token-gated &token=tucson):
 *   ?action=reject&id=..&reason=..&price=..&address=..&hood=..&url=..
 *   ?action=interest&id=..&state=on|off&price=..&address=..&hood=..&url=..
 *   ?action=list            -> {rejections:[...], interests:[...]}
 *   ?action=ping            -> {ok:true}
 */
var TOKEN = 'tucson';

function ss_() {
  var props = PropertiesService.getScriptProperties();
  var id = props.getProperty('SHEET_ID');
  var ss;
  if (id) {
    ss = SpreadsheetApp.openById(id);
  } else {
    ss = SpreadsheetApp.create('Tucson Home Finder — Feedback');
    props.setProperty('SHEET_ID', ss.getId());
  }
  ['rejections', 'interests'].forEach(function (name) {
    if (!ss.getSheetByName(name)) {
      var sh = ss.insertSheet(name);
      sh.appendRow(['when', 'listing_id', 'state_or_reason', 'price', 'address', 'neighborhood', 'url']);
    }
  });
  var s1 = ss.getSheetByName('Sheet1');
  if (s1 && ss.getSheets().length > 2) ss.deleteSheet(s1);
  return ss;
}

function out_(e, obj) {
  var payload = JSON.stringify(obj);
  var cb = e && e.parameter && e.parameter.callback;
  if (cb) {
    return ContentService.createTextOutput(cb + '(' + payload + ')')
      .setMimeType(ContentService.MimeType.JAVASCRIPT);
  }
  return ContentService.createTextOutput(payload).setMimeType(ContentService.MimeType.JSON);
}

function doGet(e) {
  try {
    var p = (e && e.parameter) || {};
    if (p.token !== TOKEN) return out_(e, { ok: false, error: 'bad token' });
    var action = p.action || 'ping';
    if (action === 'ping') return out_(e, { ok: true });

    var ss = ss_();
    if (action === 'reject') {
      ss.getSheetByName('rejections').appendRow([
        new Date(), p.id || '', p.reason || '', p.price || '', p.address || '', p.hood || '', p.url || '']);
      return out_(e, { ok: true });
    }
    if (action === 'interest') {
      ss.getSheetByName('interests').appendRow([
        new Date(), p.id || '', p.state || 'on', p.price || '', p.address || '', p.hood || '', p.url || '']);
      return out_(e, { ok: true });
    }
    if (action === 'list') {
      var grab = function (name) {
        var sh = ss.getSheetByName(name);
        var v = sh.getDataRange().getValues();
        v.shift();
        return v.map(function (r) {
          return { when: r[0], id: r[1], state_or_reason: r[2], price: r[3], address: r[4], hood: r[5], url: r[6] };
        });
      };
      return out_(e, { ok: true, rejections: grab('rejections'), interests: grab('interests') });
    }
    return out_(e, { ok: false, error: 'unknown action' });
  } catch (err) {
    return out_(e, { ok: false, error: String(err) });
  }
}

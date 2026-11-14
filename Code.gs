/**
 * 資産管理アプリ バックエンド（Google Apps Script・スプレッドシートにバインド）
 *  - 保有資産・配当受取・資産推移・設定をシートに保存
 *  - GOOGLEFINANCE で株価・為替・暗号資産・金価格を自動取得
 *  - 毎日 7時・16時 に価格更新＋資産推移を記録
 * 初回は setup() を実行してください。
 */
const TZ = 'Asia/Tokyo';
const GRAM_PER_OZ = 31.1034768;
const CATS = ['jp', 'us', 'hk', 'fund', 'crypto', 'gold', 'cash', 'other'];
const DEF_CUR = { jp: 'JPY', us: 'USD', hk: 'HKD', fund: 'JPY', crypto: 'JPY', gold: 'JPY', cash: 'JPY', other: 'JPY' };

const SCHEMA = {
  holdings: { name: '保有', text: [1, 2, 3], cols: [
    ['id', 'ID'], ['type', '種別'], ['code', 'コード'], ['name', '名称'], ['account', '口座'],
    ['qty', '数量'], ['cost', '取得単価'], ['currency', '通貨'], ['costFx', '取得時為替'],
    ['dps', '年間配当/株'], ['manualPrice', '手動価格'], ['price', '現在値'], ['prevClose', '前日終値'],
    ['priceUpdated', '価格更新'], ['memo', 'メモ']] },
  dividends: { name: '配当受取', text: [1, 2, 3], cols: [
    ['id', 'ID'], ['date', '受取日'], ['code', 'コード'], ['name', '名称'],
    ['amount', '受取額(円・税引後)'], ['account', '口座'], ['memo', 'メモ']] },
  history: { name: '資産推移', text: [1], cols: [
    ['date', '日付'], ['total', '総資産'], ['cost', '取得額'], ['jp', '日本株'], ['us', '米国株'],
    ['hk', '中国株'], ['fund', '投資信託'], ['crypto', '暗号資産'], ['gold', '金'], ['cash', '現金'], ['other', 'その他']] },
  settings: { name: '設定', text: [1], cols: [['key', '項目'], ['value', '値']] },
};

const DEFAULT_SETTINGS = { expense: 3000000, withdrawRate: 4, target: '', monthly: 100000, returnRate: 5, age: '' };

/* ---------- セットアップ ---------- */
function setup() {
  Object.keys(SCHEMA).forEach(sheet_);
  const props = PropertiesService.getScriptProperties();
  let tok = props.getProperty('TOKEN');
  if (!tok) { tok = Utilities.getUuid().replace(/-/g, '').slice(0, 16); props.setProperty('TOKEN', tok); }

  ScriptApp.getProjectTriggers().filter(t => t.getHandlerFunction() === 'dailyJob').forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('dailyJob').timeBased().atHour(7).everyDays(1).inTimezone(TZ).create();
  ScriptApp.newTrigger('dailyJob').timeBased().atHour(16).everyDays(1).inTimezone(TZ).create();

  const s = readSettings_();
  if (!Object.keys(s).length) saveSettings_(DEFAULT_SETTINGS);
  updatePrices_(); snapshot_();
  Logger.log('セットアップ完了。トークン: ' + tok);
  try { SpreadsheetApp.getUi().alert('セットアップ完了\n\nアプリに入力するトークン:\n' + tok); } catch (e) {}
}

function onOpen() {
  SpreadsheetApp.getUi().createMenu('資産管理')
    .addItem('株価を今すぐ更新', 'dailyJob')
    .addItem('日本株の株数を保有詳細へ書く', 'pushNow')
    .addItem('トークンを表示', 'showToken')
    .addItem('初期設定（setup）', 'setup')
    .addToUi();
}
function showToken() {
  SpreadsheetApp.getUi().alert('トークン:\n' + PropertiesService.getScriptProperties().getProperty('TOKEN'));
}

/* 定期実行（トリガー） */
function dailyJob() { withLock_(() => { updatePrices_(); snapshot_(); pushToHoldDetail_(); }); }
function pushNow() { withLock_(() => { const log = pushToHoldDetail_();
  try { SpreadsheetApp.getUi().alert('保有詳細へ書き込みました\n\n' + (log.join('\n') || '変更なし')); } catch (e) {} }); }

/* ---------- 日本株の株数・取得単価を保有詳細（四季報ビューア 監視銘柄）へ書く ----------
 * 日本株の株数・取得単価はこのアプリ（資産管理）が元（2026-11-14 持ち主の決定）。口座別に入力する。
 *  - 同じコードの日本株の行を合計し、取得単価は株数で加重平均して、保有詳細のC列（株数）・D列（取得単価）に書く。
 *  - 保有詳細に無い銘柄は行を作らない（ストーリーが必要なため。ログに出す）。
 *  - 保有詳細にあってこのアプリに無い銘柄は触らない（ログに出す）。
 *  - 保存・削除のたびと、毎日7時・16時に書く。
 * 四季報ビューアの保有タブで株数を直しても、次にここから書くと上書きされる。株数はこのアプリで入力する。
 */
const HOLD_SS_ID = '1FleDcwpRCVdc-19EMSSqsT7bmTMAq2cp_fzuXgJtQX0';
function pushToHoldDetail_() {
  const log = [];
  const agg = {};
  readRows_('holdings').filter(h => h.type === 'jp').forEach(h => {
    const c = String(h.code).trim().toUpperCase(), q = Number(h.qty) || 0, k = Number(h.cost) || 0;
    if (!c || q <= 0) return;
    const a = agg[c] = agg[c] || { qty: 0, amt: 0, costMissing: false };
    a.qty += q; a.amt += q * k; if (!k) a.costMissing = true;
  });
  let det;
  try { det = SpreadsheetApp.openById(HOLD_SS_ID).getSheetByName('保有詳細'); }
  catch (e) { Logger.log('保有詳細を開けません: ' + e); return ['保有詳細を開けませんでした: ' + e]; }
  if (!det) return ['保有詳細タブがありません'];
  const n = det.getLastRow() - 1;
  if (n < 1) return ['保有詳細に行がありません'];
  const vals = det.getRange(2, 1, n, 4).getValues();
  const seen = {};
  vals.forEach((r, i) => {
    const c = String(r[0]).trim().toUpperCase();
    if (!c) return;
    seen[c] = 1;
    const a = agg[c];
    if (!a) { log.push('このアプリに無いので触っていない: ' + c + ' ' + r[1]); return; }
    const cost = a.costMissing ? r[3] : Math.round(a.amt / a.qty * 10) / 10;
    if (Number(r[2]) !== a.qty || Number(r[3]) !== Number(cost)) {
      det.getRange(i + 2, 3, 1, 2).setValues([[a.qty, cost]]);
      log.push('更新 ' + c + ' ' + a.qty + '株・' + cost + '円');
    }
  });
  Object.keys(agg).filter(c => !seen[c]).forEach(c => log.push('保有詳細に行が無い（ストーリー未作成）: ' + c));
  Logger.log(log.join('\n'));
  return log;
}

/* ---------- Web API ---------- */
function doGet(e) { return handle_((e && e.parameter) || {}); }
function doPost(e) {
  let p = {};
  try { p = JSON.parse(e.postData.contents); } catch (err) {}
  return handle_(p);
}

function handle_(p) {
  try {
    const tok = PropertiesService.getScriptProperties().getProperty('TOKEN');
    if (!tok || p.token !== tok) return json_({ ok: false, error: '認証エラー：トークンが違います' });
    const d = p.data || {};
    const action = p.action || 'all';
    if (action !== 'all') {
      withLock_(() => {
        switch (action) {
          case 'saveHolding': saveHolding_(d); pushToHoldDetail_(); break;
          case 'deleteHolding': deleteRow_('holdings', d.id); snapshot_(); pushToHoldDetail_(); break;
          case 'saveDividend': saveDividend_(d); break;
          case 'deleteDividend': deleteRow_('dividends', d.id); break;
          case 'saveSettings': saveSettings_(d); break;
          case 'refresh': updatePrices_(); snapshot_(); break;
          default: throw new Error('不明な操作: ' + action);
        }
      });
    }
    return json_(Object.assign({ ok: true }, getAll_()));
  } catch (err) {
    return json_({ ok: false, error: String((err && err.message) || err) });
  }
}

function getAll_() {
  const st = state_();
  return {
    holdings: readRows_('holdings'),
    dividends: readRows_('dividends'),
    history: readRows_('history'),
    settings: readSettings_(),
    fx: st.fx, goldPerGram: st.goldPerGram || 0, updated: st.updated || '',
  };
}

/* ---------- 保存系 ---------- */
function saveHolding_(d) {
  const id = d.id || newId_();
  const row = findRow_('holdings', id);
  const old = row > 0 ? rowObj_('holdings', row) : null;
  const h = {
    id: id, type: CATS.indexOf(d.type) >= 0 ? d.type : 'other',
    code: String(d.code || '').trim().toUpperCase(), name: String(d.name || '').trim(),
    account: d.account || '', qty: numOrBlank_(d.qty), cost: numOrBlank_(d.cost),
    currency: d.currency || DEF_CUR[d.type] || 'JPY', costFx: numOrBlank_(d.costFx),
    dps: numOrBlank_(d.dps), manualPrice: numOrBlank_(d.manualPrice),
    price: old ? old.price : '', prevClose: old ? old.prevClose : '', priceUpdated: old ? old.priceUpdated : '',
    memo: d.memo || '',
  };
  if (h.manualPrice !== '') { h.price = h.manualPrice; }
  writeObj_('holdings', row > 0 ? row : sheet_('holdings').getLastRow() + 1, h);
  const changed = !old || old.code !== h.code || old.type !== h.type || String(old.manualPrice) !== String(h.manualPrice);
  if (changed) updatePrices_();
  snapshot_();
}

function saveDividend_(d) {
  const id = d.id || newId_();
  const row = findRow_('dividends', id);
  const o = { id: id, date: d.date || today_(), code: String(d.code || '').toUpperCase(), name: d.name || '',
    amount: numOrBlank_(d.amount), account: d.account || '', memo: d.memo || '' };
  writeObj_('dividends', row > 0 ? row : sheet_('dividends').getLastRow() + 1, o);
}

function saveSettings_(d) {
  const sh = sheet_('settings');
  const cur = readSettingsRows_();
  Object.keys(d).forEach(k => {
    const r = cur[k];
    if (r) sh.getRange(r, 2).setValue(d[k]);
    else { sh.appendRow([k, d[k]]); cur[k] = sh.getLastRow(); }
  });
}

/* ---------- 価格取得 ---------- */
function tickerOf_(h) {
  const code = String(h.code || '').trim().toUpperCase();
  if (!code) return null;
  switch (h.type) {
    case 'jp': return 'TYO:' + code;
    case 'us': return code;
    case 'hk': return 'HKG:' + code.replace(/^0+/, '').padStart(4, '0');
    case 'crypto': return 'CURRENCY:' + code + 'JPY';
    default: return null;
  }
}

function updatePrices_() {
  const hs = readRows_('holdings');
  const reqs = [['FX_USD', 'CURRENCY:USDJPY'], ['FX_HKD', 'CURRENCY:HKDJPY'], ['GOLD', 'CURRENCY:XAUUSD']];
  hs.forEach(h => {
    if (h.manualPrice !== '') return;
    const t = tickerOf_(h);
    if (t && !reqs.some(r => r[0] === t)) reqs.push([t, t]);
  });

  let sh = ss_().getSheetByName('_calc');
  if (!sh) { sh = ss_().insertSheet('_calc'); sh.hideSheet(); }
  sh.clear();
  sh.getRange(1, 1, reqs.length, 1).setValues(reqs.map(r => [r[0]]));
  sh.getRange(1, 2, reqs.length, 2).setFormulas(reqs.map(r => {
    const t = r[1];
    const isCur = t.indexOf('CURRENCY:') === 0;
    return ['=IFERROR(GOOGLEFINANCE("' + t + '","price"),"")',
      isCur ? '=""' : '=IFERROR(GOOGLEFINANCE("' + t + '","closeyest"),"")'];
  }));
  SpreadsheetApp.flush();
  Utilities.sleep(2500);
  let vals = sh.getRange(1, 1, reqs.length, 3).getValues();
  if (vals.filter(v => v[1] === '').length > reqs.length / 2) { Utilities.sleep(4000); vals = sh.getRange(1, 1, reqs.length, 3).getValues(); }
  const res = {};
  vals.forEach(v => { res[v[0]] = { price: typeof v[1] === 'number' ? v[1] : 0, prev: typeof v[2] === 'number' ? v[2] : 0 }; });

  const st = state_();
  if (res.FX_USD.price) st.fx.USD = res.FX_USD.price;
  if (res.FX_HKD.price) st.fx.HKD = res.FX_HKD.price;
  if (res.GOLD.price && st.fx.USD) st.goldPerGram = res.GOLD.price * st.fx.USD / GRAM_PER_OZ;
  st.updated = nowStr_();
  PropertiesService.getScriptProperties().setProperty('STATE', JSON.stringify(st));

  // 保有シートへ書き戻し
  const hsh = sheet_('holdings');
  const n = hsh.getLastRow() - 1;
  if (n < 1) return;
  const cols = SCHEMA.holdings.cols.map(c => c[0]);
  const ix = k => cols.indexOf(k);
  const rng = hsh.getRange(2, 1, n, cols.length);
  const m = rng.getValues();
  m.forEach(r => {
    if (r[0] === '') return;
    const h = {}; cols.forEach((c, i) => h[c] = norm_(r[i]));
    let price = null, prev = '';
    if (h.manualPrice !== '') price = Number(h.manualPrice);
    else if (h.type === 'cash') price = 1;
    else if (h.type === 'gold') price = st.goldPerGram ? Math.round(st.goldPerGram * 100) / 100 : null;
    else {
      const t = tickerOf_(h);
      if (t && res[t] && res[t].price) { price = res[t].price; prev = res[t].prev || ''; }
    }
    if (price !== null && price > 0) {
      r[ix('price')] = price; r[ix('prevClose')] = prev; r[ix('priceUpdated')] = st.updated;
    }
  });
  rng.setValues(m);
}

/* ---------- 資産推移の記録 ---------- */
function calc_(h, fx) {
  const cur = h.currency || DEF_CUR[h.type] || 'JPY';
  const rate = cur === 'JPY' ? 1 : (Number(fx[cur]) || 0);
  const div = h.type === 'fund' ? 10000 : 1;
  const q = Number(h.qty) || 0;
  const price = h.type === 'cash' ? 1 : (Number(h.price) || 0);
  const value = q * price / div * rate;
  const cp = h.type === 'cash' ? 1 : (Number(h.cost) || 0);
  const cfx = cur === 'JPY' ? 1 : (Number(h.costFx) || rate);
  const cost = cp > 0 ? q * cp / div * cfx : value;
  return { value: value, cost: cost };
}

function snapshot_() {
  const st = state_();
  const row = { date: today_(), total: 0, cost: 0 };
  CATS.forEach(c => row[c] = 0);
  readRows_('holdings').forEach(h => {
    const v = calc_(h, st.fx);
    row.total += v.value; row.cost += v.cost;
    row[CATS.indexOf(h.type) >= 0 ? h.type : 'other'] += v.value;
  });
  Object.keys(row).forEach(k => { if (k !== 'date') row[k] = Math.round(row[k]); });
  const r = findRow_('history', row.date);
  writeObj_('history', r > 0 ? r : sheet_('history').getLastRow() + 1, row);
}

/* ---------- シート操作ヘルパー ---------- */
function ss_() { return SpreadsheetApp.getActiveSpreadsheet(); }
function sheet_(k) {
  const s = SCHEMA[k];
  let sh = ss_().getSheetByName(s.name);
  if (!sh) {
    sh = ss_().insertSheet(s.name);
    sh.getRange(1, 1, 1, s.cols.length).setValues([s.cols.map(c => c[1])]).setFontWeight('bold');
    sh.setFrozenRows(1);
    (s.text || []).forEach(ci => sh.getRange(1, ci, sh.getMaxRows(), 1).setNumberFormat('@'));
  }
  return sh;
}
function readRows_(k) {
  const sh = sheet_(k), n = sh.getLastRow() - 1, cols = SCHEMA[k].cols;
  if (n < 1) return [];
  return sh.getRange(2, 1, n, cols.length).getValues()
    .filter(r => r[0] !== '' && r[0] !== null)
    .map(r => { const o = {}; cols.forEach((c, i) => o[c[0]] = norm_(r[i])); return o; });
}
function rowObj_(k, row) {
  const cols = SCHEMA[k].cols;
  const r = sheet_(k).getRange(row, 1, 1, cols.length).getValues()[0];
  const o = {}; cols.forEach((c, i) => o[c[0]] = norm_(r[i])); return o;
}
function findRow_(k, id) {
  const sh = sheet_(k), n = sh.getLastRow() - 1;
  if (n < 1 || id === '' || id == null) return -1;
  const ids = sh.getRange(2, 1, n, 1).getValues();
  for (let i = 0; i < n; i++) if (String(norm_(ids[i][0])) === String(id)) return i + 2;
  return -1;
}
function writeObj_(k, row, obj) {
  const cols = SCHEMA[k].cols;
  sheet_(k).getRange(row, 1, 1, cols.length)
    .setValues([cols.map(c => (obj[c[0]] === undefined || obj[c[0]] === null) ? '' : obj[c[0]])]);
}
function deleteRow_(k, id) { const r = findRow_(k, id); if (r > 0) sheet_(k).deleteRow(r); }
function readSettingsRows_() {
  const sh = sheet_('settings'), n = sh.getLastRow() - 1, o = {};
  if (n < 1) return o;
  sh.getRange(2, 1, n, 1).getValues().forEach((r, i) => { if (r[0] !== '') o[r[0]] = i + 2; });
  return o;
}
function readSettings_() {
  const o = {};
  readRows_('settings').forEach(r => o[r.key] = r.value);
  return o;
}
function state_() {
  let st = {};
  try { st = JSON.parse(PropertiesService.getScriptProperties().getProperty('STATE') || '{}'); } catch (e) {}
  st.fx = Object.assign({ JPY: 1, USD: 150, HKD: 19 }, st.fx || {});
  return st;
}
function norm_(v) {
  if (v instanceof Date) {
    const hm = Utilities.formatDate(v, TZ, 'HH:mm');
    return Utilities.formatDate(v, TZ, hm === '00:00' ? 'yyyy-MM-dd' : 'yyyy-MM-dd HH:mm');
  }
  return v;
}
function numOrBlank_(v) {
  if (v === '' || v === null || v === undefined) return '';
  const n = Number(String(v).replace(/,/g, ''));
  return isFinite(n) ? n : '';
}
function withLock_(fn) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try { return fn(); } finally { lock.releaseLock(); }
}
function newId_() { return Utilities.getUuid().slice(0, 8); }
function today_() { return Utilities.formatDate(new Date(), TZ, 'yyyy-MM-dd'); }
function nowStr_() { return Utilities.formatDate(new Date(), TZ, 'yyyy-MM-dd HH:mm'); }
function json_(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }

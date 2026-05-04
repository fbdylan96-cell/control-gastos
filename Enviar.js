/************************************************************
 * ENVIAR.GS (v6 - Espera a Classify + usa Comercio real)
 *
 * ✅ CAMBIOS CLAVE (lo que pediste):
 * - SOLO envía si processed == TRUE (Classify terminó)
 * - SIEMPRE muestra y usa "Comercio" (NO merchant_guess)
 * - Si Comercio está vacío -> fallback "Gasto" (pero NO merchant_guess)
 *
 * ✅ Antes de enviar:
 *    - Encuentra hoja "Resumen Mensual" por headers L1:R1 exactos
 *    - Si K1 (mes) no está en mes actual (yyyy-M) -> lo corrige + flush
 *    - Lee tabla L2:R (hasta L vacío)
 *
 * ✅ Email:
 *    - NO botón "Sí está bien" (silencio = positivo)
 *    - Botones: solo nombre de categoría
 *    - "Totales del mes": solo categoría (sin subcategoría)
 *    - Presupuesto: SIEMPRE 2 tablas: CRC y USD
 ************************************************************/

const ENVIAR_CFG = {
  BLACKOUT_START_HOUR: 1,
  BLACKOUT_END_HOUR: 5,

  CLIENTS_SHEET_NAME: 'Clientes',
  CLIENT_EMAILS_HEADER: 'Email',

  RAW_SHEET_NAME: 'RAW',

  // RAW columns
  RAW_COL_MESSAGE_ID: 'messageId',
  RAW_COL_INSERTED_AT: 'inserted_at',
  RAW_COL_CURRENCY: 'currency_guess',
  RAW_COL_AMOUNT: 'amount_guess',
  RAW_COL_DESC: 'desc_guess',

  // 👇 NUEVO: gate para esperar a Classify
  RAW_COL_PROCESSED: 'processed',

  // classification columns
  RAW_COL_COMERCIO: 'Comercio',
  RAW_COL_CAT: 'Categoria',
  RAW_COL_SUB: 'Subcategoria',
  RAW_COL_REASON: 'Razonamiento',

  // email control columns
  RAW_COL_EMAIL_SENT: 'email_notified',
  RAW_COL_EMAIL_SENT_AT: 'email_notified_at',
  RAW_COL_EMAIL_ACTION_AT: 'email_action_at',
  RAW_COL_EMAIL_ACTION: 'email_action_value',

  MAX_EMAILS_PER_CLIENT_PER_RUN: 2,

  // Resumen Mensual table settings (L:R)
  SUMMARY_MONTH_CELL_A1: 'K1',      // input del mes
  SUMMARY_TABLE_START_ROW: 2,       // data empieza en L2
  SUMMARY_TABLE_COL_START: 12,      // L = 12
  SUMMARY_TABLE_NUM_COLS: 7         // L..R
};

/************************************************************
 * WEB APP ENTRYPOINT
 ************************************************************/
function doGetEmailClick_(e) {
  return handleEmailClick_(e);
}

/************************************************************
 * TRIGGER
 ************************************************************/
function runEnviarOnce() {
  const hour = new Date().getHours();
  if (hour >= ENVIAR_CFG.BLACKOUT_START_HOUR && hour < ENVIAR_CFG.BLACKOUT_END_HOUR) {
    console.log('💤 Blackout 1am-5am. No enviamos correos.');
    return;
  }

  const clients = loadClientsForEnviar_();
  if (!clients.size) {
    console.log('ℹ️ No hay clientes con correos configurados.');
    return;
  }

  let sentTotal = 0;
  let failTotal = 0;

  for (const client of clients.values()) {
    const ssId = extractIdFromInput_(client.spreadsheetId);
    const ssUrl = buildSpreadsheetUrl_(ssId);

    console.log(`\n👤 Cliente: ${client.alias}`);
    console.log(`🔗 Sheet: ${ssUrl}`);
    console.log(`📧 Destinos: ${client.reportEmails}`);

    try {
      const ss = SpreadsheetApp.openById(ssId);
      const shRaw = ss.getSheetByName(ENVIAR_CFG.RAW_SHEET_NAME);
      if (!shRaw) {
        console.warn(`⚠️ No existe RAW en cliente ${client.alias}`);
        continue;
      }

      ensureEmailColumns_(shRaw);

      // ✅ Resumen Mensual determinístico desde tabla L:R
      const resumen = loadResumenMensualSnapshot_(ss);

      const res = sendPendingNotificationsForClient_(ss, shRaw, client, resumen);
      sentTotal += res.sent;
      failTotal += res.failed;

    } catch (e) {
      failTotal++;
      console.error(`❌ Error cliente ${client.alias}: ${e.message}`);
    }
  }

  console.log(`\n✅ ENVIAR terminado. enviados=${sentTotal} fallidos=${failTotal}`);
}

/************************************************************
 * ENVIAR EMAILS PENDIENTES
 ************************************************************/
function sendPendingNotificationsForClient_(ss, shRaw, client, resumenSnapshot) {
  const data = shRaw.getDataRange().getValues();
  if (!data || data.length < 2) return { sent: 0, failed: 0 };

  const headers = (data[0] || []).map(h => String(h || '').trim());
  const idx = indexMap_(headers, [
    ENVIAR_CFG.RAW_COL_MESSAGE_ID,
    ENVIAR_CFG.RAW_COL_INSERTED_AT,
    ENVIAR_CFG.RAW_COL_CURRENCY,
    ENVIAR_CFG.RAW_COL_AMOUNT,
    ENVIAR_CFG.RAW_COL_DESC,
    ENVIAR_CFG.RAW_COL_PROCESSED,     // ✅ nuevo
    ENVIAR_CFG.RAW_COL_COMERCIO,      // ✅ Comercio real
    ENVIAR_CFG.RAW_COL_CAT,
    ENVIAR_CFG.RAW_COL_SUB,
    ENVIAR_CFG.RAW_COL_EMAIL_SENT,
    ENVIAR_CFG.RAW_COL_EMAIL_SENT_AT
  ]);

  const required = [
    ENVIAR_CFG.RAW_COL_MESSAGE_ID,
    ENVIAR_CFG.RAW_COL_CURRENCY,
    ENVIAR_CFG.RAW_COL_AMOUNT,
    ENVIAR_CFG.RAW_COL_EMAIL_SENT,
    ENVIAR_CFG.RAW_COL_PROCESSED,   // ✅ requerido
    ENVIAR_CFG.RAW_COL_COMERCIO     // ✅ requerido (porque ya no usamos merchant_guess)
  ];
  for (const r of required) {
    if (idx[r] === -1) throw new Error(`Falta columna RAW: ${r}`);
  }

  const webAppUrl = getWebAppUrlOrThrow_();

  let sent = 0;
  let failed = 0;

  for (let r = 1; r < data.length; r++) {
    if (sent >= ENVIAR_CFG.MAX_EMAILS_PER_CLIENT_PER_RUN) break;

    const rowNum = r + 1;

    // ✅ 1) Solo mandamos si Classify ya terminó
    const processed = toBool_(data[r][idx[ENVIAR_CFG.RAW_COL_PROCESSED]]);
    if (!processed) continue;

    // ✅ 2) Solo mandamos si todavía no se notificó por email
    const emailSent = toBool_(data[r][idx[ENVIAR_CFG.RAW_COL_EMAIL_SENT]]);
    if (emailSent) continue;

    const messageId = String(data[r][idx[ENVIAR_CFG.RAW_COL_MESSAGE_ID]] || '').trim();
    if (!messageId) continue;

    const currency = String(data[r][idx[ENVIAR_CFG.RAW_COL_CURRENCY]] || '').trim();
    const amountStr = String(data[r][idx[ENVIAR_CFG.RAW_COL_AMOUNT]] || '').trim();

    // ✅ Comercio SIEMPRE viene de "Comercio" (depurado por Classify)
    const comercio = String(data[r][idx[ENVIAR_CFG.RAW_COL_COMERCIO]] || '').trim() || 'Gasto';

    const insertedAt = String(data[r][idx[ENVIAR_CFG.RAW_COL_INSERTED_AT]] || '').trim();
    const desc = String(data[r][idx[ENVIAR_CFG.RAW_COL_DESC]] || '').trim();

    const catRaw = idx[ENVIAR_CFG.RAW_COL_CAT] >= 0 ? String(data[r][idx[ENVIAR_CFG.RAW_COL_CAT]] || '').trim() : '';
    const subRaw = idx[ENVIAR_CFG.RAW_COL_SUB] >= 0 ? String(data[r][idx[ENVIAR_CFG.RAW_COL_SUB]] || '').trim() : '';

    const cat = catRaw || 'Sin Clasificar';
    const sub = subRaw || 'Sin subcategoría';

    const monthKey = currentMonthKeyNoPad_(); // yyyy-M

    // ✅ Totales del mes SOLO por categoría (y por moneda del gasto)
    const totals = computeMonthTotalsByCategoryOnly_(data, idx, monthKey, currency, catRaw);

    // ✅ categorías únicas desde L2
    const categoryOptions = (resumenSnapshot && resumenSnapshot.categoriesUnique)
      ? resumenSnapshot.categoriesUnique
      : [];

    const subject = `Gasto: ${currency} ${amountStr} | ${cat} | ${comercio}`.slice(0, 140);

    const html = buildEmailHtml_({
      ssId: ss.getId(),
      rawGid: shRaw.getSheetId(),
      messageId,
      comercio,
      desc,
      insertedAt,
      currency,
      amountStr,
      cat,
      sub,
      monthKey,
      totals,
      webAppUrl,
      resumenSnapshot,
      categoryOptions
    });

    try {
      GmailApp.sendEmail(client.reportEmails, subject, 'Tu cliente de email no soporta HTML.', {
        htmlBody: html
      });

      shRaw.getRange(rowNum, idx[ENVIAR_CFG.RAW_COL_EMAIL_SENT] + 1).setValue(true);
      shRaw.getRange(rowNum, idx[ENVIAR_CFG.RAW_COL_EMAIL_SENT_AT] + 1).setValue(fmtDateCR_(Date.now()));

      sent++;
      console.log(`📨 Enviado | Fila ${rowNum} | ${client.alias} | ${currency} ${amountStr} | ${cat} | Comercio=${comercio}`);

    } catch (e) {
      failed++;
      console.error(`❌ Falló envío | Fila ${rowNum} | ${client.alias}: ${e.message}`);
    }
  }

  return { sent, failed };
}

/************************************************************
 * EMAIL HTML
 ************************************************************/
function buildEmailHtml_(x) {
  const sheetUrl = buildSpreadsheetUrl_(x.ssId);
  const rawUrl = buildSheetUrl_(x.ssId, x.rawGid);

  // Botones de categorías
  const catButtons = (x.categoryOptions || []).map(catName => {
    const link = buildSignedActionUrl_(x.webAppUrl, x.ssId, x.messageId, catName, '');
    return `
      <a href="${link}"
         style="display:inline-block;margin:8px 8px 0 0;padding:10px 12px;text-decoration:none;border-radius:12px;border:1px solid #ddd;background:#fff;font-family:Arial;font-size:13px;">
         <b>${escapeHtml_(catName)}</b>
      </a>
    `;
  }).join('');

  // Totales del mes: SOLO categoría
  const totalsCat = (x.totals && isFinite(x.totals.catTotal)) ? x.totals.catTotal : 0;
  const totalsBlock = `
    <div style="margin-top:16px; padding:12px; border:1px solid #eee; border-radius:12px;">
      <div style="font-weight:700;">Totales del mes</div>
      <div style="color:#666;font-size:12px;margin-top:4px;">Mes: <b>${escapeHtml_(x.monthKey)}</b></div>

      <table style="border-collapse:collapse; margin-top:8px; width:100%; font-size:13px;">
        <tr>
          <td style="padding:6px 0; color:#444;">Total en <b>${escapeHtml_(x.cat)}</b>:</td>
          <td style="padding:6px 0; text-align:right;"><b>${formatMoney_(x.currency, totalsCat)}</b></td>
        </tr>
      </table>
    </div>
  `;

  // Presupuesto: dos tablas siempre (CRC y USD)
  const budgetBlock = buildBudgetOverviewHtmlTwoCurrencies_(x.resumenSnapshot);

  return `
  <div style="font-family:Arial, sans-serif; line-height:1.35; color:#111;">
    <div style="font-size:16px; font-weight:700;">Se registró un gasto</div>

    <div style="margin-top:6px;">
      <b>${escapeHtml_(x.comercio)}</b><br>
      <span style="color:#555;">${escapeHtml_(x.desc || '')}</span><br>
      <span style="color:#777;font-size:12px;">${escapeHtml_(x.insertedAt || '')}</span>
    </div>

    <div style="margin-top:10px; padding:12px; border:1px solid #eee; border-radius:12px;">
      <div><b>Monto:</b> ${escapeHtml_(x.currency)} ${escapeHtml_(x.amountStr)}</div>
      <div><b>Clasificación actual:</b> ${escapeHtml_(x.cat)} / ${escapeHtml_(x.sub)}</div>
    </div>

    <div style="margin-top:14px;">
      <div style="font-weight:700;">¿Está bien clasificado?</div>
      <div style="color:#444; margin-top:4px;">
        Si querés cambiar la categoría, tocá una opción:
      </div>
      <div style="margin-top:8px;">
        ${catButtons || '<i>No hay categorías (revisá la tabla L:R en la hoja de resumen mensual).</i>'}
      </div>
    </div>

    ${totalsBlock}

    <div style="margin-top:16px;">
      ${budgetBlock}
    </div>

    <div style="margin-top:14px;">
      <a href="${rawUrl}" target="_blank">Ver tabla RAW</a><br>
      <a href="${sheetUrl}" target="_blank" style="color:#666;">Abrir Spreadsheet</a>
    </div>
  </div>
  `;
}

/************************************************************
 * RESUMEN MENSUAL: encontrar hoja + forzar mes actual + leer L:R
 ************************************************************/
function loadResumenMensualSnapshot_(ss) {
  const neededHeaders = [
    'Categoria',
    'CRC Total',
    'USD Total',
    'Presupuesto CRC',
    'Presupuesto USD',
    'Restante CRC',
    'Restante USD'
  ];

  const sheets = ss.getSheets();
  let resumenSheet = null;

  for (const sh of sheets) {
    const header = sh.getRange(1, ENVIAR_CFG.SUMMARY_TABLE_COL_START, 1, ENVIAR_CFG.SUMMARY_TABLE_NUM_COLS)
      .getValues()[0]
      .map(v => String(v || '').trim());

    const ok = neededHeaders.every((h, i) => header[i] === h);
    if (ok) {
      resumenSheet = sh;
      break;
    }
  }

  if (!resumenSheet) {
    return { sheetName: '', monthKey: '', categoriesUnique: [], rows: [] };
  }

  // Forzar mes actual (yyyy-M) si K1 no coincide
  const monthCell = resumenSheet.getRange(ENVIAR_CFG.SUMMARY_MONTH_CELL_A1);
  const currentMK = currentMonthKeyNoPad_();
  const monthVal = normalizeMonthKeyNoPad_(monthCell.getValue());

  if (monthVal !== currentMK) {
    monthCell.setValue(currentMK);
    SpreadsheetApp.flush();
    Utilities.sleep(300);
    SpreadsheetApp.flush();
  }

  const startRow = ENVIAR_CFG.SUMMARY_TABLE_START_ROW;
  const lastRow = resumenSheet.getLastRow();
  const maxRows = Math.max(0, lastRow - startRow + 1);
  if (maxRows <= 0) {
    return { sheetName: resumenSheet.getName(), monthKey: currentMK, categoriesUnique: [], rows: [] };
  }

  const values = resumenSheet
    .getRange(startRow, ENVIAR_CFG.SUMMARY_TABLE_COL_START, maxRows, ENVIAR_CFG.SUMMARY_TABLE_NUM_COLS)
    .getValues();

  const rows = [];
  const seen = new Set();
  const categoriesUnique = [];

  for (const r of values) {
    const cat = String(r[0] || '').trim();
    if (!cat) break;

    if (cat.toLowerCase() === 'categoria') continue;

    const obj = {
      categoria: cat,
      crcTotal: toNumber_(r[1]),
      usdTotal: toNumber_(r[2]),
      presCRC: toNumber_(r[3]),
      presUSD: toNumber_(r[4]),
      restCRC: toNumber_(r[5]),
      restUSD: toNumber_(r[6])
    };

    rows.push(obj);

    if (!seen.has(cat)) {
      seen.add(cat);
      categoriesUnique.push(cat);
    }
  }

  return { sheetName: resumenSheet.getName(), monthKey: currentMK, categoriesUnique, rows };
}

function normalizeMonthKeyNoPad_(v) {
  if (v instanceof Date) {
    return `${v.getFullYear()}-${v.getMonth() + 1}`;
  }
  const s = String(v || '').trim();
  const m = s.match(/^(\d{4})-(\d{1,2})$/);
  if (m) return `${m[1]}-${Number(m[2])}`;
  return s;
}

function currentMonthKeyNoPad_() {
  const d = new Date();
  return `${d.getFullYear()}-${d.getMonth() + 1}`;
}

/************************************************************
 * Presupuesto: 2 tablas SIEMPRE (CRC y USD)
 ************************************************************/
function buildBudgetOverviewHtmlTwoCurrencies_(snapshot) {
  if (!snapshot || !snapshot.rows || !snapshot.rows.length) {
    return `
      <div style="padding:12px;border:1px solid #eee;border-radius:12px;">
        <b>Estado del presupuesto mensual</b><br>
        <span style="color:#666;">No se encontró una tabla L:R válida en la hoja de resumen mensual.</span>
      </div>
    `;
  }

  const meta = `
    <div style="color:#666;font-size:12px;margin-top:4px;">
      Tabla usada: <b>${escapeHtml_(snapshot.sheetName)}</b> · Mes: <b>${escapeHtml_(snapshot.monthKey)}</b>
    </div>
  `;

  const crcTable = buildBudgetTable_(snapshot.rows, 'CRC');
  const usdTable = buildBudgetTable_(snapshot.rows, 'USD');

  return `
    <div style="padding:12px;border:1px solid #eee;border-radius:12px;">
      <div style="font-weight:700;">Estado del presupuesto mensual</div>
      ${meta}

      <div style="margin-top:14px;">
        <div style="font-weight:700;">Colones (CRC)</div>
        ${crcTable}
      </div>

      <div style="margin-top:18px;">
        <div style="font-weight:700;">Dólares (USD)</div>
        ${usdTable}
      </div>
    </div>
  `;
}

function buildBudgetTable_(rows, currency) {
  const isCRC = currency === 'CRC';

  const body = rows.map(r => {
    const spent = isCRC ? r.crcTotal : r.usdTotal;
    const budget = isCRC ? r.presCRC : r.presUSD;
    const remaining = isCRC ? r.restCRC : r.restUSD;

    const pct = computePct_(spent, budget);
    const bar = `
      <div style="height:10px;background:#eee;border-radius:999px;overflow:hidden;">
        <div style="height:10px;width:${Math.min(100, Math.max(0, pct))}%;background:#111;"></div>
      </div>
    `;

    return `
      <tr>
        <td style="padding:10px 6px; border-top:1px solid #f0f0f0; vertical-align:top;">
          <div style="font-weight:700;">${escapeHtml_(r.categoria)}</div>
          <div style="margin-top:6px;">${bar}</div>
          <div style="margin-top:6px; color:#777; font-size:12px;">${pct.toFixed(0)}% usado</div>
        </td>
        <td style="padding:10px 6px; border-top:1px solid #f0f0f0; text-align:right; vertical-align:top;">
          <div style="color:#444;">Gastado</div>
          <div style="font-weight:700;">${formatMoney_(currency, spent)}</div>
        </td>
        <td style="padding:10px 6px; border-top:1px solid #f0f0f0; text-align:right; vertical-align:top;">
          <div style="color:#444;">Presupuesto</div>
          <div style="font-weight:700;">${formatMoney_(currency, budget)}</div>
        </td>
        <td style="padding:10px 6px; border-top:1px solid #f0f0f0; text-align:right; vertical-align:top;">
          <div style="color:#444;">Restante</div>
          <div style="font-weight:700;">${formatMoney_(currency, remaining)}</div>
        </td>
      </tr>
    `;
  }).join('');

  return `
    <table style="border-collapse:collapse; width:100%; margin-top:10px; font-size:13px;">
      <tr>
        <th style="text-align:left;padding:8px 6px;border-bottom:1px solid #eee;">Categoría</th>
        <th style="text-align:right;padding:8px 6px;border-bottom:1px solid #eee;">Gastado</th>
        <th style="text-align:right;padding:8px 6px;border-bottom:1px solid #eee;">Presupuesto</th>
        <th style="text-align:right;padding:8px 6px;border-bottom:1px solid #eee;">Restante</th>
      </tr>
      ${body}
    </table>
  `;
}

function computePct_(spent, budget) {
  const s = Number(spent);
  const b = Number(budget);
  if (!isFinite(s) || !isFinite(b) || b === 0) return 0;
  return (s / b) * 100;
}

/************************************************************
 * Totales del mes SOLO por categoría (sin sub)
 ************************************************************/
function computeMonthTotalsByCategoryOnly_(data, idx, monthKey, currency, catRaw) {
  const iInserted = idx[ENVIAR_CFG.RAW_COL_INSERTED_AT];
  const iCurr = idx[ENVIAR_CFG.RAW_COL_CURRENCY];
  const iAmt = idx[ENVIAR_CFG.RAW_COL_AMOUNT];
  const iCat = idx[ENVIAR_CFG.RAW_COL_CAT];

  let catTotal = 0;

  const wantEmptyCat = !catRaw;

  for (let r = 1; r < data.length; r++) {
    const ins = String(data[r][iInserted] || '').trim();
    const mk = inferMonthKeyFromInsertedAtNoPad_(ins) || currentMonthKeyNoPad_();
    if (mk !== monthKey) continue;

    const cur = String(data[r][iCurr] || '').trim();
    if (cur !== currency) continue;

    const c = iCat >= 0 ? String(data[r][iCat] || '').trim() : '';
    const amt = parseAmount_(String(data[r][iAmt] || '').trim());
    if (!isFinite(amt)) continue;

    if (wantEmptyCat) {
      if (!c) catTotal += amt;
    } else {
      if (c === catRaw) catTotal += amt;
    }
  }

  return { catTotal };
}

function inferMonthKeyFromInsertedAtNoPad_(insertedAt) {
  const s = String(insertedAt || '').trim();
  const m = s.match(/^(\d{4})-(\d{2})-/);
  if (!m) return '';
  return `${m[1]}-${Number(m[2])}`;
}

/************************************************************
 * WEB APP CLICK HANDLER
 ************************************************************/
function handleEmailClick_(e) {
  try {
    const p = (e && e.parameter) ? e.parameter : {};

    const clientId = String(p.cid || '').trim();
    const messageId = String(p.mid || '').trim();
    const cat = String(p.cat || '').trim();
    const sub = String(p.sub || '').trim();
    const sig = String(p.sig || '').trim();

    if (!clientId || !messageId || !sig) return htmlOk_('Faltan parámetros.');
    if (!verifySignature_(clientId, messageId, cat, sub, sig)) return htmlOk_('Link inválido o expirado.');

    const ss = SpreadsheetApp.openById(clientId);
    const sh = ss.getSheetByName(ENVIAR_CFG.RAW_SHEET_NAME);
    if (!sh) return htmlOk_('No existe RAW.');

    ensureEmailColumns_(sh);

    const data = sh.getDataRange().getValues();
    const headers = (data[0] || []).map(h => String(h || '').trim());

    const iMsg = headers.indexOf(ENVIAR_CFG.RAW_COL_MESSAGE_ID);
    const iCat = headers.indexOf(ENVIAR_CFG.RAW_COL_CAT);
    const iSub = headers.indexOf(ENVIAR_CFG.RAW_COL_SUB);
    const iReason = headers.indexOf(ENVIAR_CFG.RAW_COL_REASON);
    const iActAt = headers.indexOf(ENVIAR_CFG.RAW_COL_EMAIL_ACTION_AT);
    const iAct = headers.indexOf(ENVIAR_CFG.RAW_COL_EMAIL_ACTION);

    if (iMsg === -1 || iCat === -1) return htmlOk_('Faltan columnas.');

    let rowFound = -1;
    for (let r = 1; r < data.length; r++) {
      if (String(data[r][iMsg] || '').trim() === messageId) {
        rowFound = r + 1;
        break;
      }
    }
    if (rowFound === -1) return htmlOk_('No se encontró la transacción.');

    sh.getRange(rowFound, iCat + 1).setValue(cat || '');
    if (iSub !== -1) sh.getRange(rowFound, iSub + 1).setValue(sub || '');

    if (iReason !== -1) sh.getRange(rowFound, iReason + 1).setValue('[EMAIL] Reclasificado 1-click');
    if (iActAt !== -1) sh.getRange(rowFound, iActAt + 1).setValue(fmtDateCR_(Date.now()));
    if (iAct !== -1) sh.getRange(rowFound, iAct + 1).setValue(`${cat || ''}${sub ? ' / ' + sub : ''}`);

    const url = buildSpreadsheetUrl_(clientId);
    return htmlOk_(`Listo ✅<br><br>Se guardó: <b>${escapeHtml_(cat)}</b><br><br><a href="${url}" target="_blank">Abrir Sheet</a>`);

  } catch (err) {
    return htmlOk_('Error: ' + err.message);
  }
}

/************************************************************
 * RAW email columns
 ************************************************************/
function ensureEmailColumns_(sh) {
  const lastCol = sh.getLastColumn();
  if (lastCol < 1) return;

  const headers = sh.getRange(1, 1, 1, lastCol).getValues()[0].map(h => String(h || '').trim());

  const required = [
    ENVIAR_CFG.RAW_COL_EMAIL_SENT,
    ENVIAR_CFG.RAW_COL_EMAIL_SENT_AT,
    ENVIAR_CFG.RAW_COL_EMAIL_ACTION_AT,
    ENVIAR_CFG.RAW_COL_EMAIL_ACTION
  ];

  const missing = required.filter(h => !headers.includes(h));
  if (!missing.length) return;

  sh.insertColumnsAfter(lastCol, missing.length);
  sh.getRange(1, lastCol + 1, 1, missing.length).setValues([missing]);
  console.log(`🧩 RAW actualizado (email): agregadas → ${missing.join(', ')}`);
}

/************************************************************
 * CLIENTS LOADER
 ************************************************************/
function loadClientsForEnviar_() {
  const ss = SpreadsheetApp.openById(Clientes_SpreadsheetID);
  const sh = ss.getSheetByName(ENVIAR_CFG.CLIENTS_SHEET_NAME);
  if (!sh) throw new Error(`No existe la hoja "${ENVIAR_CFG.CLIENTS_SHEET_NAME}"`);

  const lastRow = sh.getLastRow();
  const lastCol = sh.getLastColumn();
  if (lastRow < 2) return new Map();

  const values = sh.getRange(1, 1, lastRow, lastCol).getValues();
  const headers = values[0].map(h => String(h || '').trim());

  const iEmail = headers.indexOf('email_forward');
  const iSsUrl = headers.indexOf('spreadsheetId');
  const iActive = headers.indexOf('active');
  const iReport = headers.indexOf(ENVIAR_CFG.CLIENT_EMAILS_HEADER);

  if (iEmail < 0 || iSsUrl < 0) throw new Error('Faltan columnas email_forward o spreadsheetId');
  if (iReport < 0) throw new Error(`Falta columna "${ENVIAR_CFG.CLIENT_EMAILS_HEADER}" en Clientes`);

  const map = new Map();

  for (let r = 1; r < values.length; r++) {
    const row = values[r];
    const alias = String(row[iEmail] || '').trim();
    const ssUrl = String(row[iSsUrl] || '').trim();
    const reportRaw = String(row[iReport] || '').trim();
    const activeVal = iActive >= 0 ? row[iActive] : true;

    if (!alias || !ssUrl) continue;

    const isActive = String(activeVal).toLowerCase() !== 'false' && String(activeVal).toUpperCase() !== 'NO';
    if (!isActive) continue;

    if (!reportRaw) continue;

    const reportEmails = normalizeEmailList_(reportRaw);
    if (!reportEmails) continue;

    const ssIdClient = extractSpreadsheetIdFromUrl_(ssUrl);
    if (!ssIdClient) continue;

    map.set(alias.toLowerCase(), {
      alias,
      spreadsheetId: ssIdClient,
      reportEmails
    });
  }

  return map;
}

/************************************************************
 * SECURITY / SIGNATURE
 ************************************************************/
function getSecret_() {
  const props = PropertiesService.getScriptProperties();
  let secret = props.getProperty('ENVIAR_SECRET');
  if (!secret) {
    secret = Utilities.getUuid() + Utilities.getUuid();
    props.setProperty('ENVIAR_SECRET', secret);
  }
  return secret;
}

function sign_(cid, mid, cat, sub) {
  const secret = getSecret_();
  const payload = `${cid}|${mid}|${cat}|${sub}`;
  const raw = Utilities.computeHmacSha256Signature(payload, secret);
  return Utilities.base64EncodeWebSafe(raw);
}

function verifySignature_(cid, mid, cat, sub, sig) {
  return sign_(cid, mid, cat, sub) === sig;
}

function buildSignedActionUrl_(webAppUrl, cid, mid, cat, sub) {
  const sig = sign_(cid, mid, cat, sub);
  const q = `cid=${encodeURIComponent(cid)}&mid=${encodeURIComponent(mid)}&cat=${encodeURIComponent(cat)}&sub=${encodeURIComponent(sub)}&sig=${encodeURIComponent(sig)}`;
  return `${webAppUrl}?${q}`;
}

/************************************************************
 * WEBAPP URL
 ************************************************************/
function getWebAppUrlOrThrow_() {
  const url = PropertiesService.getScriptProperties().getProperty('WEBAPP_URL');
  if (!url) throw new Error('Falta Script Property WEBAPP_URL (URL del Web App).');
  return url.replace(/\/$/, '');
}

/************************************************************
 * HELPERS
 ************************************************************/
function indexMap_(headers, names) {
  const out = {};
  for (const n of names) out[n] = headers.indexOf(n);
  return out;
}

function normalizeEmailList_(raw) {
  return String(raw || '')
    .split(/[;,]/g)
    .map(s => s.trim())
    .filter(Boolean)
    .join(',');
}

function parseAmount_(s) {
  if (!s) return NaN;
  let x = String(s).trim().replace(/\s/g, '');
  const hasComma = x.includes(',');
  const hasDot = x.includes('.');

  if (hasComma && hasDot) {
    const lastComma = x.lastIndexOf(',');
    const lastDot = x.lastIndexOf('.');
    if (lastComma > lastDot) x = x.replace(/\./g, '').replace(',', '.');
    else x = x.replace(/,/g, '');
  } else if (hasComma && !hasDot) {
    if (/,(\d{2})$/.test(x)) x = x.replace(',', '.');
    else x = x.replace(/,/g, '');
  } else {
    const dots = (x.match(/\./g) || []).length;
    if (dots > 1) x = x.replace(/\./g, '');
  }

  const n = Number(x);
  return isFinite(n) ? n : NaN;
}

function toNumber_(v) {
  if (typeof v === 'number') return v;
  const n = parseAmount_(String(v || '').trim());
  return isFinite(n) ? n : 0;
}

function toBool_(v) {
  if (typeof v === 'boolean') return v;
  const s = String(v || '').trim().toLowerCase();
  return s === 'true' || s === '1' || s === 'yes' || s === 'si';
}

function fmtDateCR_(dateMs) {
  return Utilities.formatDate(new Date(dateMs), 'America/Costa_Rica', 'yyyy-MM-dd HH:mm');
}

function extractIdFromInput_(input) {
  if (!input) return '';
  const m = String(input).match(/\/d\/([a-zA-Z0-9\-_]+)/);
  return m ? m[1] : input;
}

function extractSpreadsheetIdFromUrl_(url) {
  if (!url) return '';
  const m = String(url).match(/\/spreadsheets\/d\/([a-zA-Z0-9\-_]+)/);
  return m ? m[1] : '';
}

function buildSpreadsheetUrl_(spreadsheetId) {
  return `https://docs.google.com/spreadsheets/d/${spreadsheetId}/edit`;
}

function buildSheetUrl_(spreadsheetId, gid) {
  return `https://docs.google.com/spreadsheets/d/${spreadsheetId}/edit#gid=${gid}`;
}

function formatMoney_(currency, n) {
  const x = isFinite(n) ? n : 0;
  return `${currency} ${x.toFixed(2)}`;
}

function escapeHtml_(s) {
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function htmlOk_(msg) {
  return HtmlService.createHtmlOutput(
    `<div style="font-family:Arial; padding:16px; line-height:1.35;">${msg}</div>`
  );
}

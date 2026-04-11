/************************************************************
 * CLASSIFICATION ENGINE (LOCAL-LOCKED) + PROCESSED=TRUE
 *
 * OBJETIVO EXTRA (LO QUE PEDISTE):
 * - Una vez que ESTA rutina "intentó categorizar" una fila
 *   (ya sea por regla LOCAL, GLOBAL→LOCAL, o IA),
 *   y además escribió el Comercio (depurado),
 *   entonces marca la columna "processed" = TRUE.
 *
 * Importante:
 * - No depende de que la categoría quede perfecta.
 * - La idea es: si ya pasó por el motor y ya llenó Comercio,
 *   entonces tu Enviar.gs ya puede considerarlo “listo para enviar”.
 ************************************************************/

// GlobalCatalog_SpreadsheetID is defined in MataCloudAPI-credentials.js

const CLASSIFY_CFG = {
  SHEET_RAW: 'RAW',
  SHEET_RULES_LOCAL: 'Catalogo',
  SHEET_RULES_GLOBAL: 'Catalogo Global',

  BATCH_SIZE: 15,

  OPENAI_MODEL: 'gpt-4o-mini',
  TEMPERATURE: 0.0,

  COL_HEADER_MERCH_AI: 'Comercio',
  COL_HEADER_CAT: 'Categoria',
  COL_HEADER_SUB: 'Subcategoria',
  COL_HEADER_REASON: 'Razonamiento',

  COL_HEADER_PROCESSED: 'processed' // ✅ NUEVO: nombre exacto de la col
};

function runClassificationBatch() {
  // Blackout 1am-5am
  const hour = new Date().getHours();
  if (hour >= 1 && hour < 5) {
    console.log('💤 Blackout 1am-5am. Saltando clasificación.');
    return;
  }

  console.log('--- 🤖 INICIANDO CLASIFICACIÓN (LOCAL-LOCKED) ---');

  // Asume que loadClientsConfig_ existe en tu proyecto (del MASTER/Extractor)
  const clientsMap = loadClientsConfig_();
  if (!clientsMap || !clientsMap.size) {
    console.log('ℹ️ No hay clientes activos.');
    return;
  }

  console.log('📚 Cargando Catálogo Global...');
  const globalId = GlobalCatalog_SpreadsheetID;
  const globalData = loadCatalogStruct_(globalId, CLASSIFY_CFG.SHEET_RULES_GLOBAL);

  if (!globalData.taxonomyCats.size && !globalData.categoriesList.length && !globalData.map.size) {
    console.warn('⚠️ Catálogo Global vacío o no accesible. Continuando solo con LOCAL.');
  }

  let okClients = 0;
  let badClients = 0;

  for (const client of clientsMap.values()) {
    const clientId = extractIdFromInput_(client.spreadsheetId);
    const ssUrl = buildSpreadsheetUrl_(clientId);

    console.log(`\n👤 Cliente: ${client.alias}`);
    console.log(`🔗 Spreadsheet: ${ssUrl}`);

    try {
      const ss = SpreadsheetApp.openById(clientId);

      const res = classifyClientSheet_LOCAL_LOCKED_(ss, globalData, {
        alias: client.alias,
        spreadsheetId: clientId
      });

      console.log(`✅ Terminado cliente | Filas procesadas: ${res.processedRows}`);
      okClients++;
    } catch (e) {
      badClients++;
      console.error(`❌ Error cliente | ${client.alias} | ${ssUrl} | ${e.message}`);
    }
  }

  console.log('\n--- ✅ PROCESO TERMINADO ---');
  console.log(`📌 Resumen: OK=${okClients} | Fallidos=${badClients} | Total=${clientsMap.size}`);
}

/**
 * Versión LOCAL-LOCKED:
 * - La taxonomía permitida = SOLO local.
 * - Global = hint / ejemplos / match solo si existe en local.
 * - ✅ processed=TRUE cuando ya intentó y escribió Comercio.
 */
function classifyClientSheet_LOCAL_LOCKED_(ss, globalData, clientCtx) {
  const shRaw = ss.getSheetByName(CLASSIFY_CFG.SHEET_RAW);
  if (!shRaw) {
    console.log(`⚠️ No existe RAW (Cliente ${clientCtx.alias}).`);
    return { processedRows: 0 };
  }

  const rawUrl = buildSheetUrl_(ss.getId(), shRaw.getSheetId());
  console.log(`🧾 RAW: ${rawUrl}`);

  // 1) Cargar LOCAL (obligatorio)
  const localData = loadCatalogStruct_(ss.getId(), CLASSIFY_CFG.SHEET_RULES_LOCAL);

  if (!localData.taxonomyCats.size && !localData.categoriesList.length) {
    console.error(
      `❌ Catálogo LOCAL vacío (sin taxonomía/categorías). Cliente=${clientCtx.alias} | ${rawUrl}`
    );
    return { processedRows: 0 };
  }

  // 2) Taxonomía y categorías PERMITIDAS = LOCAL ONLY
  const localTaxonomy = localData.taxonomyCats;
  const taxonomyTextLocal = taxonomyToText_(localTaxonomy, 4000);
  const validCategoriesLocal = Array.isArray(localData.categoriesList)
    ? localData.categoriesList.slice()
    : [];

  // 3) Ejemplos: primero local, luego global (guía)
  const examplesText = buildExamplesText_(localData.examplesByCat, globalData.examplesByCat, 20);

  const data = shRaw.getDataRange().getValues();
  if (!data || data.length < 2) return { processedRows: 0 };

  const headers = (data[0] || []).map(h => String(h || '').trim());

  const idx = {
    merchGuess: headers.indexOf('merchant_guess'),
    subj: headers.indexOf('subject'),
    snippet: headers.indexOf('snippet'), // si no existe queda -1 y safeGet_ devuelve ""
    body: headers.indexOf('body_condensed'),
    curr: headers.indexOf('currency_guess'),
    amt: headers.indexOf('amount_guess'),

    merchAi: headers.indexOf(CLASSIFY_CFG.COL_HEADER_MERCH_AI),
    cat: headers.indexOf(CLASSIFY_CFG.COL_HEADER_CAT),
    sub: headers.indexOf(CLASSIFY_CFG.COL_HEADER_SUB),
    reason: headers.indexOf(CLASSIFY_CFG.COL_HEADER_REASON),

    processed: headers.indexOf(CLASSIFY_CFG.COL_HEADER_PROCESSED) // ✅ NUEVO
  };

  // Validar columnas destino mínimas
  const missing = [];
  if (idx.merchAi < 0) missing.push(CLASSIFY_CFG.COL_HEADER_MERCH_AI);
  if (idx.cat < 0) missing.push(CLASSIFY_CFG.COL_HEADER_CAT);
  if (idx.sub < 0) missing.push(CLASSIFY_CFG.COL_HEADER_SUB);
  if (idx.reason < 0) missing.push(CLASSIFY_CFG.COL_HEADER_REASON);
  if (idx.processed < 0) missing.push(CLASSIFY_CFG.COL_HEADER_PROCESSED); // ✅ requerido para tu flujo

  if (missing.length) {
    console.warn(
      `⚠️ Faltan columnas destino en RAW | Cliente=${clientCtx.alias} | ${rawUrl} | Faltan: ${missing.join(
        ', '
      )}`
    );
    return { processedRows: 0 };
  }

  let updates = 0;

  for (let r = 1; r < data.length; r++) {
    if (updates >= CLASSIFY_CFG.BATCH_SIZE) break;

    const rowNum = r + 1;

    // ✅ Si ya está processed=TRUE, no lo vuelvas a tocar
    const alreadyProcessed = String(safeGet_(data[r], idx.processed) || '').toLowerCase() === 'true';
    if (alreadyProcessed) continue;

    // Reclasifica si Categoria O Subcategoria está vacía (solo salta si ambas llenas)
    const catVal = String(data[r][idx.cat] || '').trim();
    const subVal = String(data[r][idx.sub] || '').trim();
    if (catVal && subVal) {
      // OJO: si ya tenía Cat/Sub llenas pero processed estaba false, igual lo marcamos TRUE
      // porque tu Enviar depende de processed.
      // Y Comercio lo dejamos como está (si está vacío, lo intentamos completar con merchant_guess).
      const existingMerch = String(safeGet_(data[r], idx.merchAi) || '').trim();
      const guessRaw0 = String(safeGet_(data[r], idx.merchGuess) || '').trim();
      const ensureMerch = existingMerch || (guessRaw0 ? formatMerchantDisplay_(guessRaw0) : '');
      if (ensureMerch) {
        writeResultAndProcessed_(shRaw, rowNum, idx, {
          merchant: ensureMerch,
          cat: catVal,
          sub: subVal,
          reason: String(safeGet_(data[r], idx.reason) || '').trim() || '[AUTO] Ya tenía Cat/Sub'
        });
        updates++;
      } else {
        // Si ni Comercio ni merchant_guess existen, igual marcamos processed para que no se quede pegado
        markProcessedTrue_(shRaw, rowNum, idx);
        updates++;
      }
      continue;
    }

    try {
      const guessRaw = String(safeGet_(data[r], idx.merchGuess) || '').trim();
      if (!guessRaw) {
        // No hay insumo, igual marcamos processed para no ciclar infinito
        writeResultAndProcessed_(shRaw, rowNum, idx, {
          merchant: '',
          cat: 'Sin Clasificar',
          sub: '',
          reason: '[SKIP] Sin merchant_guess'
        });
        updates++;
        continue;
      }

      const guessKey = cleanMerchantKey_(guessRaw);
      const guessDisplay = formatMerchantDisplay_(guessRaw);

      // ========= 1) FAST PATH: LOCAL =========
      const localHit = findRuleMatchByPrefix_(localData.map, guessKey);
      if (localHit) {
        writeResultAndProcessed_(shRaw, rowNum, idx, {
          merchant: localHit.value.display || guessDisplay,
          cat: localHit.value.cat,
          sub: localHit.value.sub,
          reason: `[LOCAL] Regla (${localHit.mode})`
        });
        updates++;
        continue;
      }

      // ========= 2) FAST PATH: GLOBAL pero SOLO si existe en LOCAL =========
      const globalHit = findRuleMatchByPrefix_(globalData.map, guessKey);
      if (globalHit) {
        const gCat = String(globalHit.value.cat || '').trim();
        const gSub = String(globalHit.value.sub || '').trim();

        const gCatOkLocal = isCategoryAllowedLocalOnly_(gCat, localTaxonomy, validCategoriesLocal);
        const gSubOkLocal = isSubcategoryAllowedLocalOnly_(gCat, gSub, localTaxonomy);

        if (gCatOkLocal && gSubOkLocal) {
          writeResultAndProcessed_(shRaw, rowNum, idx, {
            merchant: globalHit.value.display || guessDisplay,
            cat: gCat,
            sub: gSub,
            reason: `[GLOBAL→LOCAL] Match permitido`
          });
          updates++;
          continue;
        }

        // Hint global -> IA (solo local)
        const aiFromGlobalHint = analyzeWithOpenAI_LOCAL_ONLY_(
          {
            guess: guessRaw,
            subject: String(safeGet_(data[r], idx.subj) || ''),
            snippet: String(safeGet_(data[r], idx.snippet) || ''),
            body: String(safeGet_(data[r], idx.body) || ''),
            amount: `${String(safeGet_(data[r], idx.curr) || '').trim()} ${String(
              safeGet_(data[r], idx.amt) || ''
            ).trim()}`,
            global_hint: {
              merchant: globalHit.value.display || guessDisplay,
              category: gCat,
              subcategory: gSub
            }
          },
          validCategoriesLocal,
          taxonomyTextLocal,
          examplesText
        );

        const final = coerceToLocalOnly_(aiFromGlobalHint, guessRaw, localTaxonomy, validCategoriesLocal);
        // ✅ Aunque IA invente cat, coerce lo corrige; igual marcamos processed=TRUE
        writeResultAndProcessed_(shRaw, rowNum, idx, final);
        updates++;
        continue;
      }

      // ========= 3) IA (SOLO LOCAL) =========
      const ai = analyzeWithOpenAI_LOCAL_ONLY_(
        {
          guess: guessRaw,
          subject: String(safeGet_(data[r], idx.subj) || ''),
          snippet: String(safeGet_(data[r], idx.snippet) || ''),
          body: String(safeGet_(data[r], idx.body) || ''),
          amount: `${String(safeGet_(data[r], idx.curr) || '').trim()} ${String(
            safeGet_(data[r], idx.amt) || ''
          ).trim()}`,
          global_hint: null
        },
        validCategoriesLocal,
        taxonomyTextLocal,
        examplesText
      );

      const final = coerceToLocalOnly_(ai, guessRaw, localTaxonomy, validCategoriesLocal);
      writeResultAndProcessed_(shRaw, rowNum, idx, final);
      updates++;
    } catch (rowErr) {
      console.error(
        `❌ Error fila | Cliente=${clientCtx.alias} | ${rawUrl} | Fila=${rowNum} | ${rowErr.message}`
      );

      // Aun con error: marcamos processed=TRUE para que no se quede pegado;
      // y dejamos razón de error.
      try {
        writeResultAndProcessed_(shRaw, rowNum, idx, {
          merchant: '',
          cat: 'Sin Clasificar',
          sub: '',
          reason: `[ERROR] ${rowErr.message}`.slice(0, 300)
        });
        updates++;
      } catch (_) {
        try {
          markProcessedTrue_(shRaw, rowNum, idx);
          updates++;
        } catch (_) {}
      }
    }
  }

  return { processedRows: updates };
}

/**
 * Fuerza el resultado a ser LOCAL-ONLY, aunque la IA falle o invente.
 */
function coerceToLocalOnly_(ai, guessRaw, localTaxonomy, validCategoriesLocal) {
  const aiMerchantClean = String((ai && ai.merchant_clean) || '').trim();
  const finalMerchant = formatMerchantDisplay_(aiMerchantClean || guessRaw);

  const proposedCat = String((ai && ai.category_proposal) || '').trim();
  const proposedSub = String((ai && ai.subcategory_proposal) || '').trim();

  const catOk = isCategoryAllowedLocalOnly_(proposedCat, localTaxonomy, validCategoriesLocal);
  const subOk = isSubcategoryAllowedLocalOnly_(proposedCat, proposedSub, localTaxonomy);

  const finalCat = catOk ? proposedCat : 'Sin Clasificar';
  const finalSub = catOk && subOk ? proposedSub : catOk ? 'N/A' : '';

  const reason = String((ai && ai.reason) || '').trim();
  const finalReason = reason ? `[IA] ${reason}` : `[IA]`;

  return {
    merchant: finalMerchant,
    cat: finalCat,
    sub: finalSub,
    reason: finalReason
  };
}

/**
 * IA: 1 llamada = merchant_clean + clasifica usando TAXONOMÍA LOCAL.
 */
function analyzeWithOpenAI_LOCAL_ONLY_(input, validCategoriesLocal, taxonomyTextLocal, examplesText) {
  const apiKey = PropertiesService.getScriptProperties().getProperty('OPENAI_API_KEY');

  if (!apiKey) {
    return {
      merchant_clean: '',
      category_proposal: 'Error Config',
      subcategory_proposal: '',
      reason: 'Falta OPENAI_API_KEY'
    };
  }

  const hint = input && input.global_hint ? input.global_hint : null;

  const prompt = `
Vas a procesar una transacción bancaria de Costa Rica. Tenés 2 tareas:
A) NORMALIZAR el nombre del comercio (merchant_clean)
B) CLASIFICAR (category_proposal, subcategory_proposal)

REGLAS CRÍTICAS para merchant_clean:
- merchant_clean debe ser SOLO la MARCA, sin ubicación.
- NO incluyas: barrios/cantones/provincias/ciudades (ej. Curridabat, Escazú, Santa Ana, San José, Montes de Oca, Heredia, Alajuela, Cartago).
- NO incluyas malls/edificios (Multiplaza, Lincoln Plaza, Oxígeno, Terramall, City Mall).
- NO incluyas país (Costa Rica, CR, CRI).
- NO incluyas prefijos de procesador: DLC*, DL*, PAYU*, STRIPE*, SQ*, etc.

REGLA DE PREFIJOS GENÉRICOS (muy importante):
- Si merchant_guess inicia con un TIPO de negocio genérico (ej: "Servicentro", "Supermercado", "Restaurante", "Soda", "Cafetería", "Clínica", "Hospital", "Farmacia", "Panadería", "Carnicería", "Ferretería", "Barbería", "Taller", "Gasolinera"),
  entonces merchant_clean debe ser SOLO el nombre propio que sigue.
- Ejemplos:
  - "Servicentro LA Galera" -> "La Galera"
  - "Restaurante El Rincón De Los Chamos" -> "El Rincón De Los Chamos"
- Si al quitar el prefijo queda vacío o algo genérico, no lo quités.


Ejemplos obligatorios:
- "Walmart Curridabat Ocn" -> "Walmart"
- "WALMART OXIGENO" -> "Walmart"
- "DLC*UBER EATS San Jose CRI" -> "Uber Eats"

Capitalización: Title Case (Office Depot, Uber Eats, PriceSmart).

REGLA MÁS IMPORTANTE (NO INVENTAR):
- category_proposal y subcategory_proposal DEBEN SALIR EXCLUSIVAMENTE de la TAXONOMÍA LOCAL de abajo.
- Aunque recibas una sugerencia global, solo úsala como PISTA para escoger la MEJOR opción dentro de la TAXONOMÍA LOCAL.
- Si no hay match claro, elegí el mejor encaje dentro de la TAXONOMÍA LOCAL.

TAXONOMÍA LOCAL (ÚNICAS OPCIONES VÁLIDAS):
${taxonomyTextLocal || '(no disponible)'}

EJEMPLOS (guía, no opciones):
${examplesText || '(no disponible)'}

HINT GLOBAL (si existe, solo guía):
${
  hint
    ? `- global_merchant: "${hint.merchant}"
- global_category: "${hint.category}"
- global_subcategory: "${hint.subcategory}"`
    : '(sin hint global)'
}

DATOS:
- merchant_guess: "${input.guess}"
- subject: "${input.subject}"
- snippet: "${input.snippet}"
- body_condensed: "${input.body}"
- amount: "${input.amount}"

Responde SOLO este JSON:
{
  "merchant_clean": "string",
  "category_proposal": "string",
  "subcategory_proposal": "string",
  "reason": "string"
}

Reglas para reason: Máx 6 palabras refiriéndose a la justificación.
  `.trim();

  const payload = {
    model: CLASSIFY_CFG.OPENAI_MODEL,
    messages: [{ role: 'user', content: prompt }],
    temperature: CLASSIFY_CFG.TEMPERATURE,
    response_format: { type: 'json_object' }
  };

  const resp = UrlFetchApp.fetch('https://api.openai.com/v1/chat/completions', {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: `Bearer ${apiKey}` },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  });

  const code = resp.getResponseCode();
  const text = resp.getContentText();

  if (code >= 300) {
    let detail = `OpenAI ${code}`;
    try {
      const ej = JSON.parse(text);
      if (ej.error && ej.error.message) detail = ej.error.message;
    } catch (_) {
      detail = (text || '').slice(0, 160);
    }
    return {
      merchant_clean: '',
      category_proposal: 'Error AI',
      subcategory_proposal: '',
      reason: detail
    };
  }

  const json = JSON.parse(text);
  return JSON.parse(json.choices[0].message.content);
}

/* =================== CARGA CATÁLOGOS =================== */

function loadCatalogStruct_(urlOrId, sheetName) {
  const output = {
    map: new Map(),
    taxonomyCats: new Map(),
    categoriesList: [],
    examplesByCat: new Map()
  };

  try {
    const realId = extractIdFromInput_(urlOrId);
    const ss = SpreadsheetApp.openById(realId);
    const sh = ss.getSheetByName(sheetName);
    if (!sh) return output;

    const lastRow = sh.getLastRow();
    if (lastRow < 2) return output;

    const rows = sh.getRange(2, 1, lastRow - 1, 3).getValues();

    const cats = new Set();

    for (const row of rows) {
      const comercioRaw = String(row[0] || '').trim();
      const cat = String(row[1] || '').trim();
      const sub = String(row[2] || '').trim();

      if (!cat) continue;
      cats.add(cat);

      // Si no hay comercio, interpretamos como “taxonomía”
      if (!comercioRaw) {
        if (!output.taxonomyCats.has(cat)) output.taxonomyCats.set(cat, new Set());
        if (sub) output.taxonomyCats.get(cat).add(sub);
        continue;
      }

      const merchKey = cleanMerchantKey_(comercioRaw);
      if (merchKey) {
        output.map.set(merchKey, {
          cat,
          sub,
          display: formatMerchantDisplay_(comercioRaw)
        });

        if (!output.examplesByCat.has(cat)) output.examplesByCat.set(cat, []);
        const arr = output.examplesByCat.get(cat);
        if (arr.length < 10) arr.push(formatMerchantDisplay_(comercioRaw));
      }
    }

    output.categoriesList = Array.from(cats);
    return output;
  } catch (_) {
    return output;
  }
}

/* =================== HELPERS =================== */

/**
 * ✅ Esta función ES la clave:
 * - Escribe Comercio/Cat/Sub/Razón
 * - Y marca processed=TRUE (si ya escribió Comercio o al menos intentó)
 */
function writeResultAndProcessed_(shRaw, rowNum, idx, result) {
  // 1) Comercio (depurado)
  const merch = String(result.merchant || '').trim();
  if (idx.merchAi >= 0) shRaw.getRange(rowNum, idx.merchAi + 1).setValue(merch);

  // 2) Categoría / Subcategoría / Razón
  if (idx.cat >= 0) shRaw.getRange(rowNum, idx.cat + 1).setValue(result.cat || '');
  if (idx.sub >= 0) shRaw.getRange(rowNum, idx.sub + 1).setValue(result.sub || '');
  if (idx.reason >= 0) shRaw.getRange(rowNum, idx.reason + 1).setValue(result.reason || '');

  // 3) ✅ processed = TRUE
  // Tu regla: “si ya hizo su trabajo y ya puso Comercio…”
  // Incluso si Comercio queda vacío por falta de data, igual estamos “terminando el intento”.
  markProcessedTrue_(shRaw, rowNum, idx);
}

function markProcessedTrue_(shRaw, rowNum, idx) {
  if (idx.processed >= 0) {
    shRaw.getRange(rowNum, idx.processed + 1).setValue(true);
  }
}

function safeGet_(row, idx) {
  if (!row || idx < 0) return '';
  return row[idx];
}

function extractIdFromInput_(input) {
  if (!input) return '';
  const m = String(input).match(/\/d\/([a-zA-Z0-9\-_]+)/);
  return m ? m[1] : input;
}

function buildSpreadsheetUrl_(spreadsheetId) {
  return `https://docs.google.com/spreadsheets/d/${spreadsheetId}/edit`;
}

function buildSheetUrl_(spreadsheetId, gid) {
  return `https://docs.google.com/spreadsheets/d/${spreadsheetId}/edit#gid=${gid}`;
}

/**
 * Key determinístico para match: lowercase + sin acentos + sin símbolos.
 */
function cleanMerchantKey_(name) {
  if (!name) return '';
  return String(name)
    .toLowerCase()
    .trim()
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9 ]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * Limpia sufijos comunes de ubicación CR SOLO AL FINAL del merchant.
 */
function stripCostaRicaLocationSuffix_(input) {
  if (!input) return '';
  let s = String(input);

  s = s.replace(/\b(ciudad\s*y\s*pa[ií]s?|ciudad\s*y\s*pa)\b.*$/i, '').trim();

  s = s.replace(/\s+/g, ' ').trim();
  if (!s) return '';

  const stop = new Set([
    'san',
    'jose',
    'sanjose',
    'escazu',
    'santa',
    'ana',
    'curridabat',
    'moravia',
    'heredia',
    'alajuela',
    'cartago',
    'montes',
    'oca',
    'barva',
    'belen',
    'tibás',
    'tibas',
    'desamparados',
    'guadalupe',
    'pavas',
    'lindora',
    'costa',
    'rica',
    'cr',
    'cri',
    'multiplaza',
    'oxigeno',
    'oxígeno',
    'terramall',
    'lincoln',
    'plaza',
    'city',
    'mall',
    'ocn',
    'oc',
    'crl'
  ]);

  const tokens = s.split(' ');
  while (tokens.length > 1) {
    const last = tokens[tokens.length - 1];
    const lastKey = cleanMerchantKey_(last);

    if (!lastKey) {
      tokens.pop();
      continue;
    }
    if (stop.has(lastKey)) {
      tokens.pop();
      continue;
    }
    if (/^\d+$/.test(lastKey)) {
      tokens.pop();
      continue;
    }
    break;
  }

  return tokens.join(' ').trim();
}

/**
 * Display bonito:
 * - quita sufijo de ubicación
 * - Title Case
 * - excepciones
 */
function formatMerchantDisplay_(name) {
  if (!name) return '';

  let t = String(name).trim();
  t = t.replace(/\s{2,}/g, ' ').trim();

  t = stripCostaRicaLocationSuffix_(t);
  t = t.replace(/^\s*(dlc\*|dl\*|payu\*|stripe\*|sq\*)\s*/i, '').trim();

  t = toTitleCaseSmart_(t);

  t = t.replace(/\bUber Eats\b/gi, 'Uber Eats');
  t = t.replace(/\bUber\b/gi, 'Uber');
  t = t.replace(/\bPrice Smart\b/gi, 'PriceSmart');
  t = t.replace(/\bIcloud\b/gi, 'iCloud');
  t = t.replace(/\bAmazon Marketplace\b/gi, 'Amazon');

  return t;
}

function toTitleCaseSmart_(s) {
  const stop = new Set(['y', 'de', 'la', 'el', 'los', 'las', 'del', 'al', 'and', 'of', 'the']);
  const rawParts = String(s || '')
    .trim()
    .split(/\s+/)
    .filter(Boolean);

  const out = rawParts.map((orig, i) => {
    const w = orig.toLowerCase();

    if (orig.length <= 5 && /^[A-Z0-9]+$/.test(orig) && /[A-Z]/.test(orig)) return orig;
    if (i > 0 && stop.has(w)) return w;

    return w.charAt(0).toUpperCase() + w.slice(1);
  });

  return out.join(' ');
}

/**
 * Match exacto o por prefijo
 */
function findRuleMatchByPrefix_(map, cleanedKey) {
  if (!cleanedKey || !map || !map.size) return null;

  if (map.has(cleanedKey)) return { key: cleanedKey, value: map.get(cleanedKey), mode: 'EXACT' };

  for (const [ruleKey, ruleVal] of map.entries()) {
    if (!ruleKey) continue;
    if (cleanedKey.startsWith(ruleKey)) {
      return { key: ruleKey, value: ruleVal, mode: 'PREFIX' };
    }
  }
  return null;
}

function taxonomyToText_(taxonomyMap, maxChars) {
  if (!taxonomyMap || !taxonomyMap.size) return '';

  const cats = Array.from(taxonomyMap.keys()).sort((a, b) => a.localeCompare(b));
  let out = '';
  for (const cat of cats) {
    const subs = Array.from(taxonomyMap.get(cat) || [])
      .filter(Boolean)
      .sort((a, b) => a.localeCompare(b));
    const line = `- ${cat}: ${subs.length ? subs.join(', ') : 'N/A'}\n`;
    if (out.length + line.length > maxChars) break;
    out += line;
  }
  return out.trim();
}

function buildExamplesText_(localExamplesByCat, globalExamplesByCat, maxExamples) {
  const lines = [];
  const pushFrom = (map, src) => {
    if (!map) return;
    const cats = Array.from(map.keys()).sort((a, b) => a.localeCompare(b));
    for (const cat of cats) {
      const arr = map.get(cat) || [];
      for (const merch of arr) {
        if (lines.length >= maxExamples) return;
        lines.push(`- ${merch} => ${cat} (${src})`);
      }
    }
  };

  pushFrom(localExamplesByCat, 'LOCAL');
  if (lines.length < maxExamples) pushFrom(globalExamplesByCat, 'GLOBAL');

  return lines.join('\n');
}

/**
 * VALIDADORES: SOLO LOCAL
 */
function isCategoryAllowedLocalOnly_(cat, taxonomyMapLocal, validCategoriesLocal) {
  const c = String(cat || '').trim();
  if (!c) return false;
  if (taxonomyMapLocal && taxonomyMapLocal.size) return taxonomyMapLocal.has(c);
  return validCategoriesLocal && validCategoriesLocal.includes(c);
}

function isSubcategoryAllowedLocalOnly_(cat, sub, taxonomyMapLocal) {
  if (!taxonomyMapLocal || !taxonomyMapLocal.size) return true;

  const c = String(cat || '').trim();
  const s = String(sub || '').trim();

  if (!c) return false;
  if (!taxonomyMapLocal.has(c)) return false;

  const set = taxonomyMapLocal.get(c);
  if (!set || !set.size) return true;

  if (!s) return false;
  if (s === 'N/A') return true;

  return set.has(s);
}

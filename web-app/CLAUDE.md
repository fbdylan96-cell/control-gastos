# WEB APPS

## General description

This is an expense tracking tool we are building together incrementally.
**Always wait for explicit instruction before making changes.**
**Always read CLAUDE.md fully before writing any code.**
**Always match field names exactly to `create.sql` column names — it is the source of truth.**

## High-Level Workflow

```
Gmail (label: 'Finanzas Personales')
  └── Step 1-3: polls unread emails, parses body, writes transactions_raw
        └── Step 4: bank detection + field extraction → transactions_enriched
              └── Step 5: Classification  ← category/subcategory/logic → transactions_classified 
                    └── Step 6: Notification  ← email and/or WhatsApp to client 
                          └── Step 7: Reclassification  ← client can override 
```

## Databases

### `core.businesses`
Top-level multi-tenant grouping. Sentinel record
`id = '00000000-0000-0000-0000-000000009999'` (`__individual__`) is used for clients not tied to any company.

### `core.clients`
One row per person. Key fields:
- `email_forward` — the `crgastostesting+{alias}@gmail.com` forwarding address
- `active` — only active clients are processed. **Clients are created with `active = FALSE`.** Activation is done explicitly by the administrator (via `administracion`) or by the business admin (via `empresa`). Categories being present in `core.categories` does **not** trigger activation.
- `business_id` — links to `core.businesses` (or sentinel for individuals)

### `core.client_investment`
One row per client who has the **investment service** (Alpaca). Created/toggled from `administracion` (the "Cliente de inversión" checkbox / the "Inversión" column toggle).
- `enabled` — gate that makes the `persona` / `empresa` **Portafolio de Inversión** tab show real Alpaca data. When `FALSE` or no row, the tab keeps its marketing placeholder. In `empresa` the tab itself is hidden unless `enabled` (injected via `investment_enabled` context processor); in `persona` the tab is always visible and the route decides the state.
- `api_key_cipher` / `api_secret_cipher` — the client's Alpaca **API key pair, encrypted at rest** with AES-256-GCM as self-contained blobs (12-byte nonce ‖ ciphertext, client_id as AAD — `crypto.encrypt_secret`/`decrypt_secret`). The master key is `ENCRYPTION_KEY` in `.env`, **never stored in the DB**. `NULL` until the administrator loads them in Administración → Modificar Datos. **Write-only:** no endpoint ever returns them (only the boolean `alpaca_credentials_set`).
- **⚠ Read-only by code discipline, NOT by credential scope:** Alpaca API keys are full-permission (they CAN place orders). `web-app/alpaca_client.py` must only ever issue **GET** requests — never add a POST/PUT/PATCH/DELETE Alpaca call, and never log or return a credential. (The previous OAuth integration with empty scope was removed 2026-06-12 by business decision.)
- **FK to `core.clients`:** any client-delete path in `administracion` must delete the matching `core.client_investment` row first (already handled in `_wipe_individual`, `_wipe_business`, `_reassign_and_delete_member`).

### `core.categories`
Stores the category/subcategory taxonomy used by the classification engine (Step 5).

- **Scope:** Categories are **business-level** — all members of a business share the same set.  
  Rows use `(business_id, individual_id IS NULL, category, subcategory)`.  
  `individual_id` is only set when a category is scoped to a specific client (not currently used by any UI).
- **"Otros" default:** Every business (and the sentinel individual business) **must always have** a row `('Otros', NULL)`. The classification engine falls back to this category when it cannot determine a better match with sufficient confidence.
- **"Otros" is protected:** It must never be deleted. The `empresa` UI hides the delete button for it and the backend `categorias_delete` endpoint rejects the request. Do not remove these guards.
- **Unique constraint:** `UNIQUE (business_id, individual_id, category, subcategory)` — all inserts must use the upsert pattern (`WHERE NOT EXISTS …`) to be idempotent.

### `core.category_rules`
Rules used by the classification engine to map transaction attributes to a category/subcategory. Managed separately; not yet exposed in any web app UI.

### `core.transactions_raw`
One row per email notification. Raw data stored

### `core.transactions_enriched`
One row per email notification following `core.businesses`, `core.clients` and  `core.transactions_raw` indexes. Extract specific datapoints necessary for classification

### `core.transactions_classified`
One row per email notification following `core.businesses`, `core.clients` and  `core.transactions_raw` indexes. Our classification engine based on AI/rules determine category/subcategory according to categories in `core.categories` and rules in `core.category_rules`

### `core.transactions_notifications`
One row per email notification following `core.businesses`, `core.clients` and  `core.transactions_raw` indexes. Information captured from the feedback received from client via email/whatsaap is stored here. final_category and final_subcategory is the final determination

## Role of web apps

These apps are key in the onboarding of the client (insert client in databases) and expense reclassification.

### app-unificada\administracion

This one is for the use of the administrator. Will be able to add individual, bussiness and their members, access data from the databases and deactivate/activate clients

### app-unificada\empresa

This one is for the use of the bussiness clients. Will be able to add members, access data from the databases, deactivate/activate members, create categories/subcategories, download reports and reclassify transactions.

## Onboarding order of operations

### Business client
1. **administracion** creates the `core.businesses` record.
2. **administracion** creates the first client(s) via `POST /api/clients` — this also inserts the `('Otros', NULL)` category for the business.
3. Subsequent members added via **empresa** (`miembros_add`) only insert into `core.clients`; they do **not** touch `core.categories` (categories are already set at business level).
4. Business admin adds additional categories via **empresa** → Categorías.
5. Administrator or business admin explicitly sets `active = TRUE` when the client is ready.

### Individual client (sentinel business)
1. **administracion** creates the client via `POST /api/clients` with `business_id = '00000000-0000-0000-0000-000000009999'`.  
   This also upserts `('Otros', NULL)` for the sentinel business (idempotent — only inserted once across all individual clients).
2. Administrator explicitly sets `active = TRUE` when the client is ready.
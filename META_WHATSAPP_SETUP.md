# Meta WhatsApp Cloud API — Setup & Wire-Up Guide

End-to-end checklist for taking the WhatsApp notification flow from zero to
production on the AWS EC2 server hosting `control-gastos`.

Everything here assumes:
- The two message templates (`gasto_detectado_simple` and the budget variant)
  are already created and **approved** in Meta Business Manager.
- The Flask web app is running on EC2 at `https://gastos.empoweredinvestor.trade`.
- The poller (`email_reader.py`) is running on the same EC2 instance under systemd.

---

## 0. What you already have in this repo

| File | What it does |
|---|---|
| `whatsapp_client.py` | Meta Cloud API wrapper: send templates, list messages, text replies; verifies webhook signatures |
| `whatsapp_notifier.py` | Reads pending notifications and sends WhatsApp messages — invoked by `email_reader.run_once` after the email notifier |
| `db.py` (updated) | New helpers: `get_pending_whatsapp_notifications`, `mark_whatsapp_sent`, `update_whatsapp_action`, `get_notification_context`, `get_client_phone_for_notification` |
| `email_reader.py` (updated) | Calls `whatsapp_notifier.run_whatsapp_notifications(conn)` immediately after the email notifier in each poll cycle |
| `web-app/whatsapp_webhook.py` | Flask blueprint handling Meta's GET verification and POST callbacks (button taps + list selections) |
| `web-app/run.py` (updated) | Registers the blueprint at `/whatsapp` |

Public webhook URL once deployed:
```
https://gastos.empoweredinvestor.trade/whatsapp/webhook
```

---

## 1. Meta Business Manager: collect the credentials

You need four values from Meta. Go to <https://business.facebook.com/> →
**Business Settings** → **WhatsApp Accounts** → pick your WABA.

| Value | Where to find it | Used as env var |
|---|---|---|
| **Phone Number ID** | WhatsApp Manager → API Setup → "From" dropdown shows the Phone Number ID below the number | `META_WA_PHONE_ID` |
| **Permanent Access Token** | Business Settings → System Users → create or pick a system user → Generate Token → assign your WhatsApp app with `whatsapp_business_messaging` + `whatsapp_business_management` scopes | `META_WA_TOKEN` |
| **App Secret** | Meta for Developers → your App → Settings → Basic → "App secret" (click Show) | `META_WA_APP_SECRET` |
| **Verify Token** | You invent this — any random string. You'll paste it here AND into Meta's webhook config | `META_WA_VERIFY_TOKEN` |

> **Use a System User token, not a User token.** User tokens expire in 60 days.
> System User tokens are permanent (until revoked).

---

## 2. Confirm template names + variable order

Open WhatsApp Manager → Message Templates and confirm:

1. **Simple template** (no budget)
   - Name (exact): `gasto_detectado_simple` (or whatever you named it — set in env var)
   - Variables `{{1}}…{{8}}` in this order: type, currency, amount, local_amount, merchant, date, category, subcategory
   - Two QUICK_REPLY buttons in this order: **Reclasificar** (index 0), **Ir a aplicación** (index 1)

2. **Budget template**
   - Name (exact): set in env var (default suggestion `gasto_detectado_presupuesto`)
   - Variables `{{1}}…{{13}}` — first 8 same as above, then category, subcategory, spent_month, monthly_budget, percent
   - Same two QUICK_REPLY buttons in the same order

> Button order matters. The code assumes index 0 = Reclasificar, index 1 = Ir a aplicación. If your templates have them reversed, swap the order in `whatsapp_notifier._button_payloads`.

---

## 3. Env vars to add on EC2

Append to `/srv/control-gastos/.env`:

```bash
# Meta WhatsApp Cloud API
META_WA_PHONE_ID=123456789012345
META_WA_TOKEN=EAAG...long-system-user-token...
META_WA_APP_SECRET=abcd1234...
META_WA_VERIFY_TOKEN=pick-a-long-random-string-here
META_WA_TEMPLATE_SIMPLE=gasto_detectado_simple
META_WA_TEMPLATE_BUDGET=gasto_detectado_presupuesto
META_WA_TEMPLATE_SIMPLE_LANG=es
META_WA_TEMPLATE_BUDGET_LANG=es_ES
```

> The web-app reads the same `.env` (via `find_dotenv`), so a single file works for both processes.

---

## 4. Install the new dependency

```bash
cd /srv/control-gastos
sudo -u ubuntu pip install -r requirements.txt
```

(`requests` is the only new package.)

---

## 5. Configure the webhook in Meta

Meta for Developers → your App → WhatsApp → Configuration → Webhook → **Edit**:

- **Callback URL:** `https://gastos.empoweredinvestor.trade/whatsapp/webhook`
- **Verify Token:** the same string you set in `META_WA_VERIFY_TOKEN`
- Click **Verify and Save** — Meta will issue a `GET` with `hub.challenge`; our handler echoes it back.

Then under **Webhook fields**, subscribe to:
- `messages` (required — covers inbound button taps and list replies)

> You can also subscribe to `message_status` later if you want delivery
> tracking. The current code ignores statuses gracefully.

---

## 6. Make EC2 reachable from Meta

Meta calls your webhook from the public internet, so:

- The EC2 security group must allow inbound TCP **443** from the world (`0.0.0.0/0`).
- Nginx/Caddy/whatever-you-use must forward `https://gastos.empoweredinvestor.trade/whatsapp/*` to the Flask app on its bound port.
- A valid TLS certificate must be in place (Let's Encrypt is fine; Meta refuses self-signed certs).

Quick verification from your laptop:
```bash
curl -i "https://gastos.empoweredinvestor.trade/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=HoJDs1N60KzDH66&hub.challenge=ping"
# should return: 200 OK, body = "ping"
```

---

## 7. Activate clients for WhatsApp

For each client that should receive WhatsApp messages, set in `core.clients`:

```sql
UPDATE core.clients
SET whatsapp_notification = TRUE,
    phone_number = '+50688887777'   -- must start with '+', spaces allowed (stripped at send time)
WHERE id = '<client-uuid>';
```

> The phone is normalized at send time: spaces removed, `+` required (returned to Meta as digits only).

---

## 8. Restart services

```bash
cd /srv/control-gastos
git pull
sudo systemctl restart email-reader
sudo systemctl restart <web-app-service-name>     # whatever runs run.py (gunicorn, etc.)
```

Tail logs:
```bash
sudo journalctl -u email-reader -f
sudo journalctl -u <web-app-service-name> -f
```

On the next ingestion you should see:
```
WA notifier: 1 notification(s) to send
  WA sent → 50688887777 | template=gasto_detectado_simple | merchant=Shell | cat=Transporte
WA notifier complete: 1/1 sent
```

---

## 9. End-to-end smoke test

1. Forward a real bank email through the pipeline → wait for the next poll → confirm WhatsApp template arrives on the registered phone.
2. Tap **Ir a aplicación** → confirm a text with `https://gastos.empoweredinvestor.trade/persona/` (or `/empresa/`) arrives.
3. Tap **Reclasificar** → confirm a list message arrives titled "Seleccionar categoría correcta".
4. Open the list and pick any row → confirm a "✅ Reclasificación guardada: …" reply arrives.
5. Verify in the DB:
   ```sql
   SELECT id, whatsapp_notified, whatsapp_notified_at,
          whatsapp_action_at, whatsapp_action_value,
          final_category, final_subcategory, reclassified_by, reclassified_at
   FROM core.transactions_notifications
   ORDER BY created_at DESC
   LIMIT 5;
   ```

---

## 10. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Meta refuses to verify the webhook | Verify token mismatch, or the URL isn't reachable on HTTPS:443 with a valid cert |
| `WA webhook: invalid signature, rejecting` in logs | `META_WA_APP_SECRET` doesn't match the app secret in Meta for Developers |
| `WA send failed [400]: ... (#132000) Number of parameters does not match` | Template variable count drifted between Meta and `_build_simple_params` / `_build_budget_extra_params` — re-check `{{N}}` mapping |
| `WA send failed [400]: ... (#131026) Re-engagement message` | More than 24h elapsed since user's last reply AND you tried to send free-form text. Only templates can re-open the window. |
| List message opens with empty rows | Row title exceeded 24 chars (we already truncate to 20). Check `_build_reclassify_sections` if you tweak it. |
| User taps a row but nothing happens | Webhook isn't subscribed to `messages`, or `META_WA_APP_SECRET` is wrong (causes 403 silently) |
| Notification sent twice | `mark_whatsapp_sent` failed after `send_template` succeeded — check for DB connection errors around the send |

---

## 11. Limits to remember

- 24-hour customer service window: only opens after **user** sends a message or taps a button. Outside it → templates only.
- Template body params: cannot contain newlines, tabs, or >4 consecutive spaces.
- List Message: max 10 sections, 10 rows per section, 100 rows total. We truncate over 100 and log a warning.
- Row title 24 chars (we cap at 20 to match the request), section title 24, description 72.
- Payload (button/row id): 200 chars — plenty of room for our `rc|nid=…|c=…|s=…` format.

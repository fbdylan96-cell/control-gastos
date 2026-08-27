# PayPal — Motor de pagos (Fase 2)

Guía para cablear PayPal vivo. **Sandbox primero, live después.** El código ya
está integrado; esto es lo operativo que falta hacer en cada ambiente.

## Qué hace la integración

- Cobros **en USD** (PayPal no soporta CRC — verificado 2026-06-25). La app
  muestra ₡ (`amount_crc`) y cobra `amount_usd`.
- Un **Product** fijo (`NETO-CONTROL-GASTOS`) + un **Billing Plan** por fila de
  `core.subscription_plans` (id guardado en `paypal_plan_id`).
- La suscripción del cliente se crea con `custom_id = clients.id`; el cliente
  aprueba en PayPal y vuelve a `/persona/facturacion/paypal/retorno`.
- Si el cliente está en **prueba gratuita**, la suscripción se crea con
  `start_time = trial_end`: aprueba hoy, el primer cobro cae al terminar la
  prueba (por eso el plan NO lleva ciclo TRIAL — un ciclo fijo daría 30 días
  más sin importar cuánta prueba quede).
- `paypal_webhook.py` (`POST /paypal/webhook`) mantiene
  `core.client_subscriptions` al día: `active`, `past_due` (pago fallido),
  `suspended`, `cancelled`. Verificación de firma contra la API
  (`verify-webhook-signature`) con `PAYPAL_WEBHOOK_ID`, fail-closed.

## Paso a paso (sandbox)

1. **Crear la app REST** en https://developer.paypal.com → Apps & Credentials
   → ambiente *Sandbox* → Create App. Copiar Client ID y Secret.

2. **`.env` del servidor** (y local para probar):

   ```
   PAYPAL_CLIENT_ID=...
   PAYPAL_CLIENT_SECRET=...
   PAYPAL_MODE=sandbox
   PAYPAL_WEBHOOK_ID=   # se llena en el paso 4
   ```

3. **Bootstrap del catálogo** (crea Product + Plans y guarda los ids):

   ```bash
   cd web-app
   python paypal_bootstrap.py            # dry-run: muestra qué crearía
   python paypal_bootstrap.py --ejecutar
   ```

4. **Registrar el webhook** en el dashboard de la app (mismo ambiente):
   - URL: `https://gastos.empoweredinvestor.trade/paypal/webhook`
   - Eventos: `BILLING.SUBSCRIPTION.ACTIVATED`, `BILLING.SUBSCRIPTION.UPDATED`,
     `BILLING.SUBSCRIPTION.CANCELLED`, `BILLING.SUBSCRIPTION.EXPIRED`,
     `BILLING.SUBSCRIPTION.SUSPENDED`, `BILLING.SUBSCRIPTION.PAYMENT.FAILED`,
     `PAYMENT.SALE.COMPLETED`
   - Copiar el **Webhook ID** a `PAYPAL_WEBHOOK_ID` y reiniciar el webapp.

   ⚠ El webhook de sandbox igual llega a la URL pública de prod. Para probar
   sin tocar prod: correr el webapp local y exponerlo (p. ej. `ssh -R` /
   túnel), o probar directo en el server con la BD de prueba.

5. **Probar el flujo completo** con la cuenta *sandbox personal* (la crea
   PayPal automáticamente, en developer.paypal.com → Testing Tools →
   Sandbox Accounts):
   - Login en `/persona` con un cliente de prueba → Facturación → Activar con
     PayPal → aprobar con la cuenta sandbox.
   - Verificar: el tab queda "Activo", método de pago PayPal, próximo pago con
     fecha; en la BD `client_subscriptions.paypal_subscription_id` lleno.
   - En journalctl deben verse los eventos `BILLING.SUBSCRIPTION.ACTIVATED`.
   - Probar también: cancelar desde el tab, y (Testing Tools → simulador de
     webhooks NO sirve para firmas reales — usar el flujo real de sandbox).

## Pasar a live

1. Crear app REST en ambiente **Live** → nuevas credenciales.
2. En la BD de prod: `UPDATE core.subscription_plans SET paypal_plan_id = NULL;`
   (los plan ids de sandbox NO existen en live) y correr el bootstrap de nuevo
   con `PAYPAL_MODE=live`.
3. Registrar el webhook en la app live (mismos eventos) → nuevo
   `PAYPAL_WEBHOOK_ID`.
4. Actualizar `.env` de prod (`PAYPAL_MODE=live` + credenciales live) y
   reiniciar el webapp.
5. Prueba real con un plan mensual y una cuenta PayPal real; cancelar después
   desde el tab si no se quiere dejar corriendo.

## Decisiones registradas

- **USD, no CRC**: restricción dura de PayPal; `amount_crc` es solo display.
- **`start_time` en vez de ciclo TRIAL**: respeta los días de prueba restantes
  exactos.
- **Cancelación**: detiene cobros futuros de inmediato (API `cancel`); el
  acceso conceptual sigue hasta `current_period_end`
  (`cancel_at_period_end = TRUE` queda registrado). El cliente también puede
  cancelar desde su cuenta de PayPal — el webhook `CANCELLED` lo refleja.
- **Cortesías (`comp = TRUE`)**: los webhooks y la activación nunca degradan
  una cortesía (guard en los helpers de `db.py`); la UI no ofrece suscribirse.
- La app aún **no bloquea acceso** por suscripción vencida — el estado queda
  registrado para decidir eso después (gate de acceso = tarea aparte).

# Plan de implementación — Seguimiento de Asesoría (medium ticket)

Servicio: Asesoría Financiera Personal, $600 USD / 6 meses. Diseño comercial en
`Asesoria_Financiera_Neto.docx`. Este documento es la referencia técnica de
implementación.

## Fases

| Fase | Contenido | Estado |
|---|---|---|
| 1 | Tablas (`client_advisory_plans`, `advisory_message_log`) + `send_template` con header | ✅ Hecha |
| 2 | Setting del plan en Panel de Administración | ✅ Hecha |
| 3 | `advisory_scheduler.py` (jobs semanal y mensual) + systemd | ✅ Hecha |
| 4 | Hook de alertas 80%/100% en el pipeline de notificación | ✅ Hecha |
| 5 | Handler del quick reply «Ver componentes del gasto» en el webhook | ✅ Hecha |
| 6 | Reporte PDF mensual con comentario del asesor | Pendiente |

## 1. Workflow (extremo a extremo)

1. **Administración** activa el plan de asesoría del cliente → fila en
   `core.client_advisory_plans` (metas, día de envío, fechas del programa).
   Durante el mes baseline `tracking_start` queda NULL → ningún envío.
2. En la reunión de activación el asesor fija `tracking_start` → los jobs
   comienzan a enviar.
3. **Scheduler** (`advisory_scheduler.py`, proceso systemd independiente):
   - Job semanal → plantilla `seguimiento_semanal_presupuesto`.
   - Job mensual → `seguimiento_fondo_emergencia` o `_noexcedente` según el
     signo del excedente (ingresos − gastos) del mes cerrado.
4. **Pipeline** (paso de notificación de `email_reader.py`): al clasificar cada
   transacción evalúa los umbrales 80%/100% de la categoría → plantillas
   `alerta_presupuesto` / `alerta_presupuesto_superado`.
5. **Webhook**: el tap en «Ver componentes del gasto» (payload `ad|cid=…`)
   abre la ventana de 24 h; el webhook verifica que el teléfono que tocó el
   botón pertenece a ese cliente (scope server-side, como el chat de IA) y
   responde con el desglose del gasto del mes por categoría — texto libre
   determinístico (sin plantilla ni costo de LLM), máx. 12 filas + «Resto»
   agregado + total. El cliente puede seguir con el chat de IA existente.
6. **Idempotencia**: todo envío inserta primero en `core.advisory_message_log`
   (`ON CONFLICT DO NOTHING`); solo se envía si la fila se insertó. Garantiza
   disparo único por período sin lógica adicional.

## 2. Módulos de seguimiento independientes

Cada módulo tiene su propio flag en el plan — un cliente puede tener el fondo de
emergencia completo y seguir recibiendo solo el resumen semanal:

| Módulo | Flag | Consumidor |
|---|---|---|
| Resumen semanal de presupuesto | `weekly_summary_enabled` | Job semanal |
| Fondo de emergencia / tasa de ahorro (mensual) | `fund_tracking_enabled` | Job mensual |
| Alertas 80% y 100% por categoría | `budget_alerts_enabled` | Hook del pipeline |

`enabled` es el interruptor maestro (todos los módulos filtran además por él).
Completar la meta del fondo NO desactiva el módulo automáticamente: es decisión
del asesor desde el panel.

## 3. Schedulers (`advisory_scheduler.py`, Fase 3)

Proceso independiente con `BlockingScheduler` + unidad systemd
(`advisory-scheduler.service`), patrón de `rate_scheduler.py`, con alerta por
correo SMTP (neto) si un envío falla. El fallo de un cliente nunca detiene el
lote.

- **Job semanal** — todos los días a las 17:00; filtra planes con
  `enabled AND weekly_summary_enabled AND weekly_send_dow = hoy` y
  `tracking_start` vigente, cliente `active` con `whatsapp_notification`.
  Calcula ingresos del mes, gasto acumulado y días restantes (reutiliza
  `tools/finance.py`). El **% del presupuesto** usa el presupuesto global
  `ingreso_ref × (1 − target_savings_rate)` — la meta de tasa se pide a todos
  al inicio de la asesoría. `ingreso_ref` = `declared_monthly_income` si está
  configurado, si no los ingresos del mes anterior cerrado (el ingreso del mes
  en curso es inútil a inicio de mes). Sin meta o sin ingreso de referencia,
  el % va como "—". (Las alertas 80/100 de la Fase 4 sí usan el
  `monthly_budget` de cada categoría — son conceptos distintos.)
- **Job mensual** — día 1 a las 9:00 sobre el mes anterior cerrado; filtra por
  `fund_tracking_enabled`. Si `declared_monthly_income` no es NULL, sustituye a
  los ingresos capturados (bancos que no notifican créditos).
- `period_key` del log: semanal `'2026-W30'`, mensual `'2026-07'`, alertas
  `'2026-07:<categoria>'`.
- Transaccionalidad: claim del log + envío + `commit` juntos; un envío fallido
  hace rollback del claim y se reintenta en la siguiente corrida.
- El payload del botón del resumen semanal es `ad|cid=<client_id>` — lo
  resuelve el webhook en la Fase 5.

### Despliegue (EC2)

```ini
# /etc/systemd/system/advisory-scheduler.service
[Unit]
Description=Neto advisory follow-up scheduler (WhatsApp semanal/mensual)
After=network-online.target

[Service]
WorkingDirectory=/srv/control-gastos
ExecStart=/srv/control-gastos/venv/bin/python advisory_scheduler.py
Restart=always
RestartSec=10
User=ubuntu

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now advisory-scheduler
sudo journalctl -u advisory-scheduler -f
```

Variables `.env` (todas opcionales — los defaults del código son los nombres
aprobados en Meta): `META_WA_TEMPLATE_ADVISORY_WEEKLY`,
`META_WA_TEMPLATE_ADVISORY_FUND`, `META_WA_TEMPLATE_ADVISORY_FUND_NEG`,
`META_WA_TEMPLATE_ADVISORY_LANG` (default `es`) y `ADVISORY_ALERT_EMAIL`
(default: el propio `SMTP_USER`).

## 4. Alertas 80%/100% (Fase 4)

Implementadas en `advisory_alerts.py`, invocado desde `email_reader.run_once`
inmediatamente después de las notificaciones de transacción — se evalúan solo
en ciclos que procesaron transacciones nuevas. El
`PRIMARY KEY (client_id, message_type, period_key)` del log es la garantía de
disparo único por categoría/mes. Máximo 2 alertas por categoría al mes (una de
cada tipo); si una categoría salta directo del <80% a >100%, se envía solo la
del 100% y el claim del 80% se marca también para que nunca llegue después una
alerta del 80% obsoleta. Presupuesto y gasto siguen la semántica de
`db.get_budget_and_spending` (individual: presupuesto personal + gasto propio;
empresa: presupuesto y gasto a nivel negocio). `run_budget_alerts` nunca lanza
excepciones — un fallo se loguea y el pipeline sigue. La alerta del 100% es
además disparador de llamada proactiva del asesor.

## 5. Plantillas Meta (aprobadas 2026-07-26)

Todas UTILITY, idioma `es`, con header cuyo parámetro va en un componente
separado del body (numeración reinicia en {{1}}):

| Plantilla | Header | Body | Botones |
|---|---|---|---|
| `seguimiento_semanal_presupuesto` | `Resumen semanal \| {{1}}` | 4 vars | «Ver componentes del gasto» |
| `seguimiento_fondo_emergencia` | `Fondo de emergencia \| {{1}}` | 7 vars | — |
| `seguimiento_fondo_emergencia_noexcedente` | `Fondo de emergencia \| {{1}}` | 6 vars | — |
| `alerta_presupuesto` | `Alerta de presupuesto \| {{1}}` | 6 vars | — |
| `alerta_presupuesto_superado` | `Alerta de presupuesto \| {{1}}` | 3 vars | — |

Variables `.env` a agregar (Fase 3): `META_WA_TEMPLATE_ADVISORY_WEEKLY`,
`META_WA_TEMPLATE_ADVISORY_FUND`, `META_WA_TEMPLATE_ADVISORY_FUND_NEG`,
`META_WA_TEMPLATE_ADVISORY_ALERT80`, `META_WA_TEMPLATE_ADVISORY_ALERT100`
(+ sus `_LANG` si difieren de `es`).

## 6. Panel de Administración (Fase 2)

Espejo del patrón "Cliente de inversión":

- Checkbox "Cliente de asesoría" al crear/editar → crea el plan con
  `enabled = FALSE`.
- Columna "Asesoría" con toggle en la tabla de clientes → `enabled`.
- Modificar Datos → sección "Plan de asesoría": fechas (inicio programa /
  inicio seguimiento / fin), objetivo, meta tasa de ahorro, meta y saldo actual
  del fondo (el saldo lo actualiza el asesor: el fondo vive fuera de Neto),
  ingreso declarado, día de envío semanal, y los 3 toggles de módulos + notas.
- Validación al activar: requiere `phone_number`, `whatsapp_notification` y
  `messaging_approval`; advertencia si ninguna categoría tiene `monthly_budget`.
- Borrado de clientes: ambas tablas nuevas usan `ON DELETE CASCADE` (patrón
  `client_subscriptions`) — los paths de borrado de administracion no
  necesitan cambios.

## ⚠ Prerrequisito de despliegue

El `GET /api/clients` de administracion ahora hace `LEFT JOIN` a
`core.client_advisory_plans`: **correr `advisory_schema.sql` en la base de
producción ANTES de desplegar este código** (si no, el panel de administración
falla al listar clientes):

```
psql "$DB_PROD_URL" -f advisory_schema.sql   # desde el EC2
```

## Advertencia operativa

La tasa de ahorro depende de que los ingresos lleguen al pipeline: no todos los
bancos notifican créditos por correo. Validar la captura de ingresos durante el
mes baseline; si es incompleta, configurar `declared_monthly_income` en el plan.

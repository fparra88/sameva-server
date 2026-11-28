-- 81_plantillas_por_cuenta.sql
-- Plantillas de WhatsApp por cuenta (WABA) cuando el negocio tiene varios números. Idempotente.
--
--   En Meta una plantilla pertenece a la cuenta de WhatsApp Business (WABA), no al número: dos
--   números de la misma WABA comparten catálogo, y dos WABAs distintas pueden tener una plantilla
--   con el mismo nombre e idioma. Por eso:
--
--   - agentes_canal.waba_id: la WABA de cada número. Meta directo la resuelve con Graph la primera
--     vez que hace falta (services/plantillas_cuentas.py); NeuroAPI la manda en el webhook de
--     conexión (routers/canales_neuroapi_webhook.py). NULL = todavía no se sabe (números viejos).
--   - plantillas_whatsapp_tenant: la unicidad pasa de (tenant, nombre, idioma) a
--     (tenant, WABA, nombre, idioma). waba_id NULL = la cuenta única del negocio (siempre así en
--     NeuroAPI, cuya API de plantillas solo conoce la x-api-key del negocio); COALESCE para que
--     dos NULL choquen igual que antes.

ALTER TABLE agentes_canal ADD COLUMN IF NOT EXISTS waba_id VARCHAR(64);

CREATE INDEX IF NOT EXISTS ix_agentes_canal_waba
    ON agentes_canal (tenant_id, waba_id)
    WHERE waba_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_plantillas_tenant_waba_nombre_idioma
    ON plantillas_whatsapp_tenant (tenant_id, (COALESCE(waba_id, '')), nombre, idioma);

-- El índice viejo impediría tener la misma plantilla en dos WABAs del negocio. El nuevo lo
-- cubre (con waba_id NULL se comporta igual), así que se quita después de crear el nuevo.
DROP INDEX IF EXISTS ux_plantillas_tenant_nombre_idioma;

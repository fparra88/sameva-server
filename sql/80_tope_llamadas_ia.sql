-- 80_tope_llamadas_ia.sql
-- Tope mensual de llamadas de IA por plan (spec 001-tope-llamadas-ia, §3). Idempotente.
--
--   - tenant_token_usage.llamadas: llamadas al modelo que representa cada fila de consumo.
--     Las filas históricas quedan con 1 (subestima; aceptado).
--   - plan_limit_events.motivo: por qué se bloqueó ('plan' = comportamiento previo,
--     'limite_llamadas_ia' = tope de llamadas de IA agotado).
--   - idx_token_usage_ciclo: acelera la suma de llamadas del ciclo (excluye transcripciones).
--   - alertas_tipo_check: lista vigente (misma que sql/65_avisos_cita_tenant.sql, la última
--     que la redefine) + 'db_externa_error' (la emite services/db_externa.py y NUNCA estuvo
--     en la lista, así que su INSERT fallaba) + 'limite_llamadas_ia'.

ALTER TABLE tenant_token_usage
    ADD COLUMN IF NOT EXISTS llamadas INTEGER NOT NULL DEFAULT 1 CHECK (llamadas >= 0);

ALTER TABLE plan_limit_events
    ADD COLUMN IF NOT EXISTS motivo TEXT NOT NULL DEFAULT 'plan';

CREATE INDEX IF NOT EXISTS idx_token_usage_ciclo
    ON tenant_token_usage (tenant_id, created_at)
    WHERE origen <> 'transcripcion';

ALTER TABLE alertas DROP CONSTRAINT IF EXISTS alertas_tipo_check;
ALTER TABLE alertas ADD CONSTRAINT alertas_tipo_check
    CHECK (tipo IN (
        'nuevo_lead', 'cambio_etapa', 'sin_actividad', 'cuota_excedida', 'cierre',
        'reserva_creada', 'reserva_cancelada', 'conversacion_transferida',
        'perfil_incompleto', 'agenda_reprogramada', 'solicitud_escritura',
        'conversacion_asignada', 'asignacion_fallida',
        'mensaje_conversacion_asignada',
        'conversacion_tomada', 'conversacion_liberada', 'conversacion_devuelta_ia',
        'lead_asignado',
        'aviso_cita_no_enviado',
        'numeros_pausados', 'numeros_recortados', 'numero_configuracion_incompleta',
        'avisos_cita_oferta_rechazada', 'avisos_cita_plantilla_inutilizable',
        'db_externa_error',
        'limite_llamadas_ia'
    ));

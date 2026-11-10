-- 73 · Estructura que existía en la base real y no estaba versionada
--
-- Resultado de comparar estructura.sql (dump de producción) contra sql/*.sql.
-- Lo que faltaba:
--   - tablas usage_events, plan_limit_events, widget_rate_limit
--   - funciones tenant_tiene_acceso, uso_actual_tenant, valida_tenant_canal,
--     trigger_nueva_alerta (+ trigger alerta_nueva sobre alertas)
--   - columnas legadas de `planes` que esas funciones leen
-- (tenants/users/... van en 00a_base_complemento.sql; meta_apps en
-- 52a_meta_apps.sql.)
--
-- Idempotente.

-- usage_events: consumo por conversación (lo lee uso_actual_tenant) -------
CREATE TABLE IF NOT EXISTS usage_events (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    conversation_id UUID REFERENCES conversations(id),
    tipo            TEXT NOT NULL,
    tokens_input    INTEGER,
    tokens_output   INTEGER,
    creado_en       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_usage_tenant_fecha
    ON usage_events (tenant_id, creado_en);

-- plan_limit_events: mensajes rechazados por tope del plan ----------------
CREATE TABLE IF NOT EXISTS plan_limit_events (
    id         BIGSERIAL PRIMARY KEY,
    tenant_id  UUID REFERENCES tenants(id),
    canal      TEXT,
    usuario_id TEXT,
    mensaje_id TEXT,
    creado_en  TIMESTAMPTZ DEFAULT NOW()
);

-- widget_rate_limit: límite por IP del widget web -------------------------
CREATE TABLE IF NOT EXISTS widget_rate_limit (
    id         BIGSERIAL PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    ip         TEXT NOT NULL,
    session_id TEXT,
    tipo       TEXT NOT NULL DEFAULT 'respuesta',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_widget_rate_limit_lookup
    ON widget_rate_limit (tenant_id, ip, created_at DESC);

-- planes: columnas legadas (esquema n8n) ---------------------------------
-- En la base real son precio_mxn NOT NULL y los demás con default; aquí
-- precio_mxn queda NULL-able porque el backend nunca lo escribe y un NOT NULL
-- rompería los INSERT de /gerencia/planes.
ALTER TABLE planes ADD COLUMN IF NOT EXISTS precio_mxn NUMERIC(10,2);
ALTER TABLE planes ADD COLUMN IF NOT EXISTS incluye_agente_ia BOOLEAN DEFAULT false;
ALTER TABLE planes ADD COLUMN IF NOT EXISTS incluye_crm_vendedores BOOLEAN DEFAULT false;
ALTER TABLE planes ADD COLUMN IF NOT EXISTS limite_conversaciones_mes INTEGER;
ALTER TABLE planes ADD COLUMN IF NOT EXISTS limite_tokens_mes BIGINT;
ALTER TABLE planes ADD COLUMN IF NOT EXISTS limite_vendedores INTEGER;

-- Funciones -------------------------------------------------------------
CREATE OR REPLACE FUNCTION tenant_tiene_acceso(p_tenant_id UUID, p_feature TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql STABLE
AS $$
DECLARE
    v_tiene BOOLEAN;
    v_estado TEXT;
BEGIN
    SELECT ts.estado,
           CASE p_feature
               WHEN 'agente_ia' THEN p.incluye_agente_ia
               WHEN 'crm_vendedores' THEN p.incluye_crm_vendedores
               ELSE false
           END
    INTO v_estado, v_tiene
    FROM tenant_subscriptions ts
    JOIN planes p ON p.id = ts.plan_id
    WHERE ts.tenant_id = p_tenant_id
      AND ts.estado IN ('activa', 'en_periodo_gracia')
    LIMIT 1;

    RETURN COALESCE(v_tiene, false);
END;
$$;

CREATE OR REPLACE FUNCTION uso_actual_tenant(p_tenant_id UUID)
RETURNS TABLE(conversaciones BIGINT, tokens BIGINT)
LANGUAGE sql
AS $$
    SELECT COUNT(DISTINCT ue.conversation_id),
           COALESCE(SUM(ue.tokens_input + ue.tokens_output), 0)
    FROM usage_events ue
    JOIN tenant_subscriptions ts ON ts.tenant_id = ue.tenant_id
    WHERE ue.tenant_id = p_tenant_id
      AND ts.estado IN ('activa', 'en_periodo_gracia')
      AND ue.creado_en >= ts.fecha_inicio;
$$;

CREATE OR REPLACE FUNCTION valida_tenant_canal(
    p_tenant_id  UUID,
    p_channel    VARCHAR,
    p_identifier VARCHAR
)
RETURNS BOOLEAN
LANGUAGE sql STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM channel_credentials c
        WHERE c.tenant_id = p_tenant_id
          AND c.channel_type = p_channel
          AND c.is_active = true
          AND p_identifier IN (
              COALESCE(c.phone_number_id, ''),
              COALESCE(c.page_id, ''),
              COALESCE(c.ig_user_id, '')
          )
    );
$$;

-- NOTIFY por alerta nueva (canal nueva_alerta_<tenant_id>) ----------------
CREATE OR REPLACE FUNCTION trigger_nueva_alerta()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM pg_notify(
        'nueva_alerta_' || NEW.tenant_id::text,
        json_build_object(
            'alerta_id', NEW.id,
            'tipo', NEW.tipo,
            'titulo', NEW.titulo,
            'mensaje', NEW.mensaje
        )::text
    );
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS alerta_nueva ON alertas;
CREATE TRIGGER alerta_nueva
    AFTER INSERT ON alertas
    FOR EACH ROW EXECUTE FUNCTION trigger_nueva_alerta();

-- Índices únicos de la base real ---------------------------------------
-- Dedupe de mensajes entrantes por id del canal (wamid, mid...).
CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_conv_channel_msg
    ON messages (conversation_id, channel_msg_id) WHERE channel_msg_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_tenant_tools_tenant_key
    ON tenant_tools (tenant_id, tool_key);

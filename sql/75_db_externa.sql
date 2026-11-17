-- ============================================================
-- Herramienta «Base de datos externa» (tool_type = 'db_externa')
-- ============================================================
-- El negocio conecta una base MySQL o PostgreSQL propia, con un usuario de
-- solo lectura, y el agente la consulta. La configuración no secreta vive en
-- `tenant_tools.config`; la contraseña, cifrada desde Python (services/secretos.py),
-- en `tool_secretos`. Idempotente.

-- 1) La familia nueva puede ir en la lista de un plan. NO se siembra en ningún
--    plan (nace desactivada, la enciende gerencia) y NO entra en el catálogo
--    "completo" que se aplica a una prueba sin plan (limites_efectivos).
ALTER TABLE plan_herramientas DROP CONSTRAINT IF EXISTS plan_herramientas_familia_check;
ALTER TABLE plan_herramientas ADD CONSTRAINT plan_herramientas_familia_check
    CHECK (familia IN ('calendario', 'sql_lectura', 'http', 'google_sheets', 'google_docs', 'db_externa'));

-- 2) Misma regla que services/herramientas_plan.familia_de (un test lo comprueba).
CREATE OR REPLACE FUNCTION familia_de_herramienta(p_tool_type TEXT, p_tool_key TEXT)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN p_tool_key IN (
            'consultar_servicios', 'consultar_proveedores', 'consultar_disponibilidad',
            'crear_reserva', 'cancelar_reserva', 'consultar_cancelacion_reserva',
            'historial_citas_cliente', 'consultar_citas_proximas', 'reagendar_reserva'
        ) THEN 'calendario'
        WHEN p_tool_type = 'sql_lectura' THEN 'sql_lectura'
        WHEN p_tool_type IN ('http_generico', 'http_post_json') THEN 'http'
        WHEN p_tool_type = 'google_sheets' THEN 'google_sheets'
        WHEN p_tool_type = 'google_docs' THEN 'google_docs'
        WHEN p_tool_type = 'db_externa' THEN 'db_externa'
        ELSE NULL
    END;
$$;

-- 3) Secretos de las herramientas. `secreto` es un token Fernet (nunca texto
--    plano). Se borra en cascada con la herramienta y con el negocio.
CREATE TABLE IF NOT EXISTS tool_secretos (
    tenant_id   UUID         NOT NULL,
    tool_key    VARCHAR(100) NOT NULL,
    secreto     BYTEA        NOT NULL,
    created_at  TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMP    NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, tool_key),
    CONSTRAINT tool_secretos_tool_fk FOREIGN KEY (tenant_id, tool_key)
        REFERENCES tenant_tools (tenant_id, tool_key) ON DELETE CASCADE
);

-- 4) Constancia de lo que el negocio aceptó al conectar una base propia (responsabilidad
--    sobre los datos expuestos; permisos de escritura detectados y aceptados). Es evidencia
--    para reclamaciones: NO cuelga de la herramienta (sobrevive a que se borre) y solo se
--    borra con el negocio.
CREATE TABLE IF NOT EXISTS db_externa_aceptaciones (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    tool_key        VARCHAR(100) NOT NULL,
    portal_user_id  UUID,
    email           TEXT,
    tipo            VARCHAR(30)  NOT NULL
                       CHECK (tipo IN ('responsabilidad', 'permisos_escritura')),
    texto_version   VARCHAR(20)  NOT NULL,
    detalle         JSONB        NOT NULL DEFAULT '{}',
    ip              TEXT,
    user_agent      TEXT,
    creado_en       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_db_externa_aceptaciones_tenant
    ON db_externa_aceptaciones (tenant_id, creado_en DESC);

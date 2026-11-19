-- ============================================================
-- Agente multimedia: imágenes, documentos y notas de voz del cliente
-- ============================================================
-- El agente pasa de leer solo texto a analizar lo que el cliente manda:
-- imágenes (JPG/PNG) y PDF van directo al modelo; DOCX como texto extraído;
-- el audio se transcribe con un proveedor externo (services/agente/transcripcion.py).
-- Idempotente.

-- 1) La familia `multimedia` puede ir en la lista de un plan. Igual que
--    db_externa (sql/75): NO se siembra en ningún plan (nace apagada, la
--    enciende gerencia) y NO entra en el catálogo "completo" de una prueba sin
--    plan. No es un tipo de herramienta: `familia_de_herramienta` no cambia.
ALTER TABLE plan_herramientas DROP CONSTRAINT IF EXISTS plan_herramientas_familia_check;
ALTER TABLE plan_herramientas ADD CONSTRAINT plan_herramientas_familia_check
    CHECK (familia IN (
        'calendario', 'sql_lectura', 'http', 'google_sheets', 'google_docs',
        'db_externa', 'multimedia'
    ));

-- 2) Transcripciones de notas de voz. El audio NO se guarda (decisión de
--    producto: solo el texto); por eso no va en message_attachments, cuyo
--    `contenido` es obligatorio y lo sirven otros endpoints. `messages` es de
--    n8n y su `content` se queda con "[Audio]": el historial y el portal leen
--    el texto de acá.
CREATE TABLE IF NOT EXISTS message_transcripciones (
    message_id   UUID PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    texto        TEXT NOT NULL,
    -- Lo que informa el proveedor; NULL si no lo dio. Sirve para el costo.
    segundos     NUMERIC(8, 2) CHECK (segundos IS NULL OR segundos >= 0),
    proveedor    VARCHAR(30)  NOT NULL,
    modelo       VARCHAR(100) NOT NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_message_transcripciones_tenant
    ON message_transcripciones (tenant_id, created_at DESC);

-- 3) El costo de la transcripción se anota en tenant_token_usage con su
--    propio origen, para que gerencia lo vea separado del modelo de chat.
ALTER TABLE tenant_token_usage DROP CONSTRAINT IF EXISTS tenant_token_usage_origen_check;
ALTER TABLE tenant_token_usage ADD CONSTRAINT tenant_token_usage_origen_check
    CHECK (origen IN ('agente', 'herramienta', 'resumen', 'otro', 'transcripcion'));

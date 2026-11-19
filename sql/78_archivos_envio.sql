-- ============================================================
-- Archivos que el agente puede enviar al cliente (WhatsApp)
-- ============================================================
-- El negocio sube hasta 5 archivos (catálogo, lista de precios, fotos) y el
-- agente elige cuál mandar. Los bytes viven en S3 (services/almacen_archivos.py);
-- acá solo van los metadatos. Idempotente.

-- 1) Familia opcional `archivos`: igual que db_externa y multimedia, NO se
--    siembra en ningún plan (nace apagada, la enciende gerencia) y NO entra
--    en el catálogo "completo" de una prueba sin plan. No es un tipo de
--    herramienta: `familia_de_herramienta` no cambia.
ALTER TABLE plan_herramientas DROP CONSTRAINT IF EXISTS plan_herramientas_familia_check;
ALTER TABLE plan_herramientas ADD CONSTRAINT plan_herramientas_familia_check
    CHECK (familia IN (
        'calendario', 'sql_lectura', 'http', 'google_sheets', 'google_docs',
        'db_externa', 'multimedia', 'archivos'
    ));

-- 2) Catálogo de archivos del negocio. El tope de 5 lo aplica el servicio
--    (con advisory lock); el UNIQUE evita dos filas con el mismo objeto S3.
CREATE TABLE IF NOT EXISTS tenant_archivos (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    nombre       VARCHAR(100) NOT NULL,          -- nombre de archivo que ve el cliente
    descripcion  VARCHAR(500) NOT NULL,          -- cuándo debe enviarlo el agente
    mime         VARCHAR(100) NOT NULL,
    tipo_wa      VARCHAR(10)  NOT NULL CHECK (tipo_wa IN ('image', 'document')),
    bytes        INTEGER      NOT NULL CHECK (bytes > 0),
    storage_key  VARCHAR(255) NOT NULL UNIQUE,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tenant_archivos_tenant
    ON tenant_archivos (tenant_id, created_at);

-- 3) Adjuntos salientes del agente: el contenido vive en S3, no en la fila.
--    `contenido` pasa a ser opcional y exactamente uno de los dos debe existir.
ALTER TABLE message_attachments ALTER COLUMN contenido DROP NOT NULL;
ALTER TABLE message_attachments ADD COLUMN IF NOT EXISTS storage_key VARCHAR(255);
ALTER TABLE message_attachments ADD COLUMN IF NOT EXISTS expira_at TIMESTAMPTZ;
ALTER TABLE message_attachments DROP CONSTRAINT IF EXISTS message_attachments_origen_contenido_check;
ALTER TABLE message_attachments ADD CONSTRAINT message_attachments_origen_contenido_check
    CHECK ((contenido IS NOT NULL) <> (storage_key IS NOT NULL));

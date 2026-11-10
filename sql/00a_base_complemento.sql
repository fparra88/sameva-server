-- 00a · Complemento de las tablas base (00_base_local.sql)
--
-- 00_base_local.sql crea una versión mínima de tenants/users/conversations/
-- messages/tenant_channels. La base real (dump en estructura.sql, nacida del
-- lado de n8n) tiene más columnas e índices que el backend y el agente en
-- código ya usan (widget web, sesiones web, dedupe de mensajes...). Este
-- archivo los versiona. Va entre 00 y 01: solo toca tablas de 00.
--
-- Todo idempotente. Ejecutar:  python aplicar_sql.py sql/00a_base_complemento.sql

-- tenants ---------------------------------------------------------------
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'active';
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS es_tenant_interno BOOLEAN DEFAULT false;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS razon_interno VARCHAR(255);

-- users: sesión del widget web ------------------------------------------
ALTER TABLE users ADD COLUMN IF NOT EXISTS web_session_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_users_tenant_whatsapp
    ON users (tenant_id, whatsapp_id) WHERE whatsapp_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_tenant_instagram
    ON users (tenant_id, instagram_id) WHERE instagram_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_tenant_facebook
    ON users (tenant_id, facebook_id) WHERE facebook_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_tenant_web
    ON users (tenant_id, web_session_id) WHERE web_session_id IS NOT NULL;

-- conversations ---------------------------------------------------------
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_conversations_lookup
    ON conversations (tenant_id, user_id, channel_type, status);

-- messages --------------------------------------------------------------
ALTER TABLE messages ADD COLUMN IF NOT EXISTS agrupado BOOLEAN DEFAULT false;

-- tenant_channels: widget web ------------------------------------------
-- La base real tiene PK en `id`; aquí se conserva la PK (tenant_id,
-- channel_type) de 00 (el código usa ON CONFLICT con ella) y `id` queda
-- como columna con default.
ALTER TABLE tenant_channels ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE tenant_channels ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();
ALTER TABLE tenant_channels ADD COLUMN IF NOT EXISTS widget_key TEXT;
ALTER TABLE tenant_channels ADD COLUMN IF NOT EXISTS dominio_permitido TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS tenant_channels_widget_key_key
    ON tenant_channels (widget_key);

-- NOT NULL que sí tiene la base real ------------------------------------
-- (fallaría si hubiera filas huérfanas con tenant_id NULL; no debería haberlas).
ALTER TABLE users ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE tenant_channels ALTER COLUMN id SET NOT NULL;

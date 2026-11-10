-- 52a · Tabla meta_apps (apps de Meta de la plataforma)
--
-- sql/53 (re-cifrado de app_secret), sql/71 (FK desde channel_credentials) y
-- resolver_meta_app asumen que `meta_apps` existe, pero ninguna migración la
-- creaba: nació del lado de n8n y solo vivía en la base. El nombre 52a hace
-- que ordene entre 52 y 53, antes del primer uso.
--
-- app_secret_cifrado = pgp_sym_encrypt(secret, current_setting('app.cred_key')).

CREATE TABLE IF NOT EXISTS meta_apps (
    id                 SERIAL PRIMARY KEY,
    nombre             TEXT NOT NULL,
    app_id_meta        TEXT NOT NULL,
    creado_en          TIMESTAMPTZ DEFAULT NOW(),
    app_secret_cifrado BYTEA
);

-- sql/53 hace JOIN channel_credentials.meta_app_id -> meta_apps.id, pero la
-- columna solo la agregaba sql/71. Se crea aquí (idempotente; 71 repite el
-- ADD COLUMN IF NOT EXISTS sin efecto).
ALTER TABLE channel_credentials
    ADD COLUMN IF NOT EXISTS meta_app_id INTEGER REFERENCES meta_apps(id);

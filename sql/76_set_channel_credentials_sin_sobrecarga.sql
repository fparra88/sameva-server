-- ============================================================
-- set_channel_credentials: quitar la versión de 6 argumentos
-- ============================================================
-- sql/71 agregó p_meta_app_id (con DEFAULT) con CREATE OR REPLACE, pero como cambia la
-- firma Postgres lo creó como SOBRECARGA y dejó la de 6 argumentos. Una llamada con 6
-- argumentos (routers/canales.py) coincide con las dos y falla con "function is not
-- unique". La de 7 con DEFAULT ya cubre esas llamadas, así que la vieja sobra.
-- Idempotente: sin la vieja no hace nada.
DROP FUNCTION IF EXISTS set_channel_credentials(UUID, VARCHAR, TEXT, VARCHAR, VARCHAR, VARCHAR);

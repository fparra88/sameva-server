-- 74 · Plan "recomendado" para la landing
--
-- La landing pública (GET /api/planes/publicos) pinta una insignia
-- "Recomendado" en un plan. Antes estaba fija en el código del frontend;
-- ahora la decide gerencia desde /gerencia/planes. A lo más un plan a la
-- vez lo tiene encendido (lo garantiza el router, no la base).
--
-- Idempotente.

ALTER TABLE planes
    ADD COLUMN IF NOT EXISTS destacado BOOLEAN NOT NULL DEFAULT false;

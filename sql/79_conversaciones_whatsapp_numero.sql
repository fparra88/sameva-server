-- ============================================================
-- Conversaciones de WhatsApp partidas en dos por el eco de coexistencia
-- ============================================================
-- Bug: la entrada de mensajes (services/agente/ingreso.py) buscaba/creaba la
-- conversación del cliente SIN `agente_canal_id`, y el eco de la app móvil
-- (services/ecos_coexistencia.py) la buscaba CON él. Resultado: dos hilos
-- vivos del mismo cliente; el eco ponía el suyo en 'transferred' y la IA
-- seguía contestando (y gastando tokens) en el otro.
--
-- El código ya está corregido (la entrada pasa el número y adopta una
-- conversación sin número). Esta migración repara lo que quedó en la base:
--
--   1. Cada par vivo (active/transferred) del mismo negocio y cliente en
--      WhatsApp —uno SIN número y otro CON número— se fusiona en el que no
--      tiene número (es el que lleva el historial y el embudo): recibe los
--      mensajes y las filas que apunten al otro, el número, y el estado
--      'transferred' si el otro lo tenía (una persona contestó desde el
--      móvil; el portal ofrece «Devolver a la IA»). Luego se borra el vacío.
--   2. Las conversaciones de WhatsApp que siguen sin número, en negocios con
--      UN solo número vinculado, reciben ese número. Con varios números se
--      dejan: las adopta la entrada con el siguiente mensaje del cliente.
--
-- Idempotente: reaplicarla no encuentra pares ni conversaciones que arreglar.
-- ============================================================

DO $$
DECLARE
    v_par  RECORD;
    v_fk   RECORD;
BEGIN
    FOR v_par IN
        SELECT o.id               AS original,
               d.id               AS duplicada,
               d.agente_canal_id  AS agente_canal_id,
               d.status           AS estado_dup,
               d.last_message_at  AS ultimo_dup,
               d.asignado_a, d.asignado_en, d.asignado_origen, d.asignacion_nota
          FROM conversations o
          JOIN LATERAL (
                SELECT *
                  FROM conversations d
                 WHERE d.tenant_id = o.tenant_id
                   AND d.user_id = o.user_id
                   AND d.channel_type = 'whatsapp'
                   AND d.agente_canal_id IS NOT NULL
                   AND d.status IN ('active', 'transferred')
                 ORDER BY d.last_message_at DESC
                 LIMIT 1
          ) d ON true
         WHERE o.channel_type = 'whatsapp'
           AND o.agente_canal_id IS NULL
           AND o.status IN ('active', 'transferred')
    LOOP
        -- Un wamid que ya esté en la original no se mueve (índice único).
        DELETE FROM messages m
         WHERE m.conversation_id = v_par.duplicada
           AND m.channel_msg_id IS NOT NULL
           AND EXISTS (SELECT 1 FROM messages x
                        WHERE x.conversation_id = v_par.original
                          AND x.channel_msg_id = m.channel_msg_id);
        UPDATE messages SET conversation_id = v_par.original
         WHERE conversation_id = v_par.duplicada;

        -- Cualquier otra tabla con FK a conversations (asignaciones, consumo,
        -- cortes de contexto, ...): se mueve a la original.
        FOR v_fk IN
            SELECT c.conrelid::regclass AS tabla, a.attname AS columna
              FROM pg_constraint c
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
             WHERE c.contype = 'f'
               AND c.confrelid = 'conversations'::regclass
               AND c.conrelid <> 'messages'::regclass
        LOOP
            EXECUTE format('UPDATE %s SET %I = $1 WHERE %I = $2', v_fk.tabla, v_fk.columna, v_fk.columna)
              USING v_par.original, v_par.duplicada;
        END LOOP;

        UPDATE conversations o
           SET agente_canal_id = v_par.agente_canal_id,
               last_message_at = GREATEST(o.last_message_at, v_par.ultimo_dup),
               metadata = CASE WHEN v_par.estado_dup = 'transferred' AND o.status = 'active'
                               THEN jsonb_set(COALESCE(o.metadata, '{}'::jsonb),
                                              '{ack_pendiente}', 'false'::jsonb)
                               ELSE o.metadata END,
               status = CASE WHEN v_par.estado_dup = 'transferred' THEN 'transferred' ELSE o.status END,
               asignado_a      = COALESCE(o.asignado_a, v_par.asignado_a),
               asignado_en     = CASE WHEN o.asignado_a IS NULL THEN v_par.asignado_en ELSE o.asignado_en END,
               asignado_origen = CASE WHEN o.asignado_a IS NULL THEN v_par.asignado_origen ELSE o.asignado_origen END,
               asignacion_nota = CASE WHEN o.asignado_a IS NULL THEN v_par.asignacion_nota ELSE o.asignacion_nota END
         WHERE o.id = v_par.original;

        DELETE FROM conversations WHERE id = v_par.duplicada;
    END LOOP;
END $$;

-- 2) Negocios con un solo número: sus conversaciones de WhatsApp sin número son de ese.
UPDATE conversations c
   SET agente_canal_id = u.id
  FROM (
        SELECT tenant_id, (array_agg(id))[1] AS id
          FROM agentes_canal
         WHERE estado IN ('activo', 'pausado')
         GROUP BY tenant_id
        HAVING COUNT(*) = 1
  ) u
 WHERE c.tenant_id = u.tenant_id
   AND c.channel_type = 'whatsapp'
   AND c.agente_canal_id IS NULL;

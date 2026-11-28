"""
Cuentas de WhatsApp Business (WABA) de un negocio, para administrar sus plantillas
cuando tiene varios números (sql/81).

En Meta una plantilla es de la WABA, no del número: los números de la misma WABA
comparten catálogo. Por eso la pantalla agrupa por cuenta y cada número sabe en
cuál está (`agentes_canal.waba_id`).

    Meta directo  La WABA de cada número se resuelve con Graph la primera vez
                  (`meta.obtener_waba_id`) y se guarda. Cada cuenta se administra
                  contra su propia WABA. En la caché, `waba_id` = esa WABA.
    NeuroAPI      Su API de plantillas solo conoce la x-api-key del negocio (una
                  por negocio) y no documenta cómo elegir WABA: es UNA cuenta, como
                  siempre, y en la caché `waba_id` queda NULL. Si los números que
                  conocemos están en WABAs distintas, crear/borrar/probar se
                  bloquea (`varias_cuentas_neuroapi`): no sabríamos en cuál cae.
                  La WABA de cada número la manda el webhook de conexión.

Nunca se escribe el token en el log.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status

from config import settings
from services import meta
from session import execute, fetch_all, fetch_one

log = logging.getLogger("operativai.plantillas_cuentas")

CLAVE_UNICA = "unica"
BLOQUEO_NEUROAPI = "varias_cuentas_neuroapi"
MENSAJE_BLOQUEO = (
    "Tus números de WhatsApp están en cuentas de WhatsApp Business distintas y la conexión "
    "por NeuroAPI no permite elegir en cuál crear o borrar plantillas. Escríbenos para resolverlo."
)


@dataclass(frozen=True)
class Numero:
    id: UUID | None
    phone_number_id: str
    telefono_visible: str | None
    es_principal: bool
    waba_id: str | None


@dataclass
class Cuenta:
    # Lo que viaja en `?cuenta=`: la WABA (Meta directo) o "unica".
    clave: str
    # Lo que se guarda en la caché (`plantillas_whatsapp_tenant.waba_id`). None en NeuroAPI.
    waba_cache: str | None
    numeros: list[Numero] = field(default_factory=list)


@dataclass
class Cuentas:
    proveedor: str | None
    cuentas: list[Cuenta]
    bloqueo: str | None = None
    # La de los avisos de cita (la del número principal): `en_uso_por` solo aplica ahí.
    clave_avisos: str | None = None
    token: str = field(default="", repr=False)

    def elegir(self, clave: str | None) -> Cuenta:
        """La cuenta pedida. Sin clave vale solo si hay una. 404 si no es del negocio."""
        if clave is None:
            if len(self.cuentas) == 1:
                return self.cuentas[0]
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"codigo": "cuenta_requerida", "mensaje": "Elige de qué cuenta de WhatsApp es la plantilla"},
            )
        for c in self.cuentas:
            if c.clave == clave:
                return c
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cuenta de WhatsApp no encontrada")

    def exigir_escritura(self) -> None:
        if self.bloqueo:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"codigo": self.bloqueo, "mensaje": MENSAJE_BLOQUEO},
            )


def agrupar(proveedor: str, numeros: list[Numero]) -> tuple[list[Cuenta], str | None, str | None]:
    """
    Pura. (cuentas, bloqueo, clave de la cuenta de avisos). En Meta directo agrupa por
    WABA (un número sin WABA resuelta queda fuera); en NeuroAPI siempre es una cuenta.
    """
    if proveedor == "meta":
        por_waba: dict[str, Cuenta] = {}
        for n in numeros:
            if n.waba_id:
                por_waba.setdefault(n.waba_id, Cuenta(clave=n.waba_id, waba_cache=n.waba_id)).numeros.append(n)
        cuentas = list(por_waba.values())
        bloqueo = None
    else:
        cuentas = [Cuenta(clave=CLAVE_UNICA, waba_cache=None, numeros=list(numeros))]
        conocidas = {n.waba_id for n in numeros if n.waba_id}
        bloqueo = BLOQUEO_NEUROAPI if len(conocidas) > 1 else None
    principal = next((c.clave for c in cuentas for n in c.numeros if n.es_principal), None)
    clave_avisos = principal or (cuentas[0].clave if len(cuentas) == 1 else None)
    return cuentas, bloqueo, clave_avisos


async def guardar_waba(tenant_id: UUID, phone_number_id: str, waba_id: str | None) -> None:
    """La WABA de un número recién conectado (webhook de NeuroAPI, Embedded Signup de Meta)."""
    if not waba_id:
        return
    await execute(
        """
        UPDATE agentes_canal SET waba_id = $3, updated_at = NOW()
        WHERE tenant_id = $1 AND phone_number_id = $2 AND estado <> 'desvinculado'
        """,
        tenant_id,
        phone_number_id,
        str(waba_id)[:64],
    )


async def _resolver_waba_meta(tenant_id: UUID, numero: Numero, token: str) -> str | None:
    try:
        waba = await meta.obtener_waba_id(numero.phone_number_id, token)
    except meta.MetaError as e:
        log.warning("No se pudo resolver la WABA de un número del tenant %s: %s", tenant_id, e.mensaje)
        return None
    if numero.id is not None:
        await guardar_waba(tenant_id, numero.phone_number_id, waba)
    return waba


async def resolver(tenant_id: UUID) -> Cuentas:
    """
    Las cuentas del negocio con sus números (activos y pausados). Sin canal de WhatsApp
    activo devuelve `proveedor=None` y ninguna cuenta. Un negocio de antes de los
    números (sin filas en agentes_canal) es una cuenta con la línea de channel_credentials.
    """
    cred = await fetch_one("SELECT * FROM get_channel_credentials($1, $2)", tenant_id, "whatsapp")
    if cred is None:
        return Cuentas(proveedor=None, cuentas=[])
    proveedor = "meta" if cred["bsp_provider"] == "meta" else "neuroapi"
    token = cred["access_token"] or ("" if proveedor == "meta" else settings.NEUROAPI_API_KEY)

    filas = await fetch_all(
        """
        SELECT id, phone_number_id, telefono_visible, es_principal, waba_id
        FROM agentes_canal
        WHERE tenant_id = $1 AND estado <> 'desvinculado'
        ORDER BY es_principal DESC, vinculado_en
        """,
        tenant_id,
    )
    numeros = [Numero(**dict(f)) for f in filas]
    if not numeros and cred["phone_number_id"]:
        numeros = [Numero(id=None, phone_number_id=cred["phone_number_id"], telefono_visible=None, es_principal=True, waba_id=None)]

    if proveedor == "meta" and token:
        resueltos = []
        for n in numeros:
            waba = n.waba_id or await _resolver_waba_meta(tenant_id, n, token)
            resueltos.append(Numero(n.id, n.phone_number_id, n.telefono_visible, n.es_principal, waba))
        numeros = resueltos

    cuentas, bloqueo, clave_avisos = agrupar(proveedor, numeros)
    return Cuentas(proveedor=proveedor, cuentas=cuentas, bloqueo=bloqueo, clave_avisos=clave_avisos, token=token or "")


async def waba_cache_de_numero(tenant_id: UUID, agente_canal_id: UUID | None) -> Any:
    """
    La cuenta (clave de caché) del número de una conversación, para que la Bandeja
    ofrezca solo las plantillas de esa WABA. NeuroAPI = None (cuenta única). Si no se
    puede saber (sin número, WABA sin resolver), `TODAS`: se ofrecen todas, como antes.
    """
    from services.plantillas_tenant import TODAS

    cuentas = await resolver(tenant_id)
    if cuentas.proveedor != "meta":
        return None
    for c in cuentas.cuentas:
        if any(n.id == agente_canal_id for n in c.numeros):
            return c.waba_cache
    return cuentas.cuentas[0].waba_cache if len(cuentas.cuentas) == 1 else TODAS


def numero_de_envio(cuentas: Cuentas, cuenta: Cuenta) -> Numero:
    """El número que manda las pruebas de esa cuenta: el principal si está en ella."""
    return next((n for n in cuenta.numeros if n.es_principal), cuenta.numeros[0])

"""
Plantillas de WhatsApp de cada negocio, administradas contra la API de
plantillas de NeuroAPI (Meta Graph) o, si el negocio se conectó por el
Embedded Signup de Meta, contra Graph directo (según
`channel_credentials.bsp_provider`). Distinto de `/gerencia/plantillas-whatsapp`
(las dos plantillas globales de los avisos de cita, solo plataforma).

**Por cuenta (sql/81).** En Meta una plantilla es de la cuenta de WhatsApp
Business (WABA), no del número. `GET /mis-cuentas` dice qué cuentas tiene el
negocio y qué números van en cada una (services/plantillas_cuentas.py); el
resto de rutas acepta `?cuenta=<clave>`. Con una sola cuenta se puede omitir;
con varias, escribir sin ella es 422 `cuenta_requerida`, y listar o
sincronizar sin ella abarca todas. En NeuroAPI siempre hay una cuenta; si sus
números están en WABAs distintas, crear/borrar/probar es 409
`varias_cuentas_neuroapi`.

Leer (listar, detalle, cuentas) es de `negocio_actual`: owner, superadmin y
member. Crear, eliminar y enviar pruebas es de `gerencia_actual`. `en_uso_por`
dice qué aviso de cita (spec 007) usa cada plantilla —solo en la cuenta del
número principal, que es la que usan los avisos—; borrar una que usa un aviso
encendido responde 409 `plantilla_en_uso` salvo con `forzar=true`. vendedor y
proveedor reciben 403. `/api/plantillas-whatsapp` está en `RUTAS_SENSIBLES`:
desde "ver como" se puede mirar, pero no crear ni borrar, ni con permiso de edición.

La tabla local es una caché del catálogo de Meta: `GET ?sincronizar=true` la
refresca y el webhook (`message_template_status_update`) mueve el estado sin
polling. Una plantilla de otro negocio responde 404.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from deps import UsuarioActual, gerencia_actual, negocio_actual
from schemas import (
    CategoriaPlantilla,
    CuentaPlantillasOut,
    CuentasPlantillasOut,
    EstadoPlantillaMeta,
    NumeroDeCuentaOut,
    PlantillaCrearIn,
    PlantillaPruebaIn,
    PlantillaTenantOut,
)
from services import plantillas_cuentas as cuentas_svc
from services import plantillas_reglas as reglas
from services import plantillas_tenant as cache
from services.meta_plantillas import MetaTemplateService, _EnvioPlantillaMeta
from services.neuroapi_plantillas import NeuroApiTemplateService, PlantillaApiError, a_http
from services.whatsapp import ProveedorWhatsAppError
from services.whatsapp import a_http as a_http_envio

router = APIRouter(prefix="/plantillas-whatsapp", tags=["plantillas-whatsapp"])

LIMITE_SINCRONIZACION = 100

ParametroCuenta = Query(default=None, max_length=64, description="Clave de la cuenta (GET /mis-cuentas)")


def _no_encontrada() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plantilla no encontrada")


def _invalida(e: reglas.PlantillaInvalida) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))


def _tenant(usuario: UsuarioActual):
    if usuario.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El usuario no tiene un negocio asociado todavía",
        )
    return usuario.tenant_id


async def _cuentas_con_canal(tenant_id: UUID) -> cuentas_svc.Cuentas:
    """Las cuentas del negocio; sin WhatsApp conectado, el mismo 502 de siempre."""
    cuentas = await cuentas_svc.resolver(tenant_id)
    if cuentas.proveedor is None:
        raise a_http(PlantillaApiError("El negocio no tiene un canal de WhatsApp activo"))
    if not cuentas.cuentas:
        raise a_http(PlantillaApiError("No se pudo saber a qué cuenta de WhatsApp Business pertenecen tus números"))
    return cuentas


def _clave_de_fila(fila: dict) -> str:
    return fila.get("waba_id") or cuentas_svc.CLAVE_UNICA


def _aplica_avisos(cuentas: cuentas_svc.Cuentas, waba_cache: str | None) -> bool:
    """Los avisos de cita salen por el número principal: sus plantillas son las de esa cuenta."""
    if cuentas.clave_avisos is None or waba_cache is None:
        return True
    return waba_cache == cuentas.clave_avisos


def _salida(fila: dict, usos: dict[str, list[dict]], cuentas: cuentas_svc.Cuentas) -> PlantillaTenantOut:
    en_uso = usos.get(fila["nombre"], []) if _aplica_avisos(cuentas, fila.get("waba_id")) else []
    return PlantillaTenantOut(**cache.a_salida(fila), en_uso_por=en_uso, cuenta=_clave_de_fila(fila))


async def _servicio(
    tenant_id: UUID, cuentas: cuentas_svc.Cuentas, cuenta: cuentas_svc.Cuenta
) -> NeuroApiTemplateService | MetaTemplateService:
    """
    La API de plantillas de esa cuenta: Graph contra su WABA (Meta directo) o
    NeuroAPI con la x-api-key del negocio (que solo conoce una cuenta).
    """
    if cuentas.proveedor == "meta":
        if not cuentas.token:
            raise PlantillaApiError("La línea de WhatsApp del negocio está incompleta")
        return MetaTemplateService(token=cuentas.token, waba_id=cuenta.clave)
    return await NeuroApiTemplateService.desde_tenant(tenant_id)


async def _proveedor_envio(tenant_id: UUID, cuentas: cuentas_svc.Cuentas, cuenta: cuentas_svc.Cuenta):
    """Las pruebas salen por un número de esa cuenta (en otra WABA la plantilla no existe)."""
    if cuentas.proveedor == "meta":
        numero = cuentas_svc.numero_de_envio(cuentas, cuenta)
        return _EnvioPlantillaMeta(phone_number_id=numero.phone_number_id, token=cuentas.token)
    return await NeuroApiTemplateService.proveedor_envio(tenant_id)


# Antes de "/{nombre}": "mis-cuentas" lleva guion, que un nombre de plantilla no admite.
@router.get("/mis-cuentas", response_model=CuentasPlantillasOut)
async def mis_cuentas(usuario: UsuarioActual = Depends(negocio_actual)):
    cuentas = await cuentas_svc.resolver(_tenant(usuario))
    return CuentasPlantillasOut(
        proveedor=cuentas.proveedor,
        bloqueo=cuentas.bloqueo,
        mensaje_bloqueo=cuentas_svc.MENSAJE_BLOQUEO if cuentas.bloqueo else None,
        cuentas=[
            CuentaPlantillasOut(
                clave=c.clave,
                de_avisos=c.clave == cuentas.clave_avisos,
                numeros=[
                    NumeroDeCuentaOut(id=n.id, telefono_visible=n.telefono_visible, es_principal=n.es_principal)
                    for n in c.numeros
                ],
            )
            for c in cuentas.cuentas
        ],
    )


@router.get("", response_model=list[PlantillaTenantOut])
async def listar_plantillas(
    categoria: CategoriaPlantilla | None = None,
    estado: EstadoPlantillaMeta | None = None,
    sincronizar: bool = False,
    cuenta: str | None = ParametroCuenta,
    usuario: UsuarioActual = Depends(negocio_actual),
):
    tenant_id = _tenant(usuario)
    if sincronizar:
        cuentas = await _cuentas_con_canal(tenant_id)
        elegidas = [cuentas.elegir(cuenta)] if cuenta else cuentas.cuentas
        for c in elegidas:
            try:
                servicio = await _servicio(tenant_id, cuentas, c)
                remotas = await servicio.listar(limite=LIMITE_SINCRONIZACION)
            except PlantillaApiError as e:
                raise a_http(e)
            await cache.sincronizar(
                tenant_id,
                remotas,
                lista_completa=len(remotas) < LIMITE_SINCRONIZACION,
                waba_id=c.waba_cache,
                cuenta_unica=len(cuentas.cuentas) == 1,
            )
    else:
        cuentas = await cuentas_svc.resolver(tenant_id)

    if cuenta and cuentas.cuentas:
        filas = await cache.listar(tenant_id, categoria=categoria, estado=estado, waba_id=cuentas.elegir(cuenta).waba_cache)
    else:
        filas = await cache.listar(tenant_id, categoria=categoria, estado=estado)
    usos = await cache.usos_en_avisos(tenant_id)
    return [_salida(f, usos, cuentas) for f in filas]


@router.get("/{nombre}", response_model=PlantillaTenantOut)
async def detalle_plantilla(
    nombre: str,
    cuenta: str | None = ParametroCuenta,
    usuario: UsuarioActual = Depends(negocio_actual),
):
    tenant_id = _tenant(usuario)
    cuentas = await cuentas_svc.resolver(tenant_id)
    if cuenta and cuentas.cuentas:
        fila = await cache.obtener(tenant_id, nombre, waba_id=cuentas.elegir(cuenta).waba_cache)
    else:
        fila = await cache.obtener(tenant_id, nombre)
    if fila is None:
        raise _no_encontrada()
    return _salida(fila, await cache.usos_en_avisos(tenant_id), cuentas)


@router.post("", response_model=PlantillaTenantOut, status_code=status.HTTP_201_CREATED)
async def crear_plantilla(
    datos: PlantillaCrearIn,
    cuenta: str | None = ParametroCuenta,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    tenant_id = _tenant(usuario)
    try:
        nombre = reglas.validar_nombre(datos.nombre)
        idioma = reglas.validar_idioma(datos.idioma)
        componentes = reglas.construir_componentes(
            cuerpo=datos.cuerpo,
            ejemplos_cuerpo=datos.ejemplos_cuerpo,
            encabezado=datos.encabezado,
            ejemplo_encabezado=datos.ejemplo_encabezado,
            pie=datos.pie,
            botones=[b.model_dump() for b in datos.botones],
        )
    except reglas.PlantillaInvalida as e:
        raise _invalida(e)

    cuentas = await _cuentas_con_canal(tenant_id)
    cuentas.exigir_escritura()
    c = cuentas.elegir(cuenta)

    existente = await cache.obtener(tenant_id, nombre, waba_id=c.waba_cache)
    if existente is not None and existente["idioma"] == idioma:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existe una plantilla con ese nombre e idioma",
        )

    try:
        servicio = await _servicio(tenant_id, cuentas, c)
        respuesta = await servicio.crear(
            nombre=nombre, idioma=idioma, categoria=datos.categoria, componentes=componentes
        )
    except PlantillaApiError as e:
        raise a_http(e)

    guardada = cache.normalizar_meta({"name": nombre, "language": idioma, **respuesta}) or {}
    await cache.guardar(
        tenant_id,
        {
            **guardada,
            "nombre": nombre,
            "idioma": idioma,
            "categoria": respuesta.get("category") or datos.categoria,
            # Meta contesta PENDING al crear; lo demás lo empuja el webhook.
            "estado": guardada.get("estado", "PENDING"),
            "componentes": componentes,
            "waba_id": c.waba_cache,
        },
        creada_por=usuario.id,
        sincronizada=False,
    )
    fila = await cache.obtener(tenant_id, nombre, waba_id=c.waba_cache)
    return _salida(fila, await cache.usos_en_avisos(tenant_id), cuentas)


@router.delete("/{nombre}", status_code=status.HTTP_204_NO_CONTENT)
async def eliminar_plantilla(
    nombre: str,
    confirmar: bool = Query(default=False, description="Debe ser true: el borrado es permanente"),
    forzar: bool = Query(
        default=False,
        description="true para borrarla aunque la use un aviso de cita encendido (el aviso se apaga)",
    ),
    cuenta: str | None = ParametroCuenta,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    tenant_id = _tenant(usuario)
    if not confirmar:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "codigo": "confirmacion_requerida",
                "mensaje": "Eliminar una plantilla es permanente; repite con confirmar=true",
            },
        )
    cuentas = await _cuentas_con_canal(tenant_id)
    cuentas.exigir_escritura()
    c = cuentas.elegir(cuenta)
    if await cache.obtener(tenant_id, nombre, waba_id=c.waba_cache) is None:
        raise _no_encontrada()
    # Borrarla apaga el aviso de cita que la usa (reaccion_estado). Con uno
    # encendido se pide una segunda confirmación explícita: la UI la muestra
    # con el nombre del aviso, y un cliente de la API no lo apaga sin querer.
    usos = (await cache.usos_en_avisos(tenant_id)).get(nombre, []) if _aplica_avisos(cuentas, c.waba_cache) else []
    activos = [u for u in usos if u["activo"]]
    if activos and not forzar:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "codigo": "plantilla_en_uso",
                "mensaje": "La usa un aviso de cita encendido: si la eliminas, el aviso se apaga",
                "en_uso_por": activos,
            },
        )
    try:
        servicio = await _servicio(tenant_id, cuentas, c)
        await servicio.eliminar(nombre)
    except PlantillaApiError as e:
        # Si Meta ya no la tiene, solo falta limpiar la caché.
        if e.status_code != 404:
            raise a_http(e)
    # Spec 007 RF-6: si era la plantilla vigente de un aviso de cita activo, la función se apaga y se alerta.
    await cache.quitar(tenant_id, nombre, waba_id=c.waba_cache)


@router.post("/{nombre}/prueba", status_code=status.HTTP_202_ACCEPTED)
async def enviar_prueba(
    nombre: str,
    datos: PlantillaPruebaIn,
    cuenta: str | None = ParametroCuenta,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    tenant_id = _tenant(usuario)
    cuentas = await _cuentas_con_canal(tenant_id)
    cuentas.exigir_escritura()
    c = cuentas.elegir(cuenta)
    fila = await cache.obtener(tenant_id, nombre, waba_id=c.waba_cache)
    if fila is None:
        raise _no_encontrada()
    if fila["estado"] != "APPROVED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Solo se pueden enviar plantillas aprobadas por Meta",
        )
    try:
        componentes = reglas.parametros_de_envio(
            fila["componentes"], datos.valores_cuerpo, datos.valor_encabezado
        )
    except reglas.PlantillaInvalida as e:
        raise _invalida(e)
    try:
        proveedor = await _proveedor_envio(tenant_id, cuentas, c)
        resultado = await proveedor.enviar_plantilla_con_componentes(
            datos.telefono, nombre, fila["idioma"], componentes
        )
    except ProveedorWhatsAppError as e:
        raise a_http_envio(e)
    return {"id_mensaje": resultado.id_mensaje, "estado": resultado.estado}

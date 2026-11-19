"""
Descarga pública de adjuntos salientes.

NeuroAPI/Meta no tienen sesión de portal: bajan el archivo de la URL que se
les manda en `link`. Por eso este es uno de los pocos endpoints sin JWT; lo
único que autoriza la descarga es el token aleatorio de 256 bits de
`message_attachments.token_publico`. Un token desconocido o vencido da 404,
sin distinguir "no existe" de "no es tuyo".

Los adjuntos que manda el agente desde el catálogo de archivos no guardan los
bytes en la fila (`storage_key` apunta a S3) y su enlace vence en minutos
(`expira_at`): el cliente recibe el archivo nativo por WhatsApp, y la URL solo
la usa el proveedor para bajarlo.
"""

from fastapi import APIRouter, HTTPException, Response, status

from services import adjuntos, almacen_archivos
from session import fetch_one

router = APIRouter(prefix="/media", tags=["media"])


@router.get("/{token}")
async def descargar(token: str) -> Response:
    # Tope de largo antes de ir a la BD: los tokens reales miden 43.
    fila = (
        await fetch_one(
            """
            SELECT mime, nombre, contenido, storage_key
            FROM message_attachments
            WHERE token_publico = $1 AND direccion = 'out'
              AND (expira_at IS NULL OR expira_at > NOW())
            """,
            token,
        )
        if 20 <= len(token) <= 64
        else None
    )
    if fila is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No encontrado")

    if fila["contenido"] is not None:
        contenido = bytes(fila["contenido"])
    else:
        try:
            contenido = await almacen_archivos.leer(fila["storage_key"])
        except almacen_archivos.ErrorAlmacen:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="No disponible")

    return Response(
        content=contenido,
        media_type=fila["mime"],
        headers=adjuntos.cabeceras_descarga(fila["nombre"], privado=False),
    )

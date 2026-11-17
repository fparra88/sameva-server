"""
Límite de peticiones por IP para endpoints anónimos baratos.

Ventana deslizante en memoria, por proceso. Es una barrera contra scraping o
abuso casual, no un control de seguridad fuerte: con varios workers cada uno
cuenta aparte (el tope real es N veces el configurado) y se reinicia con el
proceso. Para algo más estricto haría falta Redis o la base.

La IP sale del primer valor de X-Forwarded-For si existe (la API corre detrás
de un proxy; sin él todo el mundo compartiría la IP del proxy) y si no, del
socket. Es falseable por un cliente que llegue directo, y por eso este límite
no protege nada sensible.
"""

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

# Tope de IPs recordadas: si se llena, se purgan las inactivas (evita crecer sin límite).
_MAX_CLAVES = 10_000

_golpes: dict[str, deque[float]] = defaultdict(deque)


def _ip(request: Request) -> str:
    reenviada = request.headers.get("x-forwarded-for")
    if reenviada:
        return reenviada.split(",")[0].strip()
    return request.client.host if request.client else "desconocida"


def reiniciar() -> None:
    """Vacía los contadores (para tests)."""
    _golpes.clear()


def limitar_por_ip(request: Request, maximo: int, ventana_s: int, ambito: str) -> None:
    """429 con Retry-After si esta IP ya hizo `maximo` peticiones en `ventana_s` segundos."""
    ahora = time.monotonic()
    if len(_golpes) > _MAX_CLAVES:
        for k in [k for k, d in _golpes.items() if not d or ahora - d[-1] > ventana_s]:
            del _golpes[k]

    clave = f"{ambito}:{_ip(request)}"
    cola = _golpes[clave]
    while cola and ahora - cola[0] > ventana_s:
        cola.popleft()

    if len(cola) >= maximo:
        espera = max(1, int(ventana_s - (ahora - cola[0])) + 1)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Demasiadas consultas. Intenta de nuevo en un momento.",
            headers={"Retry-After": str(espera)},
        )
    cola.append(ahora)


def limitar_por_clave(clave: str, maximo: int, ventana_s: int, ambito: str, mensaje: str | None = None) -> None:
    """Igual que `limitar_por_ip` pero por una clave propia (p. ej. el negocio). Misma ventana en memoria."""
    ahora = time.monotonic()
    cola = _golpes[f"{ambito}:{clave}"]
    while cola and ahora - cola[0] > ventana_s:
        cola.popleft()
    if len(cola) >= maximo:
        espera = max(1, int(ventana_s - (ahora - cola[0])) + 1)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=mensaje or "Demasiadas solicitudes. Intenta de nuevo en un momento.",
            headers={"Retry-After": str(espera)},
        )
    cola.append(ahora)

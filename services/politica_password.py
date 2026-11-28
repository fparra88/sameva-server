"""
Reglas para contraseñas NUEVAS (registro, aceptar invitación, restablecer).

No se aplica al iniciar sesión: las contraseñas registradas antes de esta
regla siguen sirviendo tal cual. El portal repite la misma regla en
`frontend/src/lib/password.ts` para avisar mientras se escribe; la autoridad
es esta.
"""

MINIMO = 8
MAXIMO = 128
# Al menos uno de estos es obligatorio; otros símbolos se permiten pero no cuentan.
SIMBOLOS = "@#$%.:"


def problemas_password(password: str) -> list[str]:
    """Lo que le falta a la contraseña, en español. Vacío = cumple."""
    faltan: list[str] = []
    if len(password) < MINIMO:
        faltan.append(f"al menos {MINIMO} caracteres")
    if not any(c.isalpha() for c in password):
        faltan.append("al menos una letra")
    # isascii: "²" o "٣" son isdigit() pero no son el número que pide la regla.
    if not any(c.isascii() and c.isdigit() for c in password):
        faltan.append("al menos un número")
    if not any(c in SIMBOLOS for c in password):
        faltan.append(f"al menos un símbolo ({' '.join(SIMBOLOS)})")
    return faltan


def validar_password_nueva(password: str) -> str:
    """Validador de Pydantic: ValueError con todo lo que falta, en un solo mensaje."""
    faltan = problemas_password(password)
    if faltan:
        if len(faltan) == 1:
            detalle = faltan[0]
        else:
            detalle = ", ".join(faltan[:-1]) + " y " + faltan[-1]
        raise ValueError(f"La contraseña necesita {detalle}")
    return password

#!/usr/bin/env python3
"""Configuracion del scraper.

Un unico objeto con todos los parametros ajustables, para que la CLI y un
posible servicio HTTP usen exactamente el mismo comportamiento.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class ScraperConfig:
    """Parametros del scraping.

    Los valores por defecto funcionan sin sesion (modo anonimo). Todo lo
    especifico de una sesion (sessionid) viaja como argumento, nunca dentro
    de la configuracion: asi una API puede recibirlo por request sin que
    quede en un objeto compartido.
    """

    # Capa HTTP
    timeout: int = 30
    retries: int = 2
    backoff: int = 15
    user_agent: str = DEFAULT_USER_AGENT

    # Capa GraphQL (cliente con impersonacion de navegador)
    impersonate: str = "chrome131"
    count: int = 12

    # Consultar el alcance (visualizaciones) de los reels: son peticiones
    # extra, una por reel, asi que va apagado por defecto.
    with_views: bool = False

    # Pausa entre peticiones al mismo host (evita el rate limit por rafaga).
    delay: float = 0.0

    @classmethod
    def from_env(cls) -> "ScraperConfig":
        """Construye la configuracion leyendo los overrides del entorno."""
        return cls(
            timeout=_env_int("IG_TIMEOUT", cls.timeout),
            retries=_env_int("IG_RETRIES", cls.retries),
            backoff=_env_int("IG_BACKOFF", cls.backoff),
            user_agent=os.environ.get("IG_USER_AGENT", cls.user_agent),
            impersonate=os.environ.get("IG_IMPERSONATE", cls.impersonate),
            count=_env_int("IG_COUNT", cls.count),
            with_views=_env_bool("IG_WITH_VIEWS", cls.with_views),
            delay=float(os.environ.get("IG_DELAY", cls.delay) or 0.0),
        )

    def with_overrides(self, **kwargs) -> "ScraperConfig":
        """Devuelve una copia con algunos campos cambiados."""
        clean = {k: v for k, v in kwargs.items() if v is not None}
        return replace(self, **clean) if clean else self

#!/usr/bin/env python3
"""Nucleo del scraper: punto de entrada unico, reusable.

La CLI y un eventual servicio HTTP usan exactamente esta misma clase:

    from lib.scraper import Scraper, Mode

    with Scraper() as scraper:
        data = scraper.scrape("instagram")

En un servicio FastAPI lo natural es crear el ``Scraper`` en el ``lifespan``
y cerrarlo al apagar, para no pagar el arranque del navegador en cada
request.

Modos
-----
``anonymous``  solo HTTP, sin sesion. Es el modo por defecto.
``session``   HTTP con un ``sessionid``: valida el token y saca los datos de
               la cuenta propia.
``session``   API GraphQL de Instagram con ``sessionid``. Da datos en vivo y
               alcanza cuentas privadas que el usuario siga.
``auto``      con sesion usa el navegador si esta disponible; sin sesion, HTTP.

La sesion nunca se lee del entorno dentro de la libreria: llega como
argumento. Asi el servicio puede recibirla por request sin que quede en un
objeto compartido entre peticiones.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Dict, Optional

try:
    from lib import history, ig_client, ig_graphql, ig_stories
    from lib.config import ScraperConfig
except ImportError:  # ejecucion directa del modulo
    import history
    import ig_client
    import ig_graphql
    import ig_stories
    from config import ScraperConfig

logger = logging.getLogger(__name__)


class Mode(str, Enum):
    """Formas de obtener el perfil."""

    AUTO = "auto"
    ANONYMOUS = "anonymous"
    SESSION = "session"


class Scraper:
    """Scraper de perfiles de Instagram.

    Mantiene una sesion HTTP y un cliente GraphQL abiertos entre llamadas,
    de modo que reutilizarlos es tan barato como crearlos. No hay procesos
    externos: todo corre en la misma biblioteca.
    """

    def __init__(
        self,
        config: Optional[ScraperConfig] = None,
        count: Optional[int] = None,
        with_views: Optional[bool] = None,
    ):
        self.config = (config or ScraperConfig.from_env()).with_overrides(
            count=count, with_views=with_views
        )
        self._http = ig_client.build_session(self.config.impersonate)
        self._graphql: Optional[ig_graphql.GraphQLClient] = None

    # ------------------------------------------------------------------ ciclo

    def close(self) -> None:
        """Libera los recursos (cliente GraphQL y sesion HTTP)."""
        if self._graphql is not None:
            self._graphql.close()
            self._graphql = None
        try:
            self._http.close()
        except Exception:
            pass

    def __enter__(self) -> "Scraper":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------------ scrape

    def scrape(
        self,
        username: str,
        session_id: Optional[str] = None,
        mode: Mode = Mode.AUTO,
    ) -> Dict[str, Any]:
        """Devuelve los datos de un perfil.

        Siempre devuelve el mismo esquema (ver
        :data:`lib.ig_client.PROFILE_FIELDS`), llenando con ``None`` lo que
        el modo activo no puede dar.

        :param username: usuario a scrapear.
        :param session_id: sessionid de Instagram. Si es ``None`` se trabaja
            en modo anonimo y no se toca ninguna sesion.
        :param mode: como obtener los datos.
        """
        username = username.strip().lstrip("@")
        mode = Mode(mode) if not isinstance(mode, Mode) else mode

        if mode is Mode.ANONYMOUS:
            session_id = None
        elif mode is Mode.SESSION and not session_id:
            raise ig_client.InstagramError(
                "El modo session necesita un sessionid."
            )

        if session_id and mode in (Mode.AUTO, Mode.SESSION):
            try:
                return self._scrape_session(username, session_id)
            except ig_graphql.GraphQLUnavailable as exc:
                if mode is Mode.SESSION:
                    raise
                logger.warning("%s Se continua con el modo HTTP.", exc)

        return self._scrape_anonymous(username)

    # ------------------------------------------------------------------ modos

    def _scrape_anonymous(self, username: str) -> Dict[str, Any]:
        """Perfil por HTTP, sin sesion."""
        html = ig_client.fetch_profile_html(
            username,
            session=self._http,
            timeout=self.config.timeout,
            retries=self.config.retries,
            backoff=self.config.backoff,
            impersonate=self.config.impersonate,
        )
        return ig_client.parse_profile(html)

    def _scrape_session(self, username: str, session_id: str) -> Dict[str, Any]:
        """Perfil por la API GraphQL, reutilizando el cliente si ya existe."""
        if self._graphql is None:
            self._graphql = ig_graphql.GraphQLClient(
                session_id,
                timeout=self.config.timeout,
                impersonate=self.config.impersonate,
                count=self.config.count,
            )
        profile = self._graphql.scrape(
            username, with_views=self.config.with_views
        )
        self._attach_stories(profile, username)
        return profile

    def _attach_stories(self, profile: Dict[str, Any], username: str) -> None:
        """Sumle las historias destacadas y la story activa al perfil.

        Si las historias fallan no se rompe el perfil: solo quedan vacias.
        """
        try:
            highlights = ig_stories.fetch_highlights(
                self._graphql, profile.get("user_id")
            )
        except Exception as exc:
            logger.warning("No se pudieron obtener las historias: %s", exc)
            highlights = []
        profile["stories"] = highlights
        profile["stories_count"] = len(highlights)

        try:
            profile["stories_active"] = ig_stories.fetch_active_story(
                self._graphql, username
            )
        except Exception as exc:
            logger.debug("No se pudo obtener la story activa: %s", exc)
            profile["stories_active"] = {"has_active_story": False, "expiring_at": None}

    # ------------------------------------------------------------------ extras

    def hashtags(self, profile: Dict[str, Any]) -> Dict[str, int]:
        """Hashtags mas usados en las publicaciones cargadas."""
        return ig_client.extract_hashtags(profile.get("posts_detail") or [])

    def own_account(self, session_id: str) -> Dict[str, Any]:
        """Datos de la cuenta propia, no del perfil de un tercero.

        Es informacion sobre la cuenta con la que se scrapea (fbid, categoria,
        si tiene telefono, metodo de contacto). Va aparte del perfil para que
        el contrato del perfil sea siempre el mismo.
        """
        return ig_client.fetch_own_account(session_id, timeout=self.config.timeout)

    def campaign_analysis(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        """Senales de campana publicitaria a partir de los datos del perfil."""
        return ig_stories.analyze_campaign(profile)

    # ---------------------------------------------------------------- historico

    def store(self, root: Optional[str] = None) -> "history.SnapshotStore":
        """Almacen de instantaneas historicas."""
        return history.SnapshotStore(root)

    def record(
        self,
        profile: Dict[str, Any],
        store: Optional["history.SnapshotStore"] = None,
        notes: Optional[str] = None,
        full: bool = False,
    ) -> Dict[str, Any]:
        """Guarda una instantanea del perfil para comparar luego."""
        return (store or self.store()).save(profile, notes=notes, full=full)

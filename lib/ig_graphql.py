#!/usr/bin/env python3
"""Cliente de Instagram por GraphQL, sin navegador.

Por que no ``requests``
-----------------------
Instagram rechaza las peticiones que no parecen un navegador: ``requests``
recibe ``{"__ar":1,"error":1357054}`` aunque la sesion sea valida y los
tokens correctos. El rechazo es por la **huella TLS** (JA3), no por la
sesion. ``curl_cffi`` falsifica el handshake y el mismo cuerpo pasa.

Que se necesita
---------------
1. Un cliente con impersonacion de navegador (TLS + HTTP2).
2. La cookie ``sessionid``.
3. ``fb_dtsg``, ``lsd`` y ``av``, que salen de la portada.
4. El ``doc_id`` de cada consulta, que se **extrae del bundle JS** en vez de
   hardcodearlo, para que sobreviva a los despliegues de Instagram.

Fragilidad conocida
-------------------
Instagram valida el juego exacto de flags ``__relay_internal__pv__`` que
manda el build actual. Si ese juego cambia, la respuesta llega ``200`` pero
con ``data: {}``: un fallo silencioso. :class:`GraphQLClient` lo detecta y
lo reporta con un mensaje claro en vez de devolver un perfil vacio.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

try:
    from lib import ig_client
    from lib.ig_client import BASE_URL, InstagramError, ProfileNotFound
except ImportError:  # ejecucion directa del modulo
    import ig_client
    from ig_client import BASE_URL, InstagramError, ProfileNotFound

logger = logging.getLogger(__name__)

GRAPHQL_URL = "{0}/api/graphql".format(BASE_URL)
IG_API_BASE = "https://i.instagram.com"
IG_APP_ID = "936619743392459"

# Consultas y flags que Instagram exige en el build actual.
PROFILE_QUERY = "PolarisProfilePageContentQuery"
POSTS_QUERY = "PolarisProfilePostsQuery"

PROFILE_FLAGS = {
    "PolarisCannesGuardianExperienceEnabledrelayprovider": True,
    "PolarisCASB976ProfileEnabledrelayprovider": False,
    "PolarisProfileInformTreatmentBannerEnabledrelayprovider": False,
    "PolarisWebSchoolsEnabledrelayprovider": False,
    "PolarisProfileRingsGradientEnabledrelayprovider": True,
    "PolarisDSA16EntrypointEnabledrelayprovider": False,
    "PolarisRepostsConsumptionEnabledrelayprovider": True,
    "PolarisShortDramaEnabledrelayprovider": True,
}

POSTS_FLAGS = {
    "PolarisMultiCaptionCarouselEnabledrelayprovider": True,
    "PolarisShortDramaEnabledrelayprovider": True,
    "PolarisSEOIsCrawlerrelayprovider": False,
    "PolarisProductTagIndicatorEnabledrelayprovider": False,
    "PolarisThreadsStyleCommentsEnabledrelayprovider": False,
    "PolarisReelsRecoDebugOverlayEnabledrelayprovider": False,
}

DOC_ID_TEMPLATE = r'__d\("%s_instagramRelayOperation".*?exports="(\d+)"'
FB_DTSG_RE = re.compile(r'"DTSGInitialData",\[\],\{[^{}]*"token":"([^"]+)"')
LSD_RE = re.compile(r'"LSD",\[\],\{[^{}]*"token":"([^"]+)"')
AV_RE = re.compile(r'"IG_USER_EIMU":"?(\d+)')

# Los doc_id duran hasta que Instagram despliega un build nuevo.
DOC_ID_TTL = 3600.0


class GraphQLUnavailable(InstagramError):
    """No se pudo usar la API GraphQL (falta curl_cffi, sesion, etc.)."""


class BuildChanged(InstagramError):
    """Instagram cambio el build y las consultas devuelven data vacia."""


class _FieldException(Exception):
    """Instagram respondio field_exception (no existe o rechazo transitorio)."""

    def __init__(self, errors: List[Dict[str, Any]]):
        super().__init__("field_exception")
        self.errors = errors


def _flags(prefix: str, flags: Dict[str, bool]) -> Dict[str, Any]:
    """Convierte el juego de flags al formato de variables de Instagram."""
    return {"__relay_internal__pv__" + name: value for name, value in flags.items()}


class GraphQLClient:
    """Cliente de ``/api/graphql`` reutilizable.

    Mantiene la sesion, los tokens y los ``doc_id`` cacheados entre llamadas,
    de modo que scrapear N perfiles cuesta N peticiones y no N-recargas.
    """

    def __init__(
        self,
        session_id: str,
        timeout: int = 30,
        impersonate: str = "chrome131",
        count: int = 12,
    ):
        self.session_id = session_id
        self.timeout = timeout
        self.impersonate = impersonate
        self.count = count

        self._session = None
        self._tokens: Optional[Dict[str, str]] = None
        self._doc_ids: Dict[str, str] = {}
        self._doc_ids_at: float = 0.0

    # ------------------------------------------------------------------ infra

    def _get_session(self):
        """Devuelve (creando si hace falta) la sesion HTTP con la cookie."""
        if self._session is not None:
            return self._session
        cr = ig_client._require_curl_cffi()
        session = cr.Session(impersonate=self.impersonate)
        session.cookies.set(
            "sessionid", self.session_id, domain=".instagram.com"
        )
        self._session = session
        return session

    def close(self) -> None:
        """Cierra la sesion HTTP."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None

    def __enter__(self) -> "GraphQLClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ----------------------------------------------------------------- tokens

    def _fetch_tokens(self) -> Dict[str, str]:
        """Saca ``fb_dtsg``, ``lsd`` y ``av`` de la portada."""
        session = self._get_session()
        try:
            response = session.get("{0}/".format(BASE_URL), timeout=self.timeout)
        except Exception as exc:
            raise GraphQLUnavailable(
                "No se pudo cargar la portada: {0}".format(exc)
            ) from exc

        if response.status_code != 200:
            raise GraphQLUnavailable(
                "La portada respondio {0}. Revisa el sessionid.".format(
                    response.status_code
                )
            )

        html = response.text
        dtsg = FB_DTSG_RE.search(html)
        lsd = LSD_RE.search(html)
        av = AV_RE.search(html)
        if not (dtsg and lsd and av):
            raise GraphQLUnavailable(
                "La portada no trae los tokens (fb_dtsg/lsd/av). Instagram "
                "puede haber cambiado el renderizado."
            )
        self._tokens = {
            "fb_dtsg": dtsg.group(1),
            "lsd": lsd.group(1),
            "av": av.group(1),
        }
        return self._tokens

    def tokens(self) -> Dict[str, str]:
        """Tokens vigentes, refetch si no hay."""
        if self._tokens is None:
            self._fetch_tokens()
        return self._tokens

    # ---------------------------------------------------------------- doc_ids

    def _resolve_doc_ids(self) -> Dict[str, str]:
        """Extrae los doc_id de TODAS las consultas del bundle JS.

        Se extraen del JS en vez de hardcodearlos para que sobrevivan a los
        despliegues de Instagram: cada build cambia sus numeros.
        """
        if self._doc_ids and (time.time() - self._doc_ids_at) < DOC_ID_TTL:
            return self._doc_ids

        session = self._get_session()
        try:
            home = session.get("{0}/".format(BASE_URL), timeout=self.timeout).text
        except Exception as exc:
            raise GraphQLUnavailable(
                "No se pudo leer el bundle de Instagram: {0}".format(exc)
            ) from exc

        found: Dict[str, str] = {}
        urls = sorted(
            set(re.findall(r'https://static\.cdninstagram\.com/rsrc\.php/[^"\']*\.js', home))
        )
        for url in urls:
            try:
                js = session.get(url, timeout=self.timeout).text
            except Exception:
                continue
            for name in re.findall(r'__d\("([\w]+)_instagramRelayOperation"', js):
                if name in found:
                    continue
                match = re.search(
                    DOC_ID_TEMPLATE % re.escape(name), js, re.S
                )
                if match:
                    found[name] = match.group(1)

        if not found:
            raise GraphQLUnavailable(
                "No se encontro ningun doc_id en los bundles de Instagram."
            )

        self._doc_ids = found
        self._doc_ids_at = time.time()
        return found

    def doc_id(self, name: str) -> str:
        """doc_id de una consulta, extraido del JS."""
        ids = self._resolve_doc_ids()
        if name not in ids:
            raise GraphQLUnavailable(
                "La consulta '{0}' no aparece en los bundles de Instagram.".format(name)
            )
        return ids[name]

    # ----------------------------------------------------------------- query

    def query(self, name: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        """Ejecuta una consulta y devuelve su objeto ``data``.

        Lanza :class:`BuildChanged` si la respuesta viene sin datos, que es
        lo que pasa cuando Instagram cambia el juego de flags.
        """
        for attempt in range(2):
            try:
                return self._query_once(name, variables)
            except _FieldException as exc:
                # Instagram usa el mismo error para "no existe" y para un
                # rechazo transitorio por rate limit. Se reintenta una vez
                # con tokens frescos antes de concluir que no existe.
                if attempt == 0:
                    logger.info(
                        "field_exception en %s; se reintenta con tokens nuevos",
                        name,
                    )
                    self._tokens = None
                    self._doc_ids = {}
                    self._doc_ids_at = 0.0
                    time.sleep(2)
                    continue
                raise ProfileNotFound(
                    "El usuario no existe o Instagram no lo expone."
                ) from exc

        raise ProfileNotFound("El usuario no existe o Instagram no lo expone.")

    def _query_once(self, name: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        """Una consulta, sin reintentos."""
        tokens = self.tokens()
        doc_id = self.doc_id(name)

        body = {
            "av": tokens["av"],
            "__user": "0",
            "__a": "1",
            "__req": "1",
            "dpr": "1",
            "__ccg": "EXCELLENT",
            "__s": "mb47r1",
            "__hsi": "7711567506977733946",
            "fb_dtsg": tokens["fb_dtsg"],
            "jazoest": str(2 + int(time.time())),
            "lsd": tokens["lsd"],
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": name,
            "server_timestamps": "true",
            "variables": json.dumps(variables, separators=(",", ":")),
            "doc_id": doc_id,
        }
        headers = {
            "x-ig-app-id": IG_APP_ID,
            "x-fb-friendly-name": name,
            "x-fb-lsd": tokens["lsd"],
            "x-asbd-id": "359341",
            "referer": "{0}/".format(BASE_URL),
            "content-type": "application/x-www-form-urlencoded",
            "accept-language": "en-US",
        }

        session = self._get_session()
        try:
            response = session.post(
                GRAPHQL_URL, headers=headers, data=body, timeout=self.timeout
            )
        except Exception as exc:
            raise GraphQLUnavailable(
                "Fallo la peticion a /api/graphql: {0}".format(exc)
            ) from exc

        text = response.text
        if "__ar" in text[:40]:
            raise GraphQLUnavailable(
                "Instagram rechazo la peticion: {0}".format(text[:160])
            )

        try:
            payload = response.json()
        except ValueError:
            raise GraphQLUnavailable(
                "La API no devolvio JSON (¿el sessionid caduco?): {0}".format(
                    text[:120]
                )
            ) from None

        data = payload.get("data") or {}
        errors = payload.get("errors") or []

        if data:
            # Instagram puede devolver datos validos Y errores a la vez
            # (por ejemplo, si un post tiene un campo que no se resuelve).
            # El error no invalida el resto: se avisa y se sigue.
            if errors:
                logger.warning(
                    "Instagram devolvio un error parcial (se ignora): %s",
                    str(errors[0].get("message", ""))[:120],
                )
            return data

        if errors:
            message = errors[0].get("message", "")
            if "field_exception" in message:
                raise _FieldException(errors)
            raise GraphQLUnavailable(
                "Instagram devolvio un error: {0}".format(message[:160])
            )

        raise BuildChanged(
            "Instagram cambio el build: la consulta '{0}' volvio sin datos. "
            "Hay que actualizar los flags __relay_internal__pv__.".format(name)
        )

    # ----------------------------------------------------------------- vistas

    def fetch_media_metrics(self, media_id: Any) -> Dict[str, Any]:
        """Metricas de alcance de un medio (reel o video).

        La API privada ``/api/v1/media/<id>/info/`` si responde y trae
        ``play_count`` (las visualizaciones generales) y ``like_count`` /
        ``comment_count``. En fotos no hay reproducciones.

        Devuelve un dict vacio si el endpoint no responde.
        """
        if not media_id:
            return {}
        session = self._get_session()
        try:
            response = session.get(
                "{0}/api/v1/media/{1}/info/".format(IG_API_BASE, media_id),
                headers={"x-ig-app-id": IG_APP_ID, "Accept": "*/*"},
                timeout=self.timeout,
            )
        except Exception as exc:
            logger.debug("No se pudieron obtener las metricas: %s", exc)
            return {}
        if response.status_code != 200:
            return {}
        try:
            items = (response.json() or {}).get("items") or []
        except ValueError:
            return {}
        if not items:
            return {}
        item = items[0]
        return {
            "play_count": item.get("play_count") or item.get("ig_play_count"),
            "like_count": item.get("like_count"),
            "comment_count": item.get("comment_count"),
            "media_type": item.get("media_type"),
        }

    def enrich_with_views(self, posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Anade las visualizaciones a los posts que son video.

        Solo consulta los medios que de verdad pueden tener reproducciones
        (media_type 2); para las fotos no hace falta ninguna peticion.
        """
        for post in posts:
            if post.get("media_type") != 2 or post.get("play_count"):
                continue
            metrics = self.fetch_media_metrics(post.get("pk"))
            if not metrics:
                continue
            post["play_count"] = metrics.get("play_count")
            post["views"] = metrics.get("play_count")
            if metrics.get("like_count") is not None:
                post["like_count"] = metrics["like_count"]
            if metrics.get("comment_count") is not None:
                post["comment_count"] = metrics["comment_count"]
        return posts

    # ----------------------------------------------------------------- scrape

    def scrape(self, username: str, with_views: bool = False) -> Dict[str, Any]:
        """Perfil completo usando la API GraphQL.

        Hace dos consultas: el timeline (que acepta el username y devuelve
        el pk del usuario) y el contenido del perfil (que necesita el pk).

        :param with_views: consultar el alcance de los reels y anadir las
            visualizaciones. Son peticiones extra, asi que va aparte.
        """
        username = username.strip().lstrip("@")

        timeline = self.query(
            POSTS_QUERY,
            {
                "data": {
                    "count": self.count,
                    "include_reel_media_seen_timestamp": True,
                    "include_relationship_info": True,
                    "latest_besties_reel_media": True,
                    "latest_reel_media": True,
                },
                "username": username,
                **_flags("", POSTS_FLAGS),
            },
        )

        connection = (
            timeline.get("xdt_api__v1__feed__user_timeline_graphql_connection") or {}
        )
        edges = connection.get("edges") or []
        posts = self._parse_posts(edges)

        if with_views:
            self.enrich_with_views(posts)

        # El pk del usuario viene en el propio post; si no hay posts, no hay
        # forma de pedir el perfil y hay que conformarse con el timeline.
        user_id = None
        for post in posts:
            if post.get("user_pk"):
                user_id = post["user_pk"]
                break

        profile_raw: Dict[str, Any] = {}
        if user_id:
            data = self.query(
                PROFILE_QUERY,
                {
                    "enable_integrity_filters": True,
                    "id": str(user_id),
                    **_flags("", PROFILE_FLAGS),
                },
            )
            profile_raw = data.get("user") or {}

        return ig_client.normalize_profile(
            self._merge(profile_raw, posts, username), source="graphql"
        )

    def _merge(
        self, user: Dict[str, Any], posts: List[Dict[str, Any]], username: str
    ) -> Dict[str, Any]:
        """Combina el nodo de perfil con el timeline."""
        merged: Dict[str, Any] = {
            "username": user.get("username") or username,
            "user_id": user.get("pk"),
            "fbid": user.get("fbid_v2"),
            "full_name": user.get("full_name"),
            "biography": user.get("biography") or "",
            "followers": user.get("follower_count"),
            "following": user.get("following_count"),
            "posts": user.get("media_count"),
            "is_private": bool(user.get("is_private")),
            "is_verified": bool(user.get("is_verified")),
            "is_memorialized": bool(user.get("is_memorialized")),
            "is_unpublished": bool(user.get("is_unpublished")),
            "is_business_account": bool(user.get("is_business_account")),
            "business_category_name": user.get("category_name"),
            "external_url": user.get("external_url"),
            "bio_links": user.get("bio_links") or [],
            "account_type": user.get("account_type"),
            "mutual_followers": user.get("mutual_followers_count"),
            "friendship_status": user.get("friendship_status") or {},
            "profile_pic_url": user.get("profile_pic_url"),
            "profile_pic_hd_url": (user.get("hd_profile_pic_url_info") or {}).get("url"),
            "context_links": [
                link.get("username")
                for link in (user.get("profile_context_links_with_user_ids") or [])
                if link.get("username")
            ],
            "url": "{0}/{1}/".format(BASE_URL, user.get("username") or username),
            "posts_detail": posts,
        }

        # Si el perfil no vino (cuenta sin posts), se toma lo del timeline.
        if not user and posts:
            owner = posts[0].get("owner") or {}
            merged.update(
                {
                    "user_id": owner.get("pk"),
                    "full_name": owner.get("full_name"),
                    "is_private": bool(owner.get("is_private")),
                    "is_verified": bool(owner.get("is_verified")),
                    "is_unpublished": bool(owner.get("is_unpublished")),
                    "friendship_status": owner.get("friendship_status") or {},
                    "profile_pic_url": owner.get("profile_pic_url"),
                    "profile_pic_hd_url": (owner.get("hd_profile_pic_url_info") or {}).get("url"),
                }
            )
        return merged

    def _parse_posts(self, edges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convierte las aristas del timeline en publicaciones."""
        posts = []
        for edge in edges:
            node = edge.get("node") or {}
            caption = node.get("caption") or {}
            owner = node.get("user") or {}
            versions = (node.get("image_versions2") or {}).get("candidates") or []
            image = None
            if versions:
                best = max(versions, key=lambda c: c.get("width", 0) * c.get("height", 0))
                image = best.get("url")
            video = None
            for version in node.get("video_versions") or []:
                if version.get("url"):
                    video = version["url"]
                    break
            shortcode = node.get("code")
            posts.append(
                {
                    "code": shortcode,
                    "pk": node.get("pk"),
                    "media_type": 2 if node.get("video_versions") else 1,
                    "caption": caption.get("text", "") if isinstance(caption, dict) else "",
                    "accessibility_caption": node.get("title") or "",
                    "display_uri": image,
                    "video_url": video,
                    "url": "{0}/p/{1}/".format(BASE_URL, shortcode),
                    "like_count": node.get("like_count"),
                    "comment_count": node.get("comment_count"),
                    "view_count": node.get("view_count"),
                    "taken_at": node.get("taken_at"),
                    "comments_disabled": node.get("comments_disabled"),
                    "has_liked": node.get("has_liked"),
                    "top_likers": [
                        liker.get("username")
                        if isinstance(liker, dict)
                        else liker
                        for liker in (node.get("top_likers") or [])
                        if liker
                    ],
                    "social_context": node.get("social_context"),
                    "original_width": node.get("original_width"),
                    "original_height": node.get("original_height"),
                    # Campos auxiliares para resolver el pk del usuario.
                    "user_pk": owner.get("pk"),
                    "owner": owner,
                }
            )
        return posts

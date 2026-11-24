#!/usr/bin/env python3
"""Cliente de Instagram para perfiles publicos, con modo anonimo y autenticado.

Instagram elimino los endpoints que se usaban anteriormente:

* ``?__a=1``            -> responde ``{"__ar":1,"error":1357055,...}``
* ``/api/v1/users/web_profile_info/`` -> 404/401/429 sin sesion
* ``/graphql/query/``   -> 401 ``require_login``

Lo que sigue funcionando sin cuenta es pedir la pagina publica del perfil
como lo haria un navegador. Instagram embebe los resultados de sus consultas
GraphQL dentro de ``<script type="application/json">`` siempre que la peticion
incluya las cabeceras ``Sec-Fetch-*`` de navegacion y la cookie de sesion
anonima.

Con un ``sessionid`` valido se puede consultar ademas
``i.instagram.com/api/v1/users/web_profile_info/``, que devuelve campos que el
HTML anonimo no expone (email, telefono, categoria de negocio). Ese modo es
opcional: si falla, la herramienta cae al modo anonimo.

El ``sessionid`` es un token con acceso completo a la cuenta. Obtenlo desde
las DevTools del navegador (Application > Cookies > instagram.com > sessionid)
y pasalo con la variable de entorno ``IG_SESSIONID`` para no dejarlo en el
historial de la shell.
"""

from __future__ import annotations

import base64
import binascii
import html as html_lib
import json
import logging
import re
import time
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://www.instagram.com"
API_BASE_URL = "https://i.instagram.com"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)

# User-Agent de la app movil: el endpoint autenticado lo exige.
IG_APP_USER_AGENT = (
    "Instagram 361.0.0.35.82 (iPad13,8; iOS 18_0; en_US; en-US; "
    "scale=2.00; 2048x2732; 674117118) AppleWebKit/420+"
)

# App-id publica de Instagram, necesario para las llamadas a la API privada.
IG_APP_ID = "936619743392459"

# Cookies de sesion anonima: sin ellas Instagram responde como bot.
ANONYMOUS_COOKIES = {
    "sessionid": "",
    "mid": "",
    "ig_pr": "1",
    "ig_vw": "1920",
    "csrftoken": "",
    "s_network": "",
    "ds_user_id": "",
}

# Sin estas cabeceras Instagram sirve un HTML de error en vez del perfil.
NAVIGATION_HEADERS = {
    "Accept": "text/html",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "User-Agent": USER_AGENT,
}

JSON_SCRIPT_RE = re.compile(
    r'<script type="application/json"[^>]*>(.*?)</script>', re.DOTALL
)
OG_DESCRIPTION_RE = re.compile(r'<meta property="og:description" content="([^"]*)"')
OG_IMAGE_RE = re.compile(r'<meta property="og:image" content="([^"]*)"')
POSTS_IN_OG_RE = re.compile(r"([\d.,]+[KMB]?)\s+Posts", re.IGNORECASE)
HASHTAG_RE = re.compile(r"#([\w\u00c0-\u024f]+)")


class InstagramError(Exception):
    """Error base del cliente."""


class ProfileNotFound(InstagramError):
    """El usuario no existe, fue eliminado o esta bloqueado."""


class RateLimited(InstagramError):
    """Instagram respondio 429 por demasiadas peticiones."""


# --------------------------------------------------------------------------
# Esquema canonico
#
# Todos los modos devuelven exactamente estas claves, con ``None`` cuando el
# dato no esta disponible. Un servicio HTTP necesita un contrato estable: si
# cada modo devuelve un superconjunto distinto, el cliente tiene que
# adivinar que campos existen.
# --------------------------------------------------------------------------

PROFILE_FIELDS = (
    "username",
    "user_id",
    "fbid",
    "full_name",
    "biography",
    "followers",
    "following",
    "posts",
    "posts_loaded",
    "is_private",
    "is_verified",
    "is_business_account",
    "business_category_name",
    "email",
    "phone_number",
    "external_url",
    "bio_links",
    "profile_pic_url",
    "profile_pic_hd_url",
    "url",
    "is_placeholder",
    "is_memorialized",
    "is_unpublished",
    "account_type",
    "mutual_followers",
    "friendship_status",
    "context_links",
    "posts_detail",
    "stories_count",
    "stories",
    "stories_active",
    "source",
)

POST_FIELDS = (
    "code",
    "pk",
    "media_type",
    "caption",
    "accessibility_caption",
    "display_uri",
    "video_url",
    "url",
    "like_count",
    "comment_count",
    "view_count",
    "play_count",
    "views",
    "taken_at",
    "comments_disabled",
    "has_liked",
    "top_likers",
    "social_context",
    "original_width",
    "original_height",
)

OWN_ACCOUNT_FIELDS = (
    "username",
    "user_id",
    "fbid",
    "full_name",
    "biography",
    "is_private",
    "is_business_account",
    "is_professional_account",
    "business_category_name",
    "has_phone_number",
    "business_contact_method",
    "external_url",
    "profile_pic_url",
)

PROFILE_DEFAULTS = {
    "is_private": False,
    "is_verified": False,
    "is_business_account": False,
    "is_memorialized": False,
    "is_unpublished": False,
    "is_placeholder": False,
    "posts_loaded": 0,
    "bio_links": [],
    "friendship_status": {},
    "context_links": [],
    "posts_detail": [],
    "stories": [],
    "stories_active": None,
}


def normalize_post(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Completa una publicacion con todas las claves del esquema."""
    post = {field: None for field in POST_FIELDS}
    post["top_likers"] = []
    post.update(raw)
    for key in list(post):
        if key not in POST_FIELDS:
            del post[key]
    return post


def normalize_profile(raw: Dict[str, Any], source: str) -> Dict[str, Any]:
    """Completa un perfil con todas las claves del esquema.

    ``source`` indica de donde salio el dato: ``anonymous`` (HTML publico) o
    ``session`` (``/api/graphql``).
    """
    profile = {field: None for field in PROFILE_FIELDS}
    profile.update(PROFILE_DEFAULTS)
    profile.update(raw)
    profile["source"] = source
    profile["posts_detail"] = [
        normalize_post(post) for post in (profile.get("posts_detail") or [])
    ]
    profile["posts_loaded"] = len(profile["posts_detail"])
    for key in list(profile):
        if key not in PROFILE_FIELDS:
            del profile[key]
    return profile


def normalize_own_account(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Completa los datos de la cuenta propia con el esquema."""
    account = {field: None for field in OWN_ACCOUNT_FIELDS}
    account.update(raw)
    for key in list(account):
        if key not in OWN_ACCOUNT_FIELDS:
            del account[key]
    return account


def _require_curl_cffi():
    """Importa curl_cffi o falla con un mensaje accionable.

    Se usa en toda la libreria: ``requests`` no sirve porque Instagram
    rechaza su huella TLS.
    """
    try:
        from curl_cffi import requests as cr
    except ImportError as exc:
        raise InstagramError(
            "curl_cffi no esta instalado. Usa: pip install curl_cffi"
        ) from exc
    return cr


def build_session(impersonate: str = "chrome131") -> Any:
    """Sesion HTTP con huella de navegador y cookies de visitante."""
    cr = _require_curl_cffi()
    session = cr.Session(impersonate=impersonate)
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
    )
    session.cookies.update(ANONYMOUS_COOKIES)
    return session


def fetch_profile_html(
    username: str,
    session: Optional[Any] = None,
    timeout: int = 30,
    retries: int = 2,
    backoff: int = 15,
    impersonate: str = "chrome131",
) -> str:
    """Descarga el HTML publico del perfil, reintentando ante un 429.

    Instagram redirige al login cuando limita la IP (``?is_from_rle``), asi
    que no se siguen las redirecciones para poder detectarlo.
    """
    owns_session = session is None
    session = session or build_session(impersonate)
    url = "{0}/{1}/".format(BASE_URL, username.strip().lstrip("@"))
    last_error = ""

    try:
        for attempt in range(retries + 1):
            try:
                response = session.get(
                    url,
                    headers=NAVIGATION_HEADERS,
                    timeout=timeout,
                    allow_redirects=False,
                )
            except Exception as exc:
                raise InstagramError(
                    "No se pudo conectar con Instagram: {0}".format(exc)
                ) from exc

            if response.status_code == 200:
                return response.text

            if response.status_code == 404:
                raise ProfileNotFound("El usuario '{0}' no existe.".format(username))

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location", "")
                if "is_from_rle" in location or "/accounts/login" in location:
                    raise RateLimited(
                        "Instagram limito esta IP para el trafico anonimo "
                        "(rate limit). Para saltartelo pide una sesion: "
                        "--session_id <sessionid> (o exporta IG_SESSIONID). "
                        "Tambien sirve ejecutar desde otra IP."
                    )
                raise InstagramError(
                    "Instagram redirigio a {0}.".format(location or "destino desconocido")
                )

            if response.status_code == 429:
                last_error = "Instagram limito las peticiones (429)."
                if attempt < retries:
                    wait = backoff * (2 ** attempt)
                    logger.warning(
                        "%s Reintentando en %ss...", last_error, wait
                    )
                    time.sleep(wait)
                    continue
                raise RateLimited(
                    "{0} Reintentos agotados. Pide una sesion (--session_id) "
                    "o ejecuta desde otra IP para saltarte el limite.".format(
                        last_error
                    )
                )

            raise InstagramError(
                "Instagram respondio {0} al pedir el perfil.".format(
                    response.status_code
                )
            )
    finally:
        if owns_session:
            session.close()

    raise InstagramError(last_error or "No se pudo obtener el perfil.")


def iter_nested(obj: Any, key: str) -> Iterator[Any]:
    """Recorre una estructura anidada y yielda cada valor de la clave dada."""
    if isinstance(obj, dict):
        for name, value in obj.items():
            if name == key:
                yield value
            yield from iter_nested(value, key)
    elif isinstance(obj, list):
        for value in obj:
            yield from iter_nested(value, key)


def find_module_data(blob: Any, module_name: str) -> List[Dict[str, Any]]:
    """Extrae los datos de un modulo ``require`` por su nombre.

    En el JSON de Instagram los modulos tienen la forma
    ``[nombre, parametros, datos, id]``, con el nombre en posicion 0 (no es
    una clave), asi que no sirve un busqueda por clave.
    """
    found = []

    def walk(obj: Any) -> None:
        if isinstance(obj, list):
            if (
                len(obj) >= 3
                and isinstance(obj[0], str)
                and obj[0] == module_name
                and isinstance(obj[2], dict)
            ):
                found.append(obj[2])
                return
            for value in obj:
                walk(value)
        elif isinstance(obj, dict):
            for value in obj.values():
                walk(value)

    walk(blob)
    return found


def extract_json_blobs(html: str) -> List[Any]:
    """Parsea todos los blobs JSON embebidos en la pagina."""
    blobs = []
    for match in JSON_SCRIPT_RE.finditer(html):
        try:
            blobs.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    return blobs


def expand_count(value: str) -> Optional[int]:
    """Convierte '487', '1.2K' o '3M' en un entero."""
    value = value.strip().upper().replace(",", "")
    if not value:
        return None
    factor = 1
    if value[-1] in "KMB":
        factor = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[value[-1]]
        value = value[:-1]
    try:
        return int(float(value) * factor)
    except ValueError:
        return None


def parse_posts_count(html: str) -> Optional[int]:
    """Saca el total de publicaciones del meta og:description."""
    match = OG_DESCRIPTION_RE.search(html)
    if not match:
        return None
    posts_match = POSTS_IN_OG_RE.search(match.group(1))
    if not posts_match:
        return None
    return expand_count(posts_match.group(1))


def parse_profile(html: str) -> Dict[str, Any]:
    """Extrae los datos del perfil y sus publicaciones del HTML."""
    blobs = extract_json_blobs(html)

    profile: Dict[str, Any] = {}
    timeline: Dict[str, Any] = {}

    for blob in blobs:
        for user in iter_nested(blob, "xig_user_by_username"):
            if not isinstance(user, dict):
                continue
            # El primer blob trae los metadatos, el segundo el feed de posts.
            if "follower_count" in user:
                profile.update(user)
            connection = user.get("polaris_ordered_timeline_connection")
            if isinstance(connection, dict):
                timeline = connection

    if not profile:
        raise ProfileNotFound(
            "No se encontro informacion del perfil (usuario inexistente, "
            "privado o bloqueado)."
        )

    posts = []
    for edge in timeline.get("edges") or []:
        node = edge.get("node") or {}
        caption = node.get("caption") or {}
        posts.append(
            {
                "code": node.get("code"),
                "pk": node.get("pk"),
                "media_type": node.get("media_type"),
                "caption": caption.get("text", "") if isinstance(caption, dict) else "",
                "accessibility_caption": node.get("accessibility_caption"),
                "display_uri": node.get("display_uri"),
                "url": "{0}/p/{1}/".format(BASE_URL, node.get("code")),
            }
        )

    og_image = OG_IMAGE_RE.search(html)
    profile_pic = profile.get("profile_pic_url")
    # El og:image puede venir vacio en perfiles sin foto: no debe pisar el JSON.
    if og_image and og_image.group(1).strip():
        profile_pic = html_lib.unescape(og_image.group(1))

    bio_links = profile.get("bio_links") or []
    external_url = ""
    for link in bio_links:
        if isinstance(link, dict) and link.get("url"):
            external_url = link["url"]
            break
        if isinstance(link, str) and link:
            external_url = link
            break

    return normalize_profile(
        {
            "username": profile.get("username"),
            "user_id": profile.get("pk"),
            "full_name": profile.get("full_name"),
            "biography": profile.get("biography") or "",
            "followers": profile.get("follower_count"),
            "following": profile.get("following_count"),
            "posts": parse_posts_count(html),
            "posts_loaded": len(posts),
            "is_private": bool(profile.get("is_private")),
            "is_verified": bool(profile.get("is_verified")),
            # Instagram reserva algunos nombres y crea un perfil vacio con pk real.
            "is_placeholder": _looks_like_placeholder(profile),
            "bio_links": bio_links,
            "external_url": external_url,
            "profile_pic_url": profile_pic,
            "url": "{0}/{1}/".format(BASE_URL, profile.get("username")),
            "posts_detail": posts,
        },
        source="anonymous",
    )


def _is_anonymous_avatar(pic_url: str) -> bool:
    """True si la foto de perfil es el avatar generico de Instagram.

    El identificador va en base64 dentro del parametro ``ig_cache_key``
    (``YW5vbnltb3VzX3Byb2ZpbGVfcGlj`` -> ``anonymous_profile_pic``).
    """
    if not pic_url:
        return False
    match = re.search(r"ig_cache_key=([^&]+)", pic_url)
    if not match:
        return False
    try:
        padded = match.group(1).split(".")[0]
        padded += "=" * (-len(padded) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8", "ignore")
    except (ValueError, binascii.Error):
        return False
    return "anonymous_profile_pic" in decoded


def _looks_like_placeholder(profile: Dict[str, Any]) -> bool:
    """True si el perfil existe pero esta vacio (nombre reservado por Instagram).

    Instagram reserva algunos nombres y crea un perfil sin actividad con pk
    real y el avatar generico.
    """
    if not profile.get("pk"):
        return False
    if not _is_anonymous_avatar(profile.get("profile_pic_url") or ""):
        return False
    return (
        not profile.get("full_name")
        and not (profile.get("biography") or "").strip()
        and not (profile.get("follower_count") or 0)
    )


def extract_hashtags(posts: List[Dict[str, Any]]) -> Dict[str, int]:
    """Cuenta los hashtags usados en los captions cargados."""
    counter: Dict[str, int] = {}
    for post in posts:
        for tag in HASHTAG_RE.findall(post.get("caption") or ""):
            key = tag.lower()
            counter[key] = counter.get(key, 0) + 1
    return dict(sorted(counter.items(), key=lambda kv: kv[1], reverse=True))


# --------------------------------------------------------------- modo sesion


def fetch_own_account(
    sessionid: str,
    timeout: int = 30,
) -> Dict[str, Any]:
    """Extrae los datos de la cuenta propia desde la query ``PolarisViewer``.

    Es la unica consulta que Instagram sigue sirviendo server-side con
    sesion, y aporta campos que la vista anonima no expone
    (``category_name``, ``has_phone_number``, ``business_contact_method``,
    ``fbid``). No forma parte del perfil de un tercero: es informacion de la
    cuenta con la que se scrapea.
    """
    cr = _require_curl_cffi()
    session = cr.Session(impersonate="chrome131")
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html",
            "Accept-Language": "en-US,en;q=0.8",
        }
    )
    session.cookies.update({"sessionid": sessionid, "ig_pr": "1", "ig_vw": "1920"})

    try:
        response = session.get("{0}/".format(BASE_URL), timeout=timeout)
    except Exception as exc:
        raise InstagramError(
            "No se pudo cargar la portada con la sesion: {0}".format(exc)
        ) from exc

    if response.status_code != 200:
        raise InstagramError(
            "La portada con sesion respondio {0}.".format(response.status_code)
        )

    for blob in extract_json_blobs(response.text):
        for data in find_module_data(blob, "PolarisViewer"):
            payload = data.get("data")
            if isinstance(payload, dict) and payload.get("username"):
                return normalize_own_account(
                    {
                        "username": payload.get("username"),
                        "user_id": payload.get("id"),
                        "fbid": payload.get("fbid"),
                        "full_name": payload.get("full_name"),
                        "biography": payload.get("biography"),
                        "is_private": bool(payload.get("is_private")),
                        "is_business_account": bool(payload.get("is_business_account")),
                        "is_professional_account": bool(
                            payload.get("is_professional_account")
                        ),
                        "business_category_name": payload.get("category_name"),
                        "has_phone_number": bool(payload.get("has_phone_number")),
                        "business_contact_method": payload.get(
                            "business_contact_method"
                        ),
                        "external_url": payload.get("external_url"),
                        "profile_pic_url": payload.get("profile_pic_url"),
                    }
                )

    raise InstagramError(
        "La sesion no devolvio datos de cuenta (PolarisViewer ausente)."
    )

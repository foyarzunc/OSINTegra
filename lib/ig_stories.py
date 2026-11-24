#!/usr/bin/env python3
"""Historias de Instagram y deteccion de campanas.

Que se puede obtener
--------------------
* **Historias destacadas** (``highlights``): las colecciones permanentes que
  el perfil muestra en la fila de historias. De aqui sale la cantidad y los
  titulos, que es donde las cuentas nombran sus campanas ("Black Friday",
  "Lanzamiento", "Campana Navidad"...).
* **Stories activas**: si el objetivo tiene una historia publicada ahora
  mismo, y cuando expira.
* **Impacto de las publicaciones**: likes, comentarios y marcas de tiempo ya
  vienen en el feed, y de ahi se derivan los picos de interaccion.

Limitacion
----------
Instagram no expone las metricas por story (vistas, respuestas) de una
cuenta ajena: los endpoints privados que las daban (``/api/v1/reels_media/``)
responden 404 y la query GraphQL que abre la galería de stories exige
variables que solo el codigo de la web conoce. El impacto se calcula sobre
las publicaciones, que si las entrega.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

HIGHLIGHTS_QUERY = "PolarisProfileStoryHighlightsTrayContentQuery"
TRAY_QUERY = "PolarisStoriesV3TrayContainerQuery"

# Instagram usa ids de media tipo "snowflake": los 23 bits bajos son ruido y
# el resto es un timestamp en milisegundos desde un epoch propio.
MEDIA_ID_EPOCH_MS = 1314220020940

# Un comentario pesa mas que un like en intencion, asi que se pondera.
COMMENT_WEIGHT = 3


def decode_media_id(media_id: Any) -> Optional[str]:
    """Convierte un id de media de Instagram en fecha ISO.

    Devuelve ``None`` si el id no tiene forma de timestamp.
    """
    try:
        value = int(media_id)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    millis = ((value >> 23) + MEDIA_ID_EPOCH_MS) / 1000
    try:
        stamp = datetime.fromtimestamp(millis, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return stamp.strftime("%Y-%m-%d %H:%M:%S")


def fetch_highlights(client, user_id: Any) -> List[Dict[str, Any]]:
    """Historias destacadas de un usuario.

    :param client: cliente GraphQL con sesion.
    :param user_id: pk numerico del usuario.
    """
    if not user_id:
        return []
    data = client.query(HIGHLIGHTS_QUERY, {"user_id": str(user_id)})
    highlights = data.get("highlights") or {}
    result = []
    for edge in highlights.get("edges") or []:
        node = edge.get("node") or {}
        cover = ((node.get("cover_media") or {}).get("cropped_image_version") or {})
        cover_url = cover.get("url")
        result.append(
            {
                "id": node.get("id"),
                "title": node.get("title") or "",
                "cover_url": cover_url,
                "url": "{0}/stories/highlights/{1}/".format(
                    "https://www.instagram.com",
                    str(node.get("id", "")).replace("highlight:", ""),
                ),
                # El id del medio va en el nombre del archivo de la portada
                # ("<media_id>_<owner_id>_<...>_n.jpg"). Ojo: data el MEDIO de
                # la portada, que Instagram recicla y suele ser antiguo, no el
                # momento en que se creo la destacada.
                "cover_media_taken_at": decode_media_id(_media_id_from_url(cover_url)),
            }
        )
    return result


def _media_id_from_url(url: Optional[str]) -> Optional[str]:
    """Saca el id de medio del nombre del archivo de una URL de CDN."""
    if not url:
        return None
    name = url.split("/")[-1].split("?")[0]
    head = name.split("_")[0]
    return head if head.isdigit() else None


def fetch_active_story(client, username: str) -> Optional[Dict[str, Any]]:
    """Info de la story activa del objetivo, si tiene alguna ahora.

    Sale del bandeja de stories de la cuenta con la que se scrapea: si el
    objetivo aparece ahi, tiene una story vigente.
    """
    data = client.query(
        TRAY_QUERY,
        {"data": {"is_following_feed": True, "reason": "web_home"}},
    )
    tray = data.get("xdt_api__v1__feed__reels_tray") or {}
    for item in tray.get("tray") or tray.get("items") or []:
        user = item.get("user") or {}
        if user.get("username") != username:
            continue
        expiring = item.get("expiring_at")
        return {
            "has_active_story": True,
            "expiring_at": (
                datetime.fromtimestamp(expiring, tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if expiring
                else None
            ),
            "seen": bool(item.get("seen")),
            "reel_type": item.get("reel_type"),
            "latest_reel_media": item.get("latest_reel_media"),
        }
    return {"has_active_story": False, "expiring_at": None}


def impact_score(post: Dict[str, Any]) -> int:
    """Puntaje de impacto de una publicacion."""
    likes = post.get("like_count") or 0
    comments = post.get("comment_count") or 0
    views = post.get("view_count") or 0
    return int(likes + comments * COMMENT_WEIGHT + views)


def rank_posts_by_impact(posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ordena las publicaciones por impacto, de mayor a menor."""
    ranked = []
    for post in posts:
        score = impact_score(post)
        if score <= 0 and not (post.get("like_count") or post.get("comment_count")):
            continue
        ranked.append(
            {
                "code": post.get("code"),
                "url": post.get("url"),
                "taken_at": post.get("taken_at"),
                "taken_at_iso": (
                    datetime.fromtimestamp(
                        post["taken_at"], tz=timezone.utc
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    if post.get("taken_at")
                    else None
                ),
                "likes": post.get("like_count"),
                "comments": post.get("comment_count"),
                "views": post.get("view_count"),
                "impact_score": score,
                "caption": (post.get("caption") or "")[:280],
                "hashtags": sorted(
                    {
                        word.lstrip("#").lower()
                        for word in (post.get("caption") or "").split()
                        if word.startswith("#")
                    }
                ),
            }
        )
    ranked.sort(key=lambda item: item["impact_score"], reverse=True)
    return ranked


def analyze_campaign(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Senales de campana publicitaria a partir de los datos del perfil.

    Devuelve las metricas crudas y una lectura, para que el analista decida:
    no es un clasificador, son indicadores con su numero al lado.
    """
    posts = profile.get("posts_detail") or []
    signals: Dict[str, Any] = {}
    triggered: List[str] = []

    # --- velocidad de publicacion ---
    stamps = sorted(p["taken_at"] for p in posts if p.get("taken_at"))
    if len(stamps) >= 2:
        span_days = max((stamps[-1] - stamps[0]) / 86400.0, 1.0)
        velocity = len(posts) / span_days
    else:
        span_days = 0.0
        velocity = 0.0
    signals["posts_loaded"] = len(posts)
    signals["window_days"] = round(span_days, 2)
    signals["posts_per_day"] = round(velocity, 2)
    if velocity >= 2.0:
        triggered.append("rafaga_de_publicaciones")

    # --- picos de interaccion ---
    # La mediana se calcula solo sobre las publicaciones que tienen likes: si
    # se mezclan las que no los tienen, la mediana se hunde y cualquier post
    # parece un pico.
    likes = [p["like_count"] for p in posts if p.get("like_count") is not None]
    if len(likes) >= 3:
        median_likes = statistics.median(likes)
        best = max(likes)
        ratio = (best / median_likes) if median_likes else 0.0
        # Con una mediana baja el ratio no significa nada (3 vs 441022).
        meaningful = median_likes >= 50
        spikes = [
            p["code"]
            for p in posts
            if meaningful
            and p.get("like_count")
            and median_likes
            and p["like_count"] >= 5 * median_likes
        ]
        signals["median_likes"] = int(median_likes)
        signals["max_likes"] = int(best)
        signals["spike_ratio"] = round(ratio, 2) if meaningful else None
        signals["spike_posts"] = spikes
        if ratio >= 5.0 and meaningful:
            triggered.append("pico_de_interaccion")
    else:
        signals["median_likes"] = None
        signals["max_likes"] = None
        signals["spike_ratio"] = None
        signals["spike_posts"] = []

    # --- concentracion de hashtags (serie coordinada) ---
    ranked = rank_posts_by_impact(posts)
    tag_counter: Dict[str, int] = {}
    for post in posts:
        for tag in {
            word.lstrip("#").lower()
            for word in (post.get("caption") or "").split()
            if word.startswith("#")
        }:
            tag_counter[tag] = tag_counter.get(tag, 0) + 1
    total_tags = sum(tag_counter.values())
    top_tags = sorted(tag_counter.items(), key=lambda kv: kv[1], reverse=True)[:10]
    signals["hashtags"] = [
        {"tag": tag, "count": count,
         "share": round(count / total_tags, 3) if total_tags else 0.0}
        for tag, count in top_tags
    ]
    # Un solo hashtag en el 100% de los posts no dice nada por si solo (lo
    # tiene cualquier cuenta personal). Solo cuenta si ademas hay cadencia.
    if (
        top_tags
        and total_tags
        and (top_tags[0][1] / total_tags) >= 0.5
        and len(posts) >= 4
        and velocity >= 1.0
    ):
        triggered.append("hashtag_concentrado")

    # --- historias ---
    stories = profile.get("stories") or []
    signals["stories_count"] = len(stories)
    signals["stories_titles"] = [s.get("title") for s in stories if s.get("title")]
    if len(stories) >= 3:
        triggered.append("multiples_destacadas")

    active = profile.get("stories_active") or {}
    signals["has_active_story"] = bool(active.get("has_active_story"))
    if active.get("has_active_story"):
        triggered.append("story_activa")

    likelihood = {
        0: "indeterminado",
        1: "baja",
        2: "media",
    }.get(len(triggered), "alta")

    return {
        "campaign_likelihood": likelihood,
        "signals_triggered": triggered,
        "signals": signals,
        "top_posts_by_impact": ranked[:10],
    }

#!/usr/bin/env python3
"""Baseline historico de perfiles.

Guarda instantaneas ("snapshots") de un perfil en disco para poder comparar
el estado actual contra el historico. Es lo que permite distinguir una
campana de un dia de la actividad normal de la cuenta: una cuenta que
publica 1 post por semana y de pronto publica 8 en un dia tiene una senal
que no se ve mirando solo el snapshot de hoy.

Diseno
------
* Cada snapshot es un JSON con las metricas comparables y, opcionalmente,
  el perfil completo.
* El baseline es la **mediana** de las velocidades historicas, no el ultimo
  valor: asi un dia atypico no falsea la linea base.
* Los deltas se normalizan por dias transcurridos entre capturas.
"""

from __future__ import annotations

import json
import os
import statistics
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DEFAULT_ROOT = os.path.join(
    os.path.expanduser("~"), ".osintegra", "history"
)

# Campos que se comparan entre snapshots.
COMPARABLE_FIELDS = (
    "followers",
    "following",
    "posts",
    "stories_count",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return None


def summarize(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Extrae de un perfil las metricas que se comparan entre capturas."""
    posts = profile.get("posts_detail") or []
    stamps = sorted(p["taken_at"] for p in posts if p.get("taken_at"))
    if len(stamps) >= 2:
        span_days = max((stamps[-1] - stamps[0]) / 86400.0, 1.0)
        velocity = len(posts) / span_days
    else:
        span_days = 0.0
        velocity = 0.0

    likes = [p["like_count"] for p in posts if p.get("like_count") is not None]
    median_likes = statistics.median(likes) if likes else None

    tag_counter: Dict[str, int] = {}
    for post in posts:
        for word in (post.get("caption") or "").split():
            if word.startswith("#"):
                key = word.lstrip("#").lower()
                tag_counter[key] = tag_counter.get(key, 0) + 1
    top_tags = sorted(tag_counter.items(), key=lambda kv: kv[1], reverse=True)[:10]

    return {
        "followers": profile.get("followers"),
        "following": profile.get("following"),
        "posts": profile.get("posts"),
        "stories_count": profile.get("stories_count"),
        "stats": {
            "posts_loaded": len(posts),
            "window_days": round(span_days, 2),
            "posts_per_day": round(velocity, 3),
            "median_likes": median_likes,
            "top_hashtags": [
                {"tag": tag, "count": count} for tag, count in top_tags
            ],
        },
    }


class SnapshotStore:
    """Guarda y lee instantaneas en disco.

    La estructura es ``<root>/<username>/<AAAAMMDD-HHMMSS>.json``.
    """

    def __init__(self, root: Optional[str] = None):
        self.root = os.path.abspath(
            os.path.expanduser(root or os.environ.get("OSINTEGRA_HOME", DEFAULT_ROOT))
        )

    def folder(self, username: str) -> str:
        return os.path.join(self.root, username.strip().lstrip("@"))

    def save(
        self,
        profile: Dict[str, Any],
        notes: Optional[str] = None,
        full: bool = False,
        recorded_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Guarda una instantanea del perfil y devuelve el registro.

        :param recorded_at: momento de la captura. Por defecto, ahora. Sirve
            para importar historico o para pruebas.
        """
        username = profile.get("username") or "unknown"
        stamp = (recorded_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        folder = self.folder(username)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, stamp.strftime("%Y%m%d-%H%M%S") + ".json")

        record: Dict[str, Any] = {
            "recorded_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "username": username,
            "source": profile.get("source"),
            "notes": notes,
        }
        record.update(summarize(profile))
        if full:
            record["profile"] = profile

        with open(path, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
        record["_path"] = path
        return record

    def load(self, username: str) -> List[Dict[str, Any]]:
        """Devuelve las instantaneas del usuario, de mas antigua a mas nueva."""
        folder = self.folder(username)
        if not os.path.isdir(folder):
            return []
        records = []
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as handle:
                    records.append(json.load(handle))
            except (OSError, ValueError):
                continue
        records.sort(key=lambda r: r.get("recorded_at", ""))
        return records

    def latest(self, username: str) -> Optional[Dict[str, Any]]:
        records = self.load(username)
        return records[-1] if records else None

    def delete(self, username: str) -> int:
        """Borra el historico del usuario. Devuelve cuantos habia."""
        folder = self.folder(username)
        if not os.path.isdir(folder):
            return 0
        count = 0
        for name in os.listdir(folder):
            if name.endswith(".json"):
                os.remove(os.path.join(folder, name))
                count += 1
        try:
            os.rmdir(folder)
        except OSError:
            pass
        return count


def compare(
    profile: Dict[str, Any], records: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Compara el perfil actual contra el historico guardado.

    Devuelve los deltas por campo, la velocidad de publicacion actual frente
    a la linea base (mediana historica) y si se detecta el arranque de una
    campana.
    """
    current = summarize(profile)
    notes: List[str] = []

    if not records:
        return {
            "snapshots": 0,
            "error": "No hay historico para este usuario. Usa --record para empezar.",
            "current": current,
            "notes": notes,
        }

    previous = records[-1]
    now_dt = datetime.now(timezone.utc)
    prev_dt = _parse_iso(previous.get("recorded_at", ""))
    days = (now_dt - prev_dt).total_seconds() / 86400.0 if prev_dt else 0.0

    deltas: Dict[str, Any] = {}
    # Con dos capturas casi simultaneas el "por dia" no significa nada.
    per_day = days if days >= 0.01 else None
    for field in COMPARABLE_FIELDS:
        old = previous.get(field)
        new = current.get(field)
        if isinstance(old, int) and isinstance(new, int):
            deltas[field] = {
                "before": old,
                "now": new,
                "delta": new - old,
                "per_day": round((new - old) / per_day, 2) if per_day else None,
            }

    # Linea base: mediana de las velocidades historicas.
    velocities = [
        r.get("stats", {}).get("posts_per_day")
        for r in records
        if isinstance(r.get("stats", {}).get("posts_per_day"), (int, float))
    ]
    baseline_velocity = statistics.median(velocities) if velocities else 0.0
    current_velocity = current["stats"]["posts_per_day"]

    if baseline_velocity:
        ratio = current_velocity / baseline_velocity
    elif current_velocity:
        # La linea base era 0: cualquier publicacion es un cambio infinito.
        ratio = None
    else:
        ratio = None

    medians = [
        r.get("stats", {}).get("median_likes")
        for r in records
        if isinstance(r.get("stats", {}).get("median_likes"), (int, float))
    ]
    baseline_median = statistics.median(medians) if medians else None
    current_median = current["stats"]["median_likes"]
    engagement_ratio = (
        current_median / baseline_median
        if baseline_median and current_median is not None
        else None
    )

    # ¿Arranque de campana? Velocidad al menos el doble que la linea base y
    # al menos 3 publicaciones nuevas desde la captura anterior.
    posts_delta = (deltas.get("posts") or {}).get("delta")
    enough_new_posts = isinstance(posts_delta, int) and posts_delta >= 3
    faster = (
        (ratio is not None and ratio >= 2.0)
        or (ratio is None and baseline_velocity == 0 and current_velocity >= 2.0)
    )
    campaign_started = bool(faster and enough_new_posts)

    if campaign_started:
        if ratio is not None:
            detail = "Velocidad {0:.1f}x la linea base".format(ratio)
        else:
            detail = "Velocidad {0}/dia frente a una linea base de 0".format(
                current_velocity
            )
        notes.append(
            "{0} con {1} publicaciones nuevas: posible arranque de "
            "campana.".format(detail, posts_delta)
        )
    if engagement_ratio and engagement_ratio >= 2.0:
        notes.append(
            "Interaccion mediana {0:.1f}x la linea base.".format(engagement_ratio)
        )
    if deltas.get("followers", {}).get("per_day") is not None:
        notes.append(
            "Crecimiento de {0} seguidores/dia.".format(
                deltas["followers"]["per_day"]
            )
        )
    if not notes:
        notes.append("Sin cambios significativos frente al historico.")

    return {
        "snapshots": len(records),
        "first_recorded_at": records[0].get("recorded_at"),
        "last_recorded_at": previous.get("recorded_at"),
        "days_since_last": round(days, 2),
        "deltas": deltas,
        "velocity": {
            "current": current_velocity,
            "baseline": round(baseline_velocity, 3),
            "ratio": round(ratio, 2) if ratio else None,
        },
        "engagement": {
            "current_median_likes": current_median,
            "baseline_median_likes": baseline_median,
            "ratio": round(engagement_ratio, 2) if engagement_ratio else None,
        },
        "campaign_started": campaign_started,
        "notes": notes,
        "current": current,
    }

#!/usr/bin/env python3
"""OSINTegra: scraper de perfiles de Instagram.

Instagram ya no sirve el endpoint ``?__a=1`` (error 1357055) ni
``/api/v1/users/web_profile_info/`` (401/404). Lo que funciona es pedir la
pagina publica del perfil como lo haria un navegador: con las cabeceras
``Sec-Fetch-*`` de navegacion y la cookie de sesion anonima, Instagram
embebe los resultados de sus consultas GraphQL dentro de
``<script type="application/json">``.

Por defecto NO se usa ninguna sesion: el modo anonimo no necesita cuenta.
Si quieres datos en vivo o acceso a cuentas privadas, pide una sesion
explicitamente con ``--session_id``.

El modo con sesion usa la API GraphQL de Instagram (``/api/graphql``) mediante
``curl_cffi``, que falsifica la huella TLS del navegador: con ``requests``
puro Instagram rechaza la peticion.

Uso:
    python3 test_ig_scrapper.py -u USERNAME
    python3 test_ig_scrapper.py -u USERNAME --posts --tags
    python3 test_ig_scrapper.py -u USERNAME --session_id XXX --save-dir
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
import urllib.parse
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import ig_client
from lib.ig_client import InstagramError
from lib.scraper import Mode, Scraper

DEFAULT_USERNAME = "weon__chileno"

# Formatos que se pueden generar con --output.
OUTPUT_FORMATS = ("json", "csv")

# Variable de entorno que se lee SOLO si se pide --session_id sin valor.
SESSION_ENV_VAR = "IG_SESSIONID"

# Colores del banner original del proyecto.
CY = "\033[1;36m"
GR = "\033[1;32m"
RED = "\033[1;31m"
NU = "\033[0m"


def banner() -> None:
    """Banner del proyecto."""
    print(
        f"""{CY}
  ╔═╗ ╔═╗     ╦  ╔═╗
  ╚═╗ ║ ║     ║  ║ ╦
  ╚═╝ ╚═╝  {GR}o{CY}  ╩  ╚═╝

        {GR}Coded By :
  {GR}Yezz123{NU}
	            """
    )


def resolve_session_id(value: Optional[str]) -> Optional[str]:
    """Devuelve el sessionid a usar, o ``None`` si no se pidio ninguno.

    Acepta el token tal cual (copiado de DevTools, a veces URL-encoded) y lo
    normaliza a la forma real, que usa ``:`` como separador.

    La libreria nunca lee el entorno: la sesion entra explicitamente. El
    flag sin valor es la unica via por la que se consulta ``IG_SESSIONID``,
    para no dejar el token en el historial de la shell.
    """
    if value is None:
        return None
    if value.strip():
        return _normalize_session_id(value)
    from_env = os.environ.get(SESSION_ENV_VAR, "").strip()
    return _normalize_session_id(from_env) if from_env else None


def _normalize_session_id(value: str) -> str:
    """Limpia el token: quita comillas y decodifica el %3A."""
    token = value.strip().strip('"').strip("'")
    if "%3A" in token or "%3a" in token:
        token = urllib.parse.unquote(token)
    return token


def print_profile(data: Dict[str, Any]) -> None:
    """Imprime el resumen del perfil."""
    print("Username:      {0}".format(data["username"]))
    print("Full Name:     {0}".format(data["full_name"]))
    print("Biography:     {0}".format(data["biography"]))
    print("Followers:     {0}".format(data["followers"]))
    print("Following:     {0}".format(data["following"]))
    print("Posts:         {0}".format(data["posts"]))
    print("Private:       {0}".format(data["is_private"]))
    print("Verified:      {0}".format(data["is_verified"]))
    print("Business:      {0}".format(data.get("is_business_account", False)))
    if data.get("business_category_name"):
        print("Category:      {0}".format(data["business_category_name"]))
    if data.get("email"):
        print("Email:         {0}".format(data["email"]))
    if data.get("phone_number"):
        print("Phone:         {0}".format(data["phone_number"]))
    if data.get("external_url"):
        print("External URL:  {0}".format(data["external_url"]))
    print("Profile Pic:   {0}".format(data["profile_pic_url"]))
    print("URL:           {0}".format(data["url"]))
    print("Source:        {0}".format(data.get("source", "anonymous")))

    friendship = data.get("friendship_status")
    if friendship:
        print("\n-- Relacion contigo --")
        for key in (
            "following",
            "followed_by",
            "blocking",
            "is_restricted",
            "is_bestie",
            "muting",
            "is_feed_favorite",
        ):
            if key in friendship:
                print("{0:16} : {1}".format(key, friendship[key]))
    if data.get("mutual_followers"):
        print("Mutual followers: {0}".format(data["mutual_followers"]))
    if data.get("context_links"):
        print("Context links:   {0}".format(", ".join(data["context_links"])))


def print_posts(posts: List[Dict[str, Any]]) -> None:
    """Imprime las publicaciones cargadas."""
    print("\nPublicaciones cargadas: {0}".format(len(posts)))
    for post in posts:
        print("\n  [{0}] {1}".format(post.get("media_type"), post.get("url")))
        if post.get("accessibility_caption"):
            print("  {0}".format(post["accessibility_caption"]))
        if post.get("caption"):
            print("  Caption: {0}".format(post["caption"]))
        if post.get("like_count") is not None:
            print(
                "  Likes: {0} | Comments: {1}".format(
                    post.get("like_count"), post.get("comment_count")
                )
            )


def parse_formats(value: Optional[str]) -> List[str]:
    """Parsea ``--output json,csv`` en una lista de formatos validos."""
    if not value:
        return []
    formats = []
    for chunk in value.split(","):
        name = chunk.strip().lower()
        if not name:
            continue
        if name not in OUTPUT_FORMATS:
            raise ValueError(
                "formato desconocido: '{0}' (validos: {1})".format(
                    name, ", ".join(OUTPUT_FORMATS)
                )
            )
        if name not in formats:
            formats.append(name)
    return formats


def _cell(value: Any) -> str:
    """Convierte un valor a celda CSV."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def profile_to_csv(data: Dict[str, Any]) -> str:
    """Perfil como una fila (una columna por campo del esquema)."""
    columns = [f for f in ig_client.PROFILE_FIELDS if f != "posts_detail"]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    writer.writerow([_cell(data.get(column)) for column in columns])
    return buffer.getvalue()


def posts_to_csv(data: Dict[str, Any]) -> str:
    """Publicaciones como tabla (una fila por publicacion)."""
    posts = data.get("posts_detail") or []
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(list(ig_client.POST_FIELDS))
    for post in posts:
        writer.writerow([_cell(post.get(field)) for field in ig_client.POST_FIELDS])
    return buffer.getvalue()


def write_outputs(
    data: Dict[str, Any],
    formats: List[str],
    folder: str,
    tags: Dict[str, int],
) -> List[str]:
    """Escribe los formatos pedidos en una carpeta. Devuelve los archivos."""
    target = os.path.join(os.getcwd(), folder)
    os.makedirs(target, exist_ok=True)
    written = []

    if "json" in formats:
        path = os.path.join(target, "profile.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        written.append(path)

    if "csv" in formats:
        path = os.path.join(target, "profile.csv")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(profile_to_csv(data))
        written.append(path)

        path = os.path.join(target, "posts.csv")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(posts_to_csv(data))
        written.append(path)

        path = os.path.join(target, "tags.csv")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["tag", "count"])
            for tag, count in tags.items():
                writer.writerow([tag, count])
        written.append(path)

    path = os.path.join(target, "tags.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(tags, handle, ensure_ascii=False, indent=2)
    written.append(path)

    return written


def save_folder(
    data: Dict[str, Any],
    folder: str,
    tags: Dict[str, int],
    scraper: Optional[Scraper] = None,
) -> str:
    """Guarda foto de perfil, datos del perfil, posts y tags en una carpeta."""
    target = os.path.join(os.getcwd(), folder)
    os.makedirs(target, exist_ok=True)

    pic_url = data.get("profile_pic_url")
    if pic_url:
        try:
            session = scraper._http if scraper else ig_client.build_session()
            response = session.get(pic_url, timeout=30)
            if response.status_code == 200:
                with open(os.path.join(target, "profile_pic.jpg"), "wb") as handle:
                    handle.write(response.content)
                print("{0}[+]{1} saved pic to {2}/profile_pic.jpg".format(GR, NU, target))
        except Exception as exc:
            print("{0}[!]{1} no se pudo descargar la foto de perfil: {2}".format(RED, NU, exc))

    profile_data = {
        "username": data.get("username"),
        "name": data.get("full_name"),
        "url": data.get("url"),
        "followers": data.get("followers"),
        "following": data.get("following"),
        "posts": data.get("posts"),
        "bio": data.get("biography"),
        "external url": data.get("external_url"),
        "private": data.get("is_private"),
        "verified": data.get("is_verified"),
        "business account": data.get("is_business_account"),
        "business category": data.get("business_category_name"),
        "email": data.get("email"),
        "phone": data.get("phone_number"),
        "profile pic url": data.get("profile_pic_url"),
        "posts loaded": data.get("posts_loaded"),
        "data source": data.get("source"),
    }
    with open(os.path.join(target, "profile_data.txt"), "w", encoding="utf-8") as handle:
        json.dump(profile_data, handle, ensure_ascii=False, indent=2)
    print("{0}[+]{1} saved data to {2}/profile_data.txt".format(GR, NU, target))

    posts = {}
    for index, post in enumerate(data.get("posts_detail") or []):
        posts[index] = {
            "code": post.get("code"),
            "url": post.get("url"),
            "type": post.get("media_type"),
            "caption": post.get("caption"),
            "accessibility caption": post.get("accessibility_caption"),
            "picture": post.get("display_uri"),
            "likes": post.get("like_count"),
            "comments": post.get("comment_count"),
            "timestamp": post.get("taken_at"),
            "video": post.get("video_url"),
        }
    with open(os.path.join(target, "posts_data.txt"), "w", encoding="utf-8") as handle:
        json.dump(posts, handle, ensure_ascii=False, indent=2)
    print("{0}[+]{1} saved post info to {2}/posts_data.txt".format(GR, NU, target))

    with open(os.path.join(target, "tags.txt"), "w", encoding="utf-8") as handle:
        json.dump(tags, handle, ensure_ascii=False, indent=2)
    print("{0}[+]{1} saved tags to {2}/tags.txt".format(GR, NU, target))

    return target


def print_stories(data: Dict[str, Any]) -> None:
    """Muestra las historias destacadas y el estado de la story activa."""
    stories = data.get("stories") or []
    print("\n-- Historias --")
    print("Destacadas:      {0}".format(data.get("stories_count", len(stories))))
    active = data.get("stories_active") or {}
    if active.get("has_active_story"):
        print("Story activa:   si (expira {0})".format(active.get("expiring_at")))
    else:
        print("Story activa:   no")
    for story in stories:
        print("\n  [{0}] {1}".format(story.get("id"), story.get("title") or "(sin titulo)"))
        if story.get("url"):
            print("  {0}".format(story["url"]))
        if story.get("cover_media_taken_at"):
            print("  medio de la portada: {0}".format(story["cover_media_taken_at"]))


def print_campaign(analysis: Dict[str, Any]) -> None:
    """Muestra el analisis de campana."""
    signals = analysis.get("signals") or {}
    print("\n-- Analisis de campana --")
    print("Probabilidad:    {0}".format(analysis.get("campaign_likelihood")))
    triggered = analysis.get("signals_triggered") or []
    print("Senales:        {0}".format(", ".join(triggered) if triggered else "ninguna"))

    print("\nPublicaciones cargadas: {0} en {1} dias ({2}/dia)".format(
        signals.get("posts_loaded"), signals.get("window_days"),
        signals.get("posts_per_day")))
    if signals.get("median_likes") is not None:
        print("Likes mediana:  {0} | maximo: {1} | ratio de pico: {2}".format(
            signals.get("median_likes"), signals.get("max_likes"),
            signals.get("spike_ratio")))
        if signals.get("spike_posts"):
            print("Posts con pico: {0}".format(", ".join(signals["spike_posts"])))

    tags = signals.get("hashtags") or []
    if tags:
        print("\nHashtags concentrados:")
        for tag in tags[:8]:
            print("  #{0}: {1} ({2})".format(
                tag["tag"], tag["count"], "{0:.0%}".format(tag["share"])))

    titles = signals.get("stories_titles") or []
    if titles:
        print("\nTitulos de destacadas: {0}".format(", ".join(titles)))

    top = analysis.get("top_posts_by_impact") or []
    if top:
        print("\nPublicaciones de mayor impacto:")
        for post in top[:5]:
            print("  {0} likes={1} comments={2} score={3}".format(
                post.get("code"), post.get("likes"), post.get("comments"),
                post.get("impact_score")))
            print("    {0}".format(post.get("url")))
            if post.get("caption"):
                print("    {0}".format(post["caption"][:120]))


def print_baseline(profile: Dict[str, Any], store) -> None:
    """Compara el perfil actual contra el historico."""
    from lib import history

    records = store.load(profile.get("username") or "")
    result = history.compare(profile, records)

    print("\n-- Baseline historico --")
    if result.get("error"):
        print(result["error"])
        return
    print("Instantaneas:    {0} (desde {1})".format(
        result.get("snapshots"), result.get("first_recorded_at")))
    print("Dias desde la ultima: {0}".format(result.get("days_since_last")))

    print("\nDeltas:")
    for field, delta in (result.get("deltas") or {}).items():
        print("  {0:14} {1} -> {2}  ({3:+d}, {4}/dia)".format(
            field, delta["before"], delta["now"], delta["delta"],
            delta["per_day"] if delta["per_day"] is not None else "n/d"))

    velocity = result.get("velocity") or {}
    print("\nVelocidad de publicacion:")
    print("  actual {0}/dia | linea base {1}/dia | ratio {2}".format(
        velocity.get("current"), velocity.get("baseline"), velocity.get("ratio")))
    engagement = result.get("engagement") or {}
    print("Interaccion mediana: actual {0} | base {1} | ratio {2}".format(
        engagement.get("current_median_likes"),
        engagement.get("baseline_median_likes"),
        engagement.get("ratio")))
    print("\nCampana detectada: {0}".format(result.get("campaign_started")))
    for note in result.get("notes") or []:
        print("  - {0}".format(note))


def print_history(profile: Dict[str, Any], store) -> None:
    """Muestra la serie de instantaneas del perfil."""
    records = store.load(profile.get("username") or "")
    print("\n-- Historico --")
    if not records:
        print("Sin instantaneas. Usa --record para empezar.")
        return
    print("{0:22} {1:>12} {2:>10} {3:>8} {4:>10}".format(
        "fecha", "followers", "posts", "stories", "velocidad"))
    for record in records:
        stats = record.get("stats") or {}
        print("{0:22} {1:>12} {2:>10} {3:>8} {4:>10}".format(
            record.get("recorded_at"),
            record.get("followers"),
            record.get("posts"),
            record.get("stories_count"),
            stats.get("posts_per_day")))


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scraper de perfiles publicos de Instagram (sin login)."
    )
    parser.add_argument(
        "-u", "--username", "--user", default=DEFAULT_USERNAME,
        dest="username",
        help="usuario a scrapear (default: {0})".format(DEFAULT_USERNAME),
    )
    parser.add_argument(
        "-p", "--posts", action="store_true",
        help="mostrar las publicaciones cargadas",
    )
    parser.add_argument(
        "-t", "--tags", action="store_true",
        help="mostrar los hashtags mas usados",
    )
    parser.add_argument(
        "-o", "--output", metavar="FORMATS",
        help="formatos a generar: {0} (separados por coma). Sin --save-dir "
             "se imprimen por stdout".format(",".join(OUTPUT_FORMATS)),
    )
    parser.add_argument(
        "-s", "--save", metavar="FILE",
        help="guardar los datos crudos en un archivo JSON",
    )
    parser.add_argument(
        "-S", "--save-dir", metavar="FOLDER",
        help="guardar los formatos pedidos en una carpeta",
    )
    parser.add_argument(
        "--session_id", metavar="ID", nargs="?", const="",
        help="usar una sesion (sessionid de Instagram). Sin valor se lee "
             "de {0}".format(SESSION_ENV_VAR),
    )
    parser.add_argument(
        "--mode", choices=[m.value for m in Mode], default=Mode.AUTO.value,
        help="como obtener los datos: anonymous (sin sesion) o session "
             "(GraphQL con sesion). Default: auto",
    )
    parser.add_argument(
        "--timeout", type=int, default=None, help="timeout de la peticion en segundos",
    )
    parser.add_argument(
        "--stories", action="store_true",
        help="mostrar las historias destacadas y si hay una story activa",
    )
    parser.add_argument(
        "--campaign", action="store_true",
        help="analizar senales de campana publicitaria",
    )
    parser.add_argument(
        "--views", action="store_true",
        help="consultar las visualizaciones de los reels (una peticion extra)",
    )
    parser.add_argument(
        "--record", action="store_true",
        help="guardar una instantanea del perfil para el baseline historico",
    )
    parser.add_argument(
        "--baseline", action="store_true",
        help="comparar el perfil actual contra el historico guardado",
    )
    parser.add_argument(
        "--history", action="store_true",
        help="mostrar el historico de instantaneas del perfil",
    )
    parser.add_argument(
        "--history-dir", metavar="DIR",
        help="donde guardar el historico (default: ~/.osintegra/history)",
    )
    parser.add_argument(
        "--own-account", action="store_true",
        help="mostrar los datos de la cuenta propia (requiere sesion)",
    )
    parser.add_argument(
        "--banner", action="store_true", help="mostrar el banner del proyecto",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="log detallado",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        formats = parse_formats(args.output)
    except ValueError as exc:
        print("[!] {0}".format(exc), file=sys.stderr)
        return 1

    if args.banner:
        banner()

    session_id = resolve_session_id(args.session_id)
    mode = Mode(args.mode)
    if mode is Mode.SESSION and not session_id:
        print("[!] El modo session necesita un sessionid.", file=sys.stderr)
        return 1

    with Scraper(with_views=args.views) as scraper:
        try:
            data = scraper.scrape(
                args.username, session_id=session_id, mode=mode
            )
        except InstagramError as exc:
            print("[!] {0}".format(exc), file=sys.stderr)
            return 1

        tags = scraper.hashtags(data)

        if args.save_dir:
            # Destino en carpeta: se escriben los formatos pedidos.
            written = write_outputs(
                data, formats or ["json"], args.save_dir, tags
            )
            for path in written:
                print("{0}[+]{1} saved {2}".format(GR, NU, path))
            save_folder(data, args.save_dir, tags, scraper)
        elif formats:
            # Sin destino: los formatos se imprimen por stdout.
            for fmt in formats:
                if fmt == "json":
                    print(json.dumps(data, ensure_ascii=False, indent=2))
                else:
                    sys.stdout.write(profile_to_csv(data))
                    sys.stdout.write(posts_to_csv(data))
            return 0
        else:
            # Modo legible: resumen en pantalla.
            print_profile(data)

            if args.posts:
                print_posts(data["posts_detail"])

            if args.tags:
                print("\nHashtags mas usados:")
                for tag, count in list(tags.items())[:15]:
                    print("  #{0} : {1}".format(tag, count))

            if args.stories:
                print_stories(data)

            if args.campaign:
                print_campaign(scraper.campaign_analysis(data))

            if args.record:
                record = scraper.record(
                    data, store=scraper.store(args.history_dir)
                )
                print("\n[+] Instantanea guardada: {0}".format(record["_path"]))

            if args.baseline:
                print_baseline(data, scraper.store(args.history_dir))

            if args.history:
                print_history(data, scraper.store(args.history_dir))

            if args.own_account:
                if not session_id:
                    print(
                        "[!] --own-account necesita un sessionid.",
                        file=sys.stderr,
                    )
                    return 1
                try:
                    account = scraper.own_account(session_id)
                except InstagramError as exc:
                    print("[!] {0}".format(exc), file=sys.stderr)
                    return 1
                print("\n-- Cuenta propia (sesion) --")
                for key, value in account.items():
                    print("{0:24} : {1}".format(key, value))

    if args.save:
        with open(args.save, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        print("\n[+] Datos guardados en {0}".format(args.save))

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Командная строка.

  python -m dvhectare parcel 25:28:010013:31
  python -m dvhectare here --lat 43.1155 --lon 131.8855
  python -m dvhectare zones --center 43.35,132.18 --radius-km 1
  python -m dvhectare scan --center 43.35,132.18 --radius-km 1
  python -m dvhectare scan --bbox 132.16,43.34,132.20,43.36
  python -m dvhectare agent "Найди свободные гектары возле Артёма"
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .analysis import Scanner
from .config import load_settings
from .export import save_scan
from .geo import bbox_around, parse_bbox
from .nspd.client import NspdClient


def _client(s) -> NspdClient:
    n = s.nspd
    return NspdClient(
        timeout=n.timeout,
        min_delay=n.min_delay,
        block_cooldown=n.block_cooldown_min * 60,
        retries=n.retries,
        cache_path=n.cache_path,
        cache_ttl=n.cache_ttl_days * 86400,
        ca_bundle=n.ca_bundle,
        proxy=n.proxy,
    )


def _bbox(args):
    if args.bbox:
        return parse_bbox(args.bbox)
    if args.center:
        lat, lon = (float(x) for x in args.center.split(","))
        return bbox_around(lat, lon, args.radius_km)
    raise SystemExit("Укажите --bbox lon1,lat1,lon2,lat2 или --center lat,lon [--radius-km N]")


def diagnose(s, out=print) -> list[str]:
    """Проверка связи с НСПД так же, как ходит программа: DNS, внешний IP, страница карты, API."""
    import os
    import socket

    import httpx

    from .nspd.client import BASE_URL, DEFAULT_HEADERS, NspdClient, blocked_seconds_left

    lines: list[str] = []

    def say(msg: str) -> None:
        lines.append(msg)
        out(msg)

    say(f"Прокси для НСПД в config.yaml: {s.nspd.proxy or 'нет (напрямую)'}")
    env = {k: v for k, v in os.environ.items() if k.lower() in ("http_proxy", "https_proxy", "all_proxy")}
    say(f"Системные переменные прокси: {env or 'нет'} (клиент НСПД их игнорирует)")
    try:
        say("DNS nspd.gov.ru: " + ", ".join(sorted(set(socket.gethostbyname_ex('nspd.gov.ru')[2]))))
    except Exception as e:
        say(f"DNS nspd.gov.ru: ОШИБКА {e}")

    kw = dict(timeout=10, trust_env=False, headers={"user-agent": DEFAULT_HEADERS["user-agent"]})
    if s.nspd.proxy:
        kw["proxy"] = s.nspd.proxy
    country = None
    try:
        with httpx.Client(**kw) as h:
            info = h.get("https://ipinfo.io/json").json()
        country = info.get("country")
        say(f"Внешний IP для прочих сайтов (ipinfo.io): {info.get('ip')} · {country} · {info.get('org', '')}")
    except Exception as e:
        say(f"Внешний IP: не удалось узнать ({type(e).__name__})")

    say(f"Пауза после 403 в этом запуске: {blocked_seconds_left()} с")
    c = NspdClient(timeout=s.nspd.timeout, min_delay=1.0, retries=0, cache_path=None,
                   ca_bundle=s.nspd.ca_bundle, proxy=s.nspd.proxy)
    try:
        r = c.warmup()
        if r is None:
            say("Страница карты: нет соединения (таймаут / сеть)")
        else:
            say(f"Страница карты {BASE_URL}/map: {r.status_code} · server={r.headers.get('server', '?')} · "
                f"cookies: {', '.join(c._http.cookies.keys()) or 'нет'}")
        try:
            r2 = c._http.get("/api/geoportal/v2/search/geoportal",
                             params={"thematicSearchId": 1, "query": "25:28:010013:31"})
            say(f"API поиска: {r2.status_code} · {r2.headers.get('content-type', '?')}")
            say("  ответ: " + " ".join(r2.text[:400].split()))
            if r2.status_code == 200:
                say("✓ НСПД отвечает программе нормально.")
                if country and country != "RU":
                    say("  (прочие сайты идут через VPN, а НСПД — напрямую по правилу маршрутизации: так и должно быть)")
            elif country and country != "RU":
                say("  ⚠ Внешний IP не российский — вероятно, и НСПД идёт через VPN. Проверьте правило маршрутизации.")
        except Exception as e:
            say(f"API поиска: ОШИБКА {type(e).__name__}: {e}")
    finally:
        c.close()
    return lines


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="dvhectare", description="Поиск земли под ДВ-гектар по данным НСПД")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("parcel", help="участок по кадастровому номеру")
    sp.add_argument("cad_number")

    sp = sub.add_parser("here", help="что находится в точке")
    sp.add_argument("--lat", type=float, required=True)
    sp.add_argument("--lon", type=float, required=True)
    sp.add_argument("--layers", default="parcels,terr_zones,zouit,oopt,settlements,forestry")

    for name, hlp in (("zones", "территориальные зоны с ВРИ"), ("scan", "поиск свободных гектаров")):
        sp = sub.add_parser(name, help=hlp)
        sp.add_argument("--bbox", help="lon_min,lat_min,lon_max,lat_max")
        sp.add_argument("--center", help="lat,lon")
        sp.add_argument("--radius-km", type=float, default=1.0)
        if name == "scan":
            sp.add_argument("--zone", action="append", default=None, help="регулярка по названию зоны (можно несколько)")
            sp.add_argument("--no-html", action="store_true")

    sub.add_parser("diag", help="проверить связь с НСПД (IP, страница карты, API) и сохранить отчёт в diag.txt")

    sp = sub.add_parser("agent", help="диалоговый агент на Gemini API")
    sp.add_argument("prompt")
    sp.add_argument("--model", default=None)
    sp.add_argument("--no-map", action="store_true", help="не строить итоговую карту")
    sp.add_argument("--no-open", action="store_true", help="не открывать карту в браузере")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    s = load_settings(args.config)
    if args.cmd == "diag":
        from pathlib import Path

        lines = diagnose(s)
        Path("diag.txt").write_text("\n".join(lines), encoding="utf-8")
        print("\nОтчёт сохранён в diag.txt")
        return 0

    with _client(s) as client:
        scanner = Scanner(client, s)
        if args.cmd == "parcel":
            from .agent.tools import make_handlers

            _print(make_handlers(scanner, s.output_dir)["get_parcel"](args.cad_number))
        elif args.cmd == "here":
            from .agent.tools import make_handlers

            _print(make_handlers(scanner, s.output_dir)["what_is_here"](args.lat, args.lon, args.layers.split(",")))
        elif args.cmd == "zones":
            zs = scanner.zones(_bbox(args))
            _print([{"title": f["properties"]["_title"], "permitted_uses": f["properties"].get("_permitted_uses")} for f in zs])
        elif args.cmd == "scan":
            if args.zone:
                s.scan.allowed_zone_patterns = args.zone
            res = scanner.scan(_bbox(args))
            out = save_scan(res, s.output_dir, html=not args.no_html)
            _print(res.stats)
            print(f"\nРезультаты: {out}")
            for c in res.candidates[:10]:
                print(f"  #{c.id}: {c.lat:.6f}, {c.lon:.6f}  зоны={c.zones or '-'}  флаги={c.flags or '-'}")
        elif args.cmd == "agent":
            from .agent import run_agent

            g = s.gemini
            print(
                run_agent(
                    args.prompt, scanner, s.output_dir,
                    model=args.model or g.model, proxy=g.proxy, timeout=g.timeout, api_key=g.api_key, fallback_models=g.fallback_models,
                    # флаги командной строки важнее конфига
                    make_map=s.agent.make_map and not args.no_map,
                    open_map=s.agent.open_map and not args.no_open,
                )
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())

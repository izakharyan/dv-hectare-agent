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

    sp = sub.add_parser("agent", help="диалоговый агент на Claude API")
    sp.add_argument("prompt")
    sp.add_argument("--model", default=None)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    s = load_settings(args.config)

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

            print(run_agent(args.prompt, scanner, s.output_dir, model=args.model))
    return 0


if __name__ == "__main__":
    sys.exit(main())

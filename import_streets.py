#!/usr/bin/env python3
"""
Erzeugt aus config/bezirke.json eine static/bezirke.geojson, indem die
Straßengeometrien per Overpass-API aus OpenStreetMap geholt werden.

Aufruf:  python3 import_streets.py
Einmalig ausführen (und erneut, wenn sich Bezirke/Straßen ändern).
Alle Straßen werden in EINER Anfrage geholt; bei 429/504 wird mit Wartezeit
wiederholt und auf Ausweich-Server gewechselt.

Einträge in "strassen" sind entweder ein Straßenname (ganze Straße) oder ein
Abschnitt zwischen zwei Querstraßen:
  {"name": "Hauptstraße", "von": "Rathausstraße", "bis": "Annostraße"}
Statt einer Querstraße geht auch "Bahn" (Bahnübergang). Ein optionaler "zusatz"
erscheint in der Anzeige, z. B. {"name": "Niederkasseler Straße", "zusatz": "südliche Seite"}.
Mit "ortsteil" im Bezirk werden nur Straßen innerhalb dieser Ortsteilgrenze (OSM) verwendet.
"""
import heapq
import os
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
CONFIG = Path(os.environ.get("BEZIRKE_CONFIG", BASE / "config" / "bezirke.json"))
OUT = Path(os.environ.get("GEOJSON_PATH", BASE / "static" / "bezirke.geojson"))
SERVER = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
VERSUCHE = 6
MAX_ABSTAND_M = 40  # Querstraße ohne gemeinsamen OSM-Knoten: so nah muss sie der Straße kommen
MAX_LUECKE_M = 60   # Lücken im Straßenverlauf (z. B. unbenannter Kreisverkehr) bis zu dieser Länge überbrücken
BAHN = "Bahn"       # als "von"/"bis": Kreuzung mit einer Bahnlinie
FELDWEGE = {"track", "path", "footway", "cycleway", "bridleway"}  # werden weggelassen, wenn die Straße auch andere Wege hat


def overpass(query: str) -> dict:
    data = urllib.parse.urlencode({"data": query}).encode()
    letzter = None
    for i in range(VERSUCHE):
        url = SERVER[i % len(SERVER)]
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": "losverkauf-import/1.1"})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            letzter = e
            warte = 10 * (i + 1)
            print(f"  {url.split('/')[2]}: {e} – neuer Versuch in {warte}s", flush=True)
            time.sleep(warte)
    raise SystemExit(f"Overpass nicht erreichbar: {letzter}")


def fetch_all(gemeinde: str, gruppen: dict[str | None, set[str]], bahn: bool) -> dict[tuple, list[dict]]:
    """Liefert {(ortsteil, straßenname): [way, ...]} mit Geometrie, Knoten-IDs und Tags.

    Ortsteil None = ganze Gemeinde. Bahnlinien stehen unter (None, BAHN)."""
    teile = []
    for ortsteil, names in gruppen.items():
        regex = "^(" + "|".join(re.escape(n) for n in sorted(names)) + ")$"
        if ortsteil is None:
            teile.append(f'way["highway"]["name"~"{regex}"](area.g);out geom;out count;')
        else:
            teile.append(f'rel["boundary"="administrative"]["admin_level"~"^(9|10)$"]["name"="{ortsteil}"](area.g)->.r;'
                         f'.r out ids;.r map_to_area->.o;way["highway"]["name"~"{regex}"](area.o);out geom;out count;')
    if bahn:
        teile.append('way["railway"~"^(rail|light_rail|narrow_gauge|tram)$"](area.g);out geom;out count;')
    q = f"""[out:json][timeout:170];
area["boundary"="administrative"]["admin_level"="8"]["name"="{gemeinde}"]->.g;
""" + "\n".join(teile)
    abschnitte, aktuell = [], []
    for el in overpass(q).get("elements", []):
        if el["type"] == "count":
            abschnitte.append(aktuell)
            aktuell = []
        else:
            aktuell.append(el)

    streets: dict[tuple, list] = defaultdict(list)
    for (ortsteil, names), els in zip(gruppen.items(), abschnitte):
        if ortsteil is not None and not any(e["type"] == "relation" for e in els):
            raise SystemExit(f"Ortsteil „{ortsteil}“ hat in OSM keine Grenze (admin_level 9/10) – Schreibweise prüfen "
                             "oder 'ortsteil' im Bezirk weglassen.")
        for el in els:
            name = el.get("tags", {}).get("name")
            if el["type"] == "way" and name in names:
                streets[(ortsteil, name)].append(el)
    if bahn:
        streets[(None, BAHN)] = abschnitte[len(gruppen)]
    return streets


def centroid(lines: list) -> list[float] | None:
    pts = [p for ln in lines for p in ln]
    if not pts:
        return None
    return [sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)]


# ---------- Straßeneinträge ----------
def eintrag(e: str | dict) -> dict:
    """'Name' oder {"name", "von", "bis", "zusatz"} → {"name", "von", "bis", "zusatz"}."""
    if isinstance(e, str):
        return {"name": e, "von": None, "bis": None, "zusatz": None}
    return {"name": e["name"], "von": e.get("von"), "bis": e.get("bis"), "zusatz": e.get("zusatz")}


def anzeige(s: dict) -> str:
    teile = [f"{s['von'] or '?'} – {s['bis'] or '?'}"] if s["von"] or s["bis"] else []
    teile += [s["zusatz"]] if s["zusatz"] else []
    return f"{s['name']} ({', '.join(teile)})" if teile else s["name"]


# ---------- Geometrie ----------
def punkte(way: dict) -> list[tuple[float, float]]:
    return [(p["lon"], p["lat"]) for p in way.get("geometry", [])]


def meter(a: tuple, b: tuple) -> float:
    k = math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((a[0] - b[0]) * k * 111_320, (a[1] - b[1]) * 110_574)


def abstand_zur_strecke(p: tuple, a: tuple, b: tuple) -> float:
    k = math.cos(math.radians(p[1]))
    ax, ay = (a[0] - p[0]) * k * 111_320, (a[1] - p[1]) * 110_574
    bx, by = (b[0] - p[0]) * k * 111_320, (b[1] - p[1]) * 110_574
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / l2))
    return math.hypot(ax + t * dx, ay + t * dy)


def kreuzung(ways: list[dict], quer: list[dict]) -> set[int]:
    """Knoten-IDs der Straße, an denen die Querstraße anschließt."""
    knoten = {n for w in ways for n in w["nodes"]}
    gemeinsam = knoten & {n for w in quer for n in w["nodes"]}
    if gemeinsam:
        return gemeinsam
    # Fallback, falls die Wege in OSM nicht verbunden sind: nächstgelegener Punkt der Straße
    strecken = [s for w in quer for s in zip(punkte(w), punkte(w)[1:])]
    kandidaten = [(min(abstand_zur_strecke(p, a, b) for a, b in strecken), n)
                  for w in ways for n, p in zip(w["nodes"], punkte(w))] if strecken else []
    if kandidaten:
        d, n = min(kandidaten)
        if d <= MAX_ABSTAND_M:
            return {n}
    return set()


def ueberbruecke_luecken(pos: dict, nachbarn: dict) -> None:
    """Verbindet getrennte Teile der Straße an ihrer engsten Stelle, falls die Lücke kurz ist."""
    teil, teile = {}, []
    for s in pos:
        if s in teil:
            continue
        teile.append([s])
        teil[s] = len(teile) - 1
        stapel = [s]
        while stapel:
            for m, _ in nachbarn[stapel.pop()]:
                if m not in teil:
                    teil[m] = teil[s]
                    teile[-1].append(m)
                    stapel.append(m)
    for i, a in enumerate(teile):
        for b in teile[i + 1:]:
            d, x, y = min((meter(pos[x], pos[y]), x, y) for x in a for y in b)
            if d <= MAX_LUECKE_M:
                nachbarn[x].append((y, d))
                nachbarn[y].append((x, d))


def abschnitt(ways: list[dict], start: set[int], ziel: set[int]) -> list[list[float]] | None:
    """Kürzester Weg entlang der Straße von einem Start- zu einem Zielknoten (Dijkstra)."""
    pos, nachbarn = {}, defaultdict(list)
    for w in ways:
        pos.update(zip(w["nodes"], punkte(w)))
        for a, b in zip(w["nodes"], w["nodes"][1:]):
            d = meter(pos[a], pos[b])
            nachbarn[a].append((b, d))
            nachbarn[b].append((a, d))
    ueberbruecke_luecken(pos, nachbarn)
    dist = {n: 0.0 for n in start}
    vor = {}
    heap = [(0.0, n) for n in start]
    heapq.heapify(heap)
    while heap:
        d, n = heapq.heappop(heap)
        if d > dist[n]:
            continue
        if n in ziel:
            pfad = [n]
            while pfad[-1] in vor:
                pfad.append(vor[pfad[-1]])
            return [list(pos[x]) for x in reversed(pfad)]
        for m, l in nachbarn[n]:
            if d + l < dist.get(m, math.inf):
                dist[m] = d + l
                vor[m] = n
                heapq.heappush(heap, (d + l, m))
    return None


def teilstueck(s: dict, ways: list[dict], finde) -> tuple[list | None, str | None]:
    """Liefert (Linie, None) oder (None, Fehlermeldung) für einen Abschnitt von–bis."""
    if not (s["von"] and s["bis"]):
        return None, f"{s['name']}: bitte sowohl 'von' als auch 'bis' angeben"
    enden = []
    for q in (s["von"], s["bis"]):
        quer = finde(q)
        if not quer:
            return None, f"{s['name']}: {'keine Bahnlinie' if q == BAHN else f'Querstraße „{q}“ nicht'} in OSM gefunden"
        k = kreuzung(ways, quer)
        if not k:
            return None, f"{s['name']}: trifft nicht auf „{q}“"
        enden.append(k)
    linie = abschnitt(ways, *enden)
    if not linie or len(linie) < 2:
        return None, f"{s['name']}: kein durchgehender Abschnitt zwischen „{s['von']}“ und „{s['bis']}“"
    return linie, None


def main():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    gemeinde = cfg["gemeinde"]
    for b in cfg["bezirke"]:
        b["strassen"] = [eintrag(e) for e in b["strassen"]]
    gruppen: dict[str | None, set[str]] = defaultdict(set)
    for b in cfg["bezirke"]:
        gruppen[b.get("ortsteil")] |= {n for s in b["strassen"] for n in (s["name"], s["von"], s["bis"]) if n and n != BAHN}
    bahn = any(BAHN in (s["von"], s["bis"]) for b in cfg["bezirke"] for s in b["strassen"])
    anzahl = len({n for names in gruppen.values() for n in names})
    print(f"{anzahl} Straßen in {len(cfg['bezirke'])} Bezirken werden abgefragt …", flush=True)
    streets = fetch_all(gemeinde, dict(gruppen), bahn)

    features, probleme = [], []
    for b in cfg["bezirke"]:
        def finde(name, ortsteil=b.get("ortsteil")):
            return streets.get((None, BAHN) if name == BAHN else (ortsteil, name), [])

        lines = []
        for s in b["strassen"]:
            ways = finde(s["name"])
            ways = [w for w in ways if w["tags"].get("highway") not in FELDWEGE] or ways
            if not ways:
                probleme.append((b["id"], f"{s['name']}: nicht in OSM gefunden (Schreibweise prüfen)"))
            elif s["von"] or s["bis"]:
                linie, fehler = teilstueck(s, ways, finde)
                if fehler:
                    probleme.append((b["id"], fehler))
                else:
                    lines.append(linie)
            else:
                lines.extend([list(p) for p in punkte(w)] for w in ways)
        features.append({
            "type": "Feature",
            "properties": {
                "id": b["id"], "name": b["name"], "max": b.get("max", 2),
                "strassen": [anzeige(s) for s in b["strassen"]], "center": centroid(lines),
            },
            "geometry": {"type": "MultiLineString", "coordinates": lines},
        })
        print(f"  {b['id']}: {len(lines)} Straßenabschnitte")

    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False), encoding="utf-8")
    print(f"\n→ {OUT} geschrieben ({len(features)} Bezirke).")
    if probleme:
        print("\nProbleme (diese Straßen fehlen auf der Karte):")
        for bid, text in probleme:
            print(f"  {bid}: {text}")
        sys.exit(1)


if __name__ == "__main__":
    main()

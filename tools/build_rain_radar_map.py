"""Build custom_components/tab5_lvgl/rain_radar_map.json.gz.

The rain radar draws its own map, so it needs no map server. The data is
Natural Earth (public domain, https://www.naturalearthdata.com/about/terms-of-use/):

  python -I tools/build_rain_radar_map.py <countries-10m.json> <places-10m.geojson> <output>

countries-10m.json is the TopoJSON of the npm package world-atlas@2.0.2, the
places are ne_10m_populated_places.geojson of natural-earth-vector v5.1.2.

The font beside it (rain_radar_font.ttf) is a subset of Inter 4.1 (SIL Open Font
License, rain_radar_font.LICENSE.txt; docs/font-files of rsms/inter at tag v4.1)
with the map's Latin and Cyrillic letters:

  pyftsubset Inter-Regular.woff2 --unicodes=U+0020-007E,U+00A0-017F,U+0400-045F,U+2018-201E
    --layout-features= --no-hinting --desubroutinize --flavor= --output-file=rain_radar_font.ttf

Output: {"land": [ring...], "borders": [line...], "places": [place...]}. Rings
and lines hold hundredths of a degree, the first point absolute and the others
as differences: [x0, y0, dx1, dy1, ...]. A place is [x, y, rank, name, {language:
name}], with a name only for languages where it differs.
"""

from __future__ import annotations

import gzip
import json
import sys

# Bridge languages with names of their own in the data.
LANGUAGES = ("de", "es", "fr", "it", "nl", "pl", "pt", "ru", "sv")
# In hundredths of a degree; about the spacing of the 1:10m data.
SIMPLIFY = 1


def decode_arcs(topology):
  scale_x, scale_y = topology["transform"]["scale"]
  shift_x, shift_y = topology["transform"]["translate"]
  arcs = []
  for arc in topology["arcs"]:
    x = y = 0
    points = []
    for dx, dy in arc:
      x += dx
      y += dy
      points.append((round((x * scale_x + shift_x) * 100), round((y * scale_y + shift_y) * 100)))
    arcs.append(points)
  return arcs


def ring_points(indexes, arcs):
  points = []
  for index in indexes:
    arc = arcs[index] if index >= 0 else arcs[~index][::-1]
    points.extend(arc if not points else arc[1:])
  return points


def simplify(points, tolerance=SIMPLIFY):
  """Douglas-Peucker, iterative: long coastlines would overflow the stack."""
  if len(points) < 3:
    return points
  keep = {0, len(points) - 1}
  stack = [(0, len(points) - 1)]
  while stack:
    first, last = stack.pop()
    ax, ay = points[first]
    bx, by = points[last]
    dx, dy = bx - ax, by - ay
    length = (dx * dx + dy * dy) ** 0.5
    worst, worst_index = 0.0, None
    for index in range(first + 1, last):
      px, py = points[index]
      distance = (abs(dy * (px - ax) - dx * (py - ay)) / length if length
                  else ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5)
      if distance > worst:
        worst, worst_index = distance, index
    if worst_index is not None and worst > tolerance:
      keep.add(worst_index)
      stack.append((first, worst_index))
      stack.append((worst_index, last))
  return [points[index] for index in sorted(keep)]


def delta(points):
  flat = list(points[0])
  x, y = points[0]
  for px, py in points[1:]:
    flat += [px - x, py - y]
    x, y = px, py
  return flat


def polygons_of(geometry):
  if geometry["type"] == "Polygon":
    return [geometry["arcs"]]
  if geometry["type"] == "MultiPolygon":
    return geometry["arcs"]
  if geometry["type"] == "GeometryCollection":
    return [polygon for item in geometry["geometries"] for polygon in polygons_of(item)]
  return []


def build(countries_path, places_path):
  with open(countries_path, encoding="utf-8") as handle:
    topology = json.load(handle)
  arcs = decode_arcs(topology)

  land = []
  for polygon in polygons_of(topology["objects"]["land"]):
    for ring in polygon:
      points = simplify(ring_points(ring, arcs))
      if len(points) >= 4:
        land.append(delta(points))

  # An arc two countries share is a border; an arc used once is the coast.
  uses = {}
  for polygon in polygons_of(topology["objects"]["countries"]):
    for ring in polygon:
      for index in ring:
        key = index if index >= 0 else ~index
        uses[key] = uses.get(key, 0) + 1
  borders = [delta(simplify(arcs[index])) for index, count in sorted(uses.items()) if count > 1]

  with open(places_path, encoding="utf-8") as handle:
    collection = json.load(handle)
  places = []
  for feature in collection["features"]:
    props = feature["properties"]
    name = str(props.get("NAME") or "").strip()
    if not name:
      continue
    lon, lat = feature["geometry"]["coordinates"][:2]
    names = {}
    for language in LANGUAGES:
      local = str(props.get(f"NAME_{language.upper()}") or "").strip()
      if local and local != name:
        names[language] = local
    rank = (int(props.get("SCALERANK", 10)), -int(props.get("POP_MAX") or 0))
    places.append((rank, [round(lon * 100), round(lat * 100), rank[0], name, names]))
  # Most important first: the map labels as many as fit, in this order.
  places = [place for _rank, place in sorted(places, key=lambda item: item[0])]
  return {"land": land, "borders": borders, "places": places}


def main(argv):
  if len(argv) != 4:
    print(__doc__)
    return 2
  data = build(argv[1], argv[2])
  raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
  with open(argv[3], "wb") as handle:
    handle.write(gzip.compress(raw, 9, mtime=0))
  print(f"land {len(data['land'])} rings, borders {len(data['borders'])} lines, "
        f"places {len(data['places'])}, {len(raw)} bytes raw")
  return 0


if __name__ == "__main__":
  sys.exit(main(sys.argv))

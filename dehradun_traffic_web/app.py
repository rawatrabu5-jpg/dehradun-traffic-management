from flask import Flask, jsonify, request, send_from_directory
import json
import math
import os
import socket
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
import http.client
import gzip
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(ROOT, "frontend")
ENGINE = os.path.join(ROOT, "backend", "route_engine.exe" if os.name == "nt" else "route_engine")
CACHE_FILE = os.path.join(ROOT, "backend", "dehradun_osm_graph.json")
CACHE_TTL = 60 * 60 * 6  # refresh the downloaded road network every 6 hours

# Dehradun city-area bounding box: south, west, north, east.
BBOX = (30.24, 77.90, 30.42, 78.20)
OVERPASS_URLS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
]


class IPv4HTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that deliberately uses IPv4 addresses.

    Some cloud environments can resolve an Overpass hostname to IPv6 while
    having no working IPv6 route, which produces Errno 101 (Network is
    unreachable). This connection class avoids that failure mode.
    """

    def connect(self):
        timeout = self.timeout
        if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
            timeout = None

        last_error = None
        addresses = socket.getaddrinfo(
            self.host, self.port, socket.AF_INET, socket.SOCK_STREAM
        )
        for family, socktype, proto, _canonname, sockaddr in addresses:
            sock = socket.socket(family, socktype, proto)
            try:
                if timeout is not None:
                    sock.settimeout(timeout)
                sock.connect(sockaddr)
                self.sock = sock
                if self._tunnel_host:
                    self._tunnel()
                self.sock = self._context.wrap_socket(
                    self.sock, server_hostname=self.host
                )
                return
            except OSError as exc:
                last_error = exc
                sock.close()

        if last_error is not None:
            raise last_error
        raise OSError("No IPv4 address available for Overpass host")


class IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(IPv4HTTPSConnection, req, context=ssl.create_default_context())


IPV4_OPENER = urllib.request.build_opener(IPv4HTTPSHandler)

app = Flask(__name__, static_folder=FRONTEND, static_url_path="")

# Nominatim geocoding cache/rate-limit. Search is performed server-side so
# the browser does not need direct access to the geocoding service.
GEOCODE_CACHE = {}
LAST_GEOCODE_AT = 0.0


def haversine_km(a, b):
    lat1, lon1 = a
    lat2, lon2 = b
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x))


def default_speed(highway):
    return {
        "motorway": 80, "trunk": 65, "primary": 50, "secondary": 45,
        "tertiary": 40, "unclassified": 35, "residential": 30,
        "living_street": 15, "service": 20
    }.get(highway, 30)


def parse_speed(value, highway):
    if not value:
        return default_speed(highway)
    # Handles common OSM values such as "40", "40 km/h", "50 mph".
    text = str(value).lower().strip()
    digits = ""
    for ch in text:
        if ch.isdigit() or ch == ".":
            digits += ch
        elif digits:
            break
    try:
        speed = float(digits)
        if "mph" in text:
            speed *= 1.60934
        return max(5.0, min(speed, 130.0))
    except Exception:
        return default_speed(highway)


def overpass_query(bbox):
    """Build a compact road-only Overpass query for a bounding box."""
    s, w, n, e = bbox
    return f"""[out:json][timeout:20][maxsize:134217728];
way[\"highway\"][\"highway\"!~\"^(footway|path|cycleway|steps|pedestrian|track|construction|proposed|bridleway|corridor|raceway)$\"]({s},{w},{n},{e});
out geom qt;"""


def fetch_overpass_endpoint(endpoint, query, timeout=18):
    encoded = urllib.parse.urlencode({"data": query}).encode("utf-8")
    req = urllib.request.Request(endpoint, data=encoded, headers={
        "User-Agent": "DehradunTrafficPBL/5.0 (educational project)",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "Connection": "close",
    }, method="POST")
    with IPV4_OPENER.open(req, timeout=timeout) as response:
        raw = response.read()
        if response.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


def fetch_overpass_parallel(bbox):
    """Race several Overpass mirrors so one slow mirror cannot block the app."""
    query = overpass_query(bbox)
    errors = []
    with ThreadPoolExecutor(max_workers=len(OVERPASS_URLS)) as pool:
        futures = {pool.submit(fetch_overpass_endpoint, url, query): url for url in OVERPASS_URLS}
        for future in as_completed(futures):
            url = futures[future]
            try:
                data = future.result()
                if data.get("elements"):
                    return data
                errors.append(f"{url}: empty response")
            except Exception as exc:
                errors.append(f"{url}: {exc}")
    raise RuntimeError("All Overpass servers failed: " + " | ".join(errors))


def fetch_osm_graph(bbox=None):
    """Download a route-sized OSM road graph from OpenStreetMap."""
    if bbox is None:
        bbox = BBOX
    try:
        data = fetch_overpass_parallel(bbox)
        all_elements = data.get("elements", [])
    except Exception as first_error:
        south, west, north, east = bbox
        lat_mid = (south + north) / 2
        lon_mid = (west + east) / 2
        tiles = [
            (south, west, lat_mid, lon_mid), (south, lon_mid, lat_mid, east),
            (lat_mid, west, north, lon_mid), (lat_mid, lon_mid, north, east),
        ]
        all_elements, tile_errors = [], []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(fetch_overpass_parallel, tile): i for i, tile in enumerate(tiles, 1)}
            for future in as_completed(futures):
                i = futures[future]
                try:
                    all_elements.extend(future.result().get("elements", []))
                except Exception as exc:
                    tile_errors.append(f"tile {i}: {exc}")
        if not all_elements:
            raise RuntimeError(f"Road network download failed: {first_error}; fallback: {' | '.join(tile_errors)}")

    coord_to_idx, graph_nodes, edge_map = {}, [], {}
    def node_index(lat, lon):
        key = (round(float(lat), 7), round(float(lon), 7))
        idx = coord_to_idx.get(key)
        if idx is None:
            idx = len(graph_nodes); coord_to_idx[key] = idx; graph_nodes.append([key[0], key[1]])
        return idx

    for element in all_elements:
        if element.get("type") != "way": continue
        tags = element.get("tags", {}); highway = tags.get("highway")
        geometry = element.get("geometry", [])
        if not highway or len(geometry) < 2: continue
        speed = parse_speed(tags.get("maxspeed"), highway)
        oneway_value = str(tags.get("oneway", "")).lower()
        oneway = oneway_value in ("yes", "1", "true")
        if oneway_value == "-1": geometry = list(reversed(geometry)); oneway = True
        for a, b in zip(geometry, geometry[1:]):
            try: lat1, lon1 = float(a["lat"]), float(a["lon"]); lat2, lon2 = float(b["lat"]), float(b["lon"])
            except (KeyError, TypeError, ValueError): continue
            u, v = node_index(lat1, lon1), node_index(lat2, lon2)
            d = haversine_km((lat1, lon1), (lat2, lon2))
            if d <= 0 or d > 2: continue
            t = d / speed * 60.0
            key = (u, v, int(oneway)); old = edge_map.get(key)
            if old is None or t < old[1]: edge_map[key] = (d, t)

    graph_edges = [[u, v, d, t, one_way] for (u, v, one_way), (d, t) in edge_map.items()]
    graph = {"generatedAt": time.time(), "bbox": list(bbox), "nodes": graph_nodes, "edges": graph_edges,
             "source": "OpenStreetMap road data via Overpass API (route-sized dynamic graph)"}
    if len(graph_nodes) < 100 or len(graph_edges) < 100: raise RuntimeError("The road-network API returned too little road data.")
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f: json.dump(graph, f, separators=(",", ":"))
    return graph


def graph_covers_points(graph, points):
    if not graph or not points: return False
    south, west, north, east = graph.get("bbox", BBOX)
    return all(south <= p[0] <= north and west <= p[1] <= east for p in points)


def route_bbox(source, destination):
    min_lat, max_lat = min(source[0], destination[0]), max(source[0], destination[0])
    min_lon, max_lon = min(source[1], destination[1]), max(source[1], destination[1])
    lat_pad, lon_pad = max(0.012, (max_lat-min_lat)*0.25), max(0.015, (max_lon-min_lon)*0.25)
    return (max(BBOX[0], min_lat-lat_pad), max(BBOX[1], min_lon-lon_pad),
            min(BBOX[2], max_lat+lat_pad), min(BBOX[3], max_lon+lon_pad))


def load_graph(force=False, points=None):
    if not force and os.path.exists(CACHE_FILE):
        try:
            if time.time() - os.path.getmtime(CACHE_FILE) < CACHE_TTL:
                with open(CACHE_FILE, "r", encoding="utf-8") as f: graph = json.load(f)
                if not points or graph_covers_points(graph, points): return graph
        except Exception: pass
    bbox = route_bbox(points[0], points[1]) if points and len(points) == 2 else BBOX
    return fetch_osm_graph(bbox)

def write_cpp_graph(graph):
    """Write a compact text representation consumed by the C++ Dijkstra engine."""
    path = os.path.join(ROOT, "backend", "graph_input.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{len(graph['nodes'])}\n")
        for i, (lat, lon) in enumerate(graph["nodes"]):
            f.write(f"{i} {lat:.7f} {lon:.7f}\n")
        f.write(f"{len(graph['edges'])}\n")
        for u, v, d, t, one_way in graph["edges"]:
            f.write(f"{u} {v} {d:.6f} {t:.6f} {int(one_way)}\n")
    return path


def nearest_node(graph, point):
    best_i, best_d = None, float("inf")
    for i, node in enumerate(graph["nodes"]):
        d = haversine_km(point, node)
        if d < best_d:
            best_i, best_d = i, d
    return best_i, best_d


@app.get("/")
def index():
    return send_from_directory(FRONTEND, "index.html")


@app.get("/api/geocode")
def geocode():
    """Search for a place/address in or near Dehradun using Nominatim."""
    global LAST_GEOCODE_AT
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"error": "Enter at least 2 characters to search."}), 400

    # Prefer Dehradun results while still allowing nearby addresses.
    cache_key = q.lower()
    if cache_key in GEOCODE_CACHE:
        return jsonify({"results": GEOCODE_CACHE[cache_key]})

    # Nominatim asks clients to identify themselves and avoid heavy usage.
    wait = 1.0 - (time.time() - LAST_GEOCODE_AT)
    if wait > 0:
        time.sleep(wait)

    params = urllib.parse.urlencode({
        "q": q,
        "format": "jsonv2",
        "limit": "5",
        "countrycodes": "in",
        "viewbox": f"{BBOX[1]},{BBOX[2]},{BBOX[3]},{BBOX[0]}",
        "bounded": "1",
    })
    url = "https://nominatim.openstreetmap.org/search?" + params
    req = urllib.request.Request(url, headers={
        "User-Agent": "DehradunTrafficPBL/3.0 (educational project)",
        "Accept": "application/json",
    })
    try:
        with IPV4_OPENER.open(req, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        LAST_GEOCODE_AT = time.time()
    except Exception as exc:
        return jsonify({"error": f"Location search is temporarily unavailable: {exc}"}), 503

    results = []
    for item in data:
        try:
            lat, lon = float(item["lat"]), float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        # Nominatim is bounded above, but keep an explicit safety check.
        if BBOX[0] <= lat <= BBOX[2] and BBOX[1] <= lon <= BBOX[3]:
            results.append({
                "displayName": item.get("display_name", q),
                "lat": lat,
                "lon": lon,
                "type": item.get("type", ""),
            })

    GEOCODE_CACHE[cache_key] = results
    return jsonify({"results": results})


@app.get("/api/network")
def network():
    # Status-only endpoint: opening the page never starts an expensive Overpass download.
    if not os.path.exists(CACHE_FILE):
        return jsonify({"ready": False, "message": "Road network will be downloaded when you calculate a route."})
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f: graph = json.load(f)
        return jsonify({"ready": True, "nodeCount": len(graph.get("nodes", [])), "edgeCount": len(graph.get("edges", [])),
                        "bbox": graph.get("bbox", BBOX), "source": graph.get("source", "OpenStreetMap")})
    except Exception:
        return jsonify({"ready": False, "message": "Road network will be downloaded when you calculate a route."})

@app.post("/api/refresh-network")
def refresh_network():
    try:
        graph = load_graph(force=True)
        return jsonify({"ok": True, "nodeCount": len(graph["nodes"]), "edgeCount": len(graph["edges"])})
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@app.post("/api/route")
def route():
    data = request.get_json(silent=True) or {}
    source = data.get("source")
    destination = data.get("destination")
    traffic = data.get("traffic", [])
    closed = data.get("closed", [])

    try:
        if not (isinstance(source, list) and len(source) == 2 and isinstance(destination, list) and len(destination) == 2):
            return jsonify({"error": "Choose source and destination points on the map."}), 400
        source_point = [float(source[0]), float(source[1])]
        dest_point = [float(destination[0]), float(destination[1])]

        graph = load_graph(points=[source_point, dest_point])
        graph_file = write_cpp_graph(graph)
        source_node, source_snap = nearest_node(graph, source_point)
        dest_node, dest_snap = nearest_node(graph, dest_point)

        args = [ENGINE, graph_file, str(source_node), str(dest_node)]
        # Optional manual traffic/closure overrides use graph node IDs.
        for item in traffic:
            if isinstance(item, list) and len(item) == 3:
                u, v, level = int(item[0]), int(item[1]), int(item[2])
                if level in (1, 2, 3, 4):
                    args.append(f"T:{u}:{v}:{level}")
        for item in closed:
            if isinstance(item, list) and len(item) == 2:
                args.append(f"C:{int(item[0])}:{int(item[1])}")

        completed = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
        if completed.returncode != 0:
            return jsonify({"error": completed.stderr.strip() or "C++ route engine failed"}), 500
        result = json.loads(completed.stdout)

        result["source"] = source_point
        result["destination"] = dest_point
        result["snappedSource"] = graph["nodes"][source_node]
        result["snappedDestination"] = graph["nodes"][dest_node]
        result["sourceSnapMeters"] = round(source_snap * 1000, 1)
        result["destinationSnapMeters"] = round(dest_snap * 1000, 1)
        result["nodeCount"] = len(graph["nodes"])
        result["edgeCount"] = len(graph["edges"])
        result["geometry"] = [graph["nodes"][i] for i in result.get("path", [])]
        return jsonify(result)
    except FileNotFoundError:
        return jsonify({"error": "C++ route engine is not compiled."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)

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

ROOT = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(ROOT, "frontend")
ENGINE = os.path.join(ROOT, "backend", "route_engine.exe" if os.name == "nt" else "route_engine")
CACHE_FILE = os.path.join(ROOT, "backend", "dehradun_osm_graph.json")
CACHE_TTL = 60 * 60 * 6  # refresh the downloaded road network every 6 hours

# Dehradun city-area bounding box: south, west, north, east.
BBOX = (30.28, 77.90, 30.42, 78.14)
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


def fetch_osm_graph():
    s, w, n, e = BBOX
    query = f"""[out:json][timeout:90];
way[\"highway\"][\"highway\"!~\"^(footway|path|cycleway|steps|pedestrian|track|construction|proposed|bridleway|corridor|raceway)$\"]({s},{w},{n},{e});
out body;
>;
out skel qt;"""
    encoded = urllib.parse.urlencode({"data": query}).encode("utf-8")
    headers = {
        "User-Agent": "DehradunTrafficPBL/2.1 (educational project)",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json",
    }

    errors = []
    data = None
    for endpoint in OVERPASS_URLS:
        req = urllib.request.Request(
            endpoint,
            data=encoded,
            headers=headers,
            method="POST",
        )
        try:
            with IPV4_OPENER.open(req, timeout=120) as response:
                data = json.load(response)
            break
        except Exception as exc:
            errors.append(f"{endpoint}: {exc}")

    if data is None:
        raise RuntimeError(
            "All Overpass API endpoints failed. " + " | ".join(errors)
        )

    nodes = {}
    ways = []
    for element in data.get("elements", []):
        if element.get("type") == "node" and "lat" in element and "lon" in element:
            nodes[element["id"]] = [element["lat"], element["lon"]]
        elif element.get("type") == "way":
            tags = element.get("tags", {})
            highway = tags.get("highway")
            refs = element.get("nodes", [])
            if highway and len(refs) >= 2:
                ways.append((refs, tags))

    # Keep only OSM nodes that participate in a routable road way.
    used = set()
    for refs, _ in ways:
        used.update(refs)
    used &= set(nodes)

    node_ids = sorted(used)
    id_to_idx = {osm_id: i for i, osm_id in enumerate(node_ids)}
    graph_nodes = [nodes[x] for x in node_ids]
    edges = {}

    for refs, tags in ways:
        highway = tags.get("highway", "residential")
        speed = parse_speed(tags.get("maxspeed"), highway)
        oneway = str(tags.get("oneway", "")).lower() in ("yes", "1", "true")
        if str(tags.get("oneway", "")).lower() == "-1":
            refs = list(reversed(refs))
            oneway = True

        for a, b in zip(refs, refs[1:]):
            if a not in id_to_idx or b not in id_to_idx:
                continue
            u, v = id_to_idx[a], id_to_idx[b]
            d = haversine_km(nodes[a], nodes[b])
            if d <= 0 or d > 2:
                continue
            t = d / speed * 60.0
            key = (u, v, int(oneway))
            # If multiple OSM ways create the same edge, retain the fastest estimate.
            old = edges.get(key)
            if old is None or t < old[1]:
                edges[key] = (d, t)

    graph_edges = [
        [u, v, d, t, one_way]
        for (u, v, one_way), (d, t) in edges.items()
    ]

    graph = {
        "generatedAt": time.time(),
        "bbox": list(BBOX),
        "nodes": graph_nodes,
        "edges": graph_edges,
        "source": "OpenStreetMap road data via Overpass API",
    }
    if len(graph_nodes) < 100 or len(graph_edges) < 100:
        raise RuntimeError("The road-network API returned too little road data.")

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(graph, f, separators=(",", ":"))
    return graph


def load_graph(force=False):
    if not force and os.path.exists(CACHE_FILE):
        try:
            if time.time() - os.path.getmtime(CACHE_FILE) < CACHE_TTL:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
    return fetch_osm_graph()


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


@app.get("/api/network")
def network():
    try:
        graph = load_graph()
        return jsonify({
            "nodeCount": len(graph["nodes"]),
            "edgeCount": len(graph["edges"]),
            "bbox": graph["bbox"],
            "source": graph["source"],
        })
    except Exception as e:
        return jsonify({"error": f"Could not download road network: {e}"}), 503


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

        graph = load_graph()
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

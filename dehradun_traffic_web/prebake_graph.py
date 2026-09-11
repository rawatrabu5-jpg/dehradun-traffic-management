"""Pre-bake the Dehradun OSM road graph at build/deploy time.

Run this once during your Render build step (see render.yaml) so the
graph cache file already exists and is warm before any user ever hits
/api/route. This removes the live Overpass fetch (the slowest, most
failure-prone part of the request) from the request path entirely.

Usage:
    python prebake_graph.py
"""
import sys
import time

from app import BBOX, CACHE_FILE, fetch_osm_graph


def main():
    print(f"Pre-baking road graph for bbox={BBOX} -> {CACHE_FILE}")
    start = time.time()
    try:
        graph = fetch_osm_graph(BBOX)
    except Exception as exc:
        # Don't fail the whole build over a flaky Overpass mirror: the app
        # will still fall back to fetching live on first request if this
        # step didn't produce a cache file. Just warn loudly.
        print(f"WARNING: pre-bake failed ({exc}). "
              f"The app will fetch the graph on its first live request instead.")
        sys.exit(0)
    elapsed = time.time() - start
    print(f"Done in {elapsed:.1f}s: {len(graph['nodes'])} nodes, "
          f"{len(graph['edges'])} edges written to {CACHE_FILE}")


if __name__ == "__main__":
    main()

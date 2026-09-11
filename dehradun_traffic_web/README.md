# Smart Intelligent Dehradun Traffic Management — API Road Network Version

This version removes the manually predefined locations and manually predefined graph edges.

## New architecture

Browser → Flask → **Overpass/OpenStreetMap road-network API** → generated graph → **C++ Dijkstra** → Leaflet map

The application downloads routable road ways for the Dehradun bounding box, converts OSM road nodes/ways into a graph, writes that graph to `backend/graph_input.txt`, and sends the selected nearest graph nodes to the C++ program.

The C++ program is still responsible for the shortest-path optimization. The graph is no longer hard-coded in C++.

OSRM is not required for drawing the route in this version because the generated graph preserves the OSM road nodes themselves; the returned Dijkstra path is drawn through those real road-network coordinates.

## Run

### 1. Install Python dependency

```bash
pip install flask
```

### 2. Compile C++

Windows (MinGW):

```bash
g++ -std=c++17 -O2 backend/route_engine.cpp -o backend/route_engine.exe
```

Linux/macOS:

```bash
g++ -std=c++17 -O2 backend/route_engine.cpp -o backend/route_engine
```

### 3. Start Flask

```bash
python app.py
```

Open `http://127.0.0.1:5000`.

## How it works

1. The website has **no predefined Dehradun locations**.
2. Click **Set Source on Map**, then click anywhere on the map.
3. Click **Set Destination on Map**, then click anywhere on the map.
4. Flask finds the nearest road node for both points.
5. Overpass supplies the OSM road network.
6. Flask converts the road network into a graph.
7. C++ Dijkstra calculates the optimal path.
8. The path is drawn on the map.

The road network is cached for six hours to avoid repeatedly downloading the same data. Use **Refresh Road Network API** to request fresh OSM road data.

## Important PBL limitation

OpenStreetMap/Overpass provides road-network data, not live traffic conditions. Therefore this version gives you a real API-generated road graph, but it does **not** yet provide live congestion. A separate traffic-data provider/API would be needed for real-time traffic levels.

Also, the public Overpass and OpenStreetMap services have usage policies and capacity limits. For a classroom demonstration this architecture is useful; a production deployment should use an appropriate hosted/self-managed data service.


## Public deployment (Render)

This project is deployment-ready for Render. Keep the GitHub repository private if you want the source code to remain admin-only. Render builds the C++ Dijkstra engine on the server and runs Flask with Gunicorn. Visitors receive only the web application; the C++ source and server files are not sent to their browsers.

Build command:
`pip install -r requirements.txt && g++ -std=c++17 -O2 backend/route_engine.cpp -o backend/route_engine`

Start command:
`gunicorn --bind 0.0.0.0:$PORT app:app`

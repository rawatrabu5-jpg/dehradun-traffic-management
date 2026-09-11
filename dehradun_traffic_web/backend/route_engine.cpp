#include <algorithm>
#include <cmath>
#include <fstream>
#include <iostream>
#include <limits>
#include <queue>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

using namespace std;

struct Road {
    int destination;
    double distanceKm;
    double travelTimeMin;
    int trafficLevel;
    bool closed;
};

class TrafficGraph {
    vector<double> lat, lon;
    vector<vector<Road>> adj;

public:
    bool load(const string& filename) {
        ifstream in(filename);
        if (!in) return false;
        int n = 0;
        if (!(in >> n) || n <= 0) return false;
        lat.resize(n); lon.resize(n); adj.assign(n, {});
        for (int i = 0; i < n; ++i) {
            int id; if (!(in >> id >> lat[i] >> lon[i])) return false;
        }
        int m = 0;
        if (!(in >> m) || m < 0) return false;
        for (int i = 0; i < m; ++i) {
            int u, v; double d, t; int oneWay;
            if (!(in >> u >> v >> d >> t >> oneWay)) return false;
            if (u < 0 || u >= n || v < 0 || v >= n) continue;
            adj[u].push_back({v, d, t, 1, false});
            if (!oneWay) adj[v].push_back({u, d, t, 1, false});
        }
        return true;
    }

    void updateTraffic(int u, int v, int level) {
        if (u < 0 || v < 0 || u >= (int)adj.size() || v >= (int)adj.size() || level < 1 || level > 4) return;
        for (auto& r : adj[u]) if (r.destination == v) r.trafficLevel = level;
        for (auto& r : adj[v]) if (r.destination == u) r.trafficLevel = level;
    }

    void updateClosed(int u, int v, bool closed) {
        if (u < 0 || v < 0 || u >= (int)adj.size() || v >= (int)adj.size()) return;
        for (auto& r : adj[u]) if (r.destination == v) r.closed = closed;
        for (auto& r : adj[v]) if (r.destination == u) r.closed = closed;
    }

    double weight(const Road& r) const {
        if (r.closed) return numeric_limits<double>::infinity();
        double factor = 1.0;
        if (r.trafficLevel == 2) factor = 1.25;
        else if (r.trafficLevel == 3) factor = 1.60;
        else if (r.trafficLevel == 4) factor = 2.00;
        return r.distanceKm * factor;
    }

    struct Result {
        bool found = false;
        vector<int> path;
        double cost = 0;
        double distanceKm = 0;
        double timeMin = 0;
    };

    Result dijkstra(int source, int target) const {
        Result result;
        int n = (int)adj.size();
        if (source < 0 || target < 0 || source >= n || target >= n) return result;

        vector<double> dist(n, numeric_limits<double>::infinity());
        vector<double> physical(n, 0), time(n, 0);
        vector<int> parent(n, -1);
        priority_queue<pair<double,int>, vector<pair<double,int>>, greater<pair<double,int>>> pq;

        dist[source] = 0;
        pq.push({0, source});
        while (!pq.empty()) {
            auto [cost, u] = pq.top(); pq.pop();
            if (cost > dist[u]) continue;
            if (u == target) break;
            for (const auto& r : adj[u]) {
                if (r.closed) continue;
                double w = weight(r);
                double nc = cost + w;
                if (nc < dist[r.destination]) {
                    dist[r.destination] = nc;
                    physical[r.destination] = physical[u] + r.distanceKm;
                    time[r.destination] = time[u] + r.travelTimeMin;
                    parent[r.destination] = u;
                    pq.push({nc, r.destination});
                }
            }
        }

        if (!isfinite(dist[target])) return result;
        for (int cur = target; cur != -1; cur = parent[cur]) result.path.push_back(cur);
        reverse(result.path.begin(), result.path.end());
        result.cost = dist[target];
        result.distanceKm = physical[target];
        result.timeMin = time[target];
        result.found = true;
        return result;
    }
};

int main(int argc, char* argv[]) {
    if (argc < 4) {
        cerr << "Usage: route_engine <graph_file> <source_node> <destination_node> [T:u:v:level] [C:u:v]\n";
        return 1;
    }

    TrafficGraph graph;
    if (!graph.load(argv[1])) {
        cerr << "Unable to load generated road graph.\n";
        return 2;
    }

    int source = stoi(argv[2]);
    int destination = stoi(argv[3]);

    for (int i = 4; i < argc; ++i) {
        string a = argv[i];
        if (a.rfind("T:", 0) == 0) {
            string s = a.substr(2); stringstream ss(s); string part; vector<string> p;
            while (getline(ss, part, ':')) p.push_back(part);
            if (p.size() == 3) graph.updateTraffic(stoi(p[0]), stoi(p[1]), stoi(p[2]));
        } else if (a.rfind("C:", 0) == 0) {
            string s = a.substr(2); stringstream ss(s); string a1, a2;
            if (getline(ss, a1, ':') && getline(ss, a2, ':')) graph.updateClosed(stoi(a1), stoi(a2), true);
        }
    }

    auto r = graph.dijkstra(source, destination);
    cout << "{\"found\":" << (r.found ? "true" : "false") << ",\"path\":[";
    for (size_t i = 0; i < r.path.size(); ++i) {
        if (i) cout << ',';
        cout << r.path[i];
    }
    cout << "],\"cost\":" << r.cost << ",\"distanceKm\":" << r.distanceKm
         << ",\"timeMin\":" << r.timeMin << "}";
    return 0;
}

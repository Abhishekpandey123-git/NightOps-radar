#!/usr/bin/env python3
"""
NightOps GSoC Radar — Independent Backend Service (Strategy 2: Tiered Polling).
- Tier 1 (High-Velocity Top Orgs): Polled every 60s (1 min).
- Tier 2 (Standard Orgs): Polled every 300s (5 mins).
- Deep-Dive Dossier: Fetches any historical issue beyond 30 days and tracks pinned issues.
- Strict Rule: Plain code handles all HTTP serving and deterministic filters.
  AI Gateway is escalated strictly on first encounter of unassigned issues.
"""

import os
import sys
import json
import time
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Ensure local imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backend.ai_gateway import AIGateway
from backend.scout import GSoCScout

PORT = int(os.getenv("PORT", "8080"))
HOST = os.getenv("HOST", "0.0.0.0")

def load_config():
    config_file = "config.yaml"
    if os.path.exists(config_file):
        try:
            import yaml
            with open(config_file, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {
        "organizations": [
            {"id": "sympy", "name": "SymPy", "repo": "sympy/sympy", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/orgs/sympy", "mentors": "@oscarbenjamin, @asmeurer", "tier": 1, "active": True},
            {"id": "openfoodfacts", "name": "Open Food Facts", "repo": "openfoodfacts/openfoodfacts-server", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/orgs/open-food-facts", "mentors": "@teolemon, @alexgarel", "tier": 1, "active": True},
            {"id": "apache", "name": "Apache Software Foundation", "repo": "apache/iggy", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/orgs/apache-software-foundation", "mentors": "@justinmclean", "tier": 1, "active": True},
            {"id": "blender", "name": "Blender Foundation", "repo": "blender/blender", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/orgs/blender", "mentors": "@sergey, @brecht", "tier": 1, "active": True},
            {"id": "django", "name": "Django Software Foundation", "repo": "django/django", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/orgs/django", "mentors": "@carltongibson, @charettes", "tier": 1, "active": True},
            {"id": "videolan", "name": "VideoLAN (VLC)", "repo": "videolan/vlc", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/orgs/videolan", "mentors": "@j-b, @robUx4", "tier": 2, "active": True},
            {"id": "cncf", "name": "Cloud Native Computing Foundation", "repo": "kubernetes/kubernetes", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/orgs/cloud-native-computing-foundation", "mentors": "@dims, @liggitt", "tier": 1, "active": True}
        ]
    }

CONFIG = load_config()
AI = AIGateway()
SCOUT = GSoCScout(config=CONFIG, ai_gateway=AI)

# Continuous Strategy 2 Tiered Polling Worker
def background_scheduler():
    print("[Daemon] 🕒 Strategy 2 Tiered Scheduler active:")
    print("         Tier 1 (High-Velocity): Every 60s (1 min)")
    print("         Tier 2 (Standard Orgs): Every 300s (5 mins)")
    time.sleep(1)
    
    while True:
        try:
            SCOUT.poll_tiered_cycle()
        except Exception as e:
            print(f"[Daemon] Tiered poller warning: {e}")
        time.sleep(10) # Checks eligibility queue every 10s

class RadarRequestHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ["/api/gsoc/issues", "/api/issues"]:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            
            issues = list(SCOUT.issues_db.values())
            issues.sort(key=lambda x: x.get("releasedAt", ""), reverse=True)
            payload = {
                "status": "ok",
                "strategy": "Tiered 60s/300s (30-day window + pinned)",
                "last_poll": SCOUT.last_poll_time,
                "count": len(issues),
                "ai_enabled": AI.is_available(),
                "issues": issues
            }
            self.wfile.write(json.dumps(payload, indent=2).encode("utf-8"))
            return

        elif path == "/api/gsoc/issue/deep_fetch":
            # Deep-dive fetch of historical issue beyond 30 days
            repo = query.get("repo", [""])[0]
            num_str = query.get("number", [""])[0]
            if not repo or not num_str:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Missing repo or number"}).encode("utf-8"))
                return

            try:
                num = int(num_str)
                dossier = SCOUT.fetch_historical_deep_dive(repo, num)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok", "dossier": dossier}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        elif path == "/api/gsoc/issue/pin":
            issue_id = query.get("id", [""])[0]
            enable = query.get("enabled", ["true"])[0].lower() in ["true", "1"]
            if not issue_id:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Missing issue id"}).encode("utf-8"))
                return

            res = SCOUT.pin_issue(issue_id, enable)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "issue_id": issue_id, "pinned": res}).encode("utf-8"))
            return

        elif path == "/api/gsoc/poll":
            SCOUT.poll_tiered_cycle()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "poll_triggered", "count": len(SCOUT.issues_db)}).encode("utf-8"))
            return

        elif path == "/api/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "healthy",
                "service": "nightops-gsoc-radar",
                "strategy": "Tiered 60s/300s",
                "ai_gateway": AI.is_available(),
                "tracked_issues": len(SCOUT.issues_db),
                "pinned_beyond_30d": len(SCOUT.pinned_issues)
            }).encode("utf-8"))
            return

        elif path in ["/", "/radar"]:
            self.path = "/index.html"

        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path in ["/api/gsoc/poll", "/api/gsoc/issue/deep_fetch", "/api/gsoc/issue/pin"]:
            return self.do_GET()
        self.send_response(404)
        self.end_headers()

def main():
    print("=" * 65)
    print("👻 NIGHTOPS GSOC RADAR // STRATEGY 2 TIERED SERVICE (60s / 300s)")
    print("Rules: Drops assigned issues, 30-day window default, Deep-Dive extension.")
    print(f"AI Gateway: {'Active (API Token Detected)' if AI.is_available() else 'Heuristic Fallback Mode'}")
    print("=" * 65)

    t = threading.Thread(target=background_scheduler, daemon=True)
    t.start()

    HTTPServer.allow_reuse_address = True
    server = HTTPServer((HOST, PORT), RadarRequestHandler)
    print(f"[Server] 🚀 Serving NightOps Radar on http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Server] Shutting down NightOps Radar...")
        server.server_close()

if __name__ == "__main__":
    main()

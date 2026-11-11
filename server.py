#!/usr/bin/env python3
"""
NightOps GSoC Radar — Independent Backend Server.
Strict Rule: Plain code handles all HTTP serving, routing, and timers.
AI is only called by the Scout engine when an unassigned issue needs tactical evaluation.
Runs natively with Python standard library (zero required pip dependencies) or FastAPI.
"""

import os
import sys
import json
import time
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

# Ensure local imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backend.ai_gateway import AIGateway
from backend.scout import GSoCScout

PORT = int(os.getenv("PORT", "8080"))
HOST = os.getenv("HOST", "0.0.0.0")

# Load Configuration
DEFAULT_CONFIG = {
    "poll_interval_seconds": 600, # 10 minutes
    "organizations": [
        {"id": "sympy", "name": "SymPy", "repo": "sympy/sympy", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/orgs/sympy", "mentors": "@oscarbenjamin, @asmeurer", "active": True},
        {"id": "openfoodfacts", "name": "Open Food Facts", "repo": "openfoodfacts/openfoodfacts-server", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/orgs/open-food-facts", "mentors": "@teolemon, @alexgarel", "active": True},
        {"id": "apache", "name": "Apache Software Foundation", "repo": "apache/iggy", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/orgs/apache-software-foundation", "mentors": "@justinmclean", "active": True},
        {"id": "blender", "name": "Blender Foundation", "repo": "blender/blender", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/orgs/blender", "mentors": "@sergey, @brecht", "active": True},
        {"id": "django", "name": "Django Software Foundation", "repo": "django/django", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/orgs/django", "mentors": "@carltongibson, @charettes", "active": True},
        {"id": "videolan", "name": "VideoLAN (VLC)", "repo": "videolan/vlc", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/orgs/videolan", "mentors": "@j-b, @robUx4", "active": True},
        {"id": "cncf", "name": "Cloud Native Computing Foundation", "repo": "kubernetes/kubernetes", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/orgs/cloud-native-computing-foundation", "mentors": "@dims, @liggitt", "active": True}
    ]
}

def load_config():
    config_file = "config.yaml"
    if os.path.exists(config_file):
        try:
            import yaml
            with open(config_file, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or DEFAULT_CONFIG
        except Exception:
            pass
    return DEFAULT_CONFIG

CONFIG = load_config()
AI = AIGateway()
SCOUT = GSoCScout(config=CONFIG, ai_gateway=AI)

# Background Polling Thread (10-minute cycle)
def background_scheduler():
    interval = CONFIG.get("poll_interval_seconds", 600)
    print(f"[Daemon] 🕒 Background 10-minute scheduler started (cycle = {interval}s).")
    
    # Run initial poll shortly after start
    time.sleep(2)
    SCOUT.poll_all_organizations()

    while True:
        time.sleep(interval)
        try:
            SCOUT.poll_all_organizations()
        except Exception as e:
            print(f"[Daemon] Poller error: {e}")

class RadarRequestHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # Enable CORS for cross-origin requests
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

        if path in ["/api/gsoc/issues", "/api/issues"]:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            
            issues = list(SCOUT.issues_db.values())
            # Sort newest first
            issues.sort(key=lambda x: x.get("releasedAt", ""), reverse=True)
            payload = {
                "status": "ok",
                "last_poll": SCOUT.last_poll_time,
                "count": len(issues),
                "ai_enabled": AI.is_available(),
                "issues": issues
            }
            self.wfile.write(json.dumps(payload, indent=2).encode("utf-8"))
            return

        elif path == "/api/gsoc/poll":
            # Force immediate poll
            SCOUT.poll_all_organizations()
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
                "timestamp": time.time(),
                "ai_gateway": AI.is_available(),
                "tracked_issues": len(SCOUT.issues_db)
            }).encode("utf-8"))
            return

        elif path in ["/", "/radar"]:
            self.path = "/index.html"

        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/gsoc/poll":
            return self.do_GET()
        self.send_response(404)
        self.end_headers()

def main():
    print("=" * 60)
    print("👻 NIGHTOPS GSOC RADAR // INDEPENDENT BACKEND SERVICE")
    print("Strict Rule: Plain code handles polling & serving.")
    print(f"AI Gateway: {'Active (API Token Detected)' if AI.is_available() else 'Heuristic Fallback Mode'}")
    print("=" * 60)

    # Launch background 10-minute poller
    t = threading.Thread(target=background_scheduler, daemon=True)
    t.start()

    # Launch HTTP Server
    server = HTTPServer((HOST, PORT), RadarRequestHandler)
    print(f"[Server] 🚀 Serving NightOps Radar on http://localhost:{PORT}")
    print(f"[Server] 📡 API Endpoint: http://localhost:{PORT}/api/gsoc/issues")
    print(f"[Server] 📊 UI Dashboard: http://localhost:{PORT}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Server] Shutting down NightOps Radar...")
        server.server_close()

if __name__ == "__main__":
    main()

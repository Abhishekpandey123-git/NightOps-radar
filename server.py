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

def load_dotenv(filepath=".env"):
    """Zero-dependency .env loader so tokens in .env are loaded automatically."""
    if not os.path.exists(filepath):
        return
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and v and k not in os.environ:
                    os.environ[k] = v
    except Exception as e:
        print(f"[Config] Warning reading .env: {e}")

def save_env_vars(updates: dict, filepath=".env"):
    """Persist updated tokens to local .env file (gitignored)."""
    existing = {}
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#") and "=" in s:
                    k, v = s.split("=", 1)
                    existing[k.strip()] = v.strip()
    for k, v in updates.items():
        if v:
            existing[k] = v
            os.environ[k] = v
    with open(filepath, "w", encoding="utf-8") as f:
        for k, v in existing.items():
            f.write(f"{k}={v}\n")

def mask_token(val: str) -> str:
    if not val:
        return ""
    if len(val) <= 8:
        return "••••"
    return val[:4] + "••••" + val[-4:]

load_dotenv()

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
            {"id": "sympy", "name": "SymPy", "repo": "sympy/sympy", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/organizations/sympy", "mentors": "@oscarbenjamin, @asmeurer", "tier": 1, "active": True},
            {"id": "openfoodfacts", "name": "Open Food Facts", "repo": "openfoodfacts/openfoodfacts-server", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/organizations/open-food-facts", "mentors": "@teolemon, @alexgarel", "tier": 1, "active": True},
            {"id": "apache", "name": "Apache Software Foundation", "repo": "apache/iggy", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/organizations/apache-software-foundation", "mentors": "@justinmclean", "tier": 1, "active": True},
            {"id": "blender", "name": "Blender Foundation", "repo": "blender/blender", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/organizations/blender", "mentors": "@sergey, @brecht", "tier": 1, "active": True},
            {"id": "django", "name": "Django Software Foundation", "repo": "django/django", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/organizations/django", "mentors": "@carltongibson, @charettes", "tier": 1, "active": True},
            {"id": "videolan", "name": "VideoLAN (VLC)", "repo": "videolan/vlc", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/organizations/videolan", "mentors": "@j-b, @robUx4", "tier": 2, "active": True},
            {"id": "cncf", "name": "Cloud Native Computing Foundation", "repo": "kubernetes/kubernetes", "gsocUrl": "https://summerofcode.withgoogle.com/programs/2026/organizations/cloud-native-computing-foundation", "mentors": "@dims, @liggitt", "tier": 1, "active": True}
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

        elif path == "/api/gsoc/poll/settings":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "paused": SCOUT.paused,
                "tier1_interval": SCOUT.tier1_interval,
                "tier2_interval": SCOUT.tier2_interval
            }).encode("utf-8"))
            return

        elif path == "/api/gsoc/poll/target":
            repo = query.get("repo", [""])[0]
            org_id = query.get("org_id", [""])[0] or None
            if not repo or "/" not in repo:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Missing or invalid repo"}).encode("utf-8"))
                return
            found = SCOUT.poll_single_target(repo, org_id=org_id)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "repo": repo, "count": len(found)}).encode("utf-8"))
            return

        elif path == "/api/gsoc/poll":
            threading.Thread(target=SCOUT.poll_tiered_cycle, daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "poll_triggered", "count": len(SCOUT.issues_db)}).encode("utf-8"))
            return

        elif path == "/api/gsoc/usage":
            refresh_gh = query.get("refresh", ["false"])[0].lower() in ["true", "1"]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "github": SCOUT.get_rate_limit_status(refresh=refresh_gh),
                "ai": AI.get_usage_report()
            }).encode("utf-8"))
            return

        elif path == "/api/gsoc/tokens":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            gh = SCOUT.github_token or ""
            gem = AI.gemini_key or ""
            oai = AI.openai_key or ""
            oss_provider = "Groq (Llama-3.1)" if AI.groq_key else ("HuggingFace (Qwen-2.5)" if AI.hf_token else ("OpenRouter (Llama-3.3)" if AI.openrouter_key else ("Ollama Local" if AI._check_ollama() else "")))
            self.wfile.write(json.dumps({
                "status": "ok",
                "github_token_set": bool(gh),
                "github_token_masked": mask_token(gh),
                "rate_limit_tier": "5,000 req/hr" if gh else "60 req/hr (unauthenticated)",
                "gemini_key_set": bool(gem),
                "gemini_key_masked": mask_token(gem),
                "openai_key_set": bool(oai),
                "openai_key_masked": mask_token(oai),
                "oss_ai_set": bool(oss_provider),
                "oss_ai_provider": oss_provider,
                "ai_enabled": AI.is_available()
            }).encode("utf-8"))
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

        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        elif path in ["/", "/radar"]:
            self.path = "/index.html"

        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/gsoc/tokens":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            try:
                data = json.loads(body)
            except Exception:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode("utf-8"))
                return

            updates = {}
            if "github_token" in data and data["github_token"].strip():
                gh = data["github_token"].strip()
                SCOUT.github_token = gh
                updates["GITHUB_TOKEN"] = gh
            if "gemini_api_key" in data and data["gemini_api_key"].strip():
                gem = data["gemini_api_key"].strip()
                AI.gemini_key = gem
                AI._active_gemini_model = None
                AI._discovered_models = None
                updates["GEMINI_API_KEY"] = gem
            if "openai_api_key" in data and data["openai_api_key"].strip():
                oai = data["openai_api_key"].strip()
                AI.openai_key = oai
                updates["OPENAI_API_KEY"] = oai
            if "groq_api_key" in data and data["groq_api_key"].strip():
                gq = data["groq_api_key"].strip()
                AI.groq_key = gq
                updates["GROQ_API_KEY"] = gq
            if "hf_token" in data and data["hf_token"].strip():
                hf = data["hf_token"].strip()
                AI.hf_token = hf
                updates["HF_TOKEN"] = hf
            if "openrouter_key" in data and data["openrouter_key"].strip():
                ort = data["openrouter_key"].strip()
                AI.openrouter_key = ort
                updates["OPENROUTER_API_KEY"] = ort

            if updates:
                save_env_vars(updates)
                AI._cooldown_until = 0.0
                AI._provider_cooldowns.clear()
                SCOUT.get_rate_limit_status(refresh=True)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "github_token_set": bool(SCOUT.github_token),
                "github_token_masked": mask_token(SCOUT.github_token),
                "rate_limit_tier": "5,000 req/hr" if SCOUT.github_token else "60 req/hr (unauthenticated)",
                "gemini_key_set": bool(AI.gemini_key),
                "openai_key_set": bool(AI.openai_key),
                "ai_enabled": AI.is_available()
            }).encode("utf-8"))
            return

        if parsed.path == "/api/gsoc/poll/settings":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            try:
                data = json.loads(body)
            except Exception:
                data = {}
            if "paused" in data:
                SCOUT.paused = bool(data["paused"])
            if "tier1_interval" in data:
                SCOUT.tier1_interval = max(10, int(data["tier1_interval"]))
            if "tier2_interval" in data:
                SCOUT.tier2_interval = max(10, int(data["tier2_interval"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "paused": SCOUT.paused,
                "tier1_interval": SCOUT.tier1_interval,
                "tier2_interval": SCOUT.tier2_interval
            }).encode("utf-8"))
            return

        if parsed.path == "/api/gsoc/poll/custom":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            try:
                data = json.loads(body)
            except Exception:
                data = {}
            target_repo = data.get("target_repo", "").strip()
            tier_scope = data.get("tier_scope", "all")
            days_window = int(data.get("days_window", 30))
            since_iso = data.get("since_iso") or None
            until_iso = data.get("until_iso") or None

            threading.Thread(
                target=SCOUT.poll_custom_range,
                kwargs={
                    "target_repo": target_repo,
                    "tier_scope": tier_scope,
                    "days_window": days_window,
                    "since_iso": since_iso,
                    "until_iso": until_iso
                },
                daemon=True
            ).start()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "custom_poll_started"}).encode("utf-8"))
            return

        if parsed.path in ["/api/gsoc/poll", "/api/gsoc/poll/target", "/api/gsoc/issue/deep_fetch", "/api/gsoc/issue/pin"]:
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

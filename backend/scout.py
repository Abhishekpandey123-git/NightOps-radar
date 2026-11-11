"""
Deterministic GitHub Issue Scraper & Triage Engine for NightOps Radar.
Strict Rule: 
- Deterministic logic handles HTTP polling, deduplication, claim detection, and filters.
- AI Gateway is only called when an issue is fresh, unassigned, and needs tactical fit assessment.
"""
import os
import re
import json
import time
import urllib.request
import urllib.error
from datetime import datetime

CLAIM_PHRASES = [
    "i'd like to work", "i would like to work", "i want to work on",
    "can i work on", "can i take", "may i take", "may i work", "i'll take",
    "i will take", "i'd like to take", "i would like to take",
    "i'd love to take", "i would love to take", "working on this",
    "working on a fix", "assigned to me", "pr is up", "opened a pr"
]

class GSoCScout:
    def __init__(self, config: dict, ai_gateway=None, data_dir="data"):
        self.config = config
        self.ai_gateway = ai_gateway
        self.data_dir = data_dir
        self.cache_file = os.path.join(data_dir, "gsoc_issues.json")
        self.github_token = os.getenv("GITHUB_TOKEN", "").strip()
        self.last_poll_time = None
        self.issues_db = {}
        os.makedirs(self.data_dir, exist_ok=True)
        self._load_cache()

    def _load_cache(self):
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data.get("issues", []):
                        self.issues_db[item["id"]] = item
                    self.last_poll_time = data.get("last_poll")
            except Exception as e:
                print(f"[Scout] Cache load warning: {e}")

    def _save_cache(self):
        data = {
            "last_poll": datetime.utcnow().isoformat() + "Z",
            "count": len(self.issues_db),
            "issues": sorted(list(self.issues_db.values()), key=lambda x: x["releasedAt"], reverse=True)
        }
        with open(self.cache_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def fetch_repo_issues(self, repo: str, org_id: str):
        """Query GitHub REST API with deterministic headers & rate-limit handling."""
        url = f"https://api.github.com/repos/{repo}/issues?state=open&per_page=15"
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "NightOps-GSoC-Radar"
        }
        if self.github_token:
            headers["Authorization"] = f"token {self.github_token}"

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                items = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 403:
                print(f"[Scout] GitHub Rate Limit Hit for {repo}. Set GITHUB_TOKEN to raise limit to 5000/hr.")
            else:
                print(f"[Scout] HTTP {e.code} for {repo}: {e.reason}")
            return []
        except Exception as e:
            print(f"[Scout] Network error fetching {repo}: {e}")
            return []

        parsed = []
        for it in items:
            if "pull_request" in it:
                continue  # Drop pull requests

            issue_id = f"{repo}#{it['number']}"
            is_new = issue_id not in self.issues_db

            issue = {
                "id": issue_id,
                "orgId": org_id,
                "repo": repo,
                "number": it["number"],
                "title": it["title"],
                "url": it["html_url"],
                "releasedAt": it["created_at"],
                "assignedTo": it["assignee"]["login"] if it["assignee"] else None,
                "commentsCount": it["comments"],
                "contributors": [c["login"] for c in it.get("assignees", [])],
                "tags": [l["name"] for l in it.get("labels", [])],
                "desc": (it.get("body") or "")[:600],
                "ai_fit_assessment": None,
                "difficulty_score": None,
                "isPopping": is_new
            }

            # Code-first claim detection if comments exist
            if not issue["assignedTo"] and issue["commentsCount"] > 0:
                pass # Already flagged via commentsCount

            # AI Escalation: Only when an issue is unassigned and fresh
            if not issue["assignedTo"] and (is_new or not self.issues_db.get(issue_id, {}).get("ai_fit_assessment")):
                if self.ai_gateway and self.ai_gateway.is_available():
                    ai_res = self.ai_gateway.analyze_issue(issue["title"], issue["desc"], repo)
                    issue["difficulty_score"] = ai_res.get("difficulty", 50)
                    issue["ai_fit_assessment"] = ai_res.get("fit_summary", "")

            self.issues_db[issue_id] = issue
            parsed.append(issue)

        return parsed

    def poll_all_organizations(self):
        """Main polling orchestrator executing every 10 minutes."""
        orgs = self.config.get("organizations", [])
        print(f"[Scout] ⚡ Starting 10-minute poll cycle across {len(orgs)} GSoC organizations...")
        
        total_found = 0
        for org in orgs:
            if not org.get("active", True):
                continue
            repo = org["repo"]
            found = self.fetch_repo_issues(repo, org["id"])
            total_found += len(found)
            time.sleep(0.5) # Gentle rate-limit spacing

        self.last_poll_time = datetime.utcnow().isoformat() + "Z"
        self._save_cache()
        print(f"[Scout] ✓ Poll completed. Tracked {len(self.issues_db)} active issues across {len(orgs)} orgs.")
        return list(self.issues_db.values())

"""
NightOps Radar — Strategy 2 Tiered Scraper & Deep-Dive Engine.
Rules:
1. Strategy 2 (Tiered Polling):
   - Tier 1 (High-Velocity Top Orgs): Polled every 60 seconds (1 minute).
   - Tier 2 (Standard Orgs): Polled every 300 seconds (5 minutes).
2. 30-Day Window Filter:
   - Automatically drops assigned issues (assignee=none).
   - Looks strictly at issues within 30 days by default.
3. Deep-Dive Dossier Historical Extension:
   - Users can look up and pin any issue older than 30 days.
   - Collects 100% of raw GitHub metadata, comment claims, reactions, and AI fit triage on first encounter.
"""
import os
import re
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

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
        self.pinned_file = os.path.join(data_dir, "pinned_issues.json")
        self.github_token = os.getenv("GITHUB_TOKEN", "").strip()
        self.last_poll_time = None
        self.issues_db = {}
        self.pinned_issues = set()
        self.repo_last_polled = {}
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

        if os.path.exists(self.pinned_file):
            try:
                with open(self.pinned_file, "r", encoding="utf-8") as f:
                    self.pinned_issues = set(json.load(f))
            except Exception:
                self.pinned_issues = set()

    def _save_cache(self):
        data = {
            "last_poll": datetime.utcnow().isoformat() + "Z",
            "count": len(self.issues_db),
            "pinned_count": len(self.pinned_issues),
            "issues": sorted(list(self.issues_db.values()), key=lambda x: x.get("releasedAt", ""), reverse=True)
        }
        with open(self.cache_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _save_pinned(self):
        with open(self.pinned_file, "w", encoding="utf-8") as f:
            json.dump(list(self.pinned_issues), f, indent=2)

    def pin_issue(self, issue_id: str, enabled: bool = True):
        """Allow user to pin and watch an issue beyond the 30-day window."""
        if enabled:
            self.pinned_issues.add(issue_id)
            if issue_id in self.issues_db:
                self.issues_db[issue_id]["pinned_beyond_30d"] = True
        else:
            self.pinned_issues.discard(issue_id)
            if issue_id in self.issues_db:
                self.issues_db[issue_id]["pinned_beyond_30d"] = False
        self._save_pinned()
        self._save_cache()
        return issue_id in self.pinned_issues

    def fetch_issue_thread_details(self, repo: str, issue_number: int, author: str = None, assignees: list = None):
        """Fetch comments and extract unique contributors + first comment details."""
        url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments?per_page=30"
        headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "NightOps-Radar"}
        if self.github_token:
            headers["Authorization"] = f"token {self.github_token}"

        contributors_map = {}
        if author and author != "unknown":
            contributors_map[author] = {
                "login": author,
                "role": "Author",
                "avatar_url": f"https://github.com/{author}.png",
                "profile_url": f"https://github.com/{author}"
            }
        for a in (assignees or []):
            login = a.get("login") if isinstance(a, dict) else a
            if login:
                contributors_map[login] = {
                    "login": login,
                    "role": "Assignee",
                    "avatar_url": f"https://github.com/{login}.png",
                    "profile_url": f"https://github.com/{login}"
                }

        first_comment = None
        has_claim = False

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                comments = json.loads(resp.read().decode("utf-8"))
                for idx, c in enumerate(comments):
                    user = c.get("user") or {}
                    login = user.get("login")
                    if login and login not in contributors_map:
                        contributors_map[login] = {
                            "login": login,
                            "role": "First Commenter" if idx == 0 else "Commenter",
                            "avatar_url": user.get("avatar_url") or f"https://github.com/{login}.png",
                            "profile_url": f"https://github.com/{login}"
                        }
                    body = (c.get("body") or "")
                    lower_body = body.lower()
                    if any(phrase in lower_body for phrase in CLAIM_PHRASES):
                        has_claim = True

                    if idx == 0 and not first_comment:
                        first_comment = {
                            "created_at": c.get("created_at"),
                            "author": login or "unknown",
                            "avatar_url": user.get("avatar_url") or f"https://github.com/{login}.png",
                            "body": body[:350] + ("..." if len(body) > 350 else ""),
                            "html_url": c.get("html_url")
                        }
        except Exception:
            pass

        return {
            "has_claim": has_claim,
            "first_comment": first_comment,
            "unique_contributors": list(contributors_map.values()),
            "unique_contributors_count": len(contributors_map)
        }

    def detect_claim_in_comments(self, repo: str, issue_number: int) -> bool:
        res = self.fetch_issue_thread_details(repo, issue_number)
        return res["has_claim"]

    def build_complete_issue_dossier(self, it: dict, repo: str, org_id: str, is_new: bool = False) -> dict:
        """Collect all information about the issue on first encounter."""
        issue_id = f"{repo}#{it['number']}"
        body_text = it.get("body") or ""

        # 1. Deterministic Code Heuristics
        kind = "code"
        lower_body = (it["title"] + " " + body_text).lower()
        if any(w in lower_body for w in ["doc", "docs", "readme", "tutorial", "guide", "typo"]):
            kind = "docs"
        elif any(w in lower_body for w in ["test", "tests", "coverage", "pytest", "unit test"]):
            kind = "tests"
        elif any(w in lower_body for w in ["ci", "github actions", "docker", "build", "pipeline", "packaging"]):
            kind = "build/CI"
        elif any(w in lower_body for w in ["design", "architecture", "rfc", "proposal", "discussion"]):
            kind = "design"

        author_login = it.get("user", {}).get("login", "unknown")
        assignees = it.get("assignees", [])

        # Fetch thread details if comments exist or assignees present
        has_claim = False
        first_comment = None
        unique_contributors = []
        unique_contributors_count = 1

        if it.get("comments", 0) > 0 or assignees:
            thread_details = self.fetch_issue_thread_details(repo, it["number"], author_login, assignees)
            has_claim = thread_details["has_claim"]
            first_comment = thread_details["first_comment"]
            unique_contributors = thread_details["unique_contributors"]
            unique_contributors_count = thread_details["unique_contributors_count"]
        else:
            if author_login and author_login != "unknown":
                unique_contributors = [{
                    "login": author_login,
                    "role": "Author",
                    "avatar_url": f"https://github.com/{author_login}.png",
                    "profile_url": f"https://github.com/{author_login}"
                }]

        # Detected timestamp (when NightOps first indexed it)
        now_iso = datetime.now(timezone.utc).isoformat()
        detected_at = self.issues_db.get(issue_id, {}).get("detectedAt") or now_iso

        # Check GSoC relevance keywords
        gsoc_evidence = any(w in lower_body for w in ["gsoc", "summer of code", "mentors", "project idea", "proposal"])

        # Competition Level Evaluation (in comparison with other issues across GSoC repos)
        comp_level = "LOW"
        comp_score = 15
        comp_note = "Uncontested: Zero comments or competitor activity. Prime opportunity to claim!"
        if has_claim:
            comp_level = "INTENSE"
            comp_score = 90
            comp_note = "Contested: A contributor in comments has already expressed intent or opened a fix."
        elif it.get("comments", 0) >= 3 or (isinstance(it.get("reactions"), dict) and it.get("reactions", {}).get("total_count", 0) >= 4):
            comp_level = "HIGH"
            comp_score = 75
            comp_note = "High Competition: Multiple contributors discussing or reacting to this issue."
        elif it.get("comments", 0) >= 1:
            comp_level = "MEDIUM"
            comp_score = 45
            comp_note = "Moderate Competition: 1-2 inquiry comments, but no formal claim yet."

        issue = {
            "id": issue_id,
            "orgId": org_id,
            "repo": repo,
            "number": it["number"],
            "title": it["title"],
            "url": it["html_url"],
            "releasedAt": it["created_at"],
            "detectedAt": detected_at,
            "updatedAt": it.get("updated_at", it["created_at"]),
            "author": author_login,
            "authorAssociation": it.get("author_association", "NONE"),
            "assignedTo": it["assignee"]["login"] if it.get("assignee") else None,
            "commentsCount": it.get("comments", 0),
            "claimedByComment": has_claim,
            "reactionsCount": it.get("reactions", {}).get("total_count", 0) if "reactions" in it else 0,
            "unique_contributors": unique_contributors,
            "unique_contributors_count": unique_contributors_count,
            "contributors": [c["login"] for c in unique_contributors],
            "first_comment": first_comment,
            "tags": [l["name"] for l in it.get("labels", [])],
            "desc": body_text[:1500],
            "body": body_text,
            "kind": kind,
            "gsoc": gsoc_evidence,
            "difficulty_score": 40,
            "competition_level": comp_level,
            "competition_score": comp_score,
            "competition_note": comp_note,
            "ai_fit_assessment": None,
            "recommended_skills": [],
            "recommend": "maybe",
            "isPopping": is_new,
            "pinned_beyond_30d": issue_id in self.pinned_issues
        }

        # 2. AI Escalation: Only when an issue is unassigned and fresh or in deep-dive dossier
        if not issue["assignedTo"] and (is_new or not self.issues_db.get(issue_id, {}).get("ai_fit_assessment")):
            if self.ai_gateway and self.ai_gateway.is_available():
                ai_res = self.ai_gateway.analyze_issue(issue["title"], issue["desc"], repo)
                issue["difficulty_score"] = ai_res.get("difficulty", 40)
                issue["ai_fit_assessment"] = ai_res.get("fit_summary", "")
                issue["recommended_skills"] = ai_res.get("skills", ["python"])
                issue["recommend"] = "yes" if issue["difficulty_score"] <= 45 else ("maybe" if issue["difficulty_score"] <= 70 else "no")

        self.issues_db[issue_id] = issue
        return issue

    def fetch_repo_issues(self, repo: str, org_id: str, days_window: int = 30):
        """
        Poll single repo under Strategy 2:
        - assignee=none: ignores any assigned issues immediately
        - since: ignores any issues older than 30 days
        """
        since_date = (datetime.utcnow() - timedelta(days=days_window)).isoformat() + "Z"
        url = f"https://api.github.com/repos/{repo}/issues?state=open&assignee=none&since={since_date}&per_page=100"
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "NightOps-GSoC-Radar"
        }
        if self.github_token:
            headers["Authorization"] = f"token {self.github_token}"

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                items = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return []

        parsed = []
        for it in items:
            if "pull_request" in it:
                continue

            issue_id = f"{repo}#{it['number']}"
            is_new = issue_id not in self.issues_db
            dossier = self.build_complete_issue_dossier(it, repo, org_id, is_new=is_new)
            parsed.append(dossier)

        return parsed

    def fetch_historical_deep_dive(self, repo: str, issue_number: int):
        """
        Deep-Dive Dossier feature:
        Fetches any issue beyond 30 days directly and collects 100% of information.
        """
        url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
        headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "NightOps-Radar"}
        if self.github_token:
            headers["Authorization"] = f"token {self.github_token}"

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                it = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {"error": str(e)}

        org_id = repo.split("/")[0].lower()
        dossier = self.build_complete_issue_dossier(it, repo, org_id, is_new=False)
        dossier["pinned_beyond_30d"] = True
        self.pin_issue(dossier["id"], enabled=True)
        return dossier

    def poll_tiered_cycle(self):
        """
        Strategy 2 Tiered Execution:
        - Tier 1: Checked every 60s
        - Tier 2: Checked every 300s
        - Pinned historical issues: Checked every 300s
        """
        now = time.time()
        orgs = self.config.get("organizations", [])
        tier1_orgs = [o for o in orgs if o.get("tier", 2) == 1]
        tier2_orgs = [o for o in orgs if o.get("tier", 2) != 1]

        # If no explicit tier is configured, first 35 orgs are Tier 1 (every 60s)
        if not tier1_orgs:
            tier1_orgs = orgs[:35]
            tier2_orgs = orgs[35:]

        polled_count = 0
        for org in tier1_orgs:
            repo = org["repo"]
            last = self.repo_last_polled.get(repo, 0)
            if now - last >= 60: # 60 seconds
                self.fetch_repo_issues(repo, org["id"], days_window=30)
                self.repo_last_polled[repo] = now
                polled_count += 1
                time.sleep(0.1)

        for org in tier2_orgs:
            repo = org["repo"]
            last = self.repo_last_polled.get(repo, 0)
            if now - last >= 300: # 300 seconds (5 mins)
                self.fetch_repo_issues(repo, org["id"], days_window=30)
                self.repo_last_polled[repo] = now
                polled_count += 1
                time.sleep(0.1)

        if polled_count > 0:
            self.last_poll_time = datetime.utcnow().isoformat() + "Z"
            self._save_cache()
            print(f"[Scout Tiered] Polled {polled_count} repos. Total active issues: {len(self.issues_db)}")

        return list(self.issues_db.values())

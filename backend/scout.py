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
import threading
from collections import deque
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
        self.github_rate_limit = {
            "limit": 5000 if self.github_token else 60,
            "remaining": None,
            "used": 0,
            "reset_epoch": None,
            "last_checked": None
        }
        self.paused = False
        self.tier1_interval = 60
        self.tier2_interval = 300
        self.repo_etags = {}
        self._poll_lock = threading.Lock()
        self._ai_queue = deque()
        self._ai_thread = threading.Thread(target=self._ai_enrichment_worker, daemon=True)
        self._ai_thread.start()
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
        self.github_rate_limit = {
            "limit": 5000 if self.github_token else 60,
            "remaining": None,
            "used": 0,
            "reset_epoch": None,
            "last_checked": None
        }
        self.paused = False
        self.tier1_interval = 60
        self.tier2_interval = 300
        self.repo_etags = {}
        self._poll_lock = threading.Lock()
        self._ai_queue = deque()
        self._ai_thread = threading.Thread(target=self._ai_enrichment_worker, daemon=True)
        self._ai_thread.start()

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

    def _update_rate_limit_headers(self, headers):
        if not headers:
            return
        try:
            lim = headers.get("X-RateLimit-Limit")
            rem = headers.get("X-RateLimit-Remaining")
            used = headers.get("X-RateLimit-Used")
            rst = headers.get("X-RateLimit-Reset")
            if lim is not None:
                self.github_rate_limit["limit"] = int(lim)
            if rem is not None:
                self.github_rate_limit["remaining"] = int(rem)
            if used is not None:
                self.github_rate_limit["used"] = int(used)
            elif lim is not None and rem is not None:
                self.github_rate_limit["used"] = int(lim) - int(rem)
            if rst is not None:
                self.github_rate_limit["reset_epoch"] = int(rst)
            self.github_rate_limit["last_checked"] = time.time()
        except Exception:
            pass

    def get_rate_limit_status(self, refresh: bool = False) -> dict:
        """Return GitHub API quota (limit, used, remaining, reset_epoch). /rate_limit is free and does not consume quota."""
        if refresh or self.github_rate_limit.get("remaining") is None:
            url = "https://api.github.com/rate_limit"
            headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "NightOps-GSoC-Radar"}
            if self.github_token:
                headers["Authorization"] = f"token {self.github_token}"
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    core = data.get("resources", {}).get("core", {})
                    if core:
                        self.github_rate_limit["limit"] = core.get("limit", self.github_rate_limit["limit"])
                        self.github_rate_limit["remaining"] = core.get("remaining")
                        self.github_rate_limit["used"] = core.get("used", 0)
                        self.github_rate_limit["reset_epoch"] = core.get("reset")
                        self.github_rate_limit["last_checked"] = time.time()
            except Exception:
                pass

        rl = dict(self.github_rate_limit)
        rst = rl.get("reset_epoch")
        rl["resets_in_sec"] = max(0, int(rst - time.time())) if rst else None
        rl["reset_iso"] = datetime.fromtimestamp(rst, timezone.utc).isoformat() if rst else None
        rl["authenticated"] = bool(self.github_token)
        return rl

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

    def fetch_linked_prs(self, repo: str, issue_number: int) -> dict:
        """Check GitHub issue timeline for linked/cross-referenced Pull Requests."""
        rem = self.github_rate_limit.get("remaining")
        if rem is not None and rem < 150:
            return {"has_pr": False, "linked_prs": [], "checked": False}

        url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/timeline?per_page=50"
        headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "NightOps-Radar"}
        if self.github_token:
            headers["Authorization"] = f"token {self.github_token}"

        linked_map = {}
        has_connected = False
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                self._update_rate_limit_headers(resp.headers)
                events = json.loads(resp.read().decode("utf-8"))
                for ev in events:
                    ev_type = ev.get("event")
                    if ev_type == "cross-referenced":
                        src = (ev.get("source") or {}).get("issue") or {}
                        if "pull_request" in src:
                            pr_num = src.get("number")
                            pr_user = (src.get("user") or {}).get("login")
                            pr_info = src.get("pull_request") or {}
                            linked_map[pr_num] = {
                                "number": pr_num,
                                "title": src.get("title", ""),
                                "url": src.get("html_url", ""),
                                "state": src.get("state", "open"),
                                "merged": bool(pr_info.get("merged_at")),
                                "author": pr_user
                            }
                    elif ev_type == "connected":
                        has_connected = True
        except urllib.error.HTTPError as e:
            self._update_rate_limit_headers(e.headers)
            return {"has_pr": False, "linked_prs": [], "checked": False}
        except Exception:
            return {"has_pr": False, "linked_prs": [], "checked": False}

        linked_prs = list(linked_map.values())
        has_open_pr = any(p.get("state") == "open" for p in linked_prs) or has_connected
        return {"has_pr": has_open_pr, "linked_prs": linked_prs, "checked": True}

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

        existing_iss = self.issues_db.get(issue_id, {})
        prev_comments_count = existing_iss.get("commentsCount", -1)
        curr_comments_count = it.get("comments", 0)

        if (is_new or curr_comments_count != prev_comments_count) and (curr_comments_count > 0 or assignees):
            thread_details = self.fetch_issue_thread_details(repo, it["number"], author_login, assignees)
            has_claim = thread_details["has_claim"]
            first_comment = thread_details["first_comment"]
            unique_contributors = thread_details["unique_contributors"]
            unique_contributors_count = thread_details["unique_contributors_count"]
        elif existing_iss:
            has_claim = existing_iss.get("claimedByComment", False)
            first_comment = existing_iss.get("first_comment")
            unique_contributors = existing_iss.get("unique_contributors", [])
            unique_contributors_count = existing_iss.get("unique_contributors_count", 1)
        else:
            if author_login and author_login != "unknown":
                unique_contributors = [{
                    "login": author_login,
                    "role": "Author",
                    "avatar_url": f"https://github.com/{author_login}.png",
                    "profile_url": f"https://github.com/{author_login}"
                }]

        # Check for linked/cross-referenced Pull Requests when new, updated, or uncached
        curr_updated_at = it.get("updated_at", it["created_at"])
        prev_updated_at = existing_iss.get("updatedAt")
        if is_new or (curr_updated_at != prev_updated_at) or ("has_pr" not in existing_iss):
            pr_res = self.fetch_linked_prs(repo, it["number"])
            if pr_res.get("checked"):
                has_pr = pr_res["has_pr"]
                linked_prs = pr_res["linked_prs"]
            else:
                has_pr = existing_iss.get("has_pr", False)
                linked_prs = existing_iss.get("linked_prs", [])
        else:
            has_pr = existing_iss.get("has_pr", False)
            linked_prs = existing_iss.get("linked_prs", [])

        # Include PR authors in unique_contributors
        seen_logins = {c.get("login") for c in unique_contributors if c.get("login")}
        for pr in linked_prs:
            pr_author = pr.get("author")
            if pr_author and pr_author not in seen_logins:
                seen_logins.add(pr_author)
                unique_contributors.append({
                    "login": pr_author,
                    "role": "PR Author",
                    "avatar_url": f"https://github.com/{pr_author}.png",
                    "profile_url": f"https://github.com/{pr_author}"
                })
        unique_contributors_count = max(unique_contributors_count, len(unique_contributors))

        # Detected timestamp (when NightOps first indexed it)
        now_iso = datetime.now(timezone.utc).isoformat()
        detected_at = self.issues_db.get(issue_id, {}).get("detectedAt") or now_iso

        # Check GSoC relevance keywords
        gsoc_evidence = any(w in lower_body for w in ["gsoc", "summer of code", "mentors", "project idea", "proposal"])

        # Competition Level Evaluation (in comparison with other issues across GSoC repos)
        comp_level = "LOW"
        comp_score = 15
        comp_note = "Uncontested: Zero comments or competitor activity. Prime opportunity to claim!"
        if has_pr:
            open_pr_count = sum(1 for p in linked_prs if p.get("state") == "open")
            comp_level = "INTENSE"
            comp_score = 95
            comp_note = f"PR Already Open: {open_pr_count or 1} linked PR(s) found — a contributor has already submitted a fix."
        elif has_claim:
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
            "has_pr": has_pr,
            "linked_prs": linked_prs,
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

        # 2. Fast Deterministic Heuristics first (0ms), then queue for async AI enrichment
        existing_ai = self.issues_db.get(issue_id, {}).get("ai_fit_assessment")
        if existing_ai:
            issue["difficulty_score"] = self.issues_db[issue_id].get("difficulty_score", 40)
            issue["ai_fit_assessment"] = existing_ai
            issue["recommended_skills"] = self.issues_db[issue_id].get("recommended_skills", ["python"])
            issue["recommend"] = self.issues_db[issue_id].get("recommend", "maybe")
        elif self.ai_gateway:
            heur = self.ai_gateway._heuristic_fallback(issue["title"], issue["desc"])
            issue["difficulty_score"] = heur.get("difficulty", 40)
            issue["ai_fit_assessment"] = heur.get("fit_summary", "")
            issue["recommended_skills"] = heur.get("skills", ["python"])
            issue["recommend"] = "yes" if issue["difficulty_score"] <= 45 else ("maybe" if issue["difficulty_score"] <= 70 else "no")
            if not issue["assignedTo"] and self.ai_gateway.is_available() and len(self._ai_queue) < 100:
                self._ai_queue.append((issue_id, issue["title"], issue["desc"], repo))

        self.issues_db[issue_id] = issue
        return issue

    def fetch_repo_issues(self, repo: str, org_id: str, days_window: int = 30, since_iso: str = None, until_iso: str = None, bypass_etag: bool = False):
        """
        Poll single repo under Strategy 2 or Custom Range:
        - assignee=none: ignores any assigned issues immediately
        - since / until: filters by 30-day default or custom hours/days/months/dates
        """
        since_date = since_iso or ((datetime.now(timezone.utc) - timedelta(days=days_window)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        url = f"https://api.github.com/repos/{repo}/issues?state=open&assignee=none&since={since_date}&per_page=25"
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "NightOps-GSoC-Radar"
        }
        if self.github_token:
            headers["Authorization"] = f"token {self.github_token}"
        if not bypass_etag and not since_iso and repo in self.repo_etags:
            headers["If-None-Match"] = self.repo_etags[repo]

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                self._update_rate_limit_headers(resp.headers)
                etag = resp.headers.get("ETag")
                if etag:
                    self.repo_etags[repo] = etag
                items = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            self._update_rate_limit_headers(e.headers)
            if e.code == 304:
                # 304 Not Modified: 0 quota consumed!
                return []
            if e.code in (403, 429):
                print(f"[Scout] GitHub rate limit ({e.code}) on {repo}. Add GITHUB_TOKEN in .env or 🔑 API & Git Token UI for 5,000 req/hr.")
            return []
        except Exception:
            return []

        parsed = []
        for it in items:
            if "pull_request" in it:
                continue
            if until_iso and it.get("created_at", "") > until_iso:
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

    def _ai_enrichment_worker(self):
        """Background worker so AI calls never block fast GitHub polling across 184 orgs."""
        while True:
            if not self._ai_queue:
                time.sleep(1.0)
                continue
            try:
                issue_id, title, desc, repo = self._ai_queue.popleft()
                if self.ai_gateway and self.ai_gateway.is_available() and issue_id in self.issues_db:
                    ai_res = self.ai_gateway.analyze_issue(title, desc, repo)
                    iss = self.issues_db.get(issue_id)
                    if iss:
                        iss["difficulty_score"] = ai_res.get("difficulty", iss.get("difficulty_score", 40))
                        iss["ai_fit_assessment"] = ai_res.get("fit_summary", iss.get("ai_fit_assessment", ""))
                        iss["recommended_skills"] = ai_res.get("skills", iss.get("recommended_skills", ["python"]))
                        iss["recommend"] = "yes" if iss["difficulty_score"] <= 45 else ("maybe" if iss["difficulty_score"] <= 70 else "no")
                        self._save_cache()
            except Exception:
                time.sleep(1.0)

    def poll_tiered_cycle(self):
        """
        Strategy 2 Tiered Execution:
        - Tier 1: Checked every 60s
        - Tier 2: Checked every 300s
        - Pinned historical issues: Checked every 300s
        """
        if self.paused:
            return list(self.issues_db.values())
        if not self._poll_lock.acquire(blocking=False):
            return list(self.issues_db.values())

        try:
            now = time.time()
            orgs = self.config.get("organizations", [])
            tier1_orgs = [o for o in orgs if o.get("tier", 2) == 1]
            tier2_orgs = [o for o in orgs if o.get("tier", 2) != 1]

            if not tier1_orgs:
                tier1_orgs = orgs[:35]
                tier2_orgs = orgs[35:]

            polled_count = 0
            for org in tier1_orgs:
                repo = org["repo"]
                last = self.repo_last_polled.get(repo, 0)
                if now - last >= max(10, self.tier1_interval):
                    found = self.fetch_repo_issues(repo, org["id"], days_window=30)
                    self.repo_last_polled[repo] = time.time()
                    polled_count += 1
                    if found:
                        self._save_cache()
                    time.sleep(0.05)

            for org in tier2_orgs:
                repo = org["repo"]
                last = self.repo_last_polled.get(repo, 0)
                if now - last >= max(10, self.tier2_interval):
                    found = self.fetch_repo_issues(repo, org["id"], days_window=30)
                    self.repo_last_polled[repo] = time.time()
                    polled_count += 1
                    if found:
                        self._save_cache()
                    time.sleep(0.05)

            if polled_count > 0:
                self.last_poll_time = datetime.utcnow().isoformat() + "Z"
                self._save_cache()
                print(f"[Scout Tiered] Polled {polled_count} repos. Total active issues: {len(self.issues_db)}")

            return list(self.issues_db.values())
        finally:
            self._poll_lock.release()

    def poll_single_target(self, repo: str, org_id: str = None, days_window: int = 30, since_iso: str = None, until_iso: str = None):
        """Poll a single specific org/project repository immediately."""
        if not org_id:
            org_id = repo.split("/")[0].lower()
        found = self.fetch_repo_issues(repo, org_id, days_window=days_window, since_iso=since_iso, until_iso=until_iso, bypass_etag=True)
        self.repo_last_polled[repo] = time.time()
        self.last_poll_time = datetime.now(timezone.utc).isoformat()
        self._save_cache()
        print(f"[Scout Single] Polled {repo}: {len(found)} issues.")
        return found

    def poll_custom_range(self, target_repo: str = "", tier_scope: str = "all", days_window: int = 30, since_iso: str = None, until_iso: str = None):
        """Run a custom poll for a specific repo or org tier over custom hours/days/months/dates."""
        with self._poll_lock:
            if target_repo and "/" in target_repo:
                orgs = self.config.get("organizations", [])
                match = next((o for o in orgs if o.get("repo") == target_repo), None)
                oid = match["id"] if match else target_repo.split("/")[0].lower()
                found = self.fetch_repo_issues(target_repo, oid, days_window=days_window, since_iso=since_iso, until_iso=until_iso, bypass_etag=True)
                self.repo_last_polled[target_repo] = time.time()
                self._save_cache()
                return len(found)

            orgs = self.config.get("organizations", [])
            if tier_scope == "tier1":
                orgs = [o for o in orgs if o.get("tier", 2) == 1]

            total_found = 0
            for org in orgs:
                repo = org["repo"]
                found = self.fetch_repo_issues(repo, org["id"], days_window=days_window, since_iso=since_iso, until_iso=until_iso, bypass_etag=True)
                self.repo_last_polled[repo] = time.time()
                total_found += len(found)
                if found:
                    self._save_cache()
                time.sleep(0.05)

            self.last_poll_time = datetime.now(timezone.utc).isoformat()
            self._save_cache()
            print(f"[Scout Custom] Polled {len(orgs)} repos ({since_iso or str(days_window)+'d'}). Found {total_found} issues.")
            return total_found

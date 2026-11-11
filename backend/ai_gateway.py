"""
AI Gateway for NightOps Radar.
Strict Rule: Plain code handles 95% of tasks.
AI is escalated strictly for ambiguous issue threads and tactical 1-line fit summaries.
Supports Google Gemini, OpenAI, or local heuristic fallback.
"""
import os
import json
import urllib.request
import urllib.error

class AIGateway:
    def __init__(self, provider="gemini"):
        self.gemini_key = os.getenv("GEMINI_API_KEY", "")
        self.openai_key = os.getenv("OPENAI_API_KEY", "")

    def is_available(self) -> bool:
        return bool(self.gemini_key or self.openai_key)

    def analyze_issue(self, title: str, body: str, repo: str) -> dict:
        """
        Escalation Boundary:
        Takes issue title, body, and repo.
        Returns dict: {"difficulty": 1-100, "fit_summary": str, "skills": list}
        """
        if self.gemini_key:
            return self._call_gemini(title, body, repo)
        elif self.openai_key:
            return self._call_openai(title, body, repo)
        return self._heuristic_fallback(title, body)

    def _call_gemini(self, title: str, body: str, repo: str) -> dict:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={self.gemini_key}"
        prompt = (
            f"You are the NightOps AI Gateway triage engine. Analyze this open-source issue for GSoC contributors:\n"
            f"Repository: {repo}\nTitle: {title}\nDescription:\n{body[:1200]}\n\n"
            f"Respond ONLY in valid raw JSON with this exact schema:\n"
            f'{{"difficulty": <int 1-100>, "fit_summary": "<1 concise sentence tactical summary>", "skills": ["<skill1>", "<skill2>"]}}'
        )
        payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})

        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                # Clean markdown backticks if returned
                if text.startswith("```"):
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                return json.loads(text.strip())
        except Exception as e:
            print(f"[AI Gateway] Gemini call error: {e}, falling back to code heuristics.")
            return self._heuristic_fallback(title, body)

    def _call_openai(self, title: str, body: str, repo: str) -> dict:
        url = "https://api.openai.com/v1/chat/completions"
        prompt = (
            f"Analyze this GSoC issue:\nRepo: {repo}\nTitle: {title}\nBody:\n{body[:1200]}\n"
            f'Output raw JSON: {{"difficulty": 1-100, "fit_summary": "1 sentence", "skills": []}}'
        )
        payload = json.dumps({
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"}
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.openai_key}"
        })
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return json.loads(data["choices"][0]["message"]["content"])
        except Exception as e:
            print(f"[AI Gateway] OpenAI call error: {e}, falling back to heuristics.")
            return self._heuristic_fallback(title, body)

    def _heuristic_fallback(self, title: str, body: str) -> dict:
        """Deterministic fallback when no AI token is configured or network fails."""
        text = (title + " " + body).lower()
        diff = 35
        if any(w in text for w in ["typo", "doc", "readme", "spelling", "docstring"]):
            diff = 15
        elif any(w in text for w in ["refactor", "algorithm", "race condition", "memory leak", "deadlock"]):
            diff = 75
        elif any(w in text for w in ["test", "coverage", "add test"]):
            diff = 30

        skills = []
        for kw in ["python", "rust", "c++", "javascript", "react", "go", "sql", "docker", "api"]:
            if kw in text:
                skills.append(kw)

        return {
            "difficulty": diff,
            "fit_summary": f"Targeted work identified in {skills[0] if skills else 'core codebase'}. Review discussion thread before claiming.",
            "skills": skills or ["python"]
        }

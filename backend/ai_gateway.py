"""
AI Gateway for NightOps Radar.
Strict Rule: Plain code handles 95% of tasks.
AI is escalated strictly for ambiguous issue threads and tactical 1-line fit summaries.
Cascade Order:
  1. Google Gemini (gemini-3.5-flash-lite / gemini-3.8-flash)
  2. Open-Source Cloud AI when Gemini hits 429/503/timeout:
     - Groq (Llama 3.1/3.3) via GROQ_API_KEY
     - OpenRouter Free Open-Source Router via OPENROUTER_API_KEY
     - Hugging Face Router (Qwen 2.5 / Llama) via HF_TOKEN
  3. Local Open-Source AI (Ollama on localhost:11434 or OLLAMA_BASE_URL)
  4. OpenAI (optional)
  5. Deterministic Code Heuristics
"""
import os
import time
import json
import urllib.request
import urllib.error

PREFERRED_GEMINI_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-2.5-flash",
    "gemini-2.0-flash"
]

class AIGateway:
    def __init__(self, provider="gemini"):
        self.gemini_key = os.getenv("GEMINI_API_KEY", "")
        self.openai_key = os.getenv("OPENAI_API_KEY", "")
        self.groq_key = os.getenv("GROQ_API_KEY", "")
        self.hf_token = os.getenv("HF_TOKEN", "")
        self.openrouter_key = os.getenv("OPENROUTER_API_KEY", "")
        self.ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        self.ollama_model = os.getenv("OLLAMA_MODEL", "")

        self._active_gemini_model = None
        self._discovered_models = None
        self._cooldown_until = 0.0
        self._provider_cooldowns = {}
        self._last_call_ts = 0.0
        self._min_interval_sec = 4.5
        self._ollama_checked_at = 0.0
        self._ollama_available = False

    def has_open_source_ai(self) -> bool:
        return bool(self.groq_key or self.hf_token or self.openrouter_key or self._check_ollama())

    def is_available(self) -> bool:
        return bool(self.gemini_key or self.openai_key or self.groq_key or self.hf_token or self.openrouter_key or self._check_ollama())

    def _in_cooldown(self, provider_key: str) -> bool:
        return time.time() < self._provider_cooldowns.get(provider_key, 0.0)

    def _set_cooldown(self, provider_key: str, seconds: float, reason: str):
        self._provider_cooldowns[provider_key] = time.time() + seconds
        print(f"[AI Gateway] {provider_key} paused for {int(seconds)}s ({reason}).")

    def analyze_issue(self, title: str, body: str, repo: str) -> dict:
        """
        Escalation Boundary:
        Takes issue title, body, and repo.
        Returns dict: {"difficulty": 1-100, "fit_summary": str, "skills": list}
        """
        # 1. Try Gemini if configured and not in cooldown
        if self.gemini_key and time.time() >= self._cooldown_until:
            res = self._call_gemini(title, body, repo)
            if res is not None:
                return res

        # 2. Fallback to Open-Source AI (Groq -> OpenRouter -> HuggingFace -> Local Ollama)
        oss_res = self._call_open_source_ai(title, body, repo)
        if oss_res is not None:
            return oss_res

        # 3. Fallback to OpenAI if configured
        if self.openai_key and not self._in_cooldown("OpenAI"):
            res = self._call_openai(title, body, repo)
            if res is not None:
                return res

        # 4. Deterministic Code Heuristics
        return self._heuristic_fallback(title, body)

    def _build_prompt(self, title: str, body: str, repo: str) -> str:
        return (
            f"You are the NightOps AI Gateway triage engine. Analyze this open-source issue for GSoC contributors:\n"
            f"Repository: {repo}\nTitle: {title}\nDescription:\n{body[:1200]}\n\n"
            f"Respond ONLY in valid raw JSON with this exact schema:\n"
            f'{{"difficulty": <int 1-100>, "fit_summary": "<1 concise sentence tactical summary>", "skills": ["<skill1>", "<skill2>"]}}'
        )

    def _parse_json_response(self, text: str) -> dict:
        t = text.strip()
        if "```" in t:
            parts = t.split("```")
            if len(parts) >= 2:
                t = parts[1]
                if t.startswith("json"):
                    t = t[4:]
        return json.loads(t.strip())

    def _discover_gemini_models(self) -> list:
        if self._discovered_models is not None:
            return self._discovered_models
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={self.gemini_key}"
        req = urllib.request.Request(url, headers={
            "x-goog-api-key": self.gemini_key,
            "User-Agent": "NightOps-Radar/1.0"
        })
        found = []
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for m in data.get("models", []):
                    methods = m.get("supportedGenerationMethods", [])
                    name = m.get("name", "").replace("models/", "")
                    if "generateContent" in methods and "flash" in name.lower() and "tts" not in name.lower() and "image" not in name.lower():
                        found.append(name)
        except Exception:
            pass
        found.sort(reverse=True)
        self._discovered_models = found
        return found

    def _call_gemini(self, title: str, body: str, repo: str):
        now = time.time()
        elapsed = now - self._last_call_ts
        if elapsed < self._min_interval_sec:
            time.sleep(self._min_interval_sec - elapsed)

        prompt = self._build_prompt(title, body, repo)
        payload = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.2,
                "maxOutputTokens": 256
            }
        }).encode("utf-8")

        candidates = []
        if self._active_gemini_model:
            candidates.append(self._active_gemini_model)
        for m in PREFERRED_GEMINI_MODELS:
            if m not in candidates:
                candidates.append(m)

        tried = set()
        idx = 0
        while idx < len(candidates):
            model = candidates[idx]
            idx += 1
            if model in tried:
                continue
            tried.add(model)

            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": self.gemini_key,
                    "User-Agent": "NightOps-Radar/1.0"
                }
            )
            try:
                self._last_call_ts = time.time()
                with urllib.request.urlopen(req, timeout=12) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                    self._active_gemini_model = model
                    return self._parse_json_response(text)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    if self._discovered_models is None:
                        for dm in self._discover_gemini_models():
                            if dm not in tried and dm not in candidates:
                                candidates.append(dm)
                    continue
                if e.code in (429, 503):
                    self._cooldown_until = time.time() + 60.0
                    print(f"[AI Gateway] Gemini ({model}) HTTP {e.code}. Pausing Gemini for 60s and escalating to Open-Source AI.")
                    return None
                self._cooldown_until = time.time() + 60.0
                print(f"[AI Gateway] Gemini ({model}) HTTP {e.code}, escalating to Open-Source AI fallback.")
                return None
            except Exception as e:
                self._cooldown_until = time.time() + 30.0
                print(f"[AI Gateway] Gemini ({model}) call error: {e}, escalating to Open-Source AI fallback.")
                return None

        return None

    def _call_openai_compatible(self, url: str, api_key: str, model: str, title: str, body: str, repo: str, provider_name: str, extra_headers: dict = None):
        if self._in_cooldown(provider_name):
            return None

        prompt = self._build_prompt(title, body, repo)
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 256
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "NightOps-Radar/1.0",
            "Accept": "application/json"
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if extra_headers:
            headers.update(extra_headers)

        req = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = data["choices"][0]["message"]["content"]
                return self._parse_json_response(content)
        except urllib.error.HTTPError as e:
            cd = 300.0 if e.code in (401, 403, 404) else 60.0
            self._set_cooldown(provider_name, cd, f"HTTP {e.code}")
            return None
        except Exception as e:
            self._set_cooldown(provider_name, 60.0, str(e))
            return None

    def _check_ollama(self) -> bool:
        now = time.time()
        if now - self._ollama_checked_at < 60.0:
            return self._ollama_available
        self._ollama_checked_at = now
        try:
            req = urllib.request.Request(f"{self.ollama_url}/api/tags", headers={"User-Agent": "NightOps-Radar/1.0"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                models = data.get("models", [])
                if models:
                    if not self.ollama_model:
                        self.ollama_model = models[0].get("name", "llama3.2")
                    self._ollama_available = True
                    return True
        except Exception:
            pass
        self._ollama_available = False
        return False

    def _call_open_source_ai(self, title: str, body: str, repo: str):
        # 1. Groq (Open-source Llama 3.1 - User-Agent required to pass Cloudflare)
        if self.groq_key:
            res = self._call_openai_compatible(
                "https://api.groq.com/openai/v1/chat/completions",
                self.groq_key,
                "llama-3.1-8b-instant",
                title, body, repo, "Groq OSS (Llama-3.1)"
            )
            if res:
                return res

        # 2. OpenRouter Free Open-Source Auto-Router
        if self.openrouter_key:
            res = self._call_openai_compatible(
                "https://openrouter.ai/api/v1/chat/completions",
                self.openrouter_key,
                "openrouter/free",
                title, body, repo, "OpenRouter OSS (Free)",
                extra_headers={
                    "HTTP-Referer": "https://github.com/Abhishekpandey123-git/NightOps-radar",
                    "X-Title": "NightOps-Radar"
                }
            )
            if res:
                return res

        # 3. Hugging Face Router (Open-source Qwen 2.5)
        if self.hf_token:
            res = self._call_openai_compatible(
                "https://router.huggingface.co/v1/chat/completions",
                self.hf_token,
                "Qwen/Qwen2.5-7B-Instruct",
                title, body, repo, "HuggingFace OSS (Qwen-2.5)"
            )
            if res:
                return res

        # 4. Local Ollama Open-Source Model
        if self._check_ollama():
            model = self.ollama_model or "llama3.2"
            res = self._call_openai_compatible(
                f"{self.ollama_url}/v1/chat/completions",
                "",
                model,
                title, body, repo, f"Local Ollama ({model})"
            )
            if res:
                return res

        return None

    def _call_openai(self, title: str, body: str, repo: str):
        return self._call_openai_compatible(
            "https://api.openai.com/v1/chat/completions",
            self.openai_key,
            "gpt-4o-mini",
            title, body, repo, "OpenAI"
        )

    def _heuristic_fallback(self, title: str, body: str) -> dict:
        """Deterministic fallback when no AI token is configured or rate-limited."""
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

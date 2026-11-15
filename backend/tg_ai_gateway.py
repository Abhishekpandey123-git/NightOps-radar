import os
import json
import time
import urllib.request
import urllib.error


class AIGateway:
    """
    Code-First AI Fallback Cascade for ambiguous Telegram listings:
    Gemini -> Groq -> OpenRouter -> HuggingFace -> Local Ollama -> OpenAI.
    """
    def __init__(self):
        self.cooldowns = {}
        self.last_gemini_call = 0.0
        self.gemini_min_interval = 4.5
        self.discovered_gemini_model = None

    def _in_cooldown(self, provider: str) -> bool:
        return time.time() < self.cooldowns.get(provider, 0)

    def _set_cooldown(self, provider: str, seconds: int = 300):
        self.cooldowns[provider] = time.time() + seconds

    def is_available(self) -> bool:
        keys = [
            os.environ.get("GEMINI_API_KEY"),
            os.environ.get("GROQ_API_KEY"),
            os.environ.get("OPENROUTER_API_KEY"),
            os.environ.get("HF_TOKEN"),
            os.environ.get("OPENAI_API_KEY"),
            os.environ.get("OLLAMA_URL"),
        ]
        return any(bool(k and str(k).strip()) for k in keys)

    def enrich_listing(self, text: str) -> dict:
        prompt = (
            "Extract structured JSON from this Telegram digital-product message. "
            "Return ONLY valid JSON with keys: "
            "\"intent\" (one of \"BUY\", \"SELL\", \"NEW_PRODUCT\", \"OTHER\"), "
            "\"product_name\" (short string), "
            "\"category\" (string), "
            "\"price\" (string or null), "
            "\"summary\" (max 15 words).\n\n"
            f"Message:\n{text[:1200]}"
        )

        providers = [
            ("gemini", self._call_gemini),
            ("groq", self._call_groq),
            ("openrouter", self._call_openrouter),
            ("huggingface", self._call_hf),
            ("openai", self._call_openai),
            ("ollama", self._call_ollama),
        ]

        for name, fn in providers:
            if self._in_cooldown(name):
                continue
            try:
                res = fn(prompt)
                if res and isinstance(res, dict):
                    res["ai_provider"] = name
                    return res
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 404, 429):
                    self._set_cooldown(name, 300 if e.code == 429 else 900)
            except Exception:
                self._set_cooldown(name, 120)
        return {}

    def _parse_json_response(self, raw: str) -> dict:
        txt = (raw or "").strip()
        if txt.startswith("```"):
            txt = txt.strip("`")
            if txt.lower().startswith("json"):
                txt = txt[4:].strip()
        start = txt.find("{")
        end = txt.rfind("}")
        if start != -1 and end != -1:
            return json.loads(txt[start:end + 1])
        return {}

    def _call_gemini(self, prompt: str) -> dict:
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            return {}
        wait = self.gemini_min_interval - (time.time() - self.last_gemini_call)
        if wait > 0:
            time.sleep(wait)
        self.last_gemini_call = time.time()

        model = self.discovered_gemini_model or "gemini-2.5-flash-lite"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1}
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "NightOps-Telegram-Radar/1.0"}
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return self._parse_json_response(text)

    def _call_groq(self, prompt: str) -> dict:
        key = os.environ.get("GROQ_API_KEY", "").strip()
        if not key:
            return {}
        url = "https://api.groq.com/openai/v1/chat/completions"
        payload = {
            "model": "llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "User-Agent": "NightOps-Telegram-Radar/1.0"
            }
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return self._parse_json_response(data["choices"][0]["message"]["content"])

    def _call_openrouter(self, prompt: str) -> dict:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            return {}
        url = "https://openrouter.ai/api/v1/chat/completions"
        payload = {
            "model": "openrouter/free",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "User-Agent": "NightOps-Telegram-Radar/1.0"
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return self._parse_json_response(data["choices"][0]["message"]["content"])

    def _call_hf(self, prompt: str) -> dict:
        key = os.environ.get("HF_TOKEN", "").strip()
        if not key:
            return {}
        url = "https://router.huggingface.co/v1/chat/completions"
        payload = {
            "model": "Qwen/Qwen2.5-7B-Instruct",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "User-Agent": "NightOps-Telegram-Radar/1.0"
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return self._parse_json_response(data["choices"][0]["message"]["content"])

    def _call_openai(self, prompt: str) -> dict:
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            return {}
        url = "https://api.openai.com/v1/chat/completions"
        payload = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return self._parse_json_response(data["choices"][0]["message"]["content"])

    def _call_ollama(self, prompt: str) -> dict:
        base = os.environ.get("OLLAMA_URL", "").strip()
        if not base:
            return {}
        url = f"{base.rstrip('/')}/api/generate"
        payload = {"model": "llama3.1", "prompt": prompt, "stream": False}
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return self._parse_json_response(data.get("response", ""))

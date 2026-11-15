import os
import re
import json
import time
import html as html_lib
import threading
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from html.parser import HTMLParser
import yaml

from backend.classifier import classify_and_extract
from backend.tg_ai_gateway import AIGateway


class _TelegramWebParser(HTMLParser):
    """Zero-dependency HTML parser for public https://t.me/s/<channel> pages."""
    def __init__(self):
        super().__init__()
        self.messages = []
        self._curr = None
        self._in_text = False
        self._text_parts = []
        self._in_author = False
        self._author_parts = []

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        cls = attr_dict.get("class", "")
        if tag == "div" and "tgme_widget_message " in f"{cls} " and "data-post" in attr_dict:
            self._curr = {
                "post_id": attr_dict["data-post"],
                "url": f"https://t.me/{attr_dict['data-post']}",
                "text": "",
                "author": None,
                "timestamp": None,
                "links": []
            }
        elif self._curr is not None:
            if tag == "div" and "tgme_widget_message_text" in cls:
                self._in_text = True
                self._text_parts = []
            elif tag == "a" and "tgme_widget_message_owner_name" in cls:
                self._in_author = True
                self._author_parts = []
            elif tag == "time" and "datetime" in attr_dict:
                self._curr["timestamp"] = attr_dict["datetime"]
            elif tag == "a" and "href" in attr_dict:
                href = attr_dict["href"]
                if href.startswith("http") and "t.me/" not in href:
                    self._curr["links"].append(href)
            elif self._in_text and tag == "br":
                self._text_parts.append("\n")

    def handle_endtag(self, tag):
        if self._in_text and tag == "div":
            self._in_text = False
            if self._curr is not None:
                self._curr["text"] = html_lib.unescape("".join(self._text_parts)).strip()
        elif self._in_author and tag == "a":
            self._in_author = False
            if self._curr is not None:
                self._curr["author"] = html_lib.unescape("".join(self._author_parts)).strip()
        elif self._curr is not None and tag == "div" and self._curr.get("text"):
            # Finalize when message text is captured and outer blocks close
            if not any(m["post_id"] == self._curr["post_id"] for m in self.messages):
                self.messages.append(self._curr)

    def handle_data(self, data):
        if self._in_text:
            self._text_parts.append(data)
        elif self._in_author:
            self._author_parts.append(data)


class TelegramMarketScout:
    def __init__(self, config_path: str = "telegram_config.yaml"):
        self.config_path = config_path
        self.config = self._load_config()
        self.data_dir = "data"
        os.makedirs(self.data_dir, exist_ok=True)
        self.cache_file = os.path.join(self.data_dir, "tg_listings_cache.json")

        self.listings_db = {}
        self._load_cache()

        self.ai_gateway = AIGateway()
        self._poll_lock = threading.Lock()
        self._ai_queue = deque()
        self.paused = False
        self.poll_interval = int(self.config.get("polling", {}).get("interval_seconds", 120))
        self.last_poll_iso = None
        self.last_poll_stats = {"checked": 0, "new": 0, "errors": 0}

        self._ai_thread = threading.Thread(target=self._ai_enrichment_worker, daemon=True)
        self._ai_thread.start()

    def _load_config(self) -> dict:
        if os.path.exists(self.config_path):
            with open(self.config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return {"sources": [], "polling": {"interval_seconds": 120}}

    def save_config(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.config, f, sort_keys=False, allow_unicode=True)

    def _load_cache(self):
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data.get("listings", []):
                        self.listings_db[item["id"]] = item
            except Exception:
                self.listings_db = {}

    def _save_cache(self):
        items = sorted(self.listings_db.values(), key=lambda x: x.get("releasedAt", ""), reverse=True)
        payload = {
            "last_poll": self.last_poll_iso or (datetime.now(timezone.utc).isoformat()),
            "count": len(items),
            "listings": items[:2500]
        }
        with open(self.cache_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    def get_sources(self) -> list:
        return self.config.get("sources", [])

    def add_source(self, source: dict) -> dict:
        sources = self.config.setdefault("sources", [])
        sid = source.get("id") or re.sub(r"[^a-z0-9_-]", "", source.get("handle", "src").lower())
        source["id"] = sid
        if any(s.get("id") == sid for s in sources):
            return {"status": "exists", "source": source}
        sources.append(source)
        self.save_config()
        return {"status": "added", "source": source}

    def remove_source(self, source_id: str) -> bool:
        sources = self.config.get("sources", [])
        new_sources = [s for s in sources if s.get("id") != source_id]
        if len(new_sources) == len(sources):
            return False
        self.config["sources"] = new_sources
        self.save_config()
        return True

    def _build_listing(self, msg_id: str, source: dict, text: str, released_at: str, url: str, author: str = None, extra_links: list = None) -> dict:
        extracted = classify_and_extract(text, author_handle=author)
        now_iso = datetime.now(timezone.utc).isoformat()
        is_new = msg_id not in self.listings_db
        existing = self.listings_db.get(msg_id, {})

        links = list(dict.fromkeys((extracted.get("contact_links") or []) + (extra_links or [])))
        listing = {
            "id": msg_id,
            "sourceId": source["id"],
            "sourceName": source.get("name", source["id"]),
            "sourceType": source.get("type", "tg_channel"),
            "sourceHandle": source.get("handle", ""),
            "title": extracted["product_name"],
            "productName": extracted["product_name"],
            "category": extracted["category"],
            "intent": extracted["intent"],
            "price": extracted["price"],
            "priceValue": extracted["price_value"],
            "currency": extracted["currency"],
            "clientHandle": extracted["client_handle"] or (f"@{author.lstrip('@')}" if author else None),
            "mentionedHandles": extracted["mentioned_handles"],
            "contactLinks": links[:6],
            "paymentMethods": extracted["payment_methods"],
            "hasEscrow": extracted["has_escrow"],
            "url": url,
            "releasedAt": released_at or now_iso,
            "detectedAt": existing.get("detectedAt") or now_iso,
            "rawText": text,
            "summary": existing.get("summary") or text[:240],
            "aiProvider": existing.get("aiProvider"),
            "isPopping": is_new
        }

        if extracted.get("needs_ai") and not existing.get("aiProvider") and self.ai_gateway.is_available() and len(self._ai_queue) < 80:
            self._ai_queue.append((msg_id, text))

        self.listings_db[msg_id] = listing
        return listing

    def fetch_public_tg_channel(self, source: dict) -> list:
        """Scrape public Telegram channel web preview https://t.me/s/<handle> (zero auth)."""
        handle = source.get("handle", "").lstrip("@").strip()
        if not handle:
            return []
        url = f"https://t.me/s/{handle}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) NightOps-Telegram-Radar/1.0"
        })
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw_html = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"[TG-Web] Error fetching {url}: {e}")
            return []

        parser = _TelegramWebParser()
        parser.feed(raw_html)
        results = []
        for m in parser.messages:
            if not m.get("text"):
                continue
            msg_id = f"tg:{m['post_id']}"
            results.append(self._build_listing(
                msg_id=msg_id,
                source=source,
                text=m["text"],
                released_at=m.get("timestamp") or datetime.now(timezone.utc).isoformat(),
                url=m.get("url") or url,
                author=m.get("author"),
                extra_links=m.get("links", [])
            ))
        return results

    def fetch_rss_source(self, source: dict) -> list:
        """Fetch RSS/Atom feed for external digital product marketplaces / forums."""
        feed_url = source.get("url", "").strip()
        if not feed_url:
            return []
        req = urllib.request.Request(feed_url, headers={"User-Agent": "NightOps-Telegram-Radar/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                xml_data = resp.read().decode("utf-8", errors="replace")
            root = ET.fromstring(xml_data)
        except Exception as e:
            print(f"[RSS] Error fetching {feed_url}: {e}")
            return []

        results = []
        items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for item in items[:25]:
            title = (item.findtext("title") or item.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not link:
                link_el = item.find("{http://www.w3.org/2005/Atom}link")
                if link_el is not None:
                    link = link_el.attrib.get("href", "")
            desc = (
                item.findtext("description")
                or item.findtext("{http://www.w3.org/2005/Atom}summary")
                or item.findtext("{http://www.w3.org/2005/Atom}content")
                or ""
            )
            desc_clean = re.sub(r"<[^>]+>", " ", html_lib.unescape(desc)).strip()
            pub = item.findtext("pubDate") or item.findtext("{http://www.w3.org/2005/Atom}updated") or datetime.now(timezone.utc).isoformat()
            full_text = f"{title}\n{desc_clean}".strip()
            if not full_text:
                continue
            msg_id = f"rss:{source['id']}:{abs(hash(link or title))}"
            results.append(self._build_listing(
                msg_id=msg_id,
                source=source,
                text=full_text,
                released_at=pub,
                url=link or feed_url
            ))
        return results

    def fetch_telethon_group(self, source: dict) -> list:
        """
        Optional Telethon MTProto fetcher for private/public Telegram groups & channels.
        Requires TG_API_ID, TG_API_HASH, and TG_SESSION (StringSession) in .env.
        """
        api_id = os.environ.get("TG_API_ID", "").strip()
        api_hash = os.environ.get("TG_API_HASH", "").strip()
        session_str = os.environ.get("TG_SESSION", "").strip()
        if not (api_id and api_hash and session_str):
            return []
        try:
            import asyncio
            from telethon import TelegramClient
            from telethon.sessions import StringSession
        except ImportError:
            print("[Telethon] Package not installed. Install telethon for group monitoring.")
            return []

        target = source.get("handle") or source.get("url")
        if not target:
            return []

        async def _run():
            async with TelegramClient(StringSession(session_str), int(api_id), api_hash) as client:
                msgs = []
                async for msg in client.iter_messages(target, limit=30):
                    if not msg.message:
                        continue
                    sender = await msg.get_sender()
                    username = getattr(sender, "username", None)
                    msg_url = f"https://t.me/{target.lstrip('@')}/{msg.id}" if not str(target).startswith("-") else f"https://t.me/c/{str(target).lstrip('-100')}/{msg.id}"
                    msgs.append((msg.id, msg.message, msg.date.isoformat() if msg.date else None, msg_url, username))
                return msgs

        try:
            loop = asyncio.new_event_loop()
            raw_msgs = loop.run_until_complete(_run())
            loop.close()
        except Exception as e:
            print(f"[Telethon] Error polling {target}: {e}")
            return []

        results = []
        for mid, text, dt_iso, msg_url, username in raw_msgs:
            msg_id = f"tg_grp:{source['id']}:{mid}"
            results.append(self._build_listing(
                msg_id=msg_id,
                source=source,
                text=text,
                released_at=dt_iso or datetime.now(timezone.utc).isoformat(),
                url=msg_url,
                author=username
            ))
        return results

    def poll_source(self, source: dict) -> list:
        stype = source.get("type", "tg_channel")
        if stype == "tg_channel":
            return self.fetch_public_tg_channel(source)
        elif stype == "tg_group":
            return self.fetch_telethon_group(source)
        elif stype in ("rss", "web_feed"):
            return self.fetch_rss_source(source)
        return []

    def poll_all(self) -> dict:
        if not self._poll_lock.acquire(blocking=False):
            return {"status": "busy", "count": len(self.listings_db)}
        try:
            before_count = len(self.listings_db)
            checked = 0
            errors = 0
            for src in self.get_sources():
                if not src.get("active", True):
                    continue
                checked += 1
                try:
                    self.poll_source(src)
                except Exception:
                    errors += 1
            self.last_poll_iso = datetime.now(timezone.utc).isoformat()
            new_count = max(0, len(self.listings_db) - before_count)
            self.last_poll_stats = {"checked": checked, "new": new_count, "errors": errors}
            self._save_cache()
            return {"status": "ok", "checked": checked, "new": new_count, "total": len(self.listings_db)}
        finally:
            self._poll_lock.release()

    def _ai_enrichment_worker(self):
        while True:
            if not self._ai_queue:
                time.sleep(2)
                continue
            msg_id, text = self._ai_queue.popleft()
            try:
                res = self.ai_gateway.enrich_listing(text)
                if res and msg_id in self.listings_db:
                    item = self.listings_db[msg_id]
                    if res.get("intent") in ("BUY", "SELL", "NEW_PRODUCT"):
                        item["intent"] = res["intent"]
                    if res.get("product_name"):
                        item["productName"] = res["product_name"]
                        item["title"] = res["product_name"]
                    if res.get("category"):
                        item["category"] = res["category"]
                    if res.get("price") and not item.get("price"):
                        item["price"] = str(res["price"])
                    if res.get("summary"):
                        item["summary"] = res["summary"]
                    item["aiProvider"] = res.get("ai_provider")
                    self._save_cache()
            except Exception:
                pass
            time.sleep(0.5)

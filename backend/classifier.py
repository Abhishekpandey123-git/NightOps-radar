import re
from typing import Dict, List, Optional

BUY_PATTERNS = [
    r"\bwtb\b", r"\bwant to buy\b", r"\blooking for\b", r"\blooking to buy\b",
    r"\bneed\b.*\b(script|bot|account|tool|saas|template|course|license|api|software)\b",
    r"\banyone selling\b", r"\bwho sells\b", r"\bbuying\b", r"\biso\b",
    r"\bbudget\b", r"\bhiring\b.*\b(dev|developer|coder|designer)\b",
    r"\bneed a\b", r"\bsearching for\b"
]

SELL_PATTERNS = [
    r"\bwts\b", r"\bwant to sell\b", r"\bfor sale\b", r"\bselling\b",
    r"\bdm to buy\b", r"\bavailable now\b", r"\bin stock\b", r"\bprice\s*[:\-]\b",
    r"\bstarting at\b", r"\blifetime access\b", r"\blicense key\b", r"\breseller\b",
    r"\binstant delivery\b", r"\border now\b", r"\bcheap\b.*\b(account|key|license|sub)\b"
]

NEW_PRODUCT_PATTERNS = [
    r"\bnew release\b", r"\bjust launched\b", r"\bjust dropped\b", r"\bintroducing\b",
    r"\bnew product\b", r"\bnew tool\b", r"\bnew script\b", r"\bv\d+\.\d+\b",
    r"\bbeta launch\b", r"\bbrand new\b", r"\breleased today\b", r"\bfresh drop\b"
]

CATEGORY_KEYWORDS = {
    "Scripts & Bots": ["bot", "script", "automation", "scraper", "telegram bot", "discord bot", "python", "node", "macro"],
    "SaaS & APIs": ["saas", "api", "dashboard", "web app", "portal", "crm", "ai tool", "chatbot", "plugin"],
    "Templates & UI": ["template", "ui kit", "figma", "theme", "landing page", "wordpress", "shopify", "notion"],
    "Accounts & Licenses": ["account", "license", "key", "subscription", "pro plan", "seat", "voucher", "redeem"],
    "Courses & Ebooks": ["course", "ebook", "pdf", "guide", "bootcamp", "tutorial", "bundle", "prompts"],
    "Proxies & Infra": ["proxy", "proxies", "vps", "rdp", "server", "hosting", "domain", "ssl"],
    "Design & Media": ["logo", "video", "psd", "after effects", "canva", "preset", "lut", "asset"]
}

PAYMENT_KEYWORDS = [
    "usdt", "trc20", "erc20", "btc", "eth", "ton", "sol", "crypto",
    "paypal", "upi", "gpay", "phonepe", "paytm", "wise", "stripe",
    "binance", "escrow", "mm", "middleman", "bank transfer", "card"
]

PRICE_REGEX = re.compile(
    r"(?:(?P<curr_pre>[\$₹€£])\s*(?P<amt_pre>\d+(?:[.,]\d{1,2})?))"
    r"|(?:(?P<amt_post>\d+(?:[.,]\d{1,2})?)\s*(?P<curr_post>usd|usdt|inr|eur|gbp|ton|btc|eth|rs\.?))",
    re.IGNORECASE
)

HANDLE_REGEX = re.compile(r"@([A-Za-z0-9_]{4,32})")
URL_REGEX = re.compile(r"https?://[^\s<>\"'\)\]]+")


def classify_and_extract(text: str, author_handle: Optional[str] = None) -> Dict:
    """
    Deterministic, zero-cost extraction of intent, product, price, handles,
    payment methods, and category from a Telegram/web listing.
    """
    clean = (text or "").strip()
    lower = clean.lower()

    # 1. Intent Classification
    buy_hits = sum(1 for p in BUY_PATTERNS if re.search(p, lower))
    sell_hits = sum(1 for p in SELL_PATTERNS if re.search(p, lower))
    new_hits = sum(1 for p in NEW_PRODUCT_PATTERNS if re.search(p, lower))

    if buy_hits > sell_hits and buy_hits > 0:
        intent = "BUY"
    elif new_hits > 0 and buy_hits == 0:
        intent = "NEW_PRODUCT"
    elif sell_hits > 0:
        intent = "SELL"
    elif buy_hits > 0:
        intent = "BUY"
    else:
        intent = "UNKNOWN"

    # 2. Price & Currency Extraction
    price_str = None
    price_value = None
    currency = None
    m = PRICE_REGEX.search(clean)
    if m:
        if m.group("amt_pre"):
            price_value = float(m.group("amt_pre").replace(",", ""))
            currency = m.group("curr_pre")
            price_str = f"{currency}{m.group('amt_pre')}"
        elif m.group("amt_post"):
            price_value = float(m.group("amt_post").replace(",", ""))
            currency = m.group("curr_post").upper()
            price_str = f"{m.group('amt_post')} {currency}"

    # 3. Category Detection
    category = "Digital Product"
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(kw in lower for kw in kws):
            category = cat
            break

    # 4. Product Title Heuristic (first non-empty line, trimmed)
    lines = [ln.strip() for ln in clean.splitlines() if ln.strip()]
    first_line = lines[0] if lines else "Digital Product Listing"
    product_name = re.sub(r"^(?:\[?(?:wts|wtb|new|selling|buying)\]?\s*[:\-–]?\s*)", "", first_line, flags=re.IGNORECASE)
    if len(product_name) > 90:
        product_name = product_name[:87].rstrip() + "..."
    if not product_name:
        product_name = first_line[:90]

    # 5. Handles, Links & Payment Methods
    handles = list(dict.fromkeys(f"@{h}" for h in HANDLE_REGEX.findall(clean)))
    if author_handle:
        norm_author = author_handle if author_handle.startswith("@") else f"@{author_handle}"
        if norm_author not in handles:
            handles.insert(0, norm_author)

    client_handle = handles[0] if handles else None
    urls = list(dict.fromkeys(URL_REGEX.findall(clean)))
    payments = [p.upper() if len(p) <= 4 else p.title() for p in PAYMENT_KEYWORDS if re.search(rf"\b{re.escape(p)}\b", lower)]
    payments = list(dict.fromkeys(payments))

    has_escrow = any(w in lower for w in ["escrow", "middleman", "verified seller"])
    needs_ai = (intent == "UNKNOWN") or (price_str is None and len(clean) > 40)

    return {
        "intent": intent,
        "product_name": product_name,
        "category": category,
        "price": price_str,
        "price_value": price_value,
        "currency": currency,
        "client_handle": client_handle,
        "mentioned_handles": handles,
        "contact_links": urls[:5],
        "payment_methods": payments,
        "has_escrow": has_escrow,
        "needs_ai": needs_ai
    }

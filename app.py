import html
import json
import logging
import os
import re
import threading
import time
import unicodedata

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template_string, request
from thefuzz import fuzz

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20
CACHE_TTL = int(os.getenv("DATA_CACHE_TTL", "300"))
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
SPIEL_PRODUCTS_URL = os.getenv("SPIEL_PRODUCTS_URL", "https://maps.eyeled-services.de/en/spiel26/products?columns=%5B%22ID%22%2C%22INFO%22%2C%22S_ORDER%22%2C%22TITEL%22%2C%22FIRMA_ID%22%2C%22UNTERTITEL%22%2C%22BILDER%22%2C%22BILDER_VERSIONEN%22%2C%22BILDER_TEXTE%22%5D")
TABLETOP_TOGETHER_URL = os.getenv("TABLETOP_TOGETHER_URL", "https://tabletoptogether.com/tool/share.php?key=46b4a984fef86dcddcfa5c8e5a2de1d6&c=32")
_cache_lock = threading.Lock()
_cache = {"spiel": (0.0, [], None), "tabletop": (0.0, [], None)}


def normalize_title(value):
    if value is None:
        return ""
    value = html.unescape(str(value))
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"\[[^]]*\]|\([^)]*\)", " ", value)
    return re.sub(r"\s+", " ", value).strip(" -|\u00a0")


def title_key(value):
    value = normalize_title(value).casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(word for word in value.split() if word not in {"a", "an", "the"})


def fetch(url, accept):
    try:
        response = requests.get(url, headers={"Accept": accept, "User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.text, None
    except requests.RequestException as exc:
        logger.exception("Request failed for %s", url)
        return None, f"Could not load data from {url}."


def find_records(value, title_fields=("TITEL", "title", "TITLE", "name", "NAME", "game", "GAME")):
    records = []
    if isinstance(value, dict):
        if any(value.get(field) for field in title_fields):
            records.append(value)
        for child in value.values():
            records.extend(find_records(child, title_fields))
    elif isinstance(value, list):
        for child in value:
            records.extend(find_records(child, title_fields))
    return records


def get_spiel_novelties():
    raw, error = fetch(SPIEL_PRODUCTS_URL, "application/json")
    if error:
        return [], error
    try:
        data = json.loads(raw)
    except ValueError:
        return [], "SPIEL product API returned invalid JSON."
    games, seen = [], set()
    for item in find_records(data, ("TITEL", "title", "TITLE")):
        item_id = item.get("ID") or item.get("id")
        title = normalize_title(item.get("TITEL") or item.get("title") or item.get("TITLE"))
        key = str(item_id) if item_id is not None else title_key(title)
        if title and key not in seen:
            seen.add(key)
            games.append({"id": item_id, "title": title})
    logger.info("Fetched %d SPIEL products from %s", len(games), SPIEL_PRODUCTS_URL)
    return (games, None) if games else ([], "SPIEL product API returned no products with titles.")


def add_tabletop_title(results, seen, value):
    value = normalize_title(value)
    key = title_key(value)
    if not key or len(value) > 180 or key in seen or key in {"title", "name", "game", "games", "sort", "filter", "search"}:
        return
    seen.add(key)
    results.append({"title": value})


def extract_tabletop_titles(page):
    soup = BeautifulSoup(page, "html.parser")
    results, seen = [], set()

    # Conventional tables.
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        headers = [normalize_title(c.get_text(" ", strip=True)).casefold() for c in rows[0].find_all(["th", "td"])] if rows else []
        title_index = next((i for i, h in enumerate(headers) if any(x in h for x in ("title", "name", "game"))), None)
        for row in rows[1:] if headers else rows:
            cells = row.find_all(["td", "th"])
            if cells:
                value = cells[title_index].get_text(" ", strip=True) if title_index is not None and title_index < len(cells) else (row.find("a") or cells[0]).get_text(" ", strip=True)
                add_tabletop_title(results, seen, value)

    # Tabletop Together's share view can render a grid of divs rather than a table.
    # Each game card contains a Players: detail; take the title immediately before it.
    for detail in soup.find_all(string=re.compile(r"Players\s*:", re.I)):
        container = detail.parent
        card = container
        for _ in range(4):
            if card is None:
                break
            text = normalize_title(card.get_text(" ", strip=True))
            if "Players:" in text and len(text) <= 500:
                break
            card = card.parent
        if card is None:
            continue
        candidates = card.find_all(["b", "strong", "h1", "h2", "h3", "h4", "a"], recursive=True)
        if candidates:
            add_tabletop_title(results, seen, candidates[-1].get_text(" ", strip=True))
        else:
            # For bare text nodes, use the text immediately preceding Players:.
            text = normalize_title(card.get_text(" ", strip=True))
            match = re.search(r"(.{2,100})\s+Players\s*:", text, re.I)
            if match:
                add_tabletop_title(results, seen, match.group(1))

    # Embedded structured data and game-related data attributes.
    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if not text:
            continue
        if script.get("type") in {"application/json", "application/ld+json"}:
            try:
                values = [json.loads(text)]
            except ValueError:
                values = []
        else:
            values = []
            for match in re.finditer(r"(\{.*\}|\[.*\])", text, re.DOTALL):
                try:
                    values.append(json.loads(match.group(1)))
                except ValueError:
                    pass
        for value in values:
            for item in find_records(value):
                add_tabletop_title(results, seen, item.get("title") or item.get("name") or item.get("game") or item.get("TITEL"))

    for element in soup.select("a[data-game], a[data-title], [data-game-title], [data-title]"):
        add_tabletop_title(results, seen, element.get("data-game") or element.get("data-title") or element.get("data-game-title") or element.get_text(" ", strip=True))

    return results


def get_tabletop_together_games():
    page, error = fetch(TABLETOP_TOGETHER_URL, "text/html,application/xhtml+xml,application/json")
    if error:
        return [], error
    games = extract_tabletop_titles(page)
    logger.info("Fetched %d Tabletop Together games from %s", len(games), TABLETOP_TOGETHER_URL)
    return (games, None) if games else ([], "Tabletop Together share page contained no recognizable game records.")


def cached_data(name, loader):
    with _cache_lock:
        timestamp, data, error = _cache[name]
        if time.monotonic() - timestamp < CACHE_TTL:
            return data, error
        data, error = loader()
        _cache[name] = (time.monotonic(), data, error)
        return data, error


def compare_titles(spiel_titles, tabletop_titles):
    exact = {title_key(item["title"]): item["title"] for item in tabletop_titles}
    results = []
    for spiel in spiel_titles:
        original = normalize_title(spiel["title"])
        key = title_key(original)
        match, score = exact.get(key), 100 if key in exact else 0
        if not match:
            for item in tabletop_titles:
                candidate_key = title_key(item["title"])
                candidate_score = max(fuzz.token_set_ratio(key, candidate_key), fuzz.token_sort_ratio(key, candidate_key), fuzz.ratio(key, candidate_key))
                if candidate_score > score:
                    match, score = item["title"], candidate_score
        status = "match" if match and score >= 90 else "possible match" if match and score >= 75 else "not found"
        results.append({"spiel_title": original, "best_match": match if status != "not found" else None, "status": status, "confidence": score})
    return results


@app.route("/health", methods=["GET", "HEAD"])
def health():
    return jsonify(status="ok"), 200


@app.route("/", methods=["GET", "HEAD"])
def index():
    if request.method == "HEAD":
        return "", 200
    spiel, spiel_error = cached_data("spiel", get_spiel_novelties)
    tabletop, tabletop_error = cached_data("tabletop", get_tabletop_together_games)
    matches = compare_titles(spiel, tabletop) if spiel and tabletop else []
    template = """
    <html><head><title>SPIEL Essen vs Tabletop Together</title><style>body{font-family:Arial;margin:20px}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccc;padding:8px}th{background:#f2f2f2}.warning{color:#8a3b00;background:#fff3e0;padding:10px;border:1px solid #ffcc80;margin-bottom:20px}.status-match{color:green}.status-possible-match{color:orange}.status-not-found{color:red}</style></head><body>
    <h1>SPIEL Essen vs Tabletop Together</h1>{% if spiel_error %}<div class="warning">SPIEL data unavailable: {{ spiel_error }}</div>{% endif %}{% if tabletop_error %}<div class="warning">Tabletop Together data unavailable: {{ tabletop_error }}</div>{% endif %}<p>SPIEL products: {{ spiel_count }} | Tabletop Together games: {{ tabletop_count }}</p>
    {% if matches %}<table><tr><th>SPIEL title</th><th>Tabletop Together match</th><th>Status</th><th>Confidence</th></tr>{% for item in matches %}<tr><td>{{ item.spiel_title }}</td><td>{{ item.best_match or "-" }}</td><td class="status-{{ item.status|replace(' ','-') }}">{{ item.status }}</td><td>{{ item.confidence }}%</td></tr>{% endfor %}</table>{% endif %}</body></html>"""
    return render_template_string(template, matches=matches, spiel_count=len(spiel), tabletop_count=len(tabletop), spiel_error=spiel_error, tabletop_error=tabletop_error)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

import csv
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

REQUEST_TIMEOUT = (10, 60)
CACHE_TTL = int(os.getenv("DATA_CACHE_TTL", "300"))
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SPIEL_PRODUCTS_URL = os.getenv("SPIEL_PRODUCTS_URL", "https://maps.eyeled-services.de/en/spiel26/products?columns=%5B%22ID%22%2C%22INFO%22%2C%22S_ORDER%22%2C%22TITEL%22%2C%22FIRMA_ID%22%2C%22UNTERNEHMEN%22%2C%22PROGRAMM%22%2C%22HIT%22%2C%22FIRMA%22%2C%22VERLAG%22%2C%22PUBLISHER%22%2C%22MEDIEN%22%2C%22PRESSE%22%2C%22AUSSTELLER%22%2C%22AUSSTELLUNG%22%2C%22MINT%22%2C%22PLATZ%22%2C%22HAUS%22%2C%22SEITEN%22%2C%22ERWARTUNG%22%2C%22WERTUNG%22%2C%22MUSTER_1%22%2C%22MUSTER_2%22%2C%22MUSTER_3%22%2C%22MUSTER_4%22%2C%22MUSTER_5%22%2C%22MUSTER_6%22%2C%22MUSTER_7%22%2C%22SPIELER%22%2C%22ALTER%22%2C%22DAUER%22%2C%22URL%22%5D")
TABLETOP_TOGETHER_URL = os.getenv("TABLETOP_TOGETHER_URL", "https://tabletoptogether.com/tool/share.php?key=46b4a984fef86dcddcfa5c8e5a2de1d6&c=32")
TABLETOP_TOGETHER_CSV = os.getenv(
    "TABLETOP_TOGETHER_CSV",
    os.path.join(os.path.dirname(__file__), "TabletopTogetherTool.csv"),
)
_cache_lock = threading.Lock()
_cache = {"spiel": (0.0, [], None), "tabletop": (0.0, [], None)}


def normalize_title(value):
    if value is None:
        return ""
    # Cap before regex/unicode processing so malformed upstream markup cannot
    # make parsing slow or consume excessive memory.
    value = html.unescape(str(value))[:300]
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", value)
    return re.sub(r"\s+", " ", value).strip(" -|\u00a0")


def title_key(value):
    value = normalize_title(value).casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(word for word in value.split() if word not in {"a", "an", "the"})


def fetch(url, accept):
    headers = {"Accept": accept, "Accept-Language": "en-US,en;q=0.9", "User-Agent": BROWSER_UA}
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        logger.info("Fetched %s: status=%s bytes=%s", url, response.status_code, len(response.content))
        return response.text, None
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        logger.exception("Request failed for %s (type=%s status=%s)", url, type(exc).__name__, status)
        return None, f"Could not load data from {url} ({type(exc).__name__}, status={status})."


def find_records(value, fields=("TITEL", "title", "TITLE", "name", "NAME", "game", "GAME")):
    records = []
    if isinstance(value, dict):
        if any(value.get(field) for field in fields):
            records.append(value)
        for child in value.values():
            records.extend(find_records(child, fields))
    elif isinstance(value, list):
        for child in value:
            records.extend(find_records(child, fields))
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
    logger.info("Fetched %d SPIEL products", len(games))
    return (games, None) if games else ([], "SPIEL product API returned no products with titles.")


def add_tabletop_title(results, seen, value):
    value = normalize_title(value)
    key = title_key(value)
    if not key or len(value) > 180 or key in seen or key in {"title", "name", "game", "games", "sort", "filter", "search"}:
        return
    seen.add(key)
    results.append({"title": value})


def extract_tabletop_titles(page):
    soup = BeautifulSoup(page, "lxml")
    results, seen = [], set()

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        headers = [normalize_title(c.get_text(" ", strip=True)).casefold() for c in rows[0].find_all(["th", "td"])] if rows else []
        title_index = next((i for i, h in enumerate(headers) if any(x in h for x in ("title", "name", "game"))), None)
        for row in rows[1:] if headers else rows:
            cells = row.find_all(["td", "th"])
            if cells:
                value = cells[title_index].get_text(" ", strip=True) if title_index is not None and title_index < len(cells) else (row.find("a") or cells[0]).get_text(" ", strip=True)
                add_tabletop_title(results, seen, value)

    # Do not walk up through large parent containers. Find the smallest local
    # element containing the Players marker and inspect only its direct title
    # candidates. This prevents repeated multi-megabyte get_text() calls.
    for detail in soup.find_all(string=re.compile(r"Players\s*:", re.I)):
        local = detail.parent
        text = str(detail)[:300]
        if local is not None:
            text = f"{local.get_text(' ', strip=True)[:300]}"
        if not re.search(r"Players\s*:", text, re.I):
            continue
        candidates = local.find_all(["b", "strong", "h1", "h2", "h3", "h4", "a"], recursive=False) if local else []
        if not candidates and local:
            candidates = local.find_all(["b", "strong", "h1", "h2", "h3", "h4", "a"], recursive=True, limit=3)
        if candidates:
            add_tabletop_title(results, seen, candidates[0].get_text(" ", strip=True)[:180])
        else:
            match = re.search(r"(.{2,100}?)\s+Players\s*:", text, re.I)
            if match:
                add_tabletop_title(results, seen, match.group(1))

    for element in soup.select("a[data-game], a[data-title], [data-game-title], [data-title]"):
        add_tabletop_title(results, seen, element.get("data-game") or element.get("data-title") or element.get("data-game-title") or element.get_text(" ", strip=True))
    return results


def load_csv_titles(path):
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            if reader.fieldnames is None:
                return [], "CSV file is empty or missing a header row."
            results, seen = [], set()
            for row in reader:
                if not row:
                    continue
                for key in ("name", "title", "game", "NAME", "TITLE", "GAME"):
                    value = row.get(key)
                    if value is not None:
                        add_tabletop_title(results, seen, value)
                        break
            logger.info("Loaded %d titles from CSV %s", len(results), path)
            return (results, None) if results else ([], "CSV file contained no recognizable game titles.")
    except OSError as exc:
        return [], f"Could not read CSV file {path}: {exc}"


def get_tabletop_together_games():
    if os.path.exists(TABLETOP_TOGETHER_CSV):
        return load_csv_titles(TABLETOP_TOGETHER_CSV)

    page, error = fetch(TABLETOP_TOGETHER_URL, "text/html,application/xhtml+xml,application/json")
    if error:
        return [], error
    games = extract_tabletop_titles(page)
    logger.info("Fetched %d Tabletop Together games", len(games))
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
    <html><head><title>SPIEL Essen vs CSV</title><style>
    body{font-family:Arial;margin:20px}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccc;padding:8px}th{background:#f2f2f2}.warning{color:#8a3b00;background:#fff3e0;padding:10px;border-radius:4px;margin:10px 0}.good{color:#0a5d1c;background:#eaf7ee;padding:10px;border-radius:4px;margin:10px 0}.meta{margin:10px 0 20px}small{color:#666}</style></head><body><h1>SPIEL Essen vs CSV</h1>
    {% if spiel_error %}<div class="warning">SPIEL data unavailable: {{ spiel_error }}</div>{% endif %}{% if tabletop_error %}<div class="warning">CSV data unavailable: {{ tabletop_error }}</div>{% endif %}
    <div class="meta"><strong>SPIEL products:</strong> {{ spiel_count }} | <strong>CSV titles:</strong> {{ tabletop_count }}</div>
    {% if matches %}<table><tr><th>SPIEL title</th><th>CSV match</th><th>Status</th><th>Confidence</th></tr>{% for item in matches %}<tr><td>{{ item.spiel_title }}</td><td>{{ item.best_match or '—' }}</td><td>{{ item.status }}</td><td>{{ item.confidence }}</td></tr>{% endfor %}</table>{% else %}<div class="good">No match data was available yet.</div>{% endif %}
    </body></html>"""
    return render_template_string(template, matches=matches, spiel_count=len(spiel), tabletop_count=len(tabletop), spiel_error=spiel_error, tabletop_error=tabletop_error)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

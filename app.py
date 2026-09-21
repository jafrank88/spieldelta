import csv
import html
import json
import logging
import os
import re
import threading
import time
import unicodedata
from urllib.parse import quote_plus

import requests
from flask import Flask, jsonify, render_template_string, request
from thefuzz import fuzz

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = (10, 60)
CACHE_TTL = int(os.getenv("DATA_CACHE_TTL", "300"))
SPIEL_CACHE_TTL = int(os.getenv("SPIEL_CACHE_TTL", "900"))
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SPIEL_PRODUCTS_URL = "https://maps.eyeled-services.de/en/spiel26/products?columns=%5B%22ID%22%2C%22TITEL%22%5D"
NOVELTIES_URL = "https://spiel-essen.de/en/the-spiel/novelties#egcf-product-list"
CSV_PATH = os.getenv("TABLETOP_TOGETHER_CSV", os.path.join(os.path.dirname(__file__), "TabletopTogetherTool.csv"))
_cache_lock = threading.Lock()
_cache = {"spiel": (0.0, [], None), "csv": (0.0, [], None)}


def normalize_title(value):
    if value is None:
        return ""
    value = html.unescape(str(value))[:300]
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", value)
    return re.sub(r"\s+", " ", value).strip(" -|\u00a0")


def title_key(value):
    value = normalize_title(value).casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(word for word in value.split() if word not in {"a", "an", "the"})


def bgg_search_url(title):
    return ("https://boardgamegeek.com/geeksearch.php?action=search"
            f"&objecttype=boardgame&q={quote_plus(normalize_title(title))}")


def fetch(url, accept):
    headers = {"Accept": accept, "Accept-Language": "en-US,en;q=0.9", "User-Agent": BROWSER_UA}
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        logger.info("Fetched %s: status=%s bytes=%s", url, response.status_code, len(response.content))
        return response.text, None
    except requests.HTTPError as exc:
        response = exc.response
        status = getattr(response, "status_code", None)
        body = (getattr(response, "text", "") or "")[:500]
        logger.error("HTTP request failed for %s: status=%s response_body=%r", url, status, body)
        return None, f"Could not load data from {url} (HTTPError, status={status})."
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        logger.exception("Request failed for %s (type=%s status=%s)", url, type(exc).__name__, status)
        return None, f"Could not load data from {url} ({type(exc).__name__}, status={status})."


def find_records(value, fields=("TITEL", "title", "TITLE")):
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
    for item in find_records(data):
        item_id = item.get("ID") or item.get("id")
        title = normalize_title(item.get("TITEL") or item.get("title") or item.get("TITLE"))
        key = str(item_id) if item_id is not None else title_key(title)
        if title and key not in seen:
            seen.add(key)
            games.append({"id": item_id, "title": title})
    logger.info("Fetched %d SPIEL products", len(games))
    return (games, None) if games else ([], "SPIEL product API returned no products with titles.")


def load_csv_titles(path):
    try:
        with open(path, newline="", encoding="utf-8-sig") as csv_file:
            reader = csv.DictReader(csv_file)
            if not reader.fieldnames:
                return [], "CSV file is empty or missing a header row."
            column = next(
                (header for header in reader.fieldnames
                 if any(word in header.casefold() for word in ("title", "name", "game"))),
                reader.fieldnames[0],
            )
            titles = [row[column] for row in reader if row.get(column)]
            logger.info("Loaded %d CSV titles from %s using column %s", len(titles), path, column)
            return (titles, None) if titles else ([], "CSV file contained no titles.")
    except OSError as exc:
        return [], f"Could not read CSV file {path}: {exc}"


def missing_from_csv(novelties, csv_titles, fuzzy_threshold=90):
    csv_keys = {title_key(title) for title in csv_titles}
    csv_keys.discard("")
    key_list = list(csv_keys)
    missing = []
    for item in novelties:
        key = title_key(item["title"])
        if not key:
            missing.append({**item, "closest_score": 0, "bgg_url": bgg_search_url(item["title"])})
            continue
        if key in csv_keys:
            continue
        best = max(
            (max(fuzz.ratio(key, candidate), fuzz.token_sort_ratio(key, candidate))
             for candidate in key_list),
            default=0,
        )
        if best < fuzzy_threshold:
            missing.append({
                **item,
                "closest_score": best,
                "bgg_url": bgg_search_url(item["title"]),
            })
    return missing


def cached_data(name, loader, ttl=CACHE_TTL):
    with _cache_lock:
        timestamp, data, error = _cache[name]
        if time.monotonic() - timestamp < ttl:
            return data, error
        data, error = loader()
        _cache[name] = (time.monotonic(), data, error)
        return data, error


@app.route("/health", methods=["GET", "HEAD"])
def health():
    return jsonify(status="ok"), 200


@app.route("/", methods=["GET", "HEAD"])
def index():
    if request.method == "HEAD":
        return "", 200

    spiel, spiel_error = cached_data("spiel", get_spiel_novelties, SPIEL_CACHE_TTL)
    csv_titles, csv_error = cached_data("csv", lambda: load_csv_titles(CSV_PATH))
    missing = missing_from_csv(spiel, csv_titles) if spiel and csv_titles else []

    template = """
    <html><head><title>SPIEL novelties missing from CSV</title><style>
    body{font-family:Arial;margin:20px}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccc;padding:8px}th{background:#f2f2f2}.warning{color:#8a3b00;background:#fff3e0;padding:10px;border-radius:4px;margin:10px 0}.meta{margin:10px 0 20px}a{white-space:nowrap}button{cursor:pointer;padding:4px 8px}
    </style></head><body><h1>SPIEL novelties missing from CSV</h1>
    {% if spiel_error %}<div class="warning">SPIEL data unavailable: {{ spiel_error }}</div>{% endif %}
    {% if csv_error %}<div class="warning">CSV data unavailable: {{ csv_error }}</div>{% endif %}
    <div class="meta"><strong>SPIEL novelties:</strong> {{ spiel_count }} | <strong>CSV titles:</strong> {{ csv_count }} | <strong>Missing:</strong> {{ missing_count }}</div>
    {% if missing %}<table><tr><th>SPIEL title</th><th>ID</th><th>Closest CSV score</th><th>SPIEL search</th><th>BGG</th></tr>{% for item in missing %}<tr><td>{{ item.title }}</td><td>{{ item.id or '—' }}</td><td>{{ item.closest_score }}</td><td><button type="button" data-title="{{ item.title|e }}" onclick="copyAndOpen(this)">Copy title &amp; open SPIEL</button></td><td><a href="{{ item.bgg_url }}" target="_blank" rel="noopener">Search BGG</a></td></tr>{% endfor %}</table>{% elif not spiel_error and not csv_error %}<p>No unmatched novelties found.</p>{% endif %}
    <script>
    const NOVELTIES_URL = {{ novelties_url|tojson }};
    function copyAndOpen(btn) {
      const title = btn.dataset.title;
      window.open(NOVELTIES_URL, "_blank", "noopener");
      navigator.clipboard.writeText(title).catch(() => {
        window.prompt("Copy this title:", title);
      });
      btn.textContent = "Copied — paste in search";
    }
    </script>
    </body></html>"""
    return render_template_string(
        template,
        missing=missing,
        missing_count=len(missing),
        spiel_count=len(spiel),
        csv_count=len(csv_titles),
        spiel_error=spiel_error,
        csv_error=csv_error,
        novelties_url=NOVELTIES_URL,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

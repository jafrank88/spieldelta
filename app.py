import csv
import html
import json
import logging
import math
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

CACHE_TTL = int(os.getenv("DATA_CACHE_TTL", "300"))
ERROR_TTL = 30  # seconds; failures are retried sooner than successes
FUZZY_THRESHOLD = int(os.getenv("FUZZY_THRESHOLD", "90"))

# Tabletop Together CSV export, committed next to app.py (or set the env vars).
CSV_PATH = os.getenv("TABLETOP_CSV_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "TabletopTogetherTool.csv"))
CSV_COLUMN = os.getenv("TABLETOP_CSV_COLUMN", "")  # leave empty to auto-detect

USER_AGENT = "Mozilla/5.0 (compatible; spieldelta/1.7; +https://github.com/jafrank88/spieldelta)"

# Known-good column list (the same request the SPIEL novelties page makes).
SPIEL_PRODUCTS_URL = os.getenv(
    "SPIEL_PRODUCTS_URL",
    "https://maps.eyeled-services.de/en/spiel26/products?columns=%5B%22ID%22%2C%22INFO%22%2C%22S_ORDER%22%2C%22TITEL%22%2C%22FIRMA_ID%22%2C%22UNTERTITEL%22%2C%22BILDER%22%2C%22BILDER_VERSIONEN%22%5D",
)

# The novelties list is rendered client-side and has no per-item URL or
# shareable search, so we link to the list and put the title on the clipboard.
NOVELTIES_URL = "https://spiel-essen.de/en/the-spiel/novelties#egcf-product-list"

_cache_lock = threading.Lock()
_cache = {}


# ---------------------------------------------------------------- titles ----

def normalize_title(value):
    if value is None:
        return ""
    value = html.unescape(str(value))[:300]  # cap length before any regex work
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    # Bounded quantifiers: no runaway scanning on stray brackets.
    value = re.sub(r"\[[^\]]{0,100}\]|\([^)]{0,100}\)", " ", value)
    value = re.sub(r"[\[\]]", " ", value)  # leftover stray brackets, e.g. "[2027]]"
    return re.sub(r"\s+", " ", value).strip(" -|\u00a0")


def title_key(value):
    value = normalize_title(value).casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(word for word in value.split() if word not in {"a", "an", "the"})


# ------------------------------------------------------------------ SPIEL ---

def fetch(url, accept):
    try:
        response = requests.get(
            url,
            headers={"Accept": accept, "User-Agent": USER_AGENT},
            timeout=(10, 60),
        )
        response.raise_for_status()
        return response.text, None
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        logger.exception("Request failed for %s", url)
        return None, f"Could not load SPIEL data ({type(exc).__name__}, status={status})."


def find_records(value, title_fields=("TITEL",)):
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
        logger.exception("SPIEL response was not JSON")
        return [], "SPIEL product API returned invalid JSON."

    games, seen = [], set()
    for item in find_records(data):
        item_id = item.get("ID")
        raw_title = str(item.get("TITEL") or "").strip()
        # Non-Latin titles normalize to "" (ASCII folding); keep the raw title so
        # they still appear in the results, flagged as not comparable.
        title = normalize_title(raw_title) or html.unescape(raw_title)[:180]
        key = str(item_id) if item_id is not None else title_key(title)
        if not title or key in seen:
            continue
        seen.add(key)
        exhibitor = html.unescape(str(item.get("UNTERTITEL") or "")).strip()
        games.append({
            "id": item_id,
            "title": title,
            # UNTERTITEL is the exhibitor/publisher line shown under the title.
            "exhibitor": exhibitor,
        })
    logger.info("Fetched %d SPIEL products", len(games))
    if not games:
        return [], "SPIEL product API returned no products with titles."
    return games, None


# -------------------------------------------------------------------- CSV ---

def _read_csv(path):
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with open(path, newline="", encoding=encoding) as handle:
                text = handle.read()
            return text
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("csv", b"", 0, 1, "unsupported encoding")


def get_csv_titles():
    if not os.path.exists(CSV_PATH):
        return [], f"CSV file not found: {os.path.basename(CSV_PATH)}. Commit your Tabletop Together export as TabletopTogetherTool.csv."
    try:
        text = _read_csv(CSV_PATH)
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(text.splitlines(), dialect=dialect)
        headers = reader.fieldnames or []
        if not headers:
            return [], "CSV file has no header row."

        column = CSV_COLUMN if CSV_COLUMN in headers else None
        if column is None:
            column = next(
                (h for h in headers if any(w in (h or "").casefold() for w in ("title", "name", "game"))),
                headers[0],
            )
        titles = [row[column].strip() for row in reader if row.get(column) and row[column].strip()]
    except Exception:
        logger.exception("Could not read CSV")
        return [], "Could not read the Tabletop Together CSV file."
    logger.info("Loaded %d titles from CSV column %r", len(titles), column)
    if not titles:
        return [], f"No titles found in CSV column {column!r}. Set TABLETOP_CSV_COLUMN."
    return titles, None


# ------------------------------------------------------------------ cache ---

def cached_data(name, loader):
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(name)
        if entry and now < entry[0]:
            return entry[1], entry[2]
    data, error = loader()
    ttl = ERROR_TTL if error else CACHE_TTL
    with _cache_lock:
        _cache[name] = (time.monotonic() + ttl, data, error)
    return data, error


# ------------------------------------------------------------- comparison ---

def build_index(csv_titles):
    exact, by_len = {}, {}
    for title in csv_titles:
        key = title_key(title)
        if not key or key in exact:
            continue
        exact[key] = title
        by_len.setdefault(len(key), []).append((key, title))
    return exact, by_len


def closest_match(key, by_len, threshold):
    """Best fuzzy match, only scanning candidates whose length could reach the threshold."""
    factor = threshold / (200 - threshold)  # ratio >= T  =>  min_len/max_len >= T/(200-T)
    lo, hi = math.ceil(len(key) * factor), math.floor(len(key) / factor)
    best_score, best_title = 0, None
    for length in range(lo, hi + 1):
        for cand_key, cand_title in by_len.get(length, ()):
            score = max(fuzz.ratio(key, cand_key), fuzz.token_sort_ratio(key, cand_key))
            if score > best_score:
                best_score, best_title = score, cand_title
    return best_score, best_title


def missing_from_csv(novelties, csv_titles, threshold=FUZZY_THRESHOLD):
    """Novelties with no exact or near match in the CSV."""
    exact, by_len = build_index(csv_titles)
    missing = []
    for item in novelties:
        if not item.get("exhibitor", "").strip():
            continue
        key = title_key(item["title"])
        note = ""
        if key in exact:
            continue
        if key:
            score, near = closest_match(key, by_len, threshold)
            if score >= threshold:
                continue
        else:
            score, near, note = 0, None, "title has no comparable characters"
        missing.append({
            "title": item["title"],
            "exhibitor": item["exhibitor"],
            "closest": near,
            "closest_score": score,
            "note": note,
            "bgg_url": "https://boardgamegeek.com/geeksearch.php?action=search&objecttype=boardgame&q="
                       + quote_plus(item["title"]),
        })
    missing.sort(key=lambda m: m["title"].casefold())
    return missing


# ----------------------------------------------------------------- routes ---

@app.route("/health", methods=["GET", "HEAD"])
def health():
    return jsonify(status="ok"), 200


TEMPLATE = """
<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SPIEL novelties missing from Tabletop Together</title>
<style>
body{font-family:Arial,sans-serif;margin:20px}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid #ccc;padding:8px;text-align:left;vertical-align:top}
th{background:#f2f2f2}
.warning{color:#8a3b00;background:#fff3e0;padding:10px;border:1px solid #ffcc80;margin-bottom:20px}
.muted{color:#666;font-size:.9em}
button{cursor:pointer}
#filter{padding:6px;width:min(100%,320px);margin:8px 0}
#sort{padding:6px;margin:8px 0}
</style></head><body>
<h1>SPIEL novelties not in your Tabletop Together list</h1>
{% if spiel_error %}<div class="warning">SPIEL data unavailable: {{ spiel_error }}</div>{% endif %}
{% if csv_error %}<div class="warning">CSV unavailable: {{ csv_error }}</div>{% endif %}
<p>SPIEL novelties: {{ spiel_count }} | CSV titles: {{ csv_count }} | Not found in CSV: {{ missing|length }}</p>
{% if missing %}
<label for="sort">Sort alphabetically by:</label>
<select id="sort" onchange="sortRows(this.value)">
  <option value="name">Name</option>
  <option value="exhibitor">Exhibitor</option>
</select>
<input id="filter" type="search" placeholder="Filter this list…" oninput="filterRows(this.value)">
<table id="results">
<thead><tr><th>SPIEL title</th><th>Exhibitor</th><th>BoardGameGeek</th><th>SPIEL page</th></tr></thead>
<tbody>
{% for item in missing %}
<tr>
  <td data-sort-name="{{ item.title|lower }}">{{ item.title }}{% if item.note %}<div class="muted">{{ item.note }}</div>{% endif %}</td>
  <td data-sort-exhibitor="{{ item.exhibitor|lower }}">{{ item.exhibitor }}</td>
  <td><a href="{{ item.bgg_url }}" target="_blank" rel="noopener">Search BGG</a></td>
  <td><button type="button" data-title="{{ item.title }}" onclick="copyAndOpen(this)">Copy title &amp; open SPIEL</button></td>
</tr>
{% endfor %}
</tbody>
</table>
{% elif not spiel_error and not csv_error %}
<p>Every SPIEL novelty has a match in your CSV.</p>
{% endif %}
<script>
const NOVELTIES_URL = {{ novelties_url|tojson }};
function copyAndOpen(btn) {
  const title = btn.dataset.title;
  window.open(NOVELTIES_URL, "_blank", "noopener");   // must run inside the click handler
  const done = () => { btn.textContent = "Copied - paste into the search box"; };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(title).then(done, () => prompt("Copy this title:", title));
  } else {
    prompt("Copy this title:", title);
  }
}
function sortRows(field) {
  const tbody = document.querySelector("#results tbody");
  if (!tbody) return;
  const prop = field === "name" ? "sortName" : "sortExhibitor";
  [...tbody.rows]
    .sort((a, b) => {
      const left = a.querySelector(`[data-${prop.toLowerCase()}]`)?.dataset[prop] ?? "";
      const right = b.querySelector(`[data-${prop.toLowerCase()}]`)?.dataset[prop] ?? "";
      return left.localeCompare(right, undefined, { sensitivity: "base" });
    })
    .forEach(row => tbody.appendChild(row));
}
function filterRows(q) {
  q = q.toLowerCase();
  document.querySelectorAll("#results tbody tr").forEach(row => {
    row.style.display = row.textContent.toLowerCase().includes(q) ? "" : "none";
  });
}
</script>
</body></html>
"""


@app.route("/", methods=["GET", "HEAD"])
def index():
    if request.method == "HEAD":
        return "", 200
    spiel, spiel_error = cached_data("spiel", get_spiel_novelties)
    csv_titles, csv_error = cached_data("csv", get_csv_titles)
    missing = missing_from_csv(spiel, csv_titles) if spiel and csv_titles else []
    return render_template_string(
        TEMPLATE,
        missing=missing,
        spiel_count=len(spiel),
        csv_count=len(csv_titles),
        spiel_error=spiel_error,
        csv_error=csv_error,
        novelties_url=NOVELTIES_URL,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

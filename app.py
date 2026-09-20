import html
import json
import logging
import os
import re
import unicodedata
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template_string
from thefuzz import fuzz

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; spieldelta/1.4; +https://github.com/jafrank88/spieldelta)"
SPIEL_PRODUCTS_URL = os.getenv(
    "SPIEL_PRODUCTS_URL",
    "https://maps.eyeled-services.de/en/spiel26/products?columns=%5B%22ID%22%2C%22INFO%22%2C%22S_ORDER%22%2C%22TITEL%22%2C%22FIRMA_ID%22%2C%22UNTERTITEL%22%2C%22BILDER%22%2C%22BILDER_VERSIONEN%22%2C%22BILDER_TEXTE%22%5D",
)
TABLETOP_TOGETHER_URL = os.getenv(
    "TABLETOP_TOGETHER_URL",
    "https://tabletoptogether.com/tool/share.php?key=46b4a984fef86dcddcfa5c8e5a2de1d6&c=32",
)


def normalize_title(value):
    if value is None:
        return ""
    value = html.unescape(str(value))
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"\[[^]]*\]|\([^)]*\)", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -|\u00a0")


def title_key(value):
    value = normalize_title(value).casefold()
    value = re.sub(r"&", " and ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    words = [word for word in value.split() if word not in {"a", "an", "the"}]
    return " ".join(words)


def fetch_html(url):
    try:
        response = requests.get(
            url,
            headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.text, None
    except requests.RequestException as exc:
        logger.exception("Request failed for %s", url)
        return None, f"Could not load page from {url}."


def fetch_json(url):
    try:
        response = requests.get(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json(), None
    except (requests.RequestException, ValueError) as exc:
        logger.exception("JSON request failed for %s", url)
        return None, f"Could not load product data from {url}."


def find_product_records(value):
    records = []
    if isinstance(value, dict):
        if value.get("TITEL") or value.get("title") or value.get("TITLE"):
            records.append(value)
        for child in value.values():
            records.extend(find_product_records(child))
    elif isinstance(value, list):
        for child in value:
            records.extend(find_product_records(child))
    return records


def get_spiel_novelties():
    data, error = fetch_json(SPIEL_PRODUCTS_URL)
    if error:
        return [], error

    games = []
    seen = set()
    for item in find_product_records(data):
        item_id = item.get("ID") or item.get("id")
        title = normalize_title(item.get("TITEL") or item.get("title") or item.get("TITLE"))
        key = str(item_id) if item_id is not None else title_key(title)
        if not title or key in seen:
            continue
        seen.add(key)
        games.append({
            "id": item_id,
            "title": title,
            "publisher": normalize_title(item.get("FIRMA_ID") or item.get("publisher")),
            "subtitle": normalize_title(item.get("UNTERTITEL") or item.get("subtitle")),
            "description": normalize_title(item.get("INFO") or item.get("description")),
        })

    logger.info("Fetched %d SPIEL products from %s", len(games), SPIEL_PRODUCTS_URL)
    if games:
        return games, None
    logger.error("No products found in SPIEL response: %s", json.dumps(data)[:1000])
    return [], "SPIEL product API returned no products with titles."


def extract_tabletop_titles(html_text):
    """Extract game names from the share page's table, avoiding page-wide noise."""
    soup = BeautifulSoup(html_text, "html.parser")
    results = []
    seen = set()

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        headers = [normalize_title(cell.get_text(" ", strip=True)).casefold() for cell in rows[0].find_all(["th", "td"])]
        title_index = next(
            (index for index, header in enumerate(headers)
             if any(name in header for name in ("title", "name", "game"))),
            None,
        )

        for row in rows[1:] if headers else rows:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue

            if title_index is not None and title_index < len(cells):
                candidate = cells[title_index].get_text(" ", strip=True)
            else:
                link = row.find("a")
                candidate = link.get_text(" ", strip=True) if link else cells[0].get_text(" ", strip=True)

            candidate = normalize_title(candidate)
            key = title_key(candidate)
            if not key or len(candidate) > 180 or key in seen:
                continue
            if key in {"title", "name", "game", "games", "sort", "filter", "search"}:
                continue
            seen.add(key)
            results.append({"title": candidate})

    if results:
        return results, None

    return [], "Tabletop Together share page contained no game rows."


def get_tabletop_together_games():
    page, error = fetch_html(TABLETOP_TOGETHER_URL)
    if error:
        return [], error
    games, parse_error = extract_tabletop_titles(page)
    if games:
        logger.info("Fetched %d Tabletop Together games from %s", len(games), TABLETOP_TOGETHER_URL)
        return games, None
    return [], parse_error


def compare_titles(spiel_titles, tabletop_titles):
    results = []
    exact_matches = {title_key(game["title"]): game["title"] for game in tabletop_titles if title_key(game["title"])}

    for spiel in spiel_titles:
        original = normalize_title(spiel.get("title"))
        key = title_key(original)
        best_match = exact_matches.get(key)
        best_score = 100 if best_match else 0

        if not best_match:
            for tabletop in tabletop_titles:
                candidate = normalize_title(tabletop.get("title"))
                candidate_key = title_key(candidate)
                if not candidate_key:
                    continue
                score = max(
                    fuzz.token_set_ratio(key, candidate_key),
                    fuzz.token_sort_ratio(key, candidate_key),
                    fuzz.ratio(key, candidate_key),
                )
                if score > best_score:
                    best_score = score
                    best_match = candidate

        if best_match and best_score >= 90:
            status = "match"
        elif best_match and best_score >= 75:
            status = "possible match"
        else:
            status = "not found"
            if best_score < 75:
                best_match = None

        results.append({
            "spiel_title": original,
            "best_match": best_match,
            "status": status,
            "confidence": best_score,
        })

    return results


@app.route("/")
def index():
    spiel_titles, spiel_error = get_spiel_novelties()
    tabletop_titles, tabletop_error = get_tabletop_together_games()
    matches = compare_titles(spiel_titles, tabletop_titles) if spiel_titles and tabletop_titles else []

    html_template = """
    <html><head><title>SPIEL Essen vs Tabletop Together</title>
    <style>
      body { font-family: Arial; margin: 20px; }
      table { border-collapse: collapse; width: 100%; }
      th, td { border: 1px solid #ccc; padding: 8px; }
      th { background: #f2f2f2; }
      .warning { color: #8a3b00; background: #fff3e0; padding: 10px; border: 1px solid #ffcc80; margin-bottom: 20px; }
      .status-match { color: green; } .status-possible-match { color: orange; } .status-not-found { color: red; }
    </style></head><body>
      <h1>SPIEL Essen vs Tabletop Together</h1>
      {% if spiel_error %}<div class="warning">SPIEL data unavailable: {{ spiel_error }}</div>{% endif %}
      {% if tabletop_error %}<div class="warning">Tabletop Together data unavailable: {{ tabletop_error }}</div>{% endif %}
      <p>SPIEL products: {{ spiel_count }} | Tabletop Together games: {{ tabletop_count }}</p>
      <h2>SPIEL Titles and Tabletop Together Matches</h2>
      {% if matches %}
      <table><tr><th>SPIEL title</th><th>Tabletop Together match</th><th>Status</th><th>Confidence</th></tr>
      {% for item in matches %}<tr><td>{{ item.spiel_title }}</td><td>{{ item.best_match or "-" }}</td>
      <td class="status-{{ item.status | replace(' ', '-') }}">{{ item.status }}</td><td>{{ item.confidence }}%</td></tr>{% endfor %}</table>
      {% elif not spiel_error and not tabletop_error %}<p>No comparison results were found.</p>{% endif %}
    </body></html>
    """

    return render_template_string(
        html_template,
        matches=matches,
        spiel_count=len(spiel_titles),
        tabletop_count=len(tabletop_titles),
        spiel_error=spiel_error,
        tabletop_error=tabletop_error,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

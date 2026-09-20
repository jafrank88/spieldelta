import html
import json
import logging
import os
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from flask import Flask, render_template_string
from thefuzz import fuzz
import requests

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; spieldelta/1.3; +https://github.com/jafrank88/spieldelta)"
SPIEL_PRODUCTS_URL = os.getenv(
    "SPIEL_PRODUCTS_URL",
    "https://maps.eyeled-services.de/en/spiel26/products?columns=%5B%22ID%22%2C%22INFO%22%2C%22S_ORDER%22%2C%22TITEL%22%2C%22FIRMA_ID%22%2C%22UNTERTITEL%22%2C%22BILDER%22%2C%22BILDER_VERSIONEN%22%2C%22BILDER_TEXTE%22%5D",
)
TABLETOP_TOGETHER_URL = "https://tabletoptogether.com/tool/games.php"

STOP_WORDS = {
    "home", "about", "contact", "news", "events", "login", "signup",
    "search", "shop", "cart", "privacy", "terms", "menu", "navigation",
    "more", "games", "tool", "tabletop", "tabletop together", "newsletter",
    "subscribe", "download", "details", "faq", "support",
}


def normalize_title(value):
    if value is None:
        return ""
    value = html.unescape(str(value))
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"\(.*?\)|\[.*?\]", "", value)
    return value.strip()


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
        logger.exception("Request failed for %s: %s", url, exc)
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
        logger.exception("JSON request failed for %s: %s", url, exc)
        return None, f"Could not load product data from {url}."


def iter_candidate_pages(url):
    pages = []
    base_html, base_error = fetch_html(url)
    if base_error:
        return pages, base_error

    pages.append((url, base_html))
    soup = BeautifulSoup(base_html, "html.parser")
    seen = {url}

    for frame in soup.find_all("iframe", src=True):
        frame_url = urljoin(url, frame["src"])
        if frame_url in seen:
            continue
        seen.add(frame_url)
        frame_html, frame_error = fetch_html(frame_url)
        if frame_error:
            logger.warning("Could not load iframe %s: %s", frame_url, frame_error)
            continue
        pages.append((frame_url, frame_html))

    return pages, None


def extract_title_candidates(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    seen = set()
    results = []

    for tag in soup.find_all(["a", "li", "td", "div", "h1", "h2", "h3", "span", "p", "article"]):
        text = normalize_title(tag.get_text(" ", strip=True))
        lowered = text.lower()
        if not text or len(text) < 3 or lowered in STOP_WORDS:
            continue
        if lowered.startswith(("read more", "show more")):
            continue
        if any(token in lowered for token in [
            "login", "signup", "privacy", "terms", "cart", "search",
            "newsletter", "subscribe", "hall", "booth", "support", "contact",
        ]):
            continue
        if text not in seen:
            seen.add(text)
            results.append(text)

    return results


def get_tabletoptogether_games():
    pages, error = iter_candidate_pages(TABLETOP_TOGETHER_URL)
    if error:
        return [], error

    titles = []
    seen = set()
    for _, page_html in pages:
        for title in extract_title_candidates(page_html):
            lowered = title.lower()
            if any(token in lowered for token in [
                "download", "newsletter", "subscribe", "hall", "booth",
                "support", "contact", "privacy", "terms",
            ]):
                continue
            if title not in seen:
                seen.add(title)
                titles.append({"title": title})

    if titles:
        return titles, None
    return [], "TabletTopTogether games page could not be parsed."


def find_product_records(value):
    """Recursively find product-like records in the SPIEL API response."""
    records = []
    if isinstance(value, dict):
        title = value.get("TITEL") or value.get("title") or value.get("TITLE")
        if title:
            records.append(value)
        for child in value.values():
            records.extend(find_product_records(child))
    elif isinstance(value, list):
        for child in value:
            records.extend(find_product_records(child))
    return records


def get_spiel_novelties():
    """Fetch SPIEL products from the eyeled data endpoint."""
    data, error = fetch_json(SPIEL_PRODUCTS_URL)
    if error:
        return [], error

    records = find_product_records(data)
    games = []
    seen_ids = set()
    seen_titles = set()

    for item in records:
        item_id = item.get("ID") or item.get("id")
        title = normalize_title(item.get("TITEL") or item.get("title") or item.get("TITLE"))
        if not title:
            continue
        title_key = title.casefold()
        if item_id is not None and item_id in seen_ids:
            continue
        if item_id is None and title_key in seen_titles:
            continue
        if item_id is not None:
            seen_ids.add(item_id)
        seen_titles.add(title_key)

        games.append({
            "id": item_id,
            "title": title,
            "publisher": normalize_title(item.get("FIRMA_ID") or item.get("publisher")),
            "subtitle": normalize_title(item.get("UNTERTITEL") or item.get("subtitle")),
            "description": normalize_title(item.get("INFO") or item.get("description")),
            "hall": "",
            "booth": "",
            "designer": "",
            "artist": "",
            "releaseYear": "",
        })

    logger.info("Fetched %d SPIEL products from %s", len(games), SPIEL_PRODUCTS_URL)
    if games:
        return games, None
    logger.error("No product records found in SPIEL response: %s", json.dumps(data)[:1000])
    return [], "SPIEL product API returned no products with titles."


def compare_titles(spiel_titles, tablet_titles):
    results = []
    for spiel in spiel_titles:
        spiel_title = normalize_title(spiel.get("title"))
        best_score = 0
        best_match = None

        for tablet in tablet_titles:
            tablet_title = normalize_title(tablet.get("title"))
            if not tablet_title:
                continue
            score = fuzz.ratio(spiel_title.lower(), tablet_title.lower())
            if score > best_score:
                best_score = score
                best_match = tablet_title

        if best_match is None or best_score < 75:
            status = "not found"
        elif best_score >= 92:
            status = "match"
        else:
            status = "possible match"

        results.append({
            "spiel_title": spiel_title,
            "best_match": best_match,
            "status": status,
            "confidence": best_score,
        })

    return results


@app.route("/")
def index():
    spiel_titles, spiel_error = get_spiel_novelties()
    tablet_titles, tablet_error = get_tabletop_together_games()
    matches = compare_titles(spiel_titles, tablet_titles) if spiel_titles and tablet_titles else []

    html_template = """
    <html><head><title>SPIEL Essen vs TabletTopTogether</title>
    <style>
      body { font-family: Arial; margin: 20px; }
      table { border-collapse: collapse; width: 100%; }
      th, td { border: 1px solid #ccc; padding: 8px; }
      th { background: #f2f2f2; }
      .warning { color: #8a3b00; background: #fff3e0; padding: 10px; border: 1px solid #ffcc80; margin-bottom: 20px; }
      .status-match { color: green; } .status-possible-match { color: orange; } .status-not-found { color: red; }
    </style></head><body>
      <h1>SPIEL Essen vs TabletTopTogether</h1>
      {% if spiel_error %}<div class="warning">SPIEL data unavailable: {{ spiel_error }}</div>{% endif %}
      {% if tablet_error %}<div class="warning">TabletTopTogether data unavailable: {{ tablet_error }}</div>{% endif %}
      <h2>SPIEL Titles ({{ spiel_count }})</h2>
      {% if matches %}
      <table><tr><th>Title</th><th>TabletTopTogether match</th><th>Status</th><th>Confidence</th></tr>
      {% for item in matches %}<tr><td>{{ item.spiel_title }}</td><td>{{ item.best_match or "-" }}</td>
      <td class="status-{{ item.status | replace(' ', '-') }}">{{ item.status }}</td><td>{{ item.confidence }}</td></tr>{% endfor %}</table>
      {% elif not spiel_error and not tablet_error %}<p>No comparison results were found.</p>{% endif %}
    </body></html>
    """

    return render_template_string(
        html_template,
        matches=matches,
        spiel_count=len(spiel_titles),
        spiel_error=spiel_error,
        tablet_error=tablet_error,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

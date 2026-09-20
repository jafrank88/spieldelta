import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from flask import Flask, render_template_string
import requests

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; spieldelta/1.1; +https://github.com/jafrank88/spieldelta)"

STOP_WORDS = {
    "home",
    "about",
    "contact",
    "news",
    "events",
    "login",
    "signup",
    "search",
    "shop",
    "cart",
    "privacy",
    "terms",
    "menu",
    "navigation",
    "more",
}


def normalize_text(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def fetch_html(url):
    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.text, None
    except requests.RequestException as exc:
        logger.exception("Request failed for %s: %s", url, exc)
        return None, f"Could not load page from {url}."


def collect_candidate_titles(soup, preferred_hints=None):
    """Best-effort extraction of title-like strings from a page."""
    preferred_hints = preferred_hints or []
    seen = set()
    results = []

    def add_text(value):
        text = normalize_text(value)
        if len(text) < 3:
            return
        if text.lower() in STOP_WORDS:
            return
        if text in seen:
            return
        seen.add(text)
        results.append(text)

    for hint in preferred_hints:
        add_text(hint)

    for tag in soup.find_all(["a", "h1", "h2", "h3", "h4", "li", "article", "div", "span"]):
        text = normalize_text(tag.get_text(" ", strip=True))
        if not text:
            continue
        if len(text) < 3:
            continue
        lower = text.lower()
        if lower in STOP_WORDS:
            continue
        if lower.startswith("read more"):
            continue
        if lower.startswith("show more"):
            continue
        if any(x in lower for x in ["login", "signup", "search", "privacy", "terms", "cart", "news"]):
            continue
        add_text(text)

    for link in soup.find_all("a", href=True):
        href = link.get("href", "")
        text = normalize_text(link.get_text(" ", strip=True))
        if not text:
            continue
        if any(token in href.lower() for token in ["boardgame", "product", "game", "novelty"]) or any(token in text.lower() for token in ["spiel", "preview", "release", "novelty"]):
            add_text(text)

    return results


def get_bgg_preview_titles(preview_id=93):
    """Best-effort extraction of BGG preview titles from HTML pages."""
    urls = [
        f"https://boardgamegeek.com/geekpreview/{preview_id}",
        f"https://boardgamegeek.com/preview/{preview_id}",
        "https://boardgamegeek.com/geekpreview",
        "https://boardgamegeek.com/geekpreview/",
    ]

    last_error = None
    for url in urls:
        html, error = fetch_html(url)
        if error:
            last_error = error
            continue

        soup = BeautifulSoup(html, "html.parser")
        titles = collect_candidate_titles(soup, preferred_hints=["GeekPreview", "Preview"])

        cleaned = []
        for title in titles:
            if "preview" in title.lower() and len(title) < 30:
                continue
            cleaned.append({
                "bgg_id": preview_id,
                "title": title,
                "publisher": "",
            })

        if cleaned:
            return cleaned, None

    return [], last_error or "BGG preview page could not be parsed."


def get_spiel_novelties():
    """Best-effort extraction of SPIEL novelties from the public HTML page."""
    url = "https://spiel-essen.de/en/the-spiel/novelties"
    html, error = fetch_html(url)
    if error:
        return [], error

    soup = BeautifulSoup(html, "html.parser")
    candidates = collect_candidate_titles(soup, preferred_hints=["SPIEL", "Novelties", "The Spiel"])

    cleaned = []
    for title in candidates:
        if any(token in title.lower() for token in ["subscribe", "newsletter", "event", "hall", "booth"]):
            continue
        cleaned.append({
            "title": title,
            "publisher": "",
            "hall": "",
            "booth": "",
        })

    if cleaned:
        return cleaned, None

    return [], "SPIEL novelties page could not be parsed."


@app.route("/")
def index():
    bgg_titles, bgg_error = get_bgg_preview_titles()
    spiel_titles, spiel_error = get_spiel_novelties()

    html_template = """
    <html>
    <head>
        <title>SPIEL Essen vs BGG Preview</title>
        <style>
            body { font-family: Arial; margin: 20px; }
            table { border-collapse: collapse; width: 100%; }
            th, td { border: 1px solid #ccc; padding: 8px; }
            th { background-color: #f2f2f2; }
            .warning { color: #8a3b00; background: #fff3e0; padding: 10px; border: 1px solid #ffcc80; margin-bottom: 20px; }
        </style>
    </head>
    <body>
        <h1>SPIEL Essen vs BGG Preview</h1>

        {% if bgg_error %}
        <div class="warning">BGG data unavailable: {{ bgg_error }}</div>
        {% endif %}
        {% if spiel_error %}
        <div class="warning">SPIEL data unavailable: {{ spiel_error }}</div>
        {% endif %}

        <h2>BGG Titles ({{ bgg_count }})</h2>
        <table>
            <tr><th>ID</th><th>Title</th><th>Publisher</th></tr>
            {% for item in bgg_titles %}
            <tr><td>{{ item.bgg_id }}</td><td>{{ item.title }}</td><td>{{ item.publisher }}</td></tr>
            {% endfor %}
        </table>

        <h2>SPIEL Titles ({{ spiel_count }})</h2>
        <table>
            <tr><th>Title</th><th>Publisher</th><th>Hall</th><th>Booth</th></tr>
            {% for item in spiel_titles %}
            <tr><td>{{ item.title }}</td><td>{{ item.publisher }}</td><td>{{ item.hall }}</td><td>{{ item.booth }}</td></tr>
            {% endfor %}
        </table>
    </body>
    </html>
    """
    return render_template_string(
        html_template,
        bgg_titles=bgg_titles,
        spiel_titles=spiel_titles,
        bgg_count=len(bgg_titles),
        spiel_count=len(spiel_titles),
        bgg_error=bgg_error,
        spiel_error=spiel_error,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

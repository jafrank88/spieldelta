import html
import logging
import re

from bs4 import BeautifulSoup
from flask import Flask, render_template_string
from thefuzz import fuzz
import requests

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; spieldelta/1.1; +https://github.com/jafrank88/spieldelta)"
TABLETOP_TOGETHER_URL = "https://tabletoptogether.com/tool/games.php"
SPIEL_NOVELTIES_URL = "https://spiel-essen.de/en/the-spiel/novelties"

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
    "games",
    "tool",
    "tabletop",
    "tabletop together",
    "novelties",
    "spiel",
    "the spiel",
}


def normalize_title(value):
    if value is None:
        return ""
    value = html.unescape(str(value))
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"\(.*?\)", "", value)
    value = re.sub(r"\[.*?\]", "", value)
    return value.strip()


def fetch_html(url):
    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.text, None
    except requests.RequestException as exc:
        logger.exception("Request failed for %s: %s", url, exc)
        return None, f"Could not load page from {url}."


def extract_candidate_titles(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    seen = set()
    results = []

    for tag in soup.find_all(["a", "li", "td", "div", "h1", "h2", "h3", "span", "p"]):
        text = normalize_title(tag.get_text(" ", strip=True))
        if not text or len(text) < 3:
            continue
        lowered = text.lower()
        if lowered in STOP_WORDS:
            continue
        if lowered.startswith("read more") or lowered.startswith("show more"):
            continue
        if any(token in lowered for token in ["login", "signup", "privacy", "terms", "cart", "search", "news", "newsletter"]):
            continue
        if text in seen:
            continue
        seen.add(text)
        results.append(text)

    return results


def get_tabletoptogether_games():
    html_text, error = fetch_html(TABLETOP_TOGETHER_URL)
    if error:
        return [], error

    raw_candidates = extract_candidate_titles(html_text)
    cleaned = []
    for title in raw_candidates:
        if len(title) < 3:
            continue
        cleaned.append({"title": title})

    if cleaned:
        return cleaned, None

    return [], "TabletTopTogether games page could not be parsed."


def get_spiel_novelties():
    html_text, error = fetch_html(SPIEL_NOVELTIES_URL)
    if error:
        return [], error

    raw_candidates = extract_candidate_titles(html_text)
    cleaned = []
    for title in raw_candidates:
        lowered = title.lower()
        if any(token in lowered for token in ["newsletter", "subscribe", "hall", "booth", "event", "news", "login", "signup"]):
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


def compare_titles(spiel_titles, tablet_titles):
    results = []
    for spiel in spiel_titles:
        spiel_title = normalize_title(spiel["title"])
        best_score = 0
        best_match = None

        for tablet in tablet_titles:
            tablet_title = normalize_title(tablet["title"])
            score = fuzz.ratio(spiel_title.lower(), tablet_title.lower())
            if score > best_score:
                best_score = score
                best_match = tablet_title

        if best_match is None:
            status = "not found"
            confidence = 0
        elif best_score >= 92:
            status = "match"
            confidence = best_score
        elif best_score >= 75:
            status = "possible match"
            confidence = best_score
        else:
            status = "not found"
            confidence = best_score

        results.append({
            "spiel_title": spiel_title,
            "best_match": best_match,
            "status": status,
            "confidence": confidence,
        })

    return results


@app.route("/")
def index():
    spiel_titles, spiel_error = get_spiel_novelties()
    tablet_titles, tablet_error = get_tabletoptogether_games()
    matches = compare_titles(spiel_titles, tablet_titles) if spiel_titles and tablet_titles else []

    html_template = """
    <html>
    <head>
        <title>SPIEL Essen vs TabletTopTogether</title>
        <style>
            body { font-family: Arial; margin: 20px; }
            table { border-collapse: collapse; width: 100%; }
            th, td { border: 1px solid #ccc; padding: 8px; }
            th { background-color: #f2f2f2; }
            .warning { color: #8a3b00; background: #fff3e0; padding: 10px; border: 1px solid #ffcc80; margin-bottom: 20px; }
            .status-match { color: green; }
            .status-possible { color: orange; }
            .status-not-found { color: red; }
        </style>
    </head>
    <body>
        <h1>SPIEL Essen vs TabletTopTogether</h1>

        {% if spiel_error %}
        <div class="warning">SPIEL data unavailable: {{ spiel_error }}</div>
        {% endif %}
        {% if tablet_error %}
        <div class="warning">TabletTopTogether data unavailable: {{ tablet_error }}</div>
        {% endif %}

        <h2>SPIEL Titles ({{ spiel_count }})</h2>
        <table>
            <tr><th>Title</th><th>TabletTopTogether match</th><th>Status</th><th>Confidence</th></tr>
            {% for item in matches %}
            <tr>
                <td>{{ item.spiel_title }}</td>
                <td>{{ item.best_match or "-" }}</td>
                <td class="status-{{ item.status | replace(' ', '-') }}">{{ item.status }}</td>
                <td>{{ item.confidence }}</td>
            </tr>
            {% endfor %}
        </table>
    </body>
    </html>
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

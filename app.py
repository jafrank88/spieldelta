import logging

from flask import Flask, render_template_string
import requests

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20


def fetch_json(url, headers):
    """Fetch a URL and return (data, error_message)."""
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json(), None
    except requests.RequestException as exc:
        logger.exception("Request failed for %s: %s", url, exc)
        return None, f"Could not load data from {url}."
    except ValueError as exc:
        logger.exception("Invalid JSON from %s: %s", url, exc)
        return None, f"The response from {url} was not valid JSON."


def get_bgg_preview_titles(preview_id=93):
    """Fetch titles listed in BGG's GeekPreview."""
    url = f"https://boardgamegeek.com/api/geekpreview/items?previewid={preview_id}"
    headers = {"User-Agent": "spieldelta/1.0"}
    data, error = fetch_json(url, headers)
    if error or data is None:
        logger.warning("BGG data unavailable: %s", error)
        return [], error

    if not isinstance(data, dict):
        logger.warning("BGG response was not a dictionary: %s", type(data).__name__)
        return [], "BGG returned an unexpected response format."

    items = data.get("items", [])
    if not isinstance(items, list):
        logger.warning("BGG items field was missing or not a list.")
        return [], "BGG returned an unexpected response format."

    result = [
        {
            "bgg_id": item.get("itemid"),
            "title": str(item.get("itemname") or "").strip(),
            "publisher": str(item.get("publishername") or "").strip(),
        }
        for item in items
        if isinstance(item, dict)
    ]
    return result, None


def get_spiel_novelties():
    """Fetch titles listed on the official SPIEL Essen novelties portal."""
    url = "https://www.spiel-essen.de/en/api/novelties"
    headers = {"User-Agent": "spieldelta/1.0"}
    data, error = fetch_json(url, headers)
    if error or data is None:
        logger.warning("SPIEL data unavailable: %s", error)
        return [], error

    if not isinstance(data, dict):
        logger.warning("SPIEL response was not a dictionary: %s", type(data).__name__)
        return [], "SPIEL returned an unexpected response format."

    items = data.get("data", [])
    if not isinstance(items, list):
        logger.warning("SPIEL data field was missing or not a list.")
        return [], "SPIEL returned an unexpected response format."

    result = [
        {
            "title": str(item.get("title") or "").strip(),
            "publisher": str(item.get("exhibitor") or "").strip(),
            "hall": str(item.get("hall") or ""),
            "booth": str(item.get("booth") or ""),
        }
        for item in items
        if isinstance(item, dict)
    ]
    return result, None


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

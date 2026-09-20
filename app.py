from flask import Flask, render_template_string
import pandas as pd
import requests
from thefuzz import fuzz

app = Flask(__name__)

def get_bgg_preview_titles(preview_id=93):
    """Fetches titles listed in BGG's GeekPreview."""
    url = f"https://boardgamegeek.com/api/geekpreview/items?previewid={preview_id}"
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        return []
    data = response.json()
    return [
        {
            "bgg_id": item.get("itemid"),
            "title": item.get("itemname", "").strip(),
            "publisher": item.get("publishername", "").strip(),
        }
        for item in data.get("items", [])
    ]

def get_spiel_novelties():
    """Fetches titles listed on the official SPIEL Essen novelties portal."""
    url = "https://www.spiel-essen.de/en/api/novelties"
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        return []
    data = response.json()
    return [
        {
            "title": item.get("title", "").strip(),
            "publisher": item.get("exhibitor", "").strip(),
            "hall": item.get("hall", ""),
            "booth": item.get("booth", ""),
        }
        for item in data.get("data", [])
    ]

@app.route("/")
def index():
    bgg_titles = get_bgg_preview_titles()
    spiel_titles = get_spiel_novelties()

    html_template = """
    <html>
    <head>
        <title>SPIEL Essen vs BGG Preview</title>
        <style>
            body { font-family: Arial; margin: 20px; }
            table { border-collapse: collapse; width: 100%; }
            th, td { border: 1px solid #ccc; padding: 8px; }
            th { background-color: #f2f2f2; }
        </style>
    </head>
    <body>
        <h1>SPIEL Essen vs BGG Preview</h1>
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
    return render_template_string(html_template,
                                  bgg_titles=bgg_titles,
                                  spiel_titles=spiel_titles,
                                  bgg_count=len(bgg_titles),
                                  spiel_count=len(spiel_titles))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

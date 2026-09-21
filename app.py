import csv
import html
import io
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template_string, request
from thefuzz import fuzz

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20
CACHE_TTL = int(os.getenv("DATA_CACHE_TTL", "300"))
USER_AGENT = "Mozilla/5.0 (compatible; spieldelta/1.7; +https://github.com/jafrank88/spieldelta)"

SPIEL_PRODUCTS_URL = os.getenv(
    "SPIEL_PRODUCTS_URL",
    "https://maps.eyeled-services.de/en/spiel26/products?columns=%5B%22ID%22%2C%22INFO%22%2C%22S_ORDER%22%2C%22TITEL%22%2C%22FIRMA_ID%22%2C%22UNTERTITEL%22%2C%22BILDER%22%2C%22BILDER_VERSIONEN%22%2C%22BILDER_TEXTE%22%5D",
)
# The BGG Spiel Preview / Tabletop Together list is read from a CSV committed to the repo (no website call).
# Defaults to TabletopTogetherTool.csv next to app.py; set PREVIEW_CSV to use a different path.
# (Set PREVIEW_CSV="" to auto-detect the only *.csv file next to app.py instead.)
APP_DIR = os.path.dirname(os.path.abspath(__file__))
PREVIEW_CSV = os.getenv("PREVIEW_CSV", "TabletopTogetherTool.csv").strip()

# --- BGG direct-link resolution -------------------------------------------
# BGG's XML API2 requires a registered application token (Bearer auth).
# Register at https://boardgamegeek.com/using_the_xml_api and set BGG_API_TOKEN.
# Without a token the app still works and falls back to BGG search links.
BGG_API_TOKEN = os.getenv("BGG_API_TOKEN", "").strip()
BGG_API_BASE = "https://boardgamegeek.com/xmlapi2"
# The cache lives next to app.py by default so a pre-built bgg_cache.json can be committed to the repo
# and shipped with the deploy (hosts like Heroku have ephemeral disks).
BGG_CACHE_FILE = os.getenv("BGG_CACHE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bgg_cache.json"))
# BGG's API2 wiki says a 5-second wait between requests "seems to suffice"; lower it at your own risk.
BGG_MIN_INTERVAL = float(os.getenv("BGG_MIN_INTERVAL", "5.0"))
# Matches are kept permanently. Misses expire, because BGG keeps adding SPIEL games in the weeks before the show.
BGG_NEGATIVE_TTL = float(os.getenv("BGG_NEGATIVE_TTL_DAYS", "3")) * 86400
BGG_THING_TTL = float(os.getenv("BGG_THING_TTL_DAYS", "7")) * 86400
# Only SPIEL games NOT already on the Tabletop Together / BGG Spiel Preview list get a BGG API lookup.
# "possible match" (fuzzy 75-89) is ambiguous, so it is looked up too; set to "not found" to be stricter.
BGG_LOOKUP_STATUSES = {x.strip() for x in os.getenv("BGG_LOOKUP_STATUSES", "not found,possible match").split(",") if x.strip()}
NAME_THRESHOLD = 90
PUBLISHER_THRESHOLD = 85

_cache_lock = threading.Lock()
_cache = {"spiel": (0.0, [], None), "tabletop": (0.0, [], None)}

_bgg_lock = threading.Lock()
_bgg_results = {}  # spiel game key -> {"id": int|None, "name": str|None, "checked": epoch, "sig": str}
_bgg_things = {}   # BGG id -> {"names": [...], "primary": str, "publishers": [...], "fetched": epoch}
_bgg_cache_loaded = False
_bgg_thread = None
_bgg_disabled_reason = None
_bgg_last_call = 0.0

GENERIC_PUBLISHER_WORDS = {
    "games", "game", "spiele", "spiel", "verlag", "gmbh", "co", "kg", "ltd", "llc", "inc", "sl", "srl",
    "studio", "studios", "publishing", "publications", "editions", "edition", "entertainment", "international",
}


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


def publisher_key(value):
    """Like title_key, but also drops generic corporate words ('Games', 'GmbH', ...)."""
    words = title_key(value).split()
    kept = [w for w in words if w not in GENERIC_PUBLISHER_WORDS]
    return " ".join(kept or words)


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


# --- SPIEL novelties: publisher, hall, booth --------------------------------

def extract_publishers(item):
    """Publisher names from the INFO HTML table (falls back to UNTERTITEL)."""
    publishers = []
    info = item.get("INFO")
    if info:
        soup = BeautifulSoup(info, "html.parser")
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) >= 2 and cells[0].get_text(strip=True).rstrip(":").casefold() in {"publisher", "publishers"}:
                text = cells[1].get_text(" ", strip=True)
                publishers.extend(p.strip() for p in re.split(r",|;| / ", text) if p.strip())
                break
    if not publishers and item.get("UNTERTITEL"):
        publishers.append(str(item["UNTERTITEL"]).strip())
    return publishers


def extract_stands(item):
    """Return (halls, booths) from the STAENDE list, preserving order and dropping duplicates."""
    halls, booths = [], []
    for stand in item.get("STAENDE") or []:
        if not isinstance(stand, dict):
            continue
        hall = re.sub(r"(?i)^hall\s*", "", str(stand.get("HALLE") or "")).strip()
        booth = str(stand.get("NAME") or "").strip()
        if hall and hall not in halls:
            halls.append(hall)
        if booth and booth not in booths:
            booths.append(booth)
    return halls, booths


def get_spiel_novelties():
    raw, error = fetch(SPIEL_PRODUCTS_URL, "application/json")
    if error:
        return [], error
    try:
        data = json.loads(raw)
    except ValueError as exc:
        logger.exception("SPIEL response was not JSON: %s", exc)
        return [], "SPIEL product API returned invalid JSON."
    games, seen = [], set()
    for item in find_records(data, ("TITEL", "title", "TITLE")):
        item_id = item.get("ID") or item.get("id")
        title = normalize_title(item.get("TITEL") or item.get("title") or item.get("TITLE"))
        key = str(item_id) if item_id is not None else title_key(title)
        if not title or key in seen:
            continue
        seen.add(key)
        halls, booths = extract_stands(item)
        games.append({
            "id": item_id,
            "key": key,
            "title": title,
            "raw_title": html.unescape(str(item.get("TITEL") or item.get("title") or item.get("TITLE"))).strip(),
            "publishers": extract_publishers(item),
            "hall": ", ".join(halls),
            "booth": ", ".join(booths),
        })
    logger.info("Fetched %d SPIEL products from %s", len(games), SPIEL_PRODUCTS_URL)
    return (games, None) if games else ([], "SPIEL product API returned no products with titles.")


# --- Tabletop Together -------------------------------------------------------

def add_tabletop_title(results, seen, value):
    value = normalize_title(value)
    key = title_key(value)
    if not key or len(value) > 180 or key in seen or key in {"title", "name", "game", "games", "sort", "filter", "search"}:
        return
    seen.add(key)
    results.append({"title": value})


def find_preview_csv():
    if PREVIEW_CSV:
        path = PREVIEW_CSV if os.path.isabs(PREVIEW_CSV) else os.path.join(APP_DIR, PREVIEW_CSV)
        return path, None
    found = sorted(f for f in os.listdir(APP_DIR) if f.lower().endswith(".csv"))
    if len(found) == 1:
        return os.path.join(APP_DIR, found[0]), None
    if not found:
        return None, f"No CSV found in {APP_DIR}. Commit it there or set PREVIEW_CSV to its path."
    return None, f"Several CSV files found ({', '.join(found)}); set PREVIEW_CSV to choose one."


def pick_title_column(header):
    """Prefer a column named like 'title', then 'name', then 'game'; else the first column."""
    lowered = [normalize_title(h).casefold() for h in header]
    for word in ("title", "name", "game"):
        for i, h in enumerate(lowered):
            if word in h:
                return i
    return 0


def get_preview_games():
    """Titles already on the BGG Spiel Preview list, read from the CSV in the repo."""
    path, error = find_preview_csv()
    if error:
        return [], error
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
    except OSError as exc:
        logger.warning("Could not read %s", path)
        return [], f"Could not read preview CSV {path}: {exc.strerror or exc}"
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if len(rows) < 2:
        return [], f"Preview CSV {os.path.basename(path)} has no data rows."
    index_ = pick_title_column(rows[0])
    results, seen = [], set()
    for row in rows[1:]:
        if index_ < len(row):
            add_tabletop_title(results, seen, row[index_])
    logger.info("Loaded %d preview games from %s (title column: %r)", len(results), path, rows[0][index_])
    return (results, None) if results else ([], f"No titles found in {os.path.basename(path)}.")


def cached_data(name, loader):
    now = time.monotonic()
    with _cache_lock:
        timestamp, data, error = _cache[name]
        if now - timestamp < CACHE_TTL:
            return data, error
        data, error = loader()
        _cache[name] = (time.monotonic(), data, error)
        return data, error


# --- BGG lookup ---------------------------------------------------------------

def bgg_search_url(title):
    """Fallback: general BGG search link."""
    return f"https://boardgamegeek.com/geeksearch.php?action=search&objecttype=boardgame&q={quote_plus(title)}"


def bgg_game_url(bgg_id):
    return f"https://boardgamegeek.com/boardgame/{bgg_id}"


class BGGAuthError(Exception):
    pass


def bgg_get(endpoint, params, attempts=4):
    """Rate-limited, authenticated GET against BGG XML API2. Returns an ElementTree root or None."""
    global _bgg_last_call
    headers = {"Authorization": f"Bearer {BGG_API_TOKEN}", "User-Agent": USER_AGENT}
    for attempt in range(1, attempts + 1):
        wait = BGG_MIN_INTERVAL - (time.monotonic() - _bgg_last_call)
        if wait > 0:
            time.sleep(wait)
        _bgg_last_call = time.monotonic()
        try:
            response = requests.get(f"{BGG_API_BASE}/{endpoint}", params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            logger.warning("BGG request failed (attempt %d)", attempt)
            time.sleep(2 * attempt)
            continue
        if response.status_code in (401, 403):
            raise BGGAuthError(f"BGG returned {response.status_code}; check BGG_API_TOKEN")
        if response.status_code in (202, 429, 500, 502, 503):
            time.sleep(3 * attempt)  # queued / throttled: back off and retry
            continue
        if response.status_code != 200:
            logger.warning("BGG %s returned %s", endpoint, response.status_code)
            return None
        try:
            return ET.fromstring(response.content)
        except ET.ParseError:
            logger.warning("BGG %s returned unparseable XML", endpoint)
            return None
    return None


def best_name_score(spiel_key, names):
    scores = [fuzz.ratio(spiel_key, title_key(n)) for n in names if n]
    return max(scores, default=0)


def best_publisher_score(spiel_publishers, bgg_publishers):
    scores = []
    for sp in spiel_publishers:
        sk = publisher_key(sp)
        if not sk:
            continue
        for bp in bgg_publishers:
            bk = publisher_key(bp)
            if bk:
                scores.append(max(fuzz.ratio(sk, bk), fuzz.token_set_ratio(sk, bk)))
    return max(scores, default=0)


def game_signature(game):
    """Changes if the SPIEL title or publisher changes, which invalidates a cached match."""
    pubs = "|".join(sorted(publisher_key(p) for p in game["publishers"]))
    return f"{title_key(game['title'])}|{pubs}"


def cache_entry_fresh(entry, game):
    """Matches never expire; misses are re-checked after BGG_NEGATIVE_TTL. A changed title/publisher forces a re-check."""
    if not entry or entry.get("sig") != game_signature(game):
        return False
    if entry.get("id"):
        return True
    return time.time() - entry.get("checked", 0) < BGG_NEGATIVE_TTL


def get_bgg_things(ids):
    """Names + publishers for BGG ids, served from cache where possible (one batched call for the rest)."""
    now = time.time()
    with _bgg_lock:
        missing = [i for i in ids if i not in _bgg_things or now - _bgg_things[i]["fetched"] > BGG_THING_TTL]
    if missing:
        # versions=1 also returns publishers of localized editions (e.g. Pegasus's German edition of an English game).
        root = bgg_get("thing", {"id": ",".join(missing), "versions": 1})
        if root is None:
            raise RuntimeError("thing lookup failed")  # transient: leave unresolved so it retries later
        with _bgg_lock:
            for item in root.findall("item"):
                names = [n.get("value") for n in item.findall("name")]  # primary + alternate names
                primary = next((n.get("value") for n in item.findall("name") if n.get("type") == "primary"), names[0] if names else "")
                pubs = sorted({l.get("value") for l in item.iter("link") if l.get("type") == "boardgamepublisher" and l.get("value")})
                _bgg_things[item.get("id")] = {"names": names, "primary": primary, "publishers": pubs, "fetched": now}
    with _bgg_lock:
        return [(i, _bgg_things[i]) for i in ids if i in _bgg_things]


def resolve_bgg_game(game):
    """
    Find the BGG game whose name AND publisher both match this SPIEL entry.
    Returns {"id": int, "name": str} or None. At most two API calls: search, then a batched thing lookup
    (skipped for candidates already in the thing cache).
    """
    spiel_key = title_key(game["title"])
    if not spiel_key:
        return None

    search = bgg_get("search", {"query": game.get("raw_title") or game["title"], "type": "boardgame,boardgameexpansion"})
    if search is None:
        raise RuntimeError("search failed")  # transient: leave unresolved so it retries later

    candidates = []
    for item in search.findall("item"):
        name_el = item.find("name")
        if name_el is None:
            continue
        score = fuzz.ratio(spiel_key, title_key(name_el.get("value")))
        if score >= NAME_THRESHOLD:
            candidates.append((score, item.get("id")))
    candidates = [cid for _, cid in sorted(candidates, key=lambda t: -t[0])[:8]]
    if not candidates:
        return None

    best = None
    for cid, thing in get_bgg_things(candidates):
        name_score = best_name_score(spiel_key, thing["names"])
        pub_score = best_publisher_score(game["publishers"], thing["publishers"])
        if name_score >= NAME_THRESHOLD and pub_score >= PUBLISHER_THRESHOLD:
            rank = name_score + pub_score
            if best is None or rank > best[0]:
                best = (rank, {"id": int(cid), "name": thing["primary"]})
    return best[1] if best else None


def load_bgg_cache():
    global _bgg_cache_loaded
    _bgg_cache_loaded = True
    try:
        with open(BGG_CACHE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    with _bgg_lock:
        _bgg_results.update(data.get("games", {}))
        _bgg_things.update(data.get("things", {}))
    logger.info("Loaded BGG cache from %s: %d games, %d things", BGG_CACHE_FILE, len(_bgg_results), len(_bgg_things))


def save_bgg_cache():
    try:
        with _bgg_lock:
            snapshot = {"version": 2, "games": dict(_bgg_results), "things": dict(_bgg_things)}
        tmp = f"{BGG_CACHE_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, separators=(",", ":"))
        os.replace(tmp, BGG_CACHE_FILE)  # atomic: a crash mid-write can't corrupt the cache
    except OSError:
        logger.warning("Could not write %s", BGG_CACHE_FILE)


def _bgg_worker(games):
    global _bgg_disabled_reason
    done, since_save = 0, 0
    for game in games:
        with _bgg_lock:
            if cache_entry_fresh(_bgg_results.get(game["key"]), game):
                continue
        try:
            match = resolve_bgg_game(game)
        except BGGAuthError as exc:
            _bgg_disabled_reason = str(exc)
            logger.error("Disabling BGG lookups: %s", exc)
            break
        except Exception:
            logger.exception("BGG lookup failed for %s", game["title"])
            continue  # not cached, so it is retried on the next worker run
        entry = {"id": match["id"], "name": match["name"]} if match else {"id": None}
        entry.update(checked=int(time.time()), sig=game_signature(game))
        with _bgg_lock:
            _bgg_results[game["key"]] = entry
        done += 1
        since_save += 1
        if since_save >= 10:
            save_bgg_cache()
            since_save = 0
            logger.info("BGG lookups this run: %d", done)
    save_bgg_cache()


def ensure_bgg_worker(games):
    """Start (at most one) background thread that resolves BGG links; the page fills in as results arrive."""
    global _bgg_thread
    if not BGG_API_TOKEN or _bgg_disabled_reason:
        return
    with _bgg_lock:
        if _bgg_thread and _bgg_thread.is_alive():
            return
        pending = [g for g in games if not cache_entry_fresh(_bgg_results.get(g["key"]), g)]
        if not pending:
            return
        _bgg_thread = threading.Thread(target=_bgg_worker, args=(pending,), daemon=True, name="bgg-resolver")
        _bgg_thread.start()


def build_bgg_cache():
    """CLI: `python app.py build-bgg-cache` resolves everything in the foreground and writes bgg_cache.json."""
    if not BGG_API_TOKEN:
        print("Set BGG_API_TOKEN first (register at https://boardgamegeek.com/using_the_xml_api).")
        return 1
    games, error = get_spiel_novelties()
    if error:
        print(error)
        return 1
    tabletop, error = get_preview_games()
    if error:
        print(f"{error} Refusing to look up every game without knowing which are already on the list.")
        return 1
    load_bgg_cache()
    delta = delta_games(games, compare_titles(games, tabletop))
    pending = [g for g in delta if not cache_entry_fresh(_bgg_results.get(g["key"]), g)]
    calls = 2 * len(pending)
    print(f"{len(games)} SPIEL games, {len(delta)} not on the Tabletop Together list, {len(pending)} need lookup (~{calls * BGG_MIN_INTERVAL / 60:.0f} min at worst).")
    _bgg_worker(pending)
    direct = sum(1 for g in delta if (_bgg_results.get(g["key"]) or {}).get("id"))
    print(f"Done: {direct}/{len(delta)} direct matches. Cache written to {BGG_CACHE_FILE}")
    return 1 if _bgg_disabled_reason else 0


# --- Comparison ---------------------------------------------------------------

def compare_titles(spiel_titles, tabletop_titles):
    exact = {title_key(item["title"]): item["title"] for item in tabletop_titles}
    results = []
    for spiel in spiel_titles:
        original = normalize_title(spiel["title"])
        key = title_key(original)
        match, score = exact.get(key), 100 if exact.get(key) else 0
        if not match:
            for item in tabletop_titles:
                candidate = normalize_title(item["title"])
                candidate_key = title_key(candidate)
                candidate_score = max(fuzz.token_set_ratio(key, candidate_key), fuzz.token_sort_ratio(key, candidate_key), fuzz.ratio(key, candidate_key))
                if candidate_score > score:
                    match, score = candidate, candidate_score
        status = "match" if match and score >= 90 else "possible match" if match and score >= 75 else "not found"
        if status == "not found":
            match = None

        with _bgg_lock:
            bgg = _bgg_results.get(spiel.get("key"))
        if bgg and bgg.get("id"):
            bgg_url, bgg_kind = bgg_game_url(bgg["id"]), "direct"
        else:
            bgg_url, bgg_kind = bgg_search_url(original), "search"

        results.append({
            "spiel_title": original,
            "publisher": ", ".join(spiel.get("publishers", [])),
            "hall": spiel.get("hall", ""),
            "booth": spiel.get("booth", ""),
            "best_match": match,
            "status": status,
            "confidence": score,
            "bgg_url": bgg_url,
            "bgg_kind": bgg_kind,
        })
    return results


def delta_games(spiel, matches):
    """SPIEL games missing from the Tabletop Together list. compare_titles returns one row per game, in order."""
    return [g for g, m in zip(spiel, matches) if m["status"] in BGG_LOOKUP_STATUSES]


@app.route("/health", methods=["GET", "HEAD"])
def health():
    return jsonify(status="ok"), 200


@app.route("/", methods=["GET", "HEAD"])
def index():
    if request.method == "HEAD":
        return "", 200
    spiel, spiel_error = cached_data("spiel", get_spiel_novelties)
    tabletop, tabletop_error = cached_data("tabletop", get_preview_games)
    if not _bgg_cache_loaded:
        load_bgg_cache()
    matches = compare_titles(spiel, tabletop) if spiel and tabletop else []
    # If either source failed, matches is empty and NO BGG calls are made (never fall back to looking up everything).
    delta = delta_games(spiel, matches) if matches else []
    ensure_bgg_worker(delta)

    with _bgg_lock:
        bgg_resolved = sum(1 for g in delta if cache_entry_fresh(_bgg_results.get(g["key"]), g))
        bgg_direct = sum(1 for g in delta if (_bgg_results.get(g["key"]) or {}).get("id"))
    bgg_pending = bool(BGG_API_TOKEN) and not _bgg_disabled_reason and bgg_resolved < len(delta)

    template = """
<html><head><title>SPIEL Essen vs Tabletop Together</title>
{% if bgg_pending %}<meta http-equiv="refresh" content="60">{% endif %}
<style>
body{font-family:Arial;margin:20px}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccc;padding:8px}th{background:#f2f2f2}.warning{color:#8a3b00;background:#fff3e0;padding:10px;border:1px solid #ffcc80;margin-bottom:20px}.status-match{color:green}.status-possible-match{color:orange}.status-not-found{color:red}.bgg-search{color:#888}
</style></head><body><h1>SPIEL Essen vs Tabletop Together</h1>
{% if spiel_error %}<div class="warning">SPIEL data unavailable: {{ spiel_error }}</div>{% endif %}
{% if tabletop_error %}<div class="warning">Preview list (CSV) unavailable: {{ tabletop_error }}</div>{% endif %}
{% if bgg_disabled_reason %}<div class="warning">BGG direct links disabled: {{ bgg_disabled_reason }}. Showing search links.</div>{% endif %}
<p>SPIEL products: {{ spiel_count }} | Tabletop Together games: {{ tabletop_count }}
{% if bgg_token %} | BGG lookups for games not on the Tabletop Together list: {{ bgg_resolved }}/{{ delta_count }} ({{ bgg_direct }} direct){% if bgg_pending %} &ndash; still working, page refreshes automatically{% endif %}
{% else %} | BGG direct links off (set BGG_API_TOKEN to enable){% endif %}</p>
{% if matches %}<table><tr><th>SPIEL title</th><th>Publisher</th><th>Hall</th><th>Booth</th><th>Tabletop Together match</th><th>Status</th><th>Confidence</th><th>BGG</th></tr>{% for item in matches %}<tr><td>{{ item.spiel_title }}</td><td>{{ item.publisher or "-" }}</td><td>{{ item.hall or "-" }}</td><td>{{ item.booth or "-" }}</td><td>{{ item.best_match or "-" }}</td><td class="status-{{ item.status|replace(' ','-') }}">{{ item.status }}</td><td>{{ item.confidence }}%</td><td>{% if item.bgg_kind == "direct" %}<a href="{{ item.bgg_url }}" target="_blank" rel="noopener">BGG page</a>{% else %}<a class="bgg-search" href="{{ item.bgg_url }}" target="_blank" rel="noopener">search</a>{% endif %}</td></tr>{% endfor %}</table>{% endif %}
</body></html>"""
    return render_template_string(
        template, matches=matches, spiel_count=len(spiel), tabletop_count=len(tabletop),
        spiel_error=spiel_error, tabletop_error=tabletop_error,
        bgg_token=bool(BGG_API_TOKEN), delta_count=len(delta), bgg_pending=bgg_pending, bgg_resolved=bgg_resolved,
        bgg_direct=bgg_direct, bgg_disabled_reason=_bgg_disabled_reason,
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "build-bgg-cache":
        sys.exit(build_bgg_cache())
    app.run(host="0.0.0.0", port=5000)

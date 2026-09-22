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
from rapidfuzz import fuzz as rfuzz, process as rprocess
from thefuzz import fuzz

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REQUEST_CONNECT_TIMEOUT = float(os.getenv("REQUEST_CONNECT_TIMEOUT", "5"))
REQUEST_READ_TIMEOUT = float(os.getenv("REQUEST_READ_TIMEOUT", "30"))
REQUEST_TIMEOUT = (REQUEST_CONNECT_TIMEOUT, REQUEST_READ_TIMEOUT)
CACHE_TTL = int(os.getenv("DATA_CACHE_TTL", "300"))
USER_AGENT = "Mozilla/5.0 (compatible; spieldelta/1.10; +https://github.com/jafrank88/spieldelta)"

SPIEL_PRODUCTS_URL = os.getenv(
    "SPIEL_PRODUCTS_URL",
    "https://maps.eyeled-services.de/en/spiel26/products?columns=%5B%22ID%22%2C%22INFO%22%2C%22S_ORDER%22%2C%22TITEL%22%2C%22FIRMA_ID%22%2C%22UNTERTITEL%22%2C%22BILDER%22%2C%22BILDER_VERSIONEN%22%2C%22BILDER_TEXTE%22%5D"
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
# BGG thresholds (0-100). CSV matching also uses PUBLISHER_THRESHOLD as a hard fuzzy-match gate.
NAME_THRESHOLD = int(os.getenv("BGG_NAME_THRESHOLD", "90"))
PUBLISHER_THRESHOLD = int(os.getenv("BGG_PUBLISHER_THRESHOLD", "85"))
# Fallback for new/small-press games whose BGG publisher field is empty or mismatched:
# if exactly one candidate has a near-exact name match, accept it without a publisher check.
NAME_ONLY_THRESHOLD = int(os.getenv("BGG_NAME_ONLY_THRESHOLD", "97"))
# How close a second-best candidate's name score can be to the top one before the name-only
# fallback is considered too ambiguous to trust.
NAME_ONLY_MARGIN = int(os.getenv("BGG_NAME_ONLY_MARGIN", "3"))
# How many distinct BGG search hits get a full (all-names) check. BGG search only returns each
# hit's PRIMARY name, so this pool is NOT filtered by name score at the search stage (see
# resolve_bgg_game) -- raising this is cheap, since all candidates are fetched in one batched call.
MAX_BGG_CANDIDATES = int(os.getenv("BGG_MAX_CANDIDATES", "20"))

# Manual BGG link corrections, for SPIEL novelties where the automatic search/publisher matching
# gets it wrong or can't find a candidate at all (see /bgg-why). Two required columns: spiel_title,
# bgg_id; an optional third column, note, is shown next to the link. This file is entirely optional --
# an override always wins over an automatic API match, and a game with an override is never sent to
# the BGG API at all, so it also saves calls. Missing file = no overrides, not an error.
OVERRIDES_CSV = os.getenv("OVERRIDES_CSV", "bgg_overrides.csv").strip()

_cache_lock = threading.Lock()
_cache = {"spiel": (0.0, [], None), "tabletop": (0.0, [], None)}
# One lock per cache key so a slow fetch for "spiel" doesn't block a concurrent request for "tabletop",
# and so two concurrent requests for the same stale key don't both trigger the (slow) loader.
_cache_refresh_locks = {"spiel": threading.Lock(), "tabletop": threading.Lock()}

_bgg_lock = threading.Lock()
_bgg_results = {}  # spiel game key -> {"id": int|None, "name": str|None, "checked": epoch, "sig": str}
_bgg_things = {}   # BGG id -> {"names": [...], "primary": str, "publishers": [...], "fetched": epoch}
_bgg_cache_loaded = False
_bgg_thread = None
_bgg_disabled_reason = None
_bgg_last_call = 0.0  # guarded by _bgg_lock (see bgg_get)

_overrides_lock = threading.Lock()
_overrides_cache = {"loaded_at": 0.0, "data": {}, "error": None}

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
    except requests.RequestException:
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
    if isinstance(info, bytes):
        info = info.decode("utf-8", errors="replace")
    if isinstance(info, str) and info.strip():
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
    games, seen, skipped_no_publisher = [], set(), 0
    for item in find_records(data, ("TITEL", "title", "TITLE")):
        item_id = item.get("ID") or item.get("id")
        title = normalize_title(item.get("TITEL") or item.get("title") or item.get("TITLE"))
        if not title:
            continue
        publishers = extract_publishers(item)
        # Entries with no publisher are section headings in the SPIEL data (e.g. category dividers),
        # not real novelties, so they are dropped here before anything downstream sees them.
        if not publishers:
            skipped_no_publisher += 1
            continue
        pub_sig = "|".join(sorted(publisher_key(p) for p in publishers))
        # Prefer the stable ID as the dedup key; fall back to title+publisher so two distinct games
        # that happen to share a title (different publishers) don't collide into one entry.
        key = str(item_id) if item_id is not None else f"{title_key(title)}::{pub_sig}"
        if key in seen:
            continue
        seen.add(key)
        halls, booths = extract_stands(item)
        games.append({
            "id": item_id,
            "key": key,
            "title": title,
            "raw_title": html.unescape(str(item.get("TITEL") or item.get("title") or item.get("TITLE"))).strip(),
            "publishers": publishers,
            "hall": ", ".join(halls),
            "booth": ", ".join(booths),
        })
    logger.info("Fetched %d SPIEL products (with publishers) from %s (%d skipped: no publisher)",
                len(games), SPIEL_PRODUCTS_URL, skipped_no_publisher)
    return (games, None) if games else ([], "SPIEL product API returned no products with titles and publishers.")


# --- Tabletop Together -------------------------------------------------------

def add_tabletop_title(results_by_key, value, publishers=""):
    """Add/merge a Tabletop Together title and retain its publisher metadata."""
    value = normalize_title(value)
    key = title_key(value)
    if not key or len(value) > 180 or key in {"title", "name", "game", "games", "sort", "filter", "search"}:
        return
    entry = results_by_key.setdefault(key, {"title": value, "publishers": []})
    raw_publishers = re.split(r"[,;]|\u2022|\s+/\s+", str(publishers or ""))
    for publisher in raw_publishers:
        publisher = normalize_title(publisher)
        if publisher and publisher.casefold() not in {p.casefold() for p in entry["publishers"]}:
            entry["publishers"].append(publisher)


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
    """Titles already on the BGG Spiel Preview list, including publisher metadata from the CSV."""
    path, error = find_preview_csv()
    if error:
        return [], error
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
    except UnicodeDecodeError:
        # Some exported CSVs contain legacy bytes even though the rest is UTF-8.
        try:
            with open(path, encoding="latin1", newline="") as fh:
                text = fh.read()
        except OSError as exc:
            logger.warning("Could not read %s", path)
            return [], f"Could not read preview CSV {path}: {exc.strerror or exc}"
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
    header = [normalize_title(h).casefold() for h in rows[0]]
    index_ = pick_title_column(rows[0])
    publisher_index = next((i for i, h in enumerate(header) if "publisher" in h), None)
    results_by_key = {}
    for row in rows[1:]:
        if index_ < len(row):
            publisher = row[publisher_index] if publisher_index is not None and publisher_index < len(row) else ""
            add_tabletop_title(results_by_key, row[index_], publisher)
    results = list(results_by_key.values())
    logger.info(
        "Loaded %d unique preview titles from %s (title column: %r, publisher column: %r)",
        len(results), path, rows[0][index_],
        rows[0][publisher_index] if publisher_index is not None else None,
    )
    return (results, None) if results else ([], f"No titles found in {os.path.basename(path)}.")


# --- Manual BGG overrides -----------------------------------------------------

def load_overrides_file():
    path = OVERRIDES_CSV if os.path.isabs(OVERRIDES_CSV) else os.path.join(APP_DIR, OVERRIDES_CSV)
    if not os.path.exists(path):
        return {}, None  # optional file: no overrides yet is not an error
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
    except OSError as exc:
        logger.warning("Could not read %s", path)
        return {}, f"Could not read override CSV {path}: {exc.strerror or exc}"

    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        return {}, None
    header = [normalize_title(h).casefold() for h in rows[0]]
    if "spiel_title" not in header or "bgg_id" not in header:
        return {}, f"{os.path.basename(path)} needs 'spiel_title' and 'bgg_id' columns (found: {', '.join(rows[0])})."
    title_i, id_i = header.index("spiel_title"), header.index("bgg_id")
    note_i = header.index("note") if "note" in header else None

    data, dupes, bad_ids = {}, [], []
    for row in rows[1:]:
        if title_i >= len(row) or id_i >= len(row):
            continue
        title, raw_id = row[title_i].strip(), row[id_i].strip()
        if not title or not raw_id:
            continue
        match = re.search(r"(\d+)\s*$", raw_id)  # accepts a bare id or a pasted boardgamegeek.com/boardgame/<id> URL
        if not match:
            bad_ids.append(f"{title!r}: {raw_id!r}")
            continue
        key = title_key(title)
        if key in data:
            dupes.append(title)
        data[key] = {
            "id": int(match.group(1)),
            "title": title,
            "note": row[note_i].strip() if note_i is not None and note_i < len(row) else "",
        }
    if dupes:
        logger.warning("%s has more than one row for: %s (last row wins)", os.path.basename(path), ", ".join(sorted(set(dupes))))
    if bad_ids:
        logger.warning("%s has rows with no numeric bgg_id, skipped: %s", os.path.basename(path), "; ".join(bad_ids))
    logger.info("Loaded %d BGG overrides from %s", len(data), path)
    return data, None


def get_overrides():
    """Manual BGG link corrections, keyed by normalized SPIEL title. Cached like the other data sources."""
    now = time.monotonic()
    with _overrides_lock:
        if _overrides_cache["loaded_at"] and now - _overrides_cache["loaded_at"] < CACHE_TTL:
            return _overrides_cache["data"], _overrides_cache["error"]
    data, error = load_overrides_file()
    with _overrides_lock:
        _overrides_cache.update(loaded_at=now, data=data, error=error)
    return data, error


def cached_data(name, loader):
    """
    Serve (data, error) for `name` from cache if fresh. On a miss, the (slow) loader() call runs
    OUTSIDE the main cache lock -- guarded instead by a per-key refresh lock -- so a slow fetch for
    one key never blocks requests for a different key, and two concurrent requests for the same
    stale key don't both trigger the loader (the second waits, then reuses the first's result).

    If the refresh fails, keep serving the last known-good data instead of dropping the app offline.
    """
    now = time.monotonic()
    with _cache_lock:
        timestamp, data, error = _cache[name]
        if now - timestamp < CACHE_TTL:
            return data, error
    with _cache_refresh_locks[name]:
        # Re-check: another thread may have refreshed it while we waited for this lock.
        now = time.monotonic()
        with _cache_lock:
            timestamp, data, error = _cache[name]
            if now - timestamp < CACHE_TTL:
                return data, error
        data, error = loader()
        with _cache_lock:
            if error and data:
                logger.warning("Using stale cache for %s because refresh failed: %s", name, error)
                _cache[name] = (timestamp, data, error)
                return data, error
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
        with _bgg_lock:
            wait = BGG_MIN_INTERVAL - (time.monotonic() - _bgg_last_call)
        if wait > 0:
            time.sleep(wait)
        with _bgg_lock:
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


def title_variants(value):
    """Return several conservative keys for comparing titles across editions/languages."""
    normalized = normalize_title(value)
    key = title_key(normalized)
    variants = []
    for candidate in (key, re.sub(r"\s*[:\u2013\u2014-]\s*.*$", "", key), re.sub(r"\s+", " ", key.replace(" - ", " "))):
        candidate = candidate.strip()
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


def best_name_score(spiel_key, names):
    """Score against every BGG name, including alternate/translated names."""
    spiel_variants = title_variants(spiel_key)
    scores = []
    for name in names:
        if not name:
            continue
        for bgg_variant in title_variants(name):
            for spiel_variant in spiel_variants:
                scores.extend((
                    fuzz.ratio(spiel_variant, bgg_variant),
                    fuzz.token_set_ratio(spiel_variant, bgg_variant),
                    fuzz.token_sort_ratio(spiel_variant, bgg_variant),
                ))
    return max(scores, default=0)


def best_publisher_score(spiel_publishers, bgg_publishers):
    """Compare publisher names while allowing legal suffixes, subtitles and shared-name variants."""
    scores = []
    for sp in spiel_publishers:
        sk = publisher_key(sp)
        if not sk:
            continue
        for bp in bgg_publishers:
            bk = publisher_key(bp)
            if not bk:
                continue
            scores.append(max(
                fuzz.ratio(sk, bk),
                fuzz.token_set_ratio(sk, bk),
                fuzz.token_sort_ratio(sk, bk),
            ))
            # One name being a clean token-prefix/suffix of the other is common for publisher
            # subsidiaries and edition labels (e.g. "ABC" vs "ABC Games").
            if sk in bk.split() or bk in sk.split():
                scores.append(95)
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
        # XML API2 permits at most 20 thing IDs per request, so keep this safe even when
        # BGG_MAX_CANDIDATES is increased via the environment.
        for offset in range(0, len(missing), 20):
            batch = missing[offset:offset + 20]
            # versions=1 also returns publishers of localized editions (e.g. Pegasus's German edition of an English game).
            root = bgg_get("thing", {"id": ",".join(batch), "versions": 1})
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


def resolve_bgg_game(game, trace=None):
    """Resolve a SPIEL novelty to a BGG boardgame using multiple title searches and full thing data."""
    spiel_key = title_key(game["title"])
    if not spiel_key:
        return None, "SPIEL title is empty after normalizing"

    # Search several conservative variants. BGG searches names/AKAs, but one query can miss a game
    # when SPIEL uses punctuation, a subtitle, a localized title, or an edition label. The union is
    # de-duplicated before fetching full metadata.
    raw = game.get("raw_title") or game["title"]
    queries = []
    for q in (raw, normalize_title(raw), title_variants(raw)[0] if title_variants(raw) else ""):
        q = q.strip()
        if q and q.casefold() not in {x.casefold() for x in queries}:
            queries.append(q)
    found = []
    seen_ids = set()
    for query_rank, query in enumerate(queries):
        search = bgg_get("search", {"query": query, "type": "boardgame,boardgameexpansion"})
        if search is None:
            raise RuntimeError("search failed")
        for rank, item in enumerate(search.findall("item")):
            cid = item.get("id")
            if not cid or cid in seen_ids:
                continue
            name_el = item.find("name")
            primary = name_el.get("value") if name_el is not None else ""
            seen_ids.add(cid)
            found.append((query_rank, rank, cid, primary))
        if trace is not None:
            trace.append({"step": "search", "query": query, "results": [
                {"id": item.get("id"), "name": (item.find("name").get("value") if item.find("name") is not None else ""), "search_rank": rank}
                for rank, item in enumerate(search.findall("item"))
            ][:MAX_BGG_CANDIDATES]})

    if not found:
        return None, "BGG search returned no results"

    # Rank the union by the actual primary-name similarity rather than blindly trusting BGG's
    # per-query order. This keeps a good result discovered by the normalized query from being
    # pushed out by generic results from the literal query. Alternate names are checked later via
    # the full thing lookup.
    found.sort(key=lambda x: (-best_name_score(spiel_key, [x[3]]), x[0], x[1]))
    candidates = [cid for _, _, cid, _ in found[:MAX_BGG_CANDIDATES]]

    best = None
    closest_pub = None
    name_scores = []
    closest_name = None
    for cid, thing in get_bgg_things(candidates):
        name_score = best_name_score(spiel_key, thing["names"])
        pub_score = best_publisher_score(game["publishers"], thing["publishers"])
        name_scores.append((name_score, pub_score, cid, thing))
        if trace is not None:
            trace.append({"step": "candidate", "id": cid, "bgg_name": thing["primary"], "name_score": name_score,
                          "bgg_publishers": thing["publishers"], "spiel_publishers": game["publishers"], "publisher_score": pub_score})

        # Strong name + reasonable publisher evidence is enough. A very strong exact/near-exact
        # name can tolerate a weak/missing publisher because BGG publisher metadata often lags new
        # SPIEL releases or reflects a different regional edition.
        if (name_score >= NAME_THRESHOLD and pub_score >= PUBLISHER_THRESHOLD) or (
            name_score >= 96 and pub_score >= 60
        ):
            rank = name_score * 2 + pub_score
            if best is None or rank > best[0]:
                best = (rank, {"id": int(cid), "name": thing["primary"], "publisher_confirmed": pub_score >= PUBLISHER_THRESHOLD})

        if name_score >= NAME_THRESHOLD and (closest_pub is None or pub_score > closest_pub[0]):
            closest_pub = (pub_score, cid, thing)
        if closest_name is None or name_score > closest_name[0]:
            closest_name = (name_score, cid, thing)

    if best:
        result = best[1]
        reason = "matched" if result.get("publisher_confirmed", True) else "matched on strong name; publisher not fully confirmed"
        if not result.get("publisher_confirmed", True):
            # Keep this visible in the cache/UI, but don't block a useful direct BGG link.
            result["publisher_confirmed"] = False
        else:
            result.pop("publisher_confirmed", None)
        return result, reason

    # Name-only fallback remains deliberately conservative: require a near-exact name and a clear
    # lead over the runner-up, but allow it when BGG has no publisher yet.
    name_scores.sort(key=lambda t: (-t[0], -t[1]))
    if name_scores and name_scores[0][0] >= NAME_ONLY_THRESHOLD:
        top_score, top_pub, top_id, top_thing = name_scores[0]
        runner_up_score = name_scores[1][0] if len(name_scores) > 1 else 0
        if top_score - runner_up_score >= NAME_ONLY_MARGIN:
            if trace is not None:
                trace.append({"step": "name_only_fallback", "id": top_id, "bgg_name": top_thing["primary"], "name_score": top_score, "publisher_score": top_pub})
            return {"id": int(top_id), "name": top_thing["primary"], "publisher_confirmed": False}, "matched on name only (publisher not confirmed)"

    if closest_pub:
        pub_score, cid, thing = closest_pub
        bgg_pubs = ", ".join(thing["publishers"][:4]) or "none listed yet"
        return None, (f"BGG has '{thing['primary']}' (id {cid}) but the publisher didn't match "
                      f"({int(pub_score)}%, need {PUBLISHER_THRESHOLD}%): SPIEL says '{', '.join(game['publishers'])}', BGG lists {bgg_pubs}")
    if closest_name:
        top_score, cid, thing = closest_name
        return None, f"no BGG name close enough (closest: '{thing['primary']}' at {int(top_score)}%, need {NAME_THRESHOLD}%)"
    return None, "no BGG candidate passed the name check"


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
            snapshot = {"version": 3, "games": dict(_bgg_results), "things": dict(_bgg_things)}
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
            match, reason = resolve_bgg_game(game)
        except BGGAuthError as exc:
            _bgg_disabled_reason = str(exc)
            logger.error("Disabling BGG lookups: %s", exc)
            break
        except Exception:
            logger.exception("BGG lookup failed for %s", game["title"])
            continue  # not cached, so it is retried on the next worker run
        if match:
            entry = {"id": match["id"], "name": match["name"], "reason": reason}
            if match.get("publisher_confirmed") is False:
                entry["publisher_confirmed"] = False
        else:
            entry = {"id": None, "reason": reason}
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
    overrides, overrides_error = get_overrides()
    if overrides_error:
        print(f"Warning: {overrides_error}")
    load_bgg_cache()
    row_matches = compare_titles(games, tabletop, overrides)
    not_on_list = delta_games(games, row_matches)  # before removing overridden games, for the summary line below
    delta = delta_games(games, row_matches, overrides)
    pending = [g for g in delta if not cache_entry_fresh(_bgg_results.get(g["key"]), g)]
    calls = 3 * len(pending)
    print(f"{len(games)} SPIEL games, {len(not_on_list)} not on the Tabletop Together list "
          f"({len(not_on_list) - len(delta)} of those already covered by bgg_overrides.csv), "
          f"{len(pending)} need an API lookup (~{calls * BGG_MIN_INTERVAL / 60:.0f} min at worst).")
    _bgg_worker(pending)
    direct = sum(1 for g in delta if (_bgg_results.get(g["key"]) or {}).get("id"))
    print(f"Done: {direct}/{len(delta)} direct matches. Cache written to {BGG_CACHE_FILE}")
    return 1 if _bgg_disabled_reason else 0


# --- Comparison ---------------------------------------------------------------

_match_lock = threading.Lock()
_match_cache = {"sig": None, "rows": None}


def _match_cache_signature(spiel_titles, tabletop_titles):
    """Content-based signature including publisher data, so publisher changes invalidate matches."""
    spiel_sig = tuple(
        (g.get("key"), g["title"], tuple(sorted(publisher_key(p) for p in g.get("publishers", []))))
        for g in spiel_titles
    )
    tabletop_sig = tuple(
        (t["title"], tuple(sorted(publisher_key(p) for p in t.get("publishers", []))))
        for t in tabletop_titles
    )
    return hash((spiel_sig, tabletop_sig))


def _publisher_match_score(spiel_publishers, tabletop_publishers):
    """Return the best publisher similarity, or 0 when either side has no publisher."""
    if not spiel_publishers or not tabletop_publishers:
        return 0
    return best_publisher_score(spiel_publishers, tabletop_publishers)


def _publisher_compatible(spiel_publishers, tabletop_publishers):
    """Require strong publisher evidence before accepting a fuzzy title match."""
    if not spiel_publishers or not tabletop_publishers:
        return False
    return _publisher_match_score(spiel_publishers, tabletop_publishers) >= PUBLISHER_THRESHOLD


def fuzzy_matches(spiel_titles, tabletop_titles):
    """
    Match SPIEL titles to the Tabletop Together CSV using BOTH title and publisher.

    Exact title matches are accepted only when the publisher agrees (or one side has no
    publisher). Fuzzy matches require a strong publisher match. This prevents generic
    title similarities such as "CATAN - The Card Game" -> "Alhambra: Card Game" from
    becoming false positives.
    """
    sig = _match_cache_signature(spiel_titles, tabletop_titles)
    with _match_lock:
        if _match_cache["sig"] == sig:
            return _match_cache["rows"]

    started = time.monotonic()
    display = [normalize_title(item["title"]) for item in tabletop_titles]
    keys = [title_key(item["title"]) for item in tabletop_titles]

    # Exact title can have multiple CSV records; retain all publisher variants.
    exact = {}
    for i, key in enumerate(keys):
        if key:
            exact.setdefault(key, []).append(i)

    rows = []
    for spiel in spiel_titles:
        original = normalize_title(spiel["title"])
        key = title_key(original)
        spiel_publishers = spiel.get("publishers", [])

        best = None  # (title_score, publisher_score, index)
        exact_candidates = exact.get(key, [])
        for i in exact_candidates:
            tabletop_publishers = tabletop_titles[i].get("publishers", [])
            pub_score = _publisher_match_score(spiel_publishers, tabletop_publishers)
            publisher_ok = (
                not spiel_publishers or not tabletop_publishers or
                pub_score >= PUBLISHER_THRESHOLD
            )
            if publisher_ok:
                candidate = (100, pub_score, i)
                if best is None or candidate > best:
                    best = candidate

        if best is None and key and keys:
            # First find title candidates, then reject candidates whose publisher conflicts.
            candidate_indexes = set()
            for scorer in (rfuzz.token_set_ratio, rfuzz.token_sort_ratio, rfuzz.ratio):
                for _, score, idx in rprocess.extract(key, keys, scorer=scorer, limit=12):
                    if score >= 70:
                        candidate_indexes.add(idx)

            for i in candidate_indexes:
                title_score = max(
                    rfuzz.token_set_ratio(key, keys[i]),
                    rfuzz.token_sort_ratio(key, keys[i]),
                    rfuzz.ratio(key, keys[i]),
                )
                tabletop_publishers = tabletop_titles[i].get("publishers", [])
                pub_score = _publisher_match_score(spiel_publishers, tabletop_publishers)

                # Fuzzy title matches are only eligible when both sources identify a
                # compatible publisher. Missing publisher metadata is deliberately not
                # enough for a fuzzy match.
                if not _publisher_compatible(spiel_publishers, tabletop_publishers):
                    continue
                if title_score < 75:
                    continue

                candidate = (title_score, pub_score, i)
                if best is None or candidate > best:
                    best = candidate

        if best is None:
            rows.append((original, "not found", None, 0))
            continue

        title_score, pub_score, best_index = best
        match = display[best_index]

        # Keep the existing status semantics, but only after publisher validation.
        if title_score >= 90:
            status = "match"
        elif title_score >= 75:
            status = "possible match"
        else:
            status = "not found"

        rows.append((
            original,
            status,
            match if status != "not found" else None,
            int(round(title_score)),
        ))

    with _match_lock:
        _match_cache["sig"], _match_cache["rows"] = sig, rows
    logger.info(
        "Publisher-aware matched %d SPIEL titles against %d preview titles in %.1fs",
        len(spiel_titles), len(tabletop_titles), time.monotonic() - started
    )
    return rows


def compare_titles(spiel_titles, tabletop_titles, overrides=None):
    overrides = overrides or {}
    results = []
    for spiel, (original, status, match, score) in zip(spiel_titles, fuzzy_matches(spiel_titles, tabletop_titles)):
        override = overrides.get(title_key(original))
        bgg_reason = ""
        if override:
            bgg_url, bgg_kind = bgg_game_url(override["id"]), "override"
            bgg_reason = override.get("note", "")
        else:
            with _bgg_lock:
                bgg = _bgg_results.get(spiel.get("key"))
            if bgg and bgg.get("id"):
                bgg_url, bgg_kind = bgg_game_url(bgg["id"]), "direct"
                if bgg.get("publisher_confirmed") is False:
                    bgg_reason = "matched on name only; publisher not confirmed"
            else:
                bgg_url, bgg_kind = bgg_search_url(original), "search"
                if not BGG_API_TOKEN:
                    bgg_reason = "BGG lookups are off (BGG_API_TOKEN not set)"
                elif bgg:
                    bgg_reason = bgg.get("reason", "")
                elif status in BGG_LOOKUP_STATUSES:
                    bgg_reason = "not looked up yet"

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
            "bgg_reason": bgg_reason,
        })
    return results


def delta_games(spiel, matches, overrides=None):
    """
    SPIEL games missing from the Tabletop Together list AND not already covered by a manual override.
    compare_titles returns one row per game, in order. Overridden games are excluded so they never cost an API call.
    """
    overrides = overrides or {}
    return [g for g, m in zip(spiel, matches)
            if m["status"] in BGG_LOOKUP_STATUSES and title_key(g["title"]) not in overrides]


@app.route("/bgg-why")
def bgg_why():
    """
    Explains a BGG lookup for one SPIEL title, live and uncached: /bgg-why?title=Deae+Via&key=...
    Disabled unless BGG_DEBUG_KEY is set (it spends BGG API calls, so it must not be public).
    """
    debug_key = os.getenv("BGG_DEBUG_KEY", "")
    if not debug_key or request.args.get("key") != debug_key:
        return "Not found", 404
    if not BGG_API_TOKEN:
        return jsonify(error="BGG_API_TOKEN is not set on this server, so no BGG lookups can happen."), 400
    wanted = title_key(request.args.get("title", ""))
    spiel, error = cached_data("spiel", get_spiel_novelties)
    if error or not wanted:
        return jsonify(error=error or "pass ?title=..."), 400
    game = next((g for g in spiel if title_key(g["title"]) == wanted), None) or next((g for g in spiel if wanted in title_key(g["title"])), None)
    if not game:
        return jsonify(error=f"No SPIEL novelty with a title like '{request.args.get('title')}'"), 404
    overrides, _ = get_overrides()
    override = overrides.get(title_key(game["title"]))
    if override:
        return jsonify(spiel_game={k: game[k] for k in ("id", "title", "raw_title", "publishers", "hall", "booth")},
                       result={"id": override["id"]}, reason=f"served from bgg_overrides.csv: {override.get('note') or 'no note'}",
                       cached_entry=None, trace=[])
    trace = []
    try:
        match, reason = resolve_bgg_game(game, trace)
    except BGGAuthError as exc:
        return jsonify(error=str(exc)), 502
    except Exception as exc:
        return jsonify(error=f"BGG lookup failed: {exc}"), 502
    with _bgg_lock:
        cached = _bgg_results.get(game["key"])
    return jsonify(spiel_game={k: game[k] for k in ("id", "title", "raw_title", "publishers", "hall", "booth")},
                   thresholds={"name": NAME_THRESHOLD, "publisher": PUBLISHER_THRESHOLD, "name_only": NAME_ONLY_THRESHOLD, "name_only_margin": NAME_ONLY_MARGIN},
                   result=match, reason=reason, cached_entry=cached, trace=trace)


@app.route("/health", methods=["GET", "HEAD"])
def health():
    return jsonify(status="ok"), 200


@app.route("/", methods=["GET", "HEAD"])
def index():
    if request.method == "HEAD":
        return "", 200
    spiel, spiel_error = cached_data("spiel", get_spiel_novelties)
    tabletop, tabletop_error = cached_data("tabletop", get_preview_games)
    overrides, overrides_error = get_overrides()
    if not _bgg_cache_loaded:
        load_bgg_cache()
    matches = compare_titles(spiel, tabletop, overrides) if spiel and tabletop else []
    # If either source failed, matches is empty and NO BGG calls are made (never fall back to looking up everything).
    delta = delta_games(spiel, matches, overrides) if matches else []
    ensure_bgg_worker(delta)
    # Only novelties that are NOT already on the CSV are listed. Add ?all=1 to the URL to see every row (for debugging).
    show_all = request.args.get("all") == "1"
    rows = matches if show_all else [m for m in matches if m["status"] in BGG_LOOKUP_STATUSES]

    with _bgg_lock:
        bgg_resolved = sum(1 for g in delta if cache_entry_fresh(_bgg_results.get(g["key"]), g))
        bgg_direct = sum(1 for g in delta if (_bgg_results.get(g["key"]) or {}).get("id"))
    bgg_pending = bool(BGG_API_TOKEN) and not _bgg_disabled_reason and bgg_resolved < len(delta)

    template = """
<html><head><title>SPIEL novelties not on the Tabletop Together list</title>
{% if bgg_pending %}<meta http-equiv="refresh" content="60">{% endif %}
<style>
body{font-family:Arial;margin:20px}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccc;padding:8px}th{background:#f2f2f2}.warning{color:#8a3b00;background:#fff3e0;padding:10px;border:1px solid #f0d7a0}.muted{color:#555}.badge{display:inline-block;padding:2px 6px;border-radius:10px;font-size:12px;font-weight:bold}.badge.search{background:#eef5ff;color:#2455a0}.badge.direct{background:#eafaf1;color:#1f7a47}.badge.override{background:#fff4cc;color:#7a5a00}.small{font-size:12px}.status{font-weight:bold}.status.match{color:#18672d}.status.possible{color:#8a3b00}.status.notfound{color:#8a1c1c}.clickable{cursor:pointer}
</style></head><body><h1>SPIEL novelties not on the Tabletop Together list</h1>
{% if spiel_error %}<div class="warning">SPIEL data unavailable: {{ spiel_error }}</div>{% endif %}
{% if tabletop_error %}<div class="warning">Preview list (CSV) unavailable: {{ tabletop_error }}</div>{% endif %}
{% if overrides_error %}<div class="warning">BGG override CSV problem: {{ overrides_error }}</div>{% endif %}
{% if bgg_disabled_reason %}<div class="warning">BGG direct links disabled: {{ bgg_disabled_reason }}. Showing search links.</div>{% endif %}
<p>SPIEL novelties: {{ spiel_count }} | Tabletop Together CSV titles: {{ tabletop_count }}{% if matches %} | Not on the CSV (shown below): {{ delta_count }}{% endif %}{% if overrides_count %} | Manual overrides loaded: {{ overrides_count }}{% endif %}{% if bgg_token %} | BGG lookups: {{ bgg_resolved }}/{{ delta_count }} ({{ bgg_direct }} direct){% if bgg_pending %} &ndash; still working, page refreshes automatically{% endif %}
{% else %} | BGG direct links off (set BGG_API_TOKEN to enable){% endif %}</p>
{% if matches and not rows %}<p>Every SPIEL novelty is already on the Tabletop Together list.</p>{% endif %}
{% if rows %}<p><small>Click a column heading to sort.</small></p>
<table id="results"><thead><tr><th class="sortable">Title</th><th class="sortable">Publisher</th><th class="sortable">Hall</th><th class="sortable">Booth</th><th class="sortable">BGG</th></tr></thead><tbody>
{% for r in rows %}
<tr>
  <td>{{ r.spiel_title }}</td>
  <td>{{ r.publisher or "-" }}</td>
  <td>{{ r.hall or "-" }}</td>
  <td>{{ r.booth or "-" }}</td>
  <td>
    {% if r.bgg_kind == "override" %}<span class="badge override">override</span>{% elif r.bgg_kind == "direct" %}<span class="badge direct">direct</span>{% else %}<span class="badge search">search</span>{% endif %}
    <a href="{{ r.bgg_url }}">{{ r.bgg_reason or "BGG link" }}</a>
  </td>
</tr>
{% endfor %}
</tbody></table>
<script>
(function () {
  var table = document.getElementById("results");
  if (!table) return;
  var tbody = table.tBodies[0];
  var heads = table.tHead.rows[0].cells;
  var collator = new Intl.Collator(undefined, {numeric: true, sensitivity: "base"});
  function cellValue(row, col) {
    var cell = row.cells[col];
    var v = cell.getAttribute("data-sort");
    return (v !== null ? v : cell.textContent).trim();
  }
  function sortBy(col, dir) {
    var rows = Array.prototype.slice.call(tbody.rows);
    var numeric = false;
    rows.sort(function (a, b) {
      var x = cellValue(a, col), y = cellValue(b, col);
      var ex = (x === "" || x === "-"), ey = (y === "" || y === "-");
      if (ex !== ey) return ex ? 1 : -1;  // empty cells always last
      if (numeric) return dir * ((parseFloat(x) || 0) - (parseFloat(y) || 0));
      return dir * collator.compare(x, y);
    });
    rows.forEach(function (r) { tbody.appendChild(r); });
    for (var i = 0; i < heads.length; i++) heads[i].setAttribute("data-dir", i === col ? (dir > 0 ? "asc" : "desc") : "");
    try { sessionStorage.setItem("spieldelta-sort", JSON.stringify({col: col, dir: dir})); } catch (e) {}
  }
  Array.prototype.forEach.call(heads, function (th, i) {
    th.addEventListener("click", function () {
      sortBy(i, th.getAttribute("data-dir") === "asc" ? -1 : 1);
    });
  });
  try {  // keep the chosen sort across the automatic refresh while BGG lookups are running
    var saved = JSON.parse(sessionStorage.getItem("spieldelta-sort") || "null");
    if (saved && saved.col < heads.length) sortBy(saved.col, saved.dir);
  } catch (e) {}
})();
</script>{% endif %}
</body></html>"""
    return render_template_string(
        template, matches=matches, rows=rows, show_all=show_all, spiel_count=len(spiel), tabletop_count=len(tabletop),
        spiel_error=spiel_error, tabletop_error=tabletop_error, overrides_error=overrides_error, overrides_count=len(overrides),
        bgg_token=bool(BGG_API_TOKEN), delta_count=len(delta), bgg_pending=bgg_pending, bgg_resolved=bgg_resolved,
        bgg_direct=bgg_direct, bgg_disabled_reason=_bgg_disabled_reason,
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "build-bgg-cache":
        sys.exit(build_bgg_cache())
    app.run(host="0.0.0.0", port=5000)

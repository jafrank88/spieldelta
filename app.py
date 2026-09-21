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


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REQUEST_TIMEOUT = 20
CACHE_TTL = int(os.getenv("DATA_CACHE_TTL", "300"))

USER_AGENT = (
    "Mozilla/5.0 (compatible; spieldelta/1.11; "
    "+https://github.com/jafrank88/spieldelta)"
)

SPIEL_PRODUCTS_URL = os.getenv(
    "SPIEL_PRODUCTS_URL",
    "https://maps.eyeled-services.de/en/spiel26/products"
    "?columns=%5B%22ID%22%2C%22INFO%22%2C%22S_ORDER%22%2C%22TITEL%22%2C"
    "%22FIRMA_ID%22%2C%22UNTERTITEL%22%2C%22BILDER%22%2C"
    "%22BILDER_VERSIONEN%22%2C%22BILDER_TEXTE%22%5D",
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))

PREVIEW_CSV = os.getenv(
    "PREVIEW_CSV",
    "TabletopTogetherTool.csv",
).strip()


# ---------------------------------------------------------------------------
# BGG configuration
# ---------------------------------------------------------------------------

# BGG XML API2 requires an approved application and Authorization bearer
# token. Without a token we do not scrape BGG pages; we use only the
# configured/manual ID map and ordinary BGG search links.
BGG_API_TOKEN = os.getenv("BGG_API_TOKEN", "").strip()
BGG_API_BASE = "https://boardgamegeek.com/xmlapi2"

# BGG asks clients to keep requests slow enough to avoid throttling.
BGG_MIN_INTERVAL = float(os.getenv("BGG_MIN_INTERVAL", "5.0"))

# Negative results are retried after this period because new SPIEL games
# can appear on BGG shortly before the show.
BGG_NEGATIVE_TTL = (
    float(os.getenv("BGG_NEGATIVE_TTL_DAYS", "3")) * 86400
)

# Cached BGG "thing" records expire after this period.
BGG_THING_TTL = (
    float(os.getenv("BGG_THING_TTL_DAYS", "7")) * 86400
)

# Resolver version. Bumping this invalidates old cached misses/matches
# without requiring users to manually delete bgg_cache.json.
BGG_RESOLVER_VERSION = int(
    os.getenv("BGG_RESOLVER_VERSION", "3")
)

# Cache file.
BGG_CACHE_FILE = os.getenv(
    "BGG_CACHE_FILE",
    os.path.join(APP_DIR, "bgg_cache.json"),
)

# Only these SPIEL/Tabletop statuses require BGG resolution.
BGG_LOOKUP_STATUSES = {
    x.strip()
    for x in os.getenv(
        "BGG_LOOKUP_STATUSES",
        "not found,possible match",
    ).split(",")
    if x.strip()
}

# Name matching.
NAME_THRESHOLD = int(
    os.getenv("BGG_NAME_THRESHOLD", "90")
)

NAME_ONLY_THRESHOLD = int(
    os.getenv("BGG_NAME_ONLY_THRESHOLD", "97")
)

NAME_ONLY_MARGIN = int(
    os.getenv("BGG_NAME_ONLY_MARGIN", "3")
)

# Publisher matching is confirmation, not an absolute requirement for an
# obvious title match.
PUBLISHER_THRESHOLD = int(
    os.getenv("BGG_PUBLISHER_THRESHOLD", "85")
)

# BGG search candidates. The old code used 12, which made the resolver
# dependent on BGG's ranking.
MAX_BGG_CANDIDATES = int(
    os.getenv("BGG_MAX_CANDIDATES", "50")
)

# Optional manual overrides.
#
# Format:
#
#   BGG_ID_MAP='{"Deae Via":475105,"Fearsome Floors":7805}'
#
# Or use a JSON file:
#
#   BGG_ID_MAP_FILE=bgg_id_map.json
#
# This is useful for known exceptions and does not require an API call.
BGG_ID_MAP_FILE = os.getenv(
    "BGG_ID_MAP_FILE",
    os.path.join(APP_DIR, "bgg_id_map.json"),
)

BGG_ID_MAP_RAW = os.getenv("BGG_ID_MAP", "").strip()


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()

_cache = {
    "spiel": (0.0, [], None),
    "tabletop": (0.0, [], None),
}

_cache_refresh_locks = {
    "spiel": threading.Lock(),
    "tabletop": threading.Lock(),
}


_bgg_lock = threading.Lock()

# SPIEL game key -> cached result
_bgg_results = {}

# BGG ID -> cached "thing" metadata
_bgg_things = {}

_bgg_cache_loaded = False
_bgg_thread = None
_bgg_disabled_reason = None

_bgg_last_call = 0.0


_manual_bgg_map = None


_match_lock = threading.Lock()

_match_cache = {
    "sig": None,
    "rows": None,
}


# ---------------------------------------------------------------------------
# Generic normalization
# ---------------------------------------------------------------------------

GENERIC_PUBLISHER_WORDS = {
    "games",
    "game",
    "spiele",
    "spiel",
    "verlag",
    "gmbh",
    "co",
    "kg",
    "ltd",
    "llc",
    "inc",
    "sl",
    "srl",
    "studio",
    "studios",
    "publishing",
    "publications",
    "editions",
    "edition",
    "entertainment",
    "international",
}


def normalize_title(value):
    if value is None:
        return ""

    value = html.unescape(str(value))

    value = unicodedata.normalize(
        "NFKD",
        value,
    ).encode(
        "ascii",
        "ignore",
    ).decode(
        "ascii",
    )

    value = re.sub(
        r"\[[^]]*\]|\([^)]*\)",
        " ",
        value,
    )

    return re.sub(
        r"\s+",
        " ",
        value,
    ).strip(
        " -|\u00a0"
    )


def title_key(value):
    value = normalize_title(value)

    value = (
        value.casefold()
        .replace("&", " and ")
    )

    value = re.sub(
        r"[^a-z0-9]+",
        " ",
        value,
    )

    return " ".join(
        word
        for word in value.split()
        if word not in {"a", "an", "the"}
    )


def publisher_key(value):
    words = title_key(value).split()

    kept = [
        word
        for word in words
        if word not in GENERIC_PUBLISHER_WORDS
    ]

    return " ".join(kept or words)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def fetch(url, accept):
    try:
        response = requests.get(
            url,
            headers={
                "Accept": accept,
                "User-Agent": USER_AGENT,
            },
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()

        return response.text, None

    except requests.RequestException:
        logger.exception(
            "Request failed for %s",
            url,
        )

        return (
            None,
            f"Could not load data from {url}.",
        )


# ---------------------------------------------------------------------------
# Generic recursive record extraction
# ---------------------------------------------------------------------------

def find_records(
    value,
    title_fields=(
        "TITEL",
        "title",
        "TITLE",
        "name",
        "NAME",
        "game",
        "GAME",
    ),
):
    records = []

    if isinstance(value, dict):

        if any(
            value.get(field)
            for field in title_fields
        ):
            records.append(value)

        for child in value.values():
            records.extend(
                find_records(
                    child,
                    title_fields,
                )
            )

    elif isinstance(value, list):

        for child in value:
            records.extend(
                find_records(
                    child,
                    title_fields,
                )
            )

    return records


# ---------------------------------------------------------------------------
# SPIEL novelties
# ---------------------------------------------------------------------------

def extract_publishers(item):
    publishers = []

    info = item.get("INFO")

    if info:
        soup = BeautifulSoup(
            info,
            "html.parser",
        )

        for row in soup.find_all("tr"):
            cells = row.find_all("td")

            if (
                len(cells) >= 2
                and cells[0]
                .get_text(strip=True)
                .rstrip(":")
                .casefold()
                in {"publisher", "publishers"}
            ):
                text = cells[1].get_text(
                    " ",
                    strip=True,
                )

                publishers.extend(
                    p.strip()
                    for p in re.split(
                        r",|;| / ",
                        text,
                    )
                    if p.strip()
                )

                break

    if (
        not publishers
        and item.get("UNTERTITEL")
    ):
        publishers.append(
            str(
                item["UNTERTITEL"]
            ).strip()
        )

    return publishers


def extract_stands(item):
    halls = []
    booths = []

    for stand in item.get("STAENDE") or []:

        if not isinstance(
            stand,
            dict,
        ):
            continue

        hall = re.sub(
            r"(?i)^hall\s*",
            "",
            str(
                stand.get("HALLE")
                or ""
            ),
        ).strip()

        booth = str(
            stand.get("NAME")
            or ""
        ).strip()

        if hall and hall not in halls:
            halls.append(hall)

        if booth and booth not in booths:
            booths.append(booth)

    return halls, booths


def get_spiel_novelties():
    raw, error = fetch(
        SPIEL_PRODUCTS_URL,
        "application/json",
    )

    if error:
        return [], error

    try:
        data = json.loads(raw)

    except ValueError:
        logger.exception(
            "SPIEL response was not JSON"
        )

        return (
            [],
            "SPIEL product API returned invalid JSON.",
        )

    games = []
    seen = set()
    skipped_no_publisher = 0

    for item in find_records(
        data,
        (
            "TITEL",
            "title",
            "TITLE",
        ),
    ):

        item_id = (
            item.get("ID")
            or item.get("id")
        )

        title = normalize_title(
            item.get("TITEL")
            or item.get("title")
            or item.get("TITLE")
        )

        if not title:
            continue

        publishers = extract_publishers(item)

        if not publishers:
            skipped_no_publisher += 1
            continue

        pub_sig = "|".join(
            sorted(
                publisher_key(p)
                for p in publishers
            )
        )

        key = (
            str(item_id)
            if item_id is not None
            else (
                f"{title_key(title)}"
                f"::{pub_sig}"
            )
        )

        if key in seen:
            continue

        seen.add(key)

        halls, booths = extract_stands(item)

        games.append(
            {
                "id": item_id,
                "key": key,
                "title": title,
                "raw_title": html.unescape(
                    str(
                        item.get("TITEL")
                        or item.get("title")
                        or item.get("TITLE")
                    )
                ).strip(),
                "publishers": publishers,
                "hall": ", ".join(halls),
                "booth": ", ".join(booths),
            }
        )

    logger.info(
        "Fetched %d SPIEL products "
        "(%d skipped: no publisher)",
        len(games),
        skipped_no_publisher,
    )

    if not games:
        return (
            [],
            "SPIEL product API returned no products "
            "with titles and publishers.",
        )

    return games, None


# ---------------------------------------------------------------------------
# Tabletop Together
# ---------------------------------------------------------------------------

def add_tabletop_title(
    results,
    seen,
    value,
):
    value = normalize_title(value)
    key = title_key(value)

    if (
        not key
        or len(value) > 180
        or key in seen
        or key in {
            "title",
            "name",
            "game",
            "games",
            "sort",
            "filter",
            "search",
        }
    ):
        return

    seen.add(key)

    results.append(
        {
            "title": value,
        }
    )


def find_preview_csv():
    if PREVIEW_CSV:

        path = (
            PREVIEW_CSV
            if os.path.isabs(PREVIEW_CSV)
            else os.path.join(
                APP_DIR,
                PREVIEW_CSV,
            )
        )

        return path, None

    found = sorted(
        f
        for f in os.listdir(APP_DIR)
        if f.lower().endswith(".csv")
    )

    if len(found) == 1:
        return (
            os.path.join(
                APP_DIR,
                found[0],
            ),
            None,
        )

    if not found:
        return (
            None,
            f"No CSV found in {APP_DIR}. "
            "Commit it there or set PREVIEW_CSV "
            "to its path.",
        )

    return (
        None,
        f"Several CSV files found "
        f"({', '.join(found)}); "
        "set PREVIEW_CSV to choose one.",
    )


def pick_title_column(header):
    lowered = [
        normalize_title(h).casefold()
        for h in header
    ]

    for word in (
        "title",
        "name",
        "game",
    ):

        for i, h in enumerate(lowered):

            if word in h:
                return i

    return 0


def get_preview_games():
    path, error = find_preview_csv()

    if error:
        return [], error

    try:
        with open(
            path,
            encoding="utf-8-sig",
            newline="",
        ) as fh:
            text = fh.read()

    except OSError as exc:
        logger.warning(
            "Could not read %s",
            path,
        )

        return (
            [],
            f"Could not read preview CSV "
            f"{path}: "
            f"{exc.strerror or exc}",
        )

    try:
        dialect = csv.Sniffer().sniff(
            text[:4096],
            delimiters=",;\t|",
        )

    except csv.Error:
        dialect = csv.excel

    rows = [
        r
        for r in csv.reader(
            io.StringIO(text),
            dialect,
        )
        if any(c.strip() for c in r)
    ]

    if len(rows) < 2:
        return (
            [],
            f"Preview CSV "
            f"{os.path.basename(path)} "
            "has no data rows.",
        )

    index_ = pick_title_column(
        rows[0]
    )

    results = []
    seen = set()

    for row in rows[1:]:

        if index_ < len(row):
            add_tabletop_title(
                results,
                seen,
                row[index_],
            )

    logger.info(
        "Loaded %d preview games from %s",
        len(results),
        path,
    )

    if not results:
        return (
            [],
            f"No titles found in "
            f"{os.path.basename(path)}.",
        )

    return results, None


# ---------------------------------------------------------------------------
# General cache
# ---------------------------------------------------------------------------

def cached_data(name, loader):
    now = time.monotonic()

    with _cache_lock:

        timestamp, data, error = _cache[name]

        if now - timestamp < CACHE_TTL:
            return data, error

    with _cache_refresh_locks[name]:

        now = time.monotonic()

        with _cache_lock:

            timestamp, data, error = _cache[name]

            if now - timestamp < CACHE_TTL:
                return data, error

        data, error = loader()

        with _cache_lock:
            _cache[name] = (
                time.monotonic(),
                data,
                error,
            )

        return data, error


# ---------------------------------------------------------------------------
# BGG URLs
# ---------------------------------------------------------------------------

def bgg_search_url(title):
    return (
        "https://boardgamegeek.com/"
        "geeksearch.php?action=search"
        "&objecttype=boardgame"
        f"&q={quote_plus(title)}"
    )


def bgg_game_url(bgg_id):
    return (
        f"https://boardgamegeek.com/"
        f"boardgame/{bgg_id}"
    )


# ---------------------------------------------------------------------------
# Manual BGG ID map
# ---------------------------------------------------------------------------

def load_manual_bgg_map():
    """
    Load optional title -> BGG ID overrides.

    Supported:

      BGG_ID_MAP='{"Deae Via":475105,"Fearsome Floors":7805}'

    or:

      bgg_id_map.json

    The keys are normalized with title_key().
    """

    global _manual_bgg_map

    if _manual_bgg_map is not None:
        return _manual_bgg_map

    mapping = {}

    if BGG_ID_MAP_RAW:

        try:
            data = json.loads(
                BGG_ID_MAP_RAW
            )

            if isinstance(data, dict):
                mapping.update(data)

        except ValueError:
            logger.warning(
                "BGG_ID_MAP is not valid JSON"
            )

    try:

        with open(
            BGG_ID_MAP_FILE,
            encoding="utf-8",
        ) as fh:
            data = json.load(fh)

        if isinstance(data, dict):
            mapping.update(data)

    except (
        OSError,
        ValueError,
    ):
        pass

    normalized = {}

    for title, bgg_id in mapping.items():

        key = title_key(title)

        if not key:
            continue

        try:
            bgg_id = int(bgg_id)

        except (
            TypeError,
            ValueError,
        ):
            logger.warning(
                "Ignoring invalid BGG ID "
                "for %r: %r",
                title,
                bgg_id,
            )
            continue

        normalized[key] = {
            "id": bgg_id,
            "name": normalize_title(title),
        }

    _manual_bgg_map = normalized

    logger.info(
        "Loaded %d manual BGG ID overrides",
        len(normalized),
    )

    return normalized


def manual_bgg_match(game):
    mapping = load_manual_bgg_map()

    key = title_key(
        game.get("title", "")
    )

    if key in mapping:
        item = mapping[key]

        return (
            {
                "id": int(item["id"]),
                "name": item["name"],
            },
            "manual BGG ID override",
        )

    return None, None


# ---------------------------------------------------------------------------
# BGG API
# ---------------------------------------------------------------------------

class BGGAuthError(Exception):
    pass


def bgg_get(
    endpoint,
    params,
    attempts=4,
):
    """
    Rate-limited, authenticated GET against
    BGG XML API2.

    Returns an ElementTree root or None.
    """

    global _bgg_last_call

    if not BGG_API_TOKEN:
        raise BGGAuthError(
            "BGG_API_TOKEN is not configured"
        )

    headers = {
        "Authorization": (
            f"Bearer {BGG_API_TOKEN}"
        ),
        "User-Agent": USER_AGENT,
    }

    for attempt in range(
        1,
        attempts + 1,
    ):

        with _bgg_lock:
            wait = (
                BGG_MIN_INTERVAL
                - (
                    time.monotonic()
                    - _bgg_last_call
                )
            )

        if wait > 0:
            time.sleep(wait)

        with _bgg_lock:
            _bgg_last_call = (
                time.monotonic()
            )

        try:

            response = requests.get(
                f"{BGG_API_BASE}/{endpoint}",
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )

        except requests.RequestException:

            logger.warning(
                "BGG request failed "
                "(attempt %d)",
                attempt,
            )

            time.sleep(
                2 * attempt
            )

            continue

        if response.status_code in (
            401,
            403,
        ):
            raise BGGAuthError(
                "BGG returned "
                f"{response.status_code}; "
                "check BGG_API_TOKEN"
            )

        if response.status_code in (
            202,
            429,
            500,
            502,
            503,
        ):

            logger.warning(
                "BGG %s returned %s; "
                "retrying",
                endpoint,
                response.status_code,
            )

            time.sleep(
                3 * attempt
            )

            continue

        if response.status_code != 200:

            logger.warning(
                "BGG %s returned %s",
                endpoint,
                response.status_code,
            )

            return None

        try:
            return ET.fromstring(
                response.content
            )

        except ET.ParseError:

            logger.warning(
                "BGG %s returned "
                "unparseable XML",
                endpoint,
            )

            return None

    return None


# ---------------------------------------------------------------------------
# BGG matching
# ---------------------------------------------------------------------------

def best_name_score(
    spiel_key,
    names,
):
    scores = []

    for name in names:

        if not name:
            continue

        bgg_key = title_key(name)

        if not bgg_key:
            continue

        scores.append(
            max(
                fuzz.ratio(
                    spiel_key,
                    bgg_key,
                ),
                fuzz.token_set_ratio(
                    spiel_key,
                    bgg_key,
                ),
                fuzz.token_sort_ratio(
                    spiel_key,
                    bgg_key,
                ),
            )
        )

    return max(
        scores,
        default=0,
    )


def best_publisher_score(
    spiel_publishers,
    bgg_publishers,
):
    scores = []

    for sp in spiel_publishers:

        sk = publisher_key(sp)

        if not sk:
            continue

        for bp in bgg_publishers:

            bk = publisher_key(bp)

            if not bk:
                continue

            scores.append(
                max(
                    fuzz.ratio(
                        sk,
                        bk,
                    ),
                    fuzz.token_set_ratio(
                        sk,
                        bk,
                    ),
                )
            )

    return max(
        scores,
        default=0,
    )


def game_signature(game):
    pubs = "|".join(
        sorted(
            publisher_key(p)
            for p in game["publishers"]
        )
    )

    return (
        f"{title_key(game['title'])}"
        f"|{pubs}"
    )


def cache_entry_fresh(
    entry,
    game,
):
    """
    Cached successful matches remain valid until the
    resolver version or game signature changes.

    Negative results expire.
    """

    if not entry:
        return False

    if entry.get("resolver_version") != BGG_RESOLVER_VERSION:
        return False

    if entry.get("sig") != game_signature(game):
        return False

    if entry.get("id"):
        return True

    return (
        time.time()
        - entry.get("checked", 0)
        < BGG_NEGATIVE_TTL
    )


# ---------------------------------------------------------------------------
# BGG thing lookup
# ---------------------------------------------------------------------------

def get_bgg_things(ids):
    """
    Retrieve names + publishers for BGG IDs.

    BGG permits multiple thing IDs in one request,
    so this keeps the number of API calls down.
    """

    ids = [
        str(i)
        for i in dict.fromkeys(ids)
        if i
    ]

    now = time.time()

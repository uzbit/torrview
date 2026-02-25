import json
import logging
import re
import time
import html
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# In-memory cache with TTL
# ---------------------------------------------------------------------------
_cache = {}
CACHE_TTL = 600  # 10 minutes
BATCH_SIZE = 10


def cache_get(key):
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < CACHE_TTL:
        log.info("Cache HIT: %s", key)
        return entry["value"]
    log.info("Cache MISS: %s", key)
    return None


def cache_set(key, value):
    _cache[key] = {"value": value, "ts": time.time()}


# ---------------------------------------------------------------------------
# TPB data fetcher
# ---------------------------------------------------------------------------
TPB_TOP100_URL = "https://apibay.org/precompiled/data_top100_201.json"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def fetch_torrents():
    cached = cache_get("tpb_torrents")
    if cached is not None:
        log.info("Returning %d cached torrents", len(cached))
        return cached

    log.info("Fetching top 100 movie torrents from %s", TPB_TOP100_URL)
    try:
        resp = requests.get(TPB_TOP100_URL, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        torrents = resp.json()
    except Exception:
        log.exception("Failed to fetch torrents")
        return []

    if not isinstance(torrents, list):
        log.warning("Unexpected response type: %s", type(torrents).__name__)
        return []

    # Filter out any "no results" sentinel
    torrents = [t for t in torrents if t.get("name") != "No results returned"]
    log.info("Fetched %d torrents", len(torrents))

    for t in torrents:
        t["size_human"] = human_size(int(t.get("size", 0)))
        t["date_human"] = format_timestamp(int(t.get("added", 0)))
        t["magnet"] = (
            f"magnet:?xt=urn:btih:{t.get('info_hash', '')}"
            f"&dn={t.get('name', '')}"
        )

    cache_set("tpb_torrents", torrents)
    return torrents


def human_size(nbytes):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def format_timestamp(ts):
    if ts == 0:
        return "N/A"
    try:
        return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        return "N/A"


# ---------------------------------------------------------------------------
# Movie title parser
# ---------------------------------------------------------------------------
STRIP_RE = re.compile(
    r"[\.\s\-\_\(]+"
    r"(1080[pi]|720p|480p|2160p|4[Kk]|UHD|"
    r"BRRip|BDRip|BluRay|Blu[\-\s]?Ray|WEBRip|WEB[\-\s]?DL|WEBDL|HDRip|DVDRip|"
    r"HDTV|CAM|TS|HDCAM|HC|"
    r"x264|x265|h\.?264|h\.?265|HEVC|AVC|"
    r"AAC|AC3|DTS|DD5|ATMOS|"
    r"YIFY|YTS|RARBG|EVO|FGT|SPARKS|"
    r"EXTENDED|REMASTERED|UNRATED|DIRECTORS[\.\s]?CUT)"
    r".*",
    re.IGNORECASE,
)

YEAR_RE = re.compile(r"[\(\.\s]((?:19|20)\d{2})[\)\.\s]")


def parse_title(torrent_name):
    name = torrent_name.replace(".", " ").replace("_", " ")
    # Strip from the first tag onward
    name = STRIP_RE.sub("", name)
    # Remove any trailing year in parentheses for cleaner search
    year_match = YEAR_RE.search(torrent_name)
    year = year_match.group(1) if year_match else None
    name = re.sub(r"\s*\(?\d{4}\)?\s*$", "", name).strip()
    log.info("Parsed title: %r -> %r (year=%s)", torrent_name, name, year)
    return name, year


# ---------------------------------------------------------------------------
# IMDB scraper
# ---------------------------------------------------------------------------
def fetch_imdb(imdb_id, title):
    if imdb_id and imdb_id != "0" and imdb_id.startswith("tt"):
        cache_key = f"imdb:{imdb_id}"
    else:
        cache_key = f"imdb:title:{title}"

    cached = cache_get(cache_key)
    if cached is not None:
        log.info("IMDB cache hit for %s", cache_key)
        return cached

    info = {"poster": "", "summary": "", "genre": "", "rating": ""}

    try:
        if imdb_id and imdb_id != "0" and imdb_id.startswith("tt"):
            url = f"https://www.imdb.com/title/{imdb_id}/"
            log.info("Fetching IMDB page by ID: %s", url)
        else:
            # Fallback: search by title
            search_url = f"https://www.imdb.com/find?q={requests.utils.quote(title)}&s=tt"
            log.info("Searching IMDB by title: %s", search_url)
            resp = requests.get(search_url, headers=HEADERS, timeout=10)
            soup = BeautifulSoup(resp.text, "html.parser")
            link = soup.select_one("a[href*='/title/tt']")
            if not link:
                log.info("No IMDB result found for title %r", title)
                cache_set(cache_key, info)
                return info
            href = link["href"].split("?")[0]
            url = f"https://www.imdb.com{href}"
            log.info("Following IMDB link: %s", url)

        resp = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Poster
        poster_tag = soup.select_one('img[class*="ipc-image"]')
        if poster_tag and poster_tag.get("src"):
            info["poster"] = poster_tag["src"]

        # Summary — try JSON-LD first
        ld_tag = soup.select_one('script[type="application/ld+json"]')
        if ld_tag:
            try:
                ld = json.loads(ld_tag.string)
                info["summary"] = ld.get("description", "")[:300]
                info["genre"] = (
                    ", ".join(ld["genre"])
                    if isinstance(ld.get("genre"), list)
                    else str(ld.get("genre", ""))
                )
                agg = ld.get("aggregateRating", {})
                if agg.get("ratingValue"):
                    info["rating"] = f"{agg['ratingValue']}/10"
            except Exception:
                pass

        # Fallback summary from meta tag
        if not info["summary"]:
            meta = soup.select_one('meta[name="description"]')
            if meta and meta.get("content"):
                info["summary"] = meta["content"][:300]

    except Exception:
        log.exception("Error fetching IMDB data for %s", cache_key)

    log.info("IMDB data for %r: rating=%s genre=%s poster=%s",
             title, info["rating"] or "N/A", info["genre"] or "N/A",
             "yes" if info["poster"] else "no")
    cache_set(cache_key, info)
    return info


# ---------------------------------------------------------------------------
# Rotten Tomatoes scraper
# ---------------------------------------------------------------------------
def fetch_rt(title):
    cache_key = f"rt:{title}"
    cached = cache_get(cache_key)
    if cached is not None:
        log.info("RT cache hit for %r", title)
        return cached

    info = {"tomatometer": "", "audience": "", "consensus": ""}

    try:
        search_url = f"https://www.rottentomatoes.com/search?search={requests.utils.quote(title)}"
        log.info("Searching RT: %s", search_url)
        resp = requests.get(search_url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Find first movie result link
        link = soup.select_one(
            'search-page-media-row[data-qa="data-row"] a[href*="/m/"]'
        )
        if not link:
            # Try alternative selector
            link = soup.select_one('a[href*="/m/"]')
        if not link:
            log.info("No RT result found for %r", title)
            cache_set(cache_key, info)
            return info

        movie_url = link["href"]
        if not movie_url.startswith("http"):
            movie_url = f"https://www.rottentomatoes.com{movie_url}"

        log.info("Following RT movie page: %s", movie_url)
        resp = requests.get(movie_url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Tomatometer score
        score_tag = soup.select_one('rt-button[slot="criticsScore"]')
        if score_tag:
            info["tomatometer"] = score_tag.get_text(strip=True)

        # Audience score
        aud_tag = soup.select_one('rt-button[slot="audienceScore"]')
        if aud_tag:
            info["audience"] = aud_tag.get_text(strip=True)

        # Try alternate selectors if above didn't work
        if not info["tomatometer"]:
            tm = soup.select_one('[data-qa="tomatometer"]')
            if tm:
                info["tomatometer"] = tm.get_text(strip=True)

        if not info["audience"]:
            au = soup.select_one('[data-qa="audience-score"]')
            if au:
                info["audience"] = au.get_text(strip=True)

        # Critics consensus
        consensus_tag = soup.select_one('[data-qa="critics-consensus"]')
        if consensus_tag:
            info["consensus"] = consensus_tag.get_text(strip=True)[:200]

    except Exception:
        log.exception("Error fetching RT data for %r", title)

    log.info("RT data for %r: tomatometer=%s audience=%s",
             title, info["tomatometer"] or "N/A", info["audience"] or "N/A")
    cache_set(cache_key, info)
    return info


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TorrView - Movie Torrents</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #1a1a2e; color: #e0e0e0;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    padding: 20px;
  }
  h1 {
    text-align: center; color: #e94560; margin-bottom: 20px;
    font-size: 2em; letter-spacing: 2px;
  }
  .subtitle {
    text-align: center; color: #888; margin-bottom: 30px; font-size: 0.9em;
  }
  .table-wrap { overflow-x: auto; }
  table {
    width: 100%; border-collapse: collapse;
    background: #16213e; border-radius: 8px; overflow: hidden;
  }
  th {
    background: #0f3460; color: #e94560; padding: 12px 10px;
    text-align: left; font-size: 0.85em; text-transform: uppercase;
    letter-spacing: 1px; position: sticky; top: 0; z-index: 1;
  }
  th[data-sort] { cursor: pointer; user-select: none; }
  th[data-sort]:hover { background: #1a4a80; }
  th .arrow { font-size: 0.7em; margin-left: 4px; }
  td {
    padding: 10px; border-bottom: 1px solid #1a1a3e;
    vertical-align: top; font-size: 0.85em;
  }
  tr:hover { background: #1a1a4e; }
  .poster { width: 60px; height: 90px; object-fit: cover; border-radius: 4px; }
  .no-poster {
    width: 60px; height: 90px; background: #2a2a4e; border-radius: 4px;
    display: flex; align-items: center; justify-content: center;
    color: #555; font-size: 0.7em; text-align: center;
  }
  a { color: #e94560; text-decoration: none; }
  a:hover { text-decoration: underline; color: #ff6b81; }
  .title-cell { max-width: 250px; }
  .title-cell .movie-title { font-weight: bold; font-size: 1em; }
  .title-cell .torrent-name { color: #888; font-size: 0.75em; margin-top: 4px; word-break: break-word; }
  .summary-cell { max-width: 300px; font-size: 0.8em; color: #bbb; }
  .genre-cell { color: #a0a0d0; font-size: 0.8em; }
  .score-cell { text-align: center; }
  .score-cell .tm { color: #fa320a; font-weight: bold; }
  .score-cell .aud { color: #6cc; font-weight: bold; }
  .score-cell .lbl { font-size: 0.7em; color: #888; }
  .seed { color: #4ecdc4; font-weight: bold; }
  .leech { color: #e94560; font-weight: bold; }
  .rating { color: #f9ca24; font-weight: bold; }
  #loading {
    text-align: center; padding: 40px; color: #888; font-size: 1.1em;
  }
  #loading .spinner {
    display: inline-block; width: 20px; height: 20px;
    border: 3px solid #333; border-top-color: #e94560;
    border-radius: 50%; animation: spin 0.8s linear infinite;
    vertical-align: middle; margin-right: 10px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<h1>TorrView</h1>
<p class="subtitle">Movie torrents enriched with IMDB &amp; Rotten Tomatoes data</p>
<div class="table-wrap">
<table>
<thead>
<tr>
  <th>Poster</th>
  <th data-sort="title">Title<span class="arrow"></span></th>
  <th>Genre</th>
  <th>Summary</th>
  <th data-sort="rating">Scores<span class="arrow"></span></th>
  <th data-sort="size">Size<span class="arrow"></span></th>
  <th data-sort="seeders">S<span class="arrow"></span></th>
  <th data-sort="leechers">L<span class="arrow"></span></th>
  <th data-sort="date">Date<span class="arrow"></span></th>
  <th>User</th>
</tr>
</thead>
<tbody id="tbody">
</tbody>
</table>
</div>
<div id="loading"><span class="spinner"></span> Loading torrents...</div>
<script>
(function() {
  var tbody = document.getElementById("tbody");
  var loading = document.getElementById("loading");

  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function buildRow(r) {
    var posterHtml = r.poster_url
      ? '<img class="poster" src="' + esc(r.poster_url) + '" alt="poster" loading="lazy">'
      : '<div class="no-poster">No<br>Poster</div>';

    var scores = [];
    if (r.rating) scores.push('<div><span class="rating">' + esc(r.rating) + '</span> <span class="lbl">IMDB</span></div>');
    if (r.tomatometer) scores.push('<div><span class="tm">' + esc(r.tomatometer) + '</span> <span class="lbl">Critics</span></div>');
    if (r.audience) scores.push('<div><span class="aud">' + esc(r.audience) + '</span> <span class="lbl">Audience</span></div>');

    var tr = document.createElement("tr");
    tr.setAttribute("data-sort-title", r.clean_title.toLowerCase());
    tr.setAttribute("data-sort-size", r.size_bytes);
    tr.setAttribute("data-sort-seeders", r.seeders_int);
    tr.setAttribute("data-sort-leechers", r.leechers_int);
    tr.setAttribute("data-sort-date", r.date_ts);
    tr.setAttribute("data-sort-rating", r.rating_float);

    tr.innerHTML =
      "<td>" + posterHtml + "</td>" +
      '<td><div class="title-cell">' +
        '<div class="movie-title"><a href="' + esc(r.magnet) + '">' + esc(r.clean_title) + '</a></div>' +
        '<div class="torrent-name">' + esc(r.torrent_name) + '</div>' +
      '</div></td>' +
      '<td><span class="genre-cell">' + esc(r.genre) + '</span></td>' +
      '<td><div class="summary-cell">' + esc(r.summary) + '</div></td>' +
      '<td><div class="score-cell">' + (scores.length ? scores.join("") : "\u2014") + '</div></td>' +
      "<td>" + esc(r.size_human) + "</td>" +
      '<td><span class="seed">' + esc(r.seeders) + '</span></td>' +
      '<td><span class="leech">' + esc(r.leechers) + '</span></td>' +
      "<td>" + esc(r.date_human) + "</td>" +
      "<td>" + esc(r.username) + "</td>";

    tbody.appendChild(tr);
  }

  var source = new EventSource("/api/rows");

  source.onmessage = function(e) {
    var rows = JSON.parse(e.data);
    rows.forEach(function(r) { buildRow(r); });
  };

  source.addEventListener("done", function() {
    source.close();
    loading.style.display = "none";
  });

  source.onerror = function() {
    source.close();
    loading.textContent = "Failed to load torrents.";
  };

  /* Column sorting */
  document.querySelectorAll("th[data-sort]").forEach(function(th) {
    th.addEventListener("click", function() {
      var key = th.getAttribute("data-sort");
      var asc = th.getAttribute("data-dir") !== "asc";
      var isNumeric = key !== "title";

      /* Reset all arrows */
      document.querySelectorAll("th[data-sort] .arrow").forEach(function(a) { a.textContent = ""; });
      document.querySelectorAll("th[data-sort]").forEach(function(h) { h.removeAttribute("data-dir"); });

      th.setAttribute("data-dir", asc ? "asc" : "desc");
      th.querySelector(".arrow").textContent = asc ? " \u25B2" : " \u25BC";

      var rows = Array.from(tbody.querySelectorAll("tr"));
      rows.sort(function(a, b) {
        var av = a.getAttribute("data-sort-" + key);
        var bv = b.getAttribute("data-sort-" + key);
        if (isNumeric) {
          av = parseFloat(av) || 0;
          bv = parseFloat(bv) || 0;
        }
        if (av < bv) return asc ? -1 : 1;
        if (av > bv) return asc ? 1 : -1;
        return 0;
      });
      rows.forEach(function(r) { tbody.appendChild(r); });
    });
  });
})();
</script>
</body>
</html>"""


def build_row(torrent, imdb_info, rt_info, clean_title):
    summary = imdb_info["summary"]
    if len(summary) > 200:
        summary = summary[:200] + "..."

    rating_str = imdb_info["rating"]
    rating_float = 0.0
    if rating_str:
        try:
            rating_float = float(rating_str.split("/")[0])
        except (ValueError, IndexError):
            pass

    return {
        "poster_url": imdb_info["poster"],
        "clean_title": clean_title,
        "torrent_name": torrent.get("name", ""),
        "magnet": torrent.get("magnet", ""),
        "genre": imdb_info["genre"],
        "summary": summary,
        "rating": rating_str,
        "rating_float": rating_float,
        "tomatometer": rt_info["tomatometer"],
        "audience": rt_info["audience"],
        "size_human": torrent["size_human"],
        "size_bytes": int(torrent.get("size", 0)),
        "seeders": str(torrent.get("seeders", "0")),
        "seeders_int": int(torrent.get("seeders", 0)),
        "leechers": str(torrent.get("leechers", "0")),
        "leechers_int": int(torrent.get("leechers", 0)),
        "date_human": torrent["date_human"],
        "date_ts": int(torrent.get("added", 0)),
        "username": torrent.get("username", ""),
    }


@app.route("/")
def index():
    log.info("--- Request received: GET / ---")
    return HTML_TEMPLATE


def process_torrent(t):
    clean_title, year = parse_title(t.get("name", ""))
    if not clean_title:
        clean_title = t.get("name", "Unknown")

    imdb_id = t.get("imdb", "")
    if imdb_id and not imdb_id.startswith("tt"):
        imdb_id = f"tt{imdb_id}"

    imdb_info = fetch_imdb(imdb_id, clean_title)
    rt_info = fetch_rt(clean_title)
    return build_row(t, imdb_info, rt_info, clean_title)


@app.route("/api/rows")
def api_rows():
    log.info("--- SSE stream started: /api/rows ---")

    def generate():
        torrents = fetch_torrents()
        log.info("Streaming %d torrents in batches of %d", len(torrents), BATCH_SIZE)

        for batch_start in range(0, len(torrents), BATCH_SIZE):
            batch = torrents[batch_start:batch_start + BATCH_SIZE]

            with ThreadPoolExecutor(max_workers=BATCH_SIZE) as pool:
                rows = list(pool.map(process_torrent, batch))

            batch_num = batch_start // BATCH_SIZE + 1
            log.info("Sending batch %d (%d rows)", batch_num, len(rows))
            yield f"data: {json.dumps(rows)}\n\n"

            # Rate-limit: 1 second between batches to avoid hammering IMDB/RT
            if batch_start + BATCH_SIZE < len(torrents):
                time.sleep(1)

        yield "event: done\ndata: {}\n\n"
        log.info("--- SSE stream complete ---")

    return Response(generate(), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)

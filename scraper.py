"""
animegg.org Scraper — GitHub Actions Edition
=============================================
Phase 1 → animegg_url_list.json          (committed once, at end of phase 1)
Phase 2 → animegg_with_alternate_titles.json  (committed every 50 entries)

Both files are committed and pushed directly to the repo that triggered
this Actions run. No secrets needed beyond the default GITHUB_TOKEN.

Resume support: re-running the workflow skips already-done entries in
both phases, so it's safe to re-trigger after a timeout.
"""

import json
import os
import re
import subprocess
import time
import requests
from bs4 import BeautifulSoup

# ─── Configuration ────────────────────────────────────────────────────────────

BASE_URL        = "https://www.animegg.org"
SERIES_LIST_URL = f"{BASE_URL}/popular-series"

FILE_PHASE1 = "animegg_url_list.json"
FILE_PHASE2 = "animegg_with_alternate_titles.json"

CHUNK_SIZE      = 1000   # series per list-page request
LIST_DELAY      = 1.5    # seconds between list-page requests
DETAIL_DELAY    = 0.5    # seconds between detail-page requests
COMMIT_EVERY    = 50     # commit phase-2 file every N new entries
MAX_SERIES      = None   # None = all; set int to cap for testing

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
}

# ─── Git helpers ──────────────────────────────────────────────────────────────

def git(*args: str) -> str:
    """Run a git command, return stdout, print stderr on failure."""
    result = subprocess.run(
        ["git", *args],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  [git {' '.join(args)}] stderr: {result.stderr.strip()}")
    return result.stdout.strip()


def git_commit_and_push(files: list[str], message: str) -> None:
    """Stage given files, commit if there are changes, then push."""
    git("add", *files)
    # Check if there's anything staged
    status = git("diff", "--cached", "--name-only")
    if not status:
        print(f"  [git] Nothing new to commit.")
        return
    git("commit", "-m", message)
    # Pull with rebase first to avoid diverged-branch push failures
    git("pull", "--rebase", "origin", git("rev-parse", "--abbrev-ref", "HEAD"))
    result = subprocess.run(
        ["git", "push"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print(f"  [git] Pushed: {message}")
    else:
        print(f"  [git] Push failed: {result.stderr.strip()}")

# ─── JSON helpers ─────────────────────────────────────────────────────────────

def save_json(data: list[dict], path: str) -> None:
    """Atomic write — tmp file then rename so no partial writes."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_json(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

# ─── Phase 1 — List scraper ───────────────────────────────────────────────────

def parse_series_page(html: str) -> list[dict]:
    soup    = BeautifulSoup(html, "html.parser")
    results = []
    for li in soup.select("li.fea"):
        try:
            title_link = li.select_one("div.rightpop > a")
            if not title_link:
                continue
            title = title_link.get_text(strip=True)
            if not title or title == "Watch Anime":
                continue

            href = title_link.get("href", "")
            uri  = href.strip("/").removeprefix("series/")

            tag_uls       = li.select("ul.tags")
            episode_count = None
            status        = None
            genres        = []

            if tag_uls:
                for a in tag_uls[0].select("a"):
                    t = a.get_text(strip=True)
                    if "Episode" in t:
                        try:
                            episode_count = int(t.split()[0])
                        except ValueError:
                            episode_count = t
                    elif t in ("Ongoing", "Completed"):
                        status = t

            if len(tag_uls) >= 2:
                genres = [
                    a.get_text(strip=True)
                    for a in tag_uls[1].select("a")
                    if a.get_text(strip=True)
                ]

            desc_div    = li.select_one("div.desfea")
            description = desc_div.get_text(strip=True) if desc_div else ""

            results.append({
                "uri":           uri,
                "title":         title,
                "url":           f"{BASE_URL}/series/{uri}",
                "episode_count": episode_count,
                "status":        status,
                "genres":        genres,
                "description":   description,
            })
        except Exception:
            continue
    return results


def phase1(session: requests.Session) -> list[dict]:
    # Resume: if file already exists and is complete, skip entirely
    existing = load_json(FILE_PHASE1)
    if existing:
        print(f"[Phase 1] Already complete — {len(existing)} series in {FILE_PHASE1}, skipping.")
        return existing

    print(f"[Phase 1] Scraping {SERIES_LIST_URL}  (chunk={CHUNK_SIZE})")
    print("─" * 65)

    output:    list[dict] = []
    seen_uris: set[str]   = set()
    start = 0
    page  = 1

    while True:
        limit = CHUNK_SIZE
        if MAX_SERIES is not None:
            remaining = MAX_SERIES - len(seen_uris)
            if remaining <= 0:
                break
            limit = min(CHUNK_SIZE, remaining)

        print(f"  Page {page} | start={start} limit={limit} ...", end=" ", flush=True)

        try:
            params = {
                "sortBy": "hits", "sortDirection": "DESC",
                "limit": limit, "start": start,
            }
            resp = session.get(
                SERIES_LIST_URL, params=params,
                headers=BROWSER_HEADERS, timeout=30,
            )
            resp.raise_for_status()
            chunk = parse_series_page(resp.text)
        except Exception as e:
            print(f"Error: {e}")
            break

        if not chunk:
            print("No results — done.")
            break

        new_items = [s for s in chunk if s["uri"] not in seen_uris]
        print(f"got {len(chunk)}, {len(new_items)} new | total: {len(output) + len(new_items)}")

        for s in new_items:
            seen_uris.add(s["uri"])
            output.append({
                "serial_no":     len(output) + 1,
                "title":         s["title"],
                "url":           s["url"],
                "episode_count": s["episode_count"],
                "status":        s["status"],
                "genres":        s["genres"],
                "description":   s["description"],
            })

        if len(chunk) < limit:
            print("  Reached end of results.")
            break

        start += len(chunk)
        page  += 1
        time.sleep(LIST_DELAY)

    # Save + commit phase 1
    save_json(output, FILE_PHASE1)
    git_commit_and_push(
        [FILE_PHASE1],
        f"scraper: phase 1 complete — {len(output)} series [skip ci]",
    )
    print(f"\n[Phase 1] Done — {len(output)} series → {FILE_PHASE1}\n")
    return output

# ─── Phase 2 — Alternate title fetcher ───────────────────────────────────────

_ALT_LABELS = re.compile(
    r"^(other\s*names?|alternate\s*titles?|also\s*known\s*as|synonyms?|"
    r"english\s*title|japanese\s*title)\s*:?\s*",
    re.IGNORECASE,
)
_JUNK_PREFIX = re.compile(r"^[s\s:,;|/\\–—\-]+", re.IGNORECASE)


def _clean(text: str) -> str:
    text = _ALT_LABELS.sub("", text).strip()
    text = _JUNK_PREFIX.sub("", text).strip()
    return re.sub(r"\s{2,}", " ", text)


def extract_alternate_title(soup: BeautifulSoup) -> str:
    for sel in [
        "p.other-name", "span.other-name", "div.other-name",
        "p.otherName",  "span.otherName",
        ".anime-alt-title", ".alt-title", ".alternate-title", ".synonyms",
    ]:
        el = soup.select_one(sel)
        if el:
            val = _clean(el.get_text(separator=", ", strip=True))
            if val:
                return val

    LABEL_KWDS = [
        "other name", "other names", "alternate title", "alternate titles",
        "also known as", "synonyms", "english title", "japanese title",
    ]

    for td in soup.select("td, th"):
        label = td.get_text(strip=True).lower().rstrip(":").strip()
        if any(label == kw or label.startswith(kw) for kw in LABEL_KWDS):
            sib = td.find_next_sibling("td")
            if sib:
                val = _clean(sib.get_text(separator=", ", strip=True))
                if val:
                    return val

    for dt in soup.select("dt"):
        label = dt.get_text(strip=True).lower().rstrip(":").strip()
        if any(label == kw or label.startswith(kw) for kw in LABEL_KWDS):
            dd = dt.find_next_sibling("dd")
            if dd:
                val = _clean(dd.get_text(separator=", ", strip=True))
                if val:
                    return val

    for label_tag in soup.select(
        "li span, li b, li strong, p span, p b, p strong, div b, div strong"
    ):
        label = label_tag.get_text(strip=True).lower().rstrip(":").strip()
        if any(label == kw or label.startswith(kw) for kw in LABEL_KWDS):
            parent_text = label_tag.parent.get_text(separator=" ", strip=True)
            val = _clean(parent_text[len(label_tag.get_text(strip=True)):])
            if val:
                return val

    for el in soup.select("p, li, div.info, div.anime-info, div.detail, div.desc"):
        if el.select("p, li"):
            continue
        text = el.get_text(strip=True)
        for kw in LABEL_KWDS:
            if text.lower().startswith(kw):
                val = _clean(text)
                if val:
                    return val

    return ""


def phase2(session: requests.Session, phase1_data: list[dict]) -> None:
    # Load existing progress
    existing   = load_json(FILE_PHASE2)
    done_urls  = {r["url"] for r in existing}
    output     = list(existing)

    todo = [r for r in phase1_data if r["url"] not in done_urls]
    total = len(phase1_data)

    if existing:
        print(f"[Phase 2] Resuming — {len(existing)}/{total} done, {len(todo)} remaining.")
    else:
        print(f"[Phase 2] Fetching alternate titles for {total} series.")
    print("─" * 65)

    since_last_commit = 0   # count new entries since last git commit

    for s in todo:
        try:
            resp = session.get(s["url"], headers=BROWSER_HEADERS, timeout=20)
            resp.raise_for_status()
            alt = extract_alternate_title(BeautifulSoup(resp.text, "html.parser"))
        except Exception:
            alt = ""

        output.append({
            "serial_no":       s["serial_no"],
            "title":           s["title"],
            "alternate_title": alt,
            "url":             s["url"],
            "episode_count":   s["episode_count"],
            "status":          s["status"],
            "genres":          s["genres"],
            "description":     s["description"],
        })
        output.sort(key=lambda x: x["serial_no"])
        save_json(output, FILE_PHASE2)

        since_last_commit += 1
        done_count = len(output)

        alt_display = f" → {alt[:40]}" if alt else ""
        print(
            f"  [{done_count:>5}/{total}] {'✓' if alt else '·'} "
            f"{s['title'][:48]}{alt_display}",
            flush=True,
        )

        # Commit every COMMIT_EVERY new entries
        if since_last_commit >= COMMIT_EVERY:
            git_commit_and_push(
                [FILE_PHASE2],
                f"scraper: alternate titles {done_count}/{total} [skip ci]",
            )
            since_last_commit = 0

        time.sleep(DETAIL_DELAY)

    # Final commit for any remainder
    if since_last_commit > 0:
        git_commit_and_push(
            [FILE_PHASE2],
            f"scraper: phase 2 complete — {len(output)} series [skip ci]",
        )

    print(f"\n[Phase 2] Done — {len(output)} series → {FILE_PHASE2}")
    print(f"  File size: {os.path.getsize(FILE_PHASE2):,} bytes")

# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    session = requests.Session()
    try:
        session.get(BASE_URL + "/", headers=BROWSER_HEADERS, timeout=15)
        time.sleep(LIST_DELAY)
    except Exception:
        pass

    p1_data = phase1(session)
    phase2(session, p1_data)


if __name__ == "__main__":
    main()

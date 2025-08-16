# fb_contact_enricher.py
"""
Facebook contact info enricher for AutoDeal agent lists.

Key features
------------
- INPUT: .xlsx/.xls or .csv (CSV encodings auto-tried: utf-8-sig, utf-8, cp1252, latin-1).
- OUTPUT: .xlsx or .csv (inferred from the output filename extension).
- For each row (needs "Agent Name" and "Dealership"):
    * Searches the web (DuckDuckGo) constrained to site:facebook.com.
    * Opens the best candidate Facebook page with Playwright.
    * Extracts public contact info:
        - Mobile (+63)  -> ONLY mobile numbers, normalized to +63XXXXXXXXXX
        - Landline      -> All other phone-like numbers (kept as-is; may include hyphens)
        - Email ID      -> First public email if found
    * If nothing is found: writes "NA".
- LOG: Writes a <output>_log.csv with query, candidate URL, and a note per row.

Requirements
------------
- Python 3.9+
- pip install playwright pandas openpyxl
- playwright install

Usage examples
--------------
# XLSX in → XLSX out
python fb_contact_enricher.py -i autodeal_agents1.xlsx -o autodeal_agents1_enriched.xlsx --headful

# CSV in (unknown encoding) → CSV out, force encoding if needed
python fb_contact_enricher.py -i agents.csv -o agents_enriched.csv --encoding cp1252 --headful

# Ambiguous names: use manual picking of the FB result
python fb_contact_enricher.py -i autodeal_agents1.xlsx -o out.xlsx --headful --manual
"""

import argparse
import asyncio
import csv
import random
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional

# --- Playwright ---
from playwright.async_api import async_playwright, Page

# --- Optional heavy deps only for XLSX read/write ---
# We import pandas lazily in functions so CSV-only users don't need it installed.


# ----------------------------
# Patterns & small utilities
# ----------------------------
# Very permissive digit collector; we classify later.
PHONE_GUESS_RE = re.compile(r"(?:\+?\d[\d\-\s\(\)]{6,}\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

DUCKDUCKGO = "https://duckduckgo.com/?q={}"

def sane(s: Any) -> str:
    return (str(s) if s is not None else "").strip()

def build_query(agent: str, dealer: str) -> str:
    # Constrain to FB; add Philippines to improve precision
    return f"\"{agent}\" \"{dealer}\" site:facebook.com Philippines"

def rank_facebook_results(url: str, title: str) -> int:
    u = url.lower()
    t = (title or "").lower()
    score = 0
    if "facebook.com" in u:
        score += 10
    if any(k in u for k in ["/pages/", "/pg/", "/people/", "/profile.php", "/public/", "/watch/", "/groups/"]):
        score += 2
    # down-rank transient content links
    if "story.php" in u or "/photo" in u or "/photos/" in u or "/reel/" in u:
        score -= 3
    # title cues for sales context
    if any(k in t for k in ["sales", "agent", "ford", "toyota", "mitsubishi", "nissan", "suzuki", "kia", "isuzu", "geely", "mg"]):
        score += 1
    return score


# ----------------------------
# Search & extraction helpers
# ----------------------------
async def duckduckgo_links(page: Page, query: str, max_links: int = 5) -> List[Tuple[str, str]]:
    await page.goto(DUCKDUCKGO.format(query.replace(" ", "+")), wait_until="domcontentloaded")
    sel = "a.result__a, a[data-testid='result-title-a']"
    try:
        await page.wait_for_selector(sel, timeout=6000)
    except:
        return []
    anchors = await page.query_selector_all(sel)
    links: List[Tuple[str, str]] = []
    for a in anchors[: max_links * 2]:
        href = await a.get_attribute("href")
        title = await a.inner_text() if a else ""
        if href:
            links.append((href, title))
        if len(links) >= max_links * 2:
            break
    # Keep only FB, ranked
    ranked = sorted(links, key=lambda x: rank_facebook_results(x[0], x[1]), reverse=True)
    ranked = [r for r in ranked if "facebook.com" in r[0].lower()]
    return ranked[:max_links]


def normalize_mobile_ph(num: str) -> Optional[str]:
    """
    Normalize Philippine mobile numbers to +63XXXXXXXXXX.
    Heuristics:
      - Accept +63, 63, or local 09 prefixes; require 10 digits after country code for mobile.
      - Classify as mobile only if it starts with +63 9xx...
    """
    if not num:
        return None
    # keep only digits and +
    raw = re.sub(r"[^\d+]", "", num)
    if not raw:
        return None
    # Already +63...
    if raw.startswith("+63"):
        rest = re.sub(r"^\+63", "", raw)
        if len(rest) >= 10 and rest[0:1] == "9":
            return "+63" + rest[:10]
        return None
    # 63...
    if raw.startswith("63"):
        rest = raw[2:]
        if len(rest) >= 10 and rest[0:1] == "9":
            return "+63" + rest[:10]
        return None
    # Local 09...
    if raw.startswith("09") and len(raw) >= 11:
        rest = raw[1:11]  # drop leading 0, keep next 10
        if rest[0:1] == "9":
            return "+63" + rest
        return None
    return None


def split_phones(text: str) -> Tuple[List[str], List[str]]:
    """
    Extract phones from text and split into (mobiles_normalized, landlines_rawlike).
    """
    if not text:
        return [], []
    mobiles: List[str] = []
    landlines: List[str] = []
    seen_m = set()
    seen_l = set()

    for m in PHONE_GUESS_RE.finditer(text):
        frag = m.group(0)
        mob = normalize_mobile_ph(frag)
        if mob:
            if mob not in seen_m:
                mobiles.append(mob)
                seen_m.add(mob)
        else:
            # treat as landline-like; keep original formatting fragment
            frag_clean = frag.strip()
            if frag_clean and frag_clean not in seen_l:
                landlines.append(frag_clean)
                seen_l.add(frag_clean)
    return mobiles, landlines


async def try_close_fb_dialogs(page: Page):
    # Attempt to close cookie/login/region modals; failures are non-fatal.
    selectors = [
        "div[aria-label='Close']",
        "div[aria-label='Close dialog']",
        "button[title='Close']",
        "button[aria-label='Close']",
        "div[role='dialog'] div[aria-label='Close']",
        "span:has-text('Not now')",
        "button:has-text('Allow all')",
        "button:has-text('Only allow essential cookies')",
    ]
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.click()
                await page.wait_for_timeout(400)
        except:
            pass


async def extract_contacts_from_fb(page: Page, url: str) -> Tuple[List[str], List[str], List[str], str]:
    """
    Visit a Facebook page and try to extract mobiles, landlines, emails.
    Returns: (mobiles, landlines, emails, note)
    """
    mobiles: List[str] = []
    landlines: List[str] = []
    emails: List[str] = []
    note = ""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        return [], [], [], f"nav-failed:{type(e).__name__}"

    await try_close_fb_dialogs(page)

    # Try to click an 'About/Info' link if present (non-fatal)
    try:
        about = await page.query_selector("a:has-text('About'), a:has-text('Info'), a[role='link']:has-text('About')")
        if about:
            await about.click()
            await page.wait_for_timeout(1200)
    except:
        pass

    # Pull the DOM content and collapse tags to scan text
    try:
        html = await page.content()
        # strip tags to text-ish
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        # emails
        emails = list(dict.fromkeys(EMAIL_RE.findall(text)))
        # phones
        mobiles, landlines = split_phones(text)
        if not mobiles and not landlines and not emails:
            note = "no-contacts-found"
    except Exception as e:
        note = f"content-error:{type(e).__name__}"

    return mobiles, landlines, emails, note


# ----------------------------
# I/O helpers
# ----------------------------
def read_input_rows(in_path_str: str, encoding_hint: str = "auto") -> List[Dict[str, Any]]:
    p = Path(in_path_str)
    if not p.exists():
        raise FileNotFoundError(f"Input not found: {p}")

    if p.suffix.lower() in {".xlsx", ".xls"}:
        try:
            import pandas as pd  # lazy import
        except ImportError:
            raise RuntimeError("Reading .xlsx requires pandas. Install with: pip install pandas openpyxl")
        df = pd.read_excel(p)
        return df.to_dict(orient="records")

    # CSV path
    encodings = ["utf-8-sig", "utf-8", "cp1252", "latin-1"] if encoding_hint == "auto" else [encoding_hint]
    last_err = None
    for enc in encodings:
        try:
            with p.open("r", newline="", encoding=enc) as f:
                reader = csv.DictReader(f)
                return list(reader)
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Could not read CSV (tried {encodings}). Last error: {last_err}")


def write_output(rows: List[Dict[str, Any]], out_path_str: str):
    out_p = Path(out_path_str)
    if out_p.suffix.lower() in {".xlsx", ".xls"}:
        try:
            import pandas as pd  # lazy import
        except ImportError:
            raise RuntimeError("Writing .xlsx requires pandas. Install with: pip install pandas openpyxl")
        pd.DataFrame(rows).to_excel(out_p, index=False)
        return
    # CSV fallback (UTF-8)
    if rows:
        with out_p.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)


def write_log(log_rows: List[Dict[str, Any]], base_out_path: str):
    base = Path(base_out_path)
    log_p = base.with_name(base.stem + "_log.csv")
    if log_rows:
        with log_p.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["row","agent","dealership","query","candidate_url","note","timestamp"])
            writer.writeheader()
            writer.writerows(log_rows)


# ----------------------------
# Row processing
# ----------------------------
async def process_row(context, row: Dict[str, Any], manual: bool = False, max_candidates: int = 5) -> Dict[str, Any]:
    agent = sane(row.get("Agent Name", ""))
    dealer = sane(row.get("Dealership", ""))

    out = dict(row)  # start with original fields
    out.setdefault("Mobile (+63)", "NA")
    out.setdefault("Landline", "NA")
    out.setdefault("Email ID", "NA")

    # Skip if no basic identifiers
    if not agent and not dealer:
        out["_note"] = "missing-agent-dealer"
        return out

    query = build_query(agent, dealer)
    candidate_url = ""
    note = ""

    page = await context.new_page()
    try:
        results = await duckduckgo_links(page, query, max_links=max_candidates)
        if not results:
            # fallback less strict query
            results = await duckduckgo_links(page, f"{agent} {dealer} site:facebook.com", max_links=max_candidates)

        if manual and results:
            print(f"\nSearch: {query}")
            for i, (href, title) in enumerate(results, 1):
                print(f"  [{i}] {title} | {href}")
            choice = input("Pick a result number (ENTER=1, 's'=skip): ").strip().lower()
            if choice == "s":
                candidate_url = ""
            else:
                try:
                    idx = 1 if choice == "" else int(choice)
                    idx = max(1, min(len(results), idx)) - 1
                except:
                    idx = 0
                candidate_url = results[idx][0]
        else:
            candidate_url = results[0][0] if results else ""

        if candidate_url:
            mobiles, landlines, emails, note = await extract_contacts_from_fb(page, candidate_url)
            # choose first email if multiple
            email = emails[0] if emails else None
            # assign with formatting
            out["Mobile (+63)"] = ", ".join(mobiles) if mobiles else "NA"
            out["Landline"] = ", ".join(landlines) if landlines else "NA"
            out["Email ID"] = email if email else "NA"
        else:
            note = "no-result"
    finally:
        await page.close()

    out["_query"] = query
    out["_candidate_url"] = candidate_url
    out["_note"] = note
    # polite delay
    await asyncio.sleep(random.uniform(1.0, 2.0))
    return out


# ----------------------------
# Main
# ----------------------------
async def main_async(args):
    rows = read_input_rows(args.input, encoding_hint=args.encoding)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headful)
        context = await browser.new_context(viewport={"width": 1200, "height": 900})

        enriched: List[Dict[str, Any]] = []
        log_rows: List[Dict[str, Any]] = []

        total = len(rows)
        for idx, row in enumerate(rows, 1):
            try:
                result = await process_row(context, row, manual=args.manual, max_candidates=args.max_candidates)
                enriched_row = dict(row)
                # ensure target columns
                enriched_row["Mobile (+63)"] = result.get("Mobile (+63)", "NA")
                enriched_row["Landline"] = result.get("Landline", "NA")
                enriched_row["Email ID"] = result.get("Email ID", "NA")
                enriched.append(enriched_row)

                log_rows.append({
                    "row": idx,
                    "agent": sane(row.get("Agent Name", "")),
                    "dealership": sane(row.get("Dealership", "")),
                    "query": result.get("_query", ""),
                    "candidate_url": result.get("_candidate_url", ""),
                    "note": result.get("_note", ""),
                    "timestamp": datetime.utcnow().isoformat()
                })

                print(f"[{idx}/{total}] {sane(row.get('Agent Name',''))} | "
                      f"{enriched_row['Mobile (+63)']} | {enriched_row['Email ID']}")
            except KeyboardInterrupt:
                print("Interrupted by user. Writing partial output ...")
                break
            except Exception as e:
                # Write NA for this row on error
                enriched_row = dict(row)
                enriched_row["Mobile (+63)"] = "NA"
                enriched_row["Landline"] = "NA"
                enriched_row["Email ID"] = "NA"
                enriched.append(enriched_row)

                log_rows.append({
                    "row": idx,
                    "agent": sane(row.get("Agent Name", "")),
                    "dealership": sane(row.get("Dealership", "")),
                    "query": "",
                    "candidate_url": "",
                    "note": f"error:{type(e).__name__}",
                    "timestamp": datetime.utcnow().isoformat()
                })

        await browser.close()

    # Write outputs
    write_output(enriched, args.output)
    write_log(log_rows, args.output)
    print("\nDone.")
    print(f" - Output: {args.output}")
    print(f" - Log   : {Path(args.output).with_name(Path(args.output).stem + '_log.csv')}")


def parse_args():
    ap = argparse.ArgumentParser(description="Enrich AutoDeal agents with public FB phone/email")
    ap.add_argument("-i", "--input", required=True, help="Input .xlsx/.xls/.csv with columns: Agent Name, Dealership")
    ap.add_argument("-o", "--output", required=True, help="Output path (.xlsx or .csv)")
    ap.add_argument("--encoding", default="auto",
                    help="Input CSV encoding (auto, utf-8, utf-8-sig, cp1252, latin-1)")
    ap.add_argument("--headful", action="store_true", help="Run with a visible browser window")
    ap.add_argument("--manual", action="store_true", help="Prompt to pick the best FB result per row")
    ap.add_argument("--max-candidates", type=int, default=5, help="Max DuckDuckGo results to consider per row")
    return ap.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        sys.exit(1)

import sys
import time
import json
from pathlib import Path
from urllib.parse import urljoin, urlparse
from openai import OpenAI
from playwright.sync_api import sync_playwright

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import OPENAI_API_KEY, AGENT_POLL_INTERVAL
from database.connection import get_connection
from logger import get_logger

log    = get_logger("classifier")
client = OpenAI(api_key=OPENAI_API_KEY)

PAGE_TIMEOUT        = 30_000
NETWORKIDLE_TIMEOUT = 8_000
MAX_NAV_PAGES       = 8     # max pages to scrape from nav links


# ── database ──────────────────────────────────────────────────────────────────

def claim_record():
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("START TRANSACTION")
        cur.execute("""
            SELECT id, fo_name, fo_website FROM records
            WHERE discovery_check = 1 AND classifier_check IS NULL
            AND fo_website IS NOT NULL
            LIMIT 1 FOR UPDATE
        """)
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return None
        conn.commit()
        return row
    except Exception as e:
        conn.rollback()
        log.error("Claim failed: %s", e)
        return None
    finally:
        cur.close()
        conn.close()


def save_scraped_page(record_id, page_url, page_content):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO scrape_cache (record_id, page_url, page_content)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE
                page_content = VALUES(page_content),
                scraped_at   = CURRENT_TIMESTAMP
        """, (record_id, page_url, page_content))
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.error("Save scrape failed for %s: %s", page_url, e)
    finally:
        cur.close()
        conn.close()


def save_result(record_id, is_fo, fo_type, proof_url, proof_text, reasoning):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            UPDATE records SET
                classifier_check    = %s,
                fo_type             = %s,
                classifier_proof_url  = %s,
                classifier_proof_text = %s,
                classifier_reasoning  = %s
            WHERE id = %s
        """, (1 if is_fo else 0,
              fo_type if is_fo else None,
              proof_url, proof_text, reasoning,
              record_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.error("Save result failed for record %s: %s", record_id, e)
    finally:
        cur.close()
        conn.close()


def mark_unreachable(record_id, reason):
    """
    Real browser couldn't get usable content from the site (blocked, down, timeout, etc).
    We can't confirm or rule out family-office status, so classifier_check stays a
    pass-through (1) rather than a drop - fo_type is left 'Unknown' for later stages
    (search_enrichment can still try to resolve it; validation requires a confirmed type).
    """
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            UPDATE records SET
                classifier_check     = 1,
                fo_type              = 'Unknown',
                classifier_reasoning = %s,
                error_message        = %s
            WHERE id = %s
        """, (reason[:500], reason[:500], record_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def mark_retry(record_id, reason):
    """Transient failure (e.g. LLM API error) - leave classifier_check NULL so it's retried."""
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            UPDATE records SET error_message = %s WHERE id = %s
        """, (reason[:500], record_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()


# ── playwright helpers ────────────────────────────────────────────────────────

def get_page_text(page):
    return page.evaluate("""() => {
        const els = document.querySelectorAll('p, h1, h2, h3, h4, h5, h6, li, span, div');
        const seen = new Set();
        const lines = [];
        for (const el of els) {
            const t = el.innerText?.trim();
            if (t && t.length > 20 && !seen.has(t)) {
                seen.add(t);
                lines.push(t);
            }
        }
        return lines.join('\\n');
    }""")


def scroll_to_bottom(page):
    page.evaluate("""async () => {
        await new Promise(resolve => {
            let total = 0;
            const timer = setInterval(() => {
                window.scrollBy(0, 400);
                total += 400;
                if (total >= document.body.scrollHeight) {
                    clearInterval(timer);
                    resolve();
                }
            }, 100);
        });
    }""")
    page.wait_for_timeout(500)


def extract_nav_links(page, base_url):
    """Extract internal links from header → footer → nav → top 20% of page."""
    base_domain = urlparse(base_url).netloc

    links = page.evaluate("""(baseDomain) => {
        function getLinks(selector) {
            const el = document.querySelector(selector);
            if (!el) return [];
            return Array.from(el.querySelectorAll('a[href]')).map(a => ({
                href: a.href,
                text: a.innerText?.trim()
            }));
        }

        // Priority: header → footer → nav → top 20% of body
        let links = getLinks('header');
        if (links.length === 0) links = getLinks('footer');
        if (links.length === 0) links = getLinks('nav');
        if (links.length === 0) {
            // fallback: all links but only from top 20% of page height
            const threshold = document.body.scrollHeight * 0.2;
            links = Array.from(document.querySelectorAll('a[href]'))
                .filter(a => {
                    const rect = a.getBoundingClientRect();
                    return (rect.top + window.scrollY) < threshold;
                })
                .map(a => ({ href: a.href, text: a.innerText?.trim() }));
        }
        return links;
    }""", base_domain)

    seen  = set()
    clean = []
    for link in links:
        href = (link.get("href") or "").strip()
        text = (link.get("text") or "").strip()
        if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        parsed = urlparse(href)
        if parsed.netloc and parsed.netloc != base_domain:
            continue  # external link
        full = urljoin(base_url, href).split("#")[0].rstrip("/")
        if full not in seen and full != base_url.rstrip("/"):
            seen.add(full)
            clean.append({"url": full, "text": text})

    return clean[:MAX_NAV_PAGES]


def scrape_page(page, url):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        try:
            page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT)
        except Exception:
            pass
        scroll_to_bottom(page)
        return page.url, get_page_text(page)
    except Exception as e:
        log.warning("Failed to scrape %s: %s", url, e)
        return url, None


# ── scrape all nav pages ──────────────────────────────────────────────────────

def scrape_website(record_id, fo_website, browser):
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    )
    page        = context.new_page()
    all_pages   = []   # [{url, content}]

    try:
        # scrape home page
        page.goto(fo_website, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        try:
            page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT)
        except Exception:
            pass
        scroll_to_bottom(page)
        home_content = get_page_text(page)
        home_url     = page.url

        if home_content:
            save_scraped_page(record_id, home_url, home_content)
            all_pages.append({"url": home_url, "content": home_content})
            log.info("  Home: %d chars", len(home_content))

        # extract nav links and scrape each
        nav_links = extract_nav_links(page, fo_website)
        log.info("  Nav links found: %d", len(nav_links))

        for link in nav_links:
            final_url, content = scrape_page(page, link["url"])
            if content:
                save_scraped_page(record_id, final_url, content)
                all_pages.append({"url": final_url, "content": content})
                log.info("  Scraped: %s (%d chars)", link["text"] or final_url, len(content))

    except Exception as e:
        log.error("Scrape error for %s: %s", fo_website, e)
    finally:
        context.close()

    return all_pages


# ── LLM classification ────────────────────────────────────────────────────────

CLASSIFY_PROMPT = """You are analyzing a financial firm's website to determine if it is a family office.

A family office privately manages wealth for one family (SFO) or a small number of wealthy families (MFO).
They are NOT retail advisers, banks, hedge funds, or general asset managers.

Analyze the pages below and respond in valid JSON only. No extra text.

{{
  "is_family_office": true or false,
  "fo_type": "SFO" or "MFO" or "unknown",
  "confidence": 0-100,
  "proof_url": "the exact page URL that best confirms your decision",
  "proof_text": "the exact sentence or phrase from that page that confirms it",
  "reasoning": "one sentence explaining your decision"
}}

Pages scraped:
{pages}"""


def classify(fo_name, all_pages):
    if not all_pages:
        return None, "No content scraped"

    # build pages block - truncate each page to avoid token limits
    pages_text = ""
    for p in all_pages:
        content = (p["content"] or "")[:1500]
        pages_text += f"\n--- PAGE: {p['url']} ---\n{content}\n"

    pages_text = pages_text[:6000]

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": CLASSIFY_PROMPT.format(pages=pages_text)
            }],
            temperature=0,
            max_tokens=300,
            response_format={"type": "json_object"}
        )
        raw  = resp.choices[0].message.content.strip()
        result = json.loads(raw)
        return result, None

    except Exception as e:
        log.error("LLM classify error for %s: %s", fo_name, e)
        return None, str(e)


# ── main loop ─────────────────────────────────────────────────────────────────

def main():
    log.info("Classifier agent started.")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        log.info("Browser ready (non-headless).")

        while True:
            record = claim_record()
            if not record:
                log.info("No pending records. Sleeping %ds...", AGENT_POLL_INTERVAL)
                time.sleep(AGENT_POLL_INTERVAL)
                continue

            record_id, fo_name, fo_website = record
            log.info("Processing [%d]: %s | %s", record_id, fo_name, fo_website)

            all_pages = scrape_website(record_id, fo_website, browser)

            if not all_pages:
                mark_unreachable(record_id, "No content scraped from any page - site inaccessible via real browser")
                time.sleep(AGENT_POLL_INTERVAL)
                continue

            result, error = classify(fo_name, all_pages)

            if error or not result:
                mark_retry(record_id, error or "LLM returned no result")
                time.sleep(AGENT_POLL_INTERVAL)
                continue

            is_fo    = result.get("is_family_office", False)
            fo_type  = result.get("fo_type", "unknown").upper()
            proof_url  = result.get("proof_url")
            proof_text = result.get("proof_text")
            reasoning  = result.get("reasoning")

            if fo_type not in ("SFO", "MFO"):
                fo_type = "Unknown"

            save_result(record_id, is_fo, fo_type, proof_url, proof_text, reasoning)

            if is_fo:
                log.info("  CONFIRMED [%s] conf:%s%% - %s", fo_type, result.get("confidence"), reasoning)
            else:
                log.info("  NOT FO - %s", reasoning)

            time.sleep(AGENT_POLL_INTERVAL)

        browser.close()


if __name__ == "__main__":
    main()

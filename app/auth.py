"""
Basic auth + role-based data masking for the dashboard.

Two roles:
  1 = restricted - principal_email and any LinkedIn URL are masked, EXCEPT on
      the one record that matches the logged-in user's own email domain
      (e.g. asif@screenbison.com can see the full "Screen Bison" record if
      its fo_website domain is screenbison.com).
  2 = full access - sees everything exactly as-is, no masking.

Password is stored and compared in PLAIN TEXT deliberately - this is a demo
phase, not a hardened production login system.

IMPORTANT: masking must be applied at the point data is fetched, before it
is ever handed to an LLM or serialized into any response - never as a
post-hoc display-only filter. See apply_masking() / mask_document_text().
"""
import sys
import re
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Request, HTTPException

sys.path.append(str(Path(__file__).resolve().parent.parent))
from database.connection import get_connection

LINKEDIN_FIELDS = ("principal_linkedin", "fo_corporate_linkedin")


# ── domain helpers ────────────────────────────────────────────────────────────

def _strip_www(host: str) -> str:
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def get_email_domain(email):
    if not email or "@" not in email:
        return None
    return _strip_www(email.split("@", 1)[1])


def get_url_domain(url):
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    netloc = urlparse(url).netloc
    return _strip_www(netloc) if netloc else None


def is_own_record(fo_website, user):
    if not fo_website or not user:
        return False
    user_domain   = get_email_domain(user.get("email"))
    record_domain = get_url_domain(fo_website)
    return bool(user_domain) and bool(record_domain) and user_domain == record_domain


# ── masking ────────────────────────────────────────────────────────────────────

def mask_email(email):
    if not email or "@" not in email:
        return email
    return f"****@{email.split('@', 1)[1]}"


def mask_linkedin_url(url):
    if not url:
        return url
    m = re.match(r"^(?:https?://)?(?:www\.)?(linkedin\.com/(?:in|company)/)", url, re.IGNORECASE)
    prefix = m.group(1) if m else "linkedin.com/"
    return f"{prefix}****"


def apply_masking(record: dict, user: dict) -> dict:
    """
    Returns a masked copy of `record` for role=1 users, unless it's their own
    company's record. role=2 (or no user) gets the record back untouched.
    Only touches whichever of principal_email/principal_linkedin/
    fo_corporate_linkedin keys are actually present, so this is safe to call
    on any dict shape (full DB rows, RAG metadata, tool results, etc).
    """
    if not record or not user or user.get("role") == 2:
        return record
    if is_own_record(record.get("fo_website"), user):
        return record

    masked = dict(record)
    if masked.get("principal_email"):
        masked["principal_email"] = mask_email(masked["principal_email"])
    for field in LINKEDIN_FIELDS:
        if masked.get(field):
            masked[field] = mask_linkedin_url(masked[field])
    return masked


def mask_document_text(text: str, record: dict, user: dict) -> str:
    """
    Scrubs the literal email/LinkedIn substrings out of free text (e.g. a RAG
    chunk that has "Contact email: real@address.com." baked into it) using the
    UNMASKED record to know what to look for. Call this on the original record
    BEFORE apply_masking() strips those values out of it.
    """
    if not text or not user or user.get("role") == 2:
        return text
    if is_own_record(record.get("fo_website"), user):
        return text

    scrubbed = text
    email = record.get("principal_email")
    if email:
        scrubbed = scrubbed.replace(email, mask_email(email))
    for field in LINKEDIN_FIELDS:
        url = record.get(field)
        if url:
            scrubbed = scrubbed.replace(url, mask_linkedin_url(url))
    return scrubbed


# ── authentication ─────────────────────────────────────────────────────────────

def authenticate(email: str, password: str):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT id, email, password, role FROM users WHERE email = %s", (email,))
        row = cur.fetchone()
        if not row or password != row[2]:
            return None
        return {"id": row[0], "email": row[1], "role": row[3]}
    finally:
        cur.close()
        conn.close()


# ── session dependencies ──────────────────────────────────────────────────────

def get_current_user(request: Request) -> dict:
    """For API routes - 401s if not logged in."""
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def get_current_user_optional(request: Request):
    """For page routes that want to redirect manually instead of 401ing."""
    return request.session.get("user")


def require_admin(request: Request) -> dict:
    """For role=2-only routes (e.g. raw CSV downloads)."""
    user = get_current_user(request)
    if user.get("role") != 2:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

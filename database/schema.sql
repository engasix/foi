-- PolarityIQ Stage 1 - Family Office Dataset Pipeline
-- Database schema for parallel agent architecture

-- ─────────────────────────────────────────────
-- CORE TABLE: records
-- Central table all agents read from and write to
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS records (
    id                      INT AUTO_INCREMENT PRIMARY KEY,

    -- Identity
    fo_name                 VARCHAR(255) NOT NULL,
    fo_type                 ENUM('SFO', 'MFO', 'Unknown') DEFAULT NULL,
    fo_website              VARCHAR(500),
    fo_corporate_linkedin   VARCHAR(500),
    fo_city                 VARCHAR(255),
    fo_state                VARCHAR(100),
    fo_country              VARCHAR(100),
    fo_phone                VARCHAR(50),
    crd_number              VARCHAR(50),

    -- Entity Attributes
    fo_background           TEXT,
    fo_aum                  VARCHAR(100),
    fo_investing_thesis     TEXT,
    fo_investing_mandates   TEXT,

    -- Principal (Decision-Maker)
    principal_name          VARCHAR(255),
    principal_title         VARCHAR(255),
    principal_linkedin      VARCHAR(500),
    principal_email         VARCHAR(255),
    principal_email_code    VARCHAR(50),    -- Hunter.io: valid/accept_all/invalid/disposable/unknown
    principal_email_source  VARCHAR(500),   -- Serper.dev: URL where email was found publicly
    principal_phone         VARCHAR(50),

    -- Classifier decision evidence
    classifier_proof_url    VARCHAR(500),              -- page URL that confirmed FO decision
    classifier_proof_text   TEXT,                      -- exact text excerpt from that page
    classifier_reasoning    TEXT,                      -- LLM one-sentence reasoning

    -- Pipeline flags (NULL=not processed, 1=passed, 0=failed)
    discovery_check           TINYINT(1) DEFAULT NULL,   -- Agent 1: SEC record found (no website-alive pre-filter)
    classifier_check          TINYINT(1) DEFAULT NULL,   -- Agent 2: 1 = confirmed FO OR site unreachable (fo_type='Unknown'), 0 = LLM confirmed NOT a family office
    web_enrichment_check      TINYINT(1) DEFAULT NULL,   -- Agent 3: 1 after any attempt (fields may still be NULL if nothing could be scraped)
    search_enrichment_check   TINYINT(1) DEFAULT NULL,   -- Agent 4: Serper.dev multi-query/multi-page search for missing data (incl. fo_type)
    linkedin_enrichment_check TINYINT(1) DEFAULT NULL,   -- Agent 5: LinkedIn company + principal profiles
    email_enrichment_check    TINYINT(1) DEFAULT NULL,   -- Agent 6: Hunter.io principal email lookup
    signals_check             TINYINT(1) DEFAULT NULL,   -- inactive: signals_agent is not part of the active pipeline
    validation_check          TINYINT(1) DEFAULT NULL,   -- Agent 7: cross-verified, confidence scored (requires confirmed SFO/MFO fo_type)
    index_check               TINYINT(1) DEFAULT NULL,   -- Agent 8: embedded into ChromaDB

    -- Loop tracking (inactive: staleness_agent is not part of the active pipeline)
    run_cycle               TINYINT UNSIGNED DEFAULT 1,  -- which scheduled run discovered this record
    staleness_check         TINYINT(1) DEFAULT NULL,     -- NULL=not checked, 1=stale, 0=still valid
    staleness_reason        TEXT DEFAULT NULL,           -- evidence: what changed or went dark

    -- Meta
    discovery_source        VARCHAR(500),
    confidence_score        TINYINT UNSIGNED DEFAULT 0,   -- 0-100
    error_message           TEXT,
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_fo_name (fo_name)
);


-- ─────────────────────────────────────────────
-- SCRAPE CACHE TABLE
-- Stores raw scraped content separately from the main records table.
-- Keeps records table clean - enrichment agents read from here.
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scrape_cache (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    record_id       INT NOT NULL,

    page_url        VARCHAR(500) NOT NULL,
    page_content    LONGTEXT,

    scraped_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (record_id) REFERENCES records(id) ON DELETE CASCADE,
    UNIQUE KEY uq_scrape_page (record_id, page_url)
);


-- ─────────────────────────────────────────────
-- SIGNALS TABLE
-- Separate table for recent activities.
-- One FO can have multiple signals per type.
-- signal_date drives freshness scoring accurately.
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    record_id    INT NOT NULL,

    headline     VARCHAR(500),
    source       VARCHAR(200),
    signal_type  ENUM('investment', 'fund_commitment', 'key_hire', 'news') NOT NULL,
    description  TEXT NOT NULL,   -- LLM one-sentence summary of what this signal means
    signal_date  DATE,
    source_url   VARCHAR(500),
    image_url    VARCHAR(1000),   -- og:image from article page (best-effort)

    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (record_id) REFERENCES records(id) ON DELETE CASCADE
);


-- ─────────────────────────────────────────────
-- VALIDATION CHAIN TABLE
-- Stores the full validation chain for each record.
-- Task requires full chain for 3 selected records.
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS validation_chain (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    record_id           INT NOT NULL,

    discovery_source    TEXT,           -- where the FO was originally found
    extraction_method   TEXT,           -- how data was pulled (SEC EDGAR, scraping, Hunter.io, etc.) - can list up to 6 methods, was VARCHAR(255) and overflowed
    enrichment_steps    TEXT,           -- what enrichment was applied and in what order
    validation_logic    TEXT,           -- how each field was cross-checked
    confidence_assessment TEXT,         -- reasoning behind the confidence score
    exact_sources       TEXT,           -- exact URLs and links used

    validated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (record_id) REFERENCES records(id) ON DELETE CASCADE,
    UNIQUE KEY uq_record_chain (record_id)
);


-- ─────────────────────────────────────────────
-- USERS TABLE
-- Basic auth for the dashboard. role=1 gets masked contact data (except
-- their own company's record, matched by email domain vs fo_website domain).
-- role=2 sees everything unmasked.
-- password is stored in PLAIN TEXT deliberately for now (demo phase, not
-- production) - do not treat this as a security-hardened login system.
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    email      VARCHAR(255) NOT NULL UNIQUE,
    password   VARCHAR(255) NOT NULL,   -- plain text - demo only
    role       TINYINT NOT NULL DEFAULT 1,  -- 1 = restricted (masked), 2 = full access

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

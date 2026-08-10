from dotenv import load_dotenv
import os

load_dotenv()

# Database
DB_HOST     = os.getenv("DB_HOST", "localhost")
DB_PORT     = int(os.getenv("DB_PORT", 3306))
DB_USER     = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME     = os.getenv("DB_NAME", "polarityiq")

# APIs
SERPER_API_KEY    = os.getenv("SERPER_API_KEY")
HUNTER_API_KEY    = os.getenv("HUNTER_API_KEY")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SEC_API_KEY       = os.getenv("SEC_API_KEY")

# Agent tuning
AGENT_POLL_INTERVAL  = int(os.getenv("AGENT_POLL_INTERVAL", 10))
MIN_CONFIDENCE_SCORE = int(os.getenv("MIN_CONFIDENCE_SCORE", 40))
DISCOVERY_TARGET     = int(os.getenv("DISCOVERY_TARGET", 75))

# Dashboard auth
SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY", "dev-only-change-me")

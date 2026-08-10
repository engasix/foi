import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from database.connection import get_connection

def truncate_all():
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        cur.execute("TRUNCATE TABLE validation_chain")
        cur.execute("TRUNCATE TABLE signals")
        cur.execute("TRUNCATE TABLE scrape_cache")
        cur.execute("TRUNCATE TABLE records")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        conn.commit()
        print("All tables truncated successfully.")
    except Exception as e:
        conn.rollback()
        print(f"Error: {e}")
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    confirm = input("This will delete ALL data. Type 'yes' to confirm: ")
    if confirm.strip().lower() == "yes":
        truncate_all()
    else:
        print("Aborted.")

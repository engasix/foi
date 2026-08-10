import os
import sys
import mysql.connector
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME


def run():
    # Connect without selecting a database first
    conn = mysql.connector.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        autocommit=True
    )
    cursor = conn.cursor()

    # Create database if not exists
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    print(f"Database `{DB_NAME}` ready.")

    # Switch to the database
    cursor.execute(f"USE `{DB_NAME}`")

    # Read and execute schema.sql
    schema_path = Path(__file__).parent / "schema.sql"
    schema_sql = schema_path.read_text()

    # Split on semicolons to run each statement separately
    statements = [s.strip() for s in schema_sql.split(";") if s.strip()]
    for statement in statements:
        cursor.execute(statement)
        print(f"  OK: {statement[:60].replace(chr(10), ' ')}...")

    cursor.close()
    conn.close()
    print("\nMigration complete.")


if __name__ == "__main__":
    run()

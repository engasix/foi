import sys
from pathlib import Path
from datetime import datetime

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import DB_NAME
from database.connection import get_connection

BACKUP_DIR = Path(__file__).parent / "backups"


def run():
    BACKUP_DIR.mkdir(exist_ok=True)

    now         = datetime.now()
    timestamp   = now.strftime("%d-%m-%y ") + now.strftime("%I-%M%p").lstrip("0").lower()
    output_file = BACKUP_DIR / f"foi {timestamp}.sql"

    conn = get_connection()
    cur  = conn.cursor()

    print(f"Backing up `{DB_NAME}` → {output_file.name}")

    with open(output_file, "w") as f:
        f.write(f"-- Backup of `{DB_NAME}` created {datetime.now().isoformat()}\n")
        f.write("SET FOREIGN_KEY_CHECKS=0;\n\n")

        # Get all tables
        cur.execute("SHOW TABLES")
        tables = [row[0] for row in cur.fetchall()]

        for table in tables:
            print(f"  Exporting table: {table}")

            # CREATE TABLE statement
            cur.execute(f"SHOW CREATE TABLE `{table}`")
            create_sql = cur.fetchone()[1]
            f.write(f"DROP TABLE IF EXISTS `{table}`;\n")
            f.write(f"{create_sql};\n\n")

            # Data rows as INSERT statements
            cur.execute(f"SELECT * FROM `{table}`")
            rows = cur.fetchall()
            if rows:
                cols = [desc[0] for desc in cur.description]
                col_list = ", ".join(f"`{c}`" for c in cols)
                for row in rows:
                    values = ", ".join(
                        "NULL" if v is None
                        else f"'{str(v).replace(chr(39), chr(39)*2)}'"
                        for v in row
                    )
                    f.write(f"INSERT INTO `{table}` ({col_list}) VALUES ({values});\n")
                f.write("\n")

        f.write("SET FOREIGN_KEY_CHECKS=1;\n")

    cur.close()
    conn.close()

    size_kb = output_file.stat().st_size // 1024
    print(f"Done. {size_kb} KB → {output_file}")


if __name__ == "__main__":
    run()

from app import db

with db.engine.connect() as conn:
    conn.execute("ALTER TABLE player ADD COLUMN description VARCHAR(255)")
    conn.commit()

print("Column added successfully")
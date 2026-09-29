import sqlite3
import sys
sys.path.insert(0, r'C:\Users\Hello\OneDrive\Documents\Default Project\backend')

db_path = r'C:\Users\Hello\OneDrive\Documents\Default Project\backend\intradayai.db'
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# List all tables
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cursor.fetchall()
print('Tables:', tables)

# For each table, show schema
for table in tables:
    table_name = table[0]
    cursor.execute(f'PRAGMA table_info({table_name})')
    columns = cursor.fetchall()
    print(f'\nTable {table_name}:')
    for col in columns:
        print(f'  {col}')
    
    # Show row count
    cursor.execute(f'SELECT COUNT(*) FROM {table_name}')
    count = cursor.fetchone()[0]
    print(f'  Row count: {count}')

conn.close()
print('\nDatabase inspection complete.')
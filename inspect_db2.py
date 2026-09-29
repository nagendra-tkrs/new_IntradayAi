import sqlite3

db_path = r'C:\Users\Hello\OneDrive\Documents\Default Project\backend\intradayai.db'
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# List all tables and row counts
cursor.execute('SELECT name FROM sqlite_master WHERE type=\"table\" ORDER BY name')
tables = cursor.fetchall()
print('Tables:')
for t in tables:
    table_name = t[0]
    cursor.execute(f'SELECT COUNT(*) FROM {table_name}')
    count = cursor.fetchone()[0]
    print(f'  {table_name}: {count} rows')

# Check users
cursor.execute('SELECT id, email, name, auth_provider FROM users')
users = cursor.fetchall()
print(f'\nUsers ({len(users)}):')
for u in users:
    print(f'  {u}')

# Check portfolio table
cursor.execute('SELECT * FROM portfolio')
portfolio = cursor.fetchall()
print(f'\nPortfolio rows: {len(portfolio)}')

# Check positions table  
cursor.execute('SELECT * FROM positions')
positions = cursor.fetchall()
print(f'Position rows: {len(positions)}')

# Check trades table
cursor.execute('SELECT * FROM trades')
trades = cursor.fetchall()
print(f'Trade rows: {len(trades)}')

# Check user_trade_setups
cursor.execute('SELECT * FROM user_trade_setups')
sets = cursor.fetchall()
print(f'User trade setups rows: {len(sets)}')

conn.close()
print('\nDatabase inspection complete.')
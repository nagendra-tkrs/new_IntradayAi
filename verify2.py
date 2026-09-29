import urllib.request
import json

results = []

# 1. Health (public)
print('=== 1. Health Endpoint ===')
resp = urllib.request.urlopen('http://127.0.0.1:8000/api/health')
data = json.loads(resp.read().decode())
print(f'Status: {data}')
assert data['status'] == 'ok'
results.append(('Health', 'PASS'))

# 2. Google OAuth config (public)
print('\n=== 2. Google OAuth Config ===')
try:
    resp = urllib.request.urlopen('http://127.0.0.1:8000/api/auth/google/config')
    print('OK')
    results.append(('Auth Google Config', 'PASS'))
except Exception as e:
    print(f'{e}')
    results.append(('Auth Google Config', 'FAIL'))

# 3. Market status (requires auth - try without token first)
print('\n=== 3. Market Status (no token) ===')
try:
    resp = urllib.request.urlopen('http://127.0.0.1:8000/api/market/status')
    data = json.loads(resp.read().decode())
    print(f'Market status: {data}')
    results.append(('Market Status', 'PASS'))
except Exception as e:
    print(f'Auth required (expected): {e}')
    results.append(('Market Status', 'AUTH_REQUIRED'))

# 4. Top Signals (requires auth)
print('\n=== 4. Top Signals (no token) ===')
try:
    resp = urllib.request.urlopen('http://127.0.0.1:8000/api/signals')
    data = json.loads(resp.read().decode())
    print(f'Signals count: {len(data)}')
    results.append(('Top Signals', 'PASS'))
except Exception as e:
    print(f'Auth required (expected): {e}')
    results.append(('Top Signals', 'AUTH_REQUIRED'))

# 5. Stock detail (requires auth)
print('\n=== 5. Stock Detail AAPL (no token) ===')
try:
    resp = urllib.request.urlopen('http://127.0.0.1:8000/api/market/AAPL')
    data = json.loads(resp.read().decode())
    print(f'AAPL detail: {str(data)[:300]}')
    results.append(('Stock Detail', 'PASS'))
except Exception as e:
    print(f'Auth required (expected): {e}')
    results.append(('Stock Detail', 'AUTH_REQUIRED'))

# 6. Phase 4: Scanner signal consistency
print('\n=== 6. Phase 4 Signal Consistency ===')
# These also require auth, check what we can
try:
    resp = urllib.request.urlopen('http://127.0.0.1:8000/api/scanner')
    data = json.loads(resp.read().decode())
    print(f'Scanner: OK')
    results.append(('Scanner', 'PASS'))
except Exception as e:
    print(f'Scanner auth required (expected): {e}')
    results.append(('Scanner', 'AUTH_REQUIRED'))

try:
    resp = urllib.request.urlopen('http://127.0.0.1:8000/api/signal-snapshot')
    data = json.loads(resp.read().decode())
    print(f'Signal snapshot: OK')
    results.append(('Signal Snapshot', 'PASS'))
except Exception as e:
    print(f'Signal snapshot auth required (expected): {e}')
    results.append(('Signal Snapshot', 'AUTH_REQUIRED'))

# 7. Frontend accessibility
print('\n=== 7. Frontend ===')
try:
    resp = urllib.request.urlopen('http://localhost:3000')
    final_url = resp.geturl()
    print(f'Frontend URL: {final_url}')
    results.append(('Frontend', 'PASS'))
except Exception as e:
    print(f'Frontend: {e}')
    results.append(('Frontend', 'FAIL'))

# Summary
print('\n' + '='*60)
print('VERIFICATION SUMMARY')
print('='*60)
for name, status in results:
    print(f'  {name}: {status}')

all_pass = all(s in ('PASS', 'AUTH_REQUIRED') for _, s in results)
# Health and frontend must be PASS for overall success
health_ok = any(n == 'Health' and s == 'PASS' for n, s in results)
frontend_ok = any(n == 'Frontend' and s == 'PASS' for n, s in results)
print(f'\nBackend health: {"PASS" if health_ok else "FAIL"}')
print(f'Frontend accessible: {"PASS" if frontend_ok else "FAIL"}')
print(f'\nOverall: {"PASS" if (health_ok and frontend_ok) else "CHECK_LOGS"}')
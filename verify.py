import urllib.request
import json

results = []

# 1. Health
print('=== 1. Health Endpoint ===')
resp = urllib.request.urlopen('http://127.0.0.1:8000/api/health')
data = json.loads(resp.read().decode())
print(f'Status: {data}')
assert data['status'] == 'ok'
results.append(('Health', 'PASS'))

# 2. Auth check - Google OAuth config
print('\n=== 2. Auth Flow ===')
try:
    resp = urllib.request.urlopen('http://127.0.0.1:8000/api/auth/google/config')
    print('Google OAuth config: OK')
    results.append(('Auth Google Config', 'PASS'))
except Exception as e:
    print(f'Google OAuth config: {e}')
    results.append(('Auth Google Config', 'FAIL'))

# 3. Market status
print('\n=== 3. Market Status ===')
try:
    resp = urllib.request.urlopen('http://127.0.0.1:8000/api/market/status')
    data = json.loads(resp.read().decode())
    print(f'Market status: {data}')
    results.append(('Market Status', 'PASS'))
except Exception as e:
    print(f'Market status: {e}')
    results.append(('Market Status', 'FAIL'))

# 4. Top Signals
print('\n=== 4. Top Signals ===')
try:
    resp = urllib.request.urlopen('http://127.0.0.1:8000/api/signals')
    data = json.loads(resp.read().decode())
    print(f'Signals count: {len(data)}')
    for s in data[:3]:
        print(f'  - {s.get("symbol")}: {s.get("signal")}')
    results.append(('Top Signals', 'PASS'))
except Exception as e:
    print(f'Signals: {e}')
    results.append(('Top Signals', 'FAIL'))

# 5. Stock detail (AAPL)
print('\n=== 5. Stock Detail (AAPL) ===')
try:
    resp = urllib.request.urlopen('http://127.0.0.1:8000/api/market/AAPL')
    data = json.loads(resp.read().decode())
    print(f'AAPL detail: {str(data)[:300]}')
    results.append(('Stock Detail', 'PASS'))
except Exception as e:
    print(f'Stock detail: {e}')
    results.append(('Stock Detail', 'FAIL'))

# 6. Phase 4: Check signal consistency endpoints
print('\n=== 6. Phase 4 Signal Consistency ===')
try:
    # Check scanner signals
    resp = urllib.request.urlopen('http://127.0.0.1:8000/api/scanner')
    data = json.loads(resp.read().decode())
    print(f'Scanner signals: {len(data)}')
    results.append(('Scanner', 'PASS'))
except Exception as e:
    print(f'Scanner: {e}')
    results.append(('Scanner', 'FAIL'))

try:
    # Check signal snapshot / consistency
    resp = urllib.request.urlopen('http://127.0.0.1:8000/api/signal-snapshot')
    data = json.loads(resp.read().decode())
    print(f'Signal snapshot: {str(data)[:200]}')
    results.append(('Signal Snapshot', 'PASS'))
except Exception as e:
    print(f'Signal snapshot: {e}')
    results.append(('Signal Snapshot', 'FAIL'))

# Summary
print('\n' + '='*50)
print('VERIFICATION SUMMARY')
print('='*50)
for name, status in results:
    print(f'  {name}: {status}')

all_pass = all(s == 'PASS' for _, s in results)
print(f'\nOverall: {"PASS" if all_pass else "FAIL"}')
import subprocess
import sys
import time
import json
import urllib.request
import urllib.error
import jwt
from datetime import datetime, timedelta
sys.path.insert(0, r'C:\Users\Hello\OneDrive\Documents\Default Project\backend')
from app.core.config import settings

def get_token():
    payload = {
        'sub': 'd54bc7220da54520',
        'email': 'default@intradayai.local',
        'exp': datetime.utcnow() + timedelta(minutes=settings.JWT_EXPIRE_MINUTES),
        'iat': datetime.utcnow(),
    }
    tok = jwt.encode(payload, settings.JWT_SECRET, algorithm='HS256')
    auth_headers = {'Authorization': f'Bearer {tok}'}
    return auth_headers

# Start backend process
proc = subprocess.Popen(
    [sys.executable, '-m', 'uvicorn', 'app.main:app', '--port', '8003', '--host', '127.0.0.1'],
    cwd=r'C:\Users\Hello\OneDrive\Documents\Default Project\backend',
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
)

# Wait for startup
time.sleep(3)

if proc.poll() is not None:
    print('Backend failed to start')
    sys.exit(1)

try:
    headers = get_token()
    
    endpoints = [
        ('/api/health', 'http://127.0.0.1:8003/api/health'),
        ('/api/portfolio', 'http://127.0.0.1:8003/api/portfolio'),
        ('/api/paper/positions', 'http://127.0.0.1:8003/api/paper/positions'),
        ('/api/paper/trades?limit=50', 'http://127.0.0.1:8003/api/paper/trades?limit=50'),
        ('/api/paper/pending', 'http://127.0.0.1:8003/api/paper/pending'),
        ('/api/paper/performance', 'http://127.0.0.1:8003/api/paper/performance'),
    ]
    
    print('=== WITH AUTH ===')
    results = {}
    for name, url in endpoints:
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req)
            data = resp.read().decode()
            print(f'{name}: 200 - {data[:150]}')
            results[name] = ('PASS', data)
        except urllib.error.HTTPError as e:
            body = e.read().decode() if hasattr(e, 'read') else 'no body'
            print(f'{name}: {e.code} - {body[:150]}')
            results[name] = ('FAIL', body)
        except Exception as e:
            print(f'{name}: ERROR - {e}')
            results[name] = ('ERROR', str(e))
    
    print()
    print('=== API VERIFICATION SUMMARY ===')
    for name, (status, data) in results.items():
        print(f'  {name}: {status}')
    
    all_pass = all(r[0] == 'PASS' for r in results.values())
    print(f'\\nAll APIs PASS: {all_pass}')
    
finally:
    proc.terminate()
    proc.wait()
    print('\\nBackend stopped.')
import subprocess
import sys
import os
import time
import json
import urllib.request
import urllib.error

sys.path.insert(0, r'C:\Users\Hello\OneDrive\Documents\Default Project\backend')

# Start backend process
proc = subprocess.Popen(
    [sys.executable, '-m', 'uvicorn', 'app.main:app', '--port', '8003', '--host', '127.0.0.1'],
    cwd=r'C:\Users\Hello\OneDrive\Documents\Default Project\backend',
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
)

# Wait for startup
time.sleep(3)

# Check if process is running
if proc.poll() is not None:
    print('Backend failed to start')
    print('STDOUT:', proc.stdout.read())
    print('STDERR:', proc.stderr.read())
    sys.exit(1)

print(f'Backend started PID: {proc.pid}')

try:
    # Generate token for default user
    import jwt
    from datetime import datetime, timedelta
    
    # Setup path for settings
    sys.path.insert(0, r'C:\Users\Hello\OneDrive\Documents\Default Project\backend')
    from app.core.config import settings
    
    payload = {
        'sub': 'd54bc7220da54520',
        'email': 'default@intradayai.local',
        'exp': datetime.utcnow() + timedelta(minutes=settings.JWT_EXPIRE_MINUTES),
        'iat': datetime.utcnow(),
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    
    headers = {'Authorization': f'Bearer {token}'}
    
    # Test endpoints
    endpoints = [
        ('GET /api/health', 'http://127.0.0.1:8003/api/health'),
        ('GET /api/portfolio', 'http://127.0.0.1:8003/api/portfolio'),
        ('GET /api/paper/positions', 'http://127.0.0.1:8003/api/paper/positions'),
        ('GET /api/paper/trades?limit=50', 'http://127.0.0.1:8003/api/paper/trades?limit=50'),
        ('GET /api/paper/pending', 'http://127.0.0.1:8003/api/paper/pending'),
        ('GET /api/paper/performance', 'http://127.0.0.1:8003/api/paper/performance'),
    ]
    
    for name, url in endpoints:
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req)
            data = json.loads(resp.read().decode())
            print(f'{name}: 200 - {json.dumps(data)[:200]}')
        except urllib.error.HTTPError as e:
            body = e.read().decode() if hasattr(e, 'read') else 'no body'
            print(f'{name}: {e.code} - {body[:200]}')
        except Exception as e:
            print(f'{name}: ERROR - {e}')
    
    # Also test WITHOUT auth
    print()
    print('--- Without auth ---')
    for name, url in endpoints:
        try:
            req = urllib.request.Request(url)
            resp = urllib.request.urlopen(req)
            data = json.loads(resp.read().decode())
            print(f'{name}: 200 - {json.dumps(data)[:200]}')
        except urllib.error.HTTPError as e:
            body = e.read().decode() if hasattr(e, 'read') else 'no body'
            print(f'{name}: {e.code} - {body[:200]}')
        except Exception as e:
            print(f'{name}: ERROR - {e}')
    
finally:
    proc.terminate()
    proc.wait()
    print('\\nBackend stopped.')
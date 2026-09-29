import subprocess
import sys
import time
import json
import urllib.request
import urllib.error
import traceback

# Start backend process
proc = subprocess.Popen(
    [sys.executable, '-m', 'uvicorn', 'app.main:app', '--port', '8002', '--host', '127.0.0.1'],
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
    
    # Test /api/paper/trades?limit=50 WITH auth - this was 500
    print('=== Testing /api/paper/trades?limit=50 WITH auth ===')
    try:
        req = urllib.request.Request(
            'http://127.0.0.1:8002/api/paper/trades?limit=50',
            headers=headers
        )
        resp = urllib.request.urlopen(req)
        data = resp.read().decode()
        print(f'Success: {data[:200]}')
    except urllib.error.HTTPError as e:
        body = e.read().decode() if hasattr(e, 'read') else 'no body'
        print(f'HTTP error: {e.code}')
        print(f'Response body: {body[:500]}')
        # Try to get traceback from server
        print(f'Headers: {dict(e.headers)}')
    except Exception as e:
        print(f'Error: {e}')
        traceback.print_exc()
    
finally:
    proc.terminate()
    proc.wait()
    print('\\nBackend stopped.')
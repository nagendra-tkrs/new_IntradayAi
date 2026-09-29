import subprocess
import sys
import time
import json
import urllib.request
import urllib.error

# Start backend process
proc = subprocess.Popen(
    [sys.executable, '-m', 'uvicorn', 'app.main:app', '--port', '8005', '--host', '127.0.0.1'],
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
    # Test /api/auth/google/config
    print('=== Testing /api/auth/google/config ===')
    try:
        resp = urllib.request.urlopen('http://127.0.0.1:8005/api/auth/google/config')
        data = resp.read().decode()
        print(f'Status: 200')
        print(f'Body: {data[:300]}')
    except urllib.error.HTTPError as e:
        print(f'HTTP error: {e.code}')
        body = e.read().decode() if hasattr(e, 'read') else 'no body'
        print(f'Body: {body[:300]}')
    except Exception as e:
        print(f'Error: {e}')
    
    # Test /api/health
    print()
    print('=== Testing /api/health ===')
    try:
        resp = urllib.request.urlopen('http://127.0.0.1:8005/api/health')
        data = resp.read().decode()
        print(f'Status: 200')
        print(f'Body: {data[:200]}')
    except urllib.error.HTTPError as e:
        print(f'HTTP error: {e.code}')
        body = e.read().decode() if hasattr(e, 'read') else 'no body'
        print(f'Body: {body[:200]}')
    except Exception as e:
        print(f'Error: {e}')
        
finally:
    proc.terminate()
    proc.wait()
    print('\\nBackend stopped.')
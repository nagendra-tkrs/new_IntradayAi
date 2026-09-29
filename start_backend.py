import subprocess
import sys
import time
import urllib.request
import urllib.error

print('Starting IntradayAI Backend...')

# Start backend process
proc = subprocess.Popen(
    [sys.executable, '-m', 'uvicorn', 'app.main:app', '--port', '8020', '--host', '127.0.0.1'],
    cwd=r'C:\Users\Hello\OneDrive\Documents\Default Project\backend',
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
)

# Wait for startup
time.sleep(3)

# Check if process is running
if proc.poll() is not None:
    print('FAILED to start backend')
    print('STDERR:', proc.stderr.read()[:300])
    sys.exit(1)

print(f'Backend started PID: {proc.pid}')

# Wait for port to be available
for attempt in range(10):
    try:
        resp = urllib.request.urlopen('http://127.0.0.1:8020/api/health')
        print(f'Backend health check passed: {resp.read().decode()}')
        break
    except Exception as e:
        print(f'Health check attempt {attempt+1} failed: {e}')
        time.sleep(1)
else:
    print('Backend did not become healthy in time')
    proc.terminate()
    sys.exit(1)

print('\\nBackend is running and healthy.')
print(f'URL: http://localhost:8020')
import subprocess
import sys
import time
import urllib.request
import urllib.error

proc = subprocess.Popen(
    [sys.executable, '-m', 'uvicorn', 'app.main:app', '--port', '8000', '--host', '127.0.0.1'],
    cwd=r'C:\Users\Hello\OneDrive\Documents\Default Project\backend',
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
)
time.sleep(5)

if proc.poll() is not None:
    print('FAILED to start')
    print('STDERR:', proc.stderr.read()[:300])
else:
    print('Backend running, testing endpoints...')
    
    # Test health with retries
    for attempt in range(5):
        try:
            resp = urllib.request.urlopen('http://127.0.0.1:8000/api/health')
            print(f'Health ({attempt+1}):', resp.status, resp.read().decode())
            break
        except Exception as e:
            print(f'Health attempt {attempt+1} failed:', e)
            time.sleep(1)
    
    # Test google config
    try:
        resp = urllib.request.urlopen('http://127.0.0.1:8000/api/auth/google/config')
        print('Google config:', resp.status, resp.read().decode())
    except urllib.error.HTTPError as e:
        print('Google config error:', e.code, '-', e.read().decode() if hasattr(e, 'read') else 'no body')
    except Exception as e:
        print('Google config error:', e)
    
    proc.terminate()
    proc.wait()
    print('Stopped')
import subprocess
import sys
import time

print('Starting IntradayAI Frontend...')

# Start frontend process
proc = subprocess.Popen(
    [sys.executable, '-m', 'npm', 'run', 'dev', '--', '-p', '3005'],
    cwd=r'C:\Users\Hello\OneDrive\Documents\Default Project\frontend',
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
)

# Wait for startup
time.sleep(5)

# Check if process is running
if proc.poll() is not None:
    print('FAILED to start frontend')
    print('STDOUT:', proc.stdout.read()[:300])
    print('STDERR:', proc.stderr.read()[:300])
    sys.exit(1)

print(f'Frontend process started PID: {proc.pid}')

# Wait for port 3005 to be available
for attempt in range(10):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect(('127.0.0.1', 3005))
        print('Frontend port 3005 is open')
        s.close()
        break
    except Exception:
        print(f'Frontend port check attempt {attempt+1} failed')
        time.sleep(1)
else:
    print('Frontend port not open in time')
    proc.terminate()
    sys.exit(1)

print('Frontend is running and healthy.')
print(f'URL: http://localhost:3005')
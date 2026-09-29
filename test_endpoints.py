import urllib.request
import json

token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0LXVzZXItMTIzIiwiZW1haWwiOiJ0ZXN0QGV4YW1wbGUuY29tIiwiZXhwIjoxNzg4Njc4NjE1LCJpYXQiOjE3ODg1OTIyMTV9"

# Test /api/portfolio with token
print("=== /api/portfolio with token ===")
try:
    req = urllib.request.Request(
        'http://127.0.0.1:8000/api/portfolio',
        headers={"Authorization": f"Bearer {token}"}
    )
    resp = urllib.request.urlopen(req)
    data = json.loads(resp.read().decode())
    print(f"Success: {json.dumps(data, indent=2)[:300]}")
except Exception as e:
    print(f"Error: {e}")

# Test /api/paper/trades?limit=50 with token
print("\n=== /api/paper/trades?limit=50 with token ===")
try:
    req = urllib.request.Request(
        'http://127.0.0.1:8000/api/paper/trades?limit=50',
        headers={"Authorization": f"Bearer {token}"}
    )
    resp = urllib.request.urlopen(req)
    data = json.loads(resp.read().decode())
    print(f"Success: {json.dumps(data, indent=2)[:300]}")
except Exception as e:
    print(f"Error: {e}")
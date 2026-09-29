import jwt
import sys
from datetime import datetime, timedelta
sys.path.insert(0, r'C:\Users\Hello\OneDrive\Documents\Default Project\backend')
from app.core.config import settings

payload = {
    "sub": "test-user-123",
    "email": "test@example.com",
    "exp": datetime.utcnow() + timedelta(minutes=settings.JWT_EXPIRE_MINUTES),
    "iat": datetime.utcnow(),
}
token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
print(f"Token: {token}")
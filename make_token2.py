import jwt
from datetime import datetime, timedelta
import sys
sys.path.insert(0, r'C:\Users\Hello\OneDrive\Documents\Default Project\backend')
from app.core.config import settings

payload = {
    "sub": "16f07ece93c6436f",
    "email": "test@example.com",
    "exp": datetime.utcnow() + timedelta(minutes=settings.JWT_EXPIRE_MINUTES),
    "iat": datetime.utcnow(),
}
token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
print(f"Token: {token}")
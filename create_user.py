import asyncio
import sys
from sqlalchemy import select, text
sys.path.insert(0, r'C:\Users\Hello\OneDrive\Documents\Default Project\backend')

from app.core.database import engine, init_db, async_session
from app.models.models import User

async def create_user():
    await init_db()
    async with async_session() as db:
        # Check if user already exists
        result = await db.execute(select(User).where(User.email == "test@example.com"))
        user = result.scalar_one_or_none()
        if user:
            print("User already exists")
            return user
        
        user = User(
            email="test@example.com",
            name="Test User",
            auth_provider="google",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        print(f"Created user: {user.id}")
        return user

asyncio.run(create_user())
import sys
sys.path.insert(0, r'C:\Users\Hello\OneDrive\Documents\Default Project\backend')

from app.core.database import engine, init_db, async_session
from app.models.models import User
from sqlalchemy import select
import asyncio

async def test():
    await init_db()
    async with async_session() as db:
        # Check if default user exists
        result = await db.execute(select(User).where(User.email == "default@intradayai.local"))
        user = result.scalar_one_or_none()
        if user:
            print(f"Default user exists: {user.id} - {user.email}")
        else:
            # Create the user
            user = User(
                email="default@intradayai.local",
                name="Default User",
                auth_provider="system",
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            print(f"Created default user: {user.id} - {user.email}")

asyncio.run(test())
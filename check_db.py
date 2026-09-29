import asyncio
import sys
from sqlalchemy import select
sys.path.insert(0, r'C:\Users\Hello\OneDrive\Documents\Default Project\backend')

from app.core.database import async_session, init_db
from app.models.models import User

async def check():
    await init_db()
    async with async_session() as db:
        result = await db.execute(select(User))
        users = result.scalars().all()
        print(f"Users in DB: {len(users)}")
        for u in users:
            print(f"  - {u.id}: {u.email}")

asyncio.run(check())
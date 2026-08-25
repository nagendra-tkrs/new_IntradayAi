import asyncio
from app.services.daily_improvement import DailyImprovementFramework

async def main():
    framework = DailyImprovementFramework()
    await framework.run_daily()

asyncio.run(main())

import os, asyncio, sys, datetime
sys.path.insert(0, '/app/backend')
from dotenv import load_dotenv
load_dotenv('/app/backend/.env')
from motor.motor_asyncio import AsyncIOMotorClient


async def m():
    db = AsyncIOMotorClient(os.environ['MONGO_URL'])[os.environ['DB_NAME']]
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)).isoformat()
    print('rh launches detected_at >= 24h ago:', await db.launches.count_documents({'chain': 'rh', 'detected_at': {'$gte': since}}))
    cur = db.launches.find({'chain': 'rh', 'detected_at': {'$gte': since}}, {'_id': 0, 'symbol': 1, 'usd_market_cap': 1, 'unique_buyers': 1, 'curve_fill_pct': 1, 'rh_gate': 1, 'detected_at': 1, 'backfilled': 1}).sort('usd_market_cap', -1).limit(12)
    async for l in cur:
        print(l)

asyncio.run(m())

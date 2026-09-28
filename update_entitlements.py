from app.database import get_db
from datetime import datetime, timezone

db = get_db()
now = datetime.now(timezone.utc)

# Update entitlements for employers with sponsored jobs
employer_ids = ['6aabb69d29f7669c8320fb0d', '6aacd9578c2d41e3bd245f40']
for employer_id in employer_ids:
    db.entitlement_cache.update_one(
        {"employer_id": employer_id},
        {"$set": {"tier": "enterprise", "cached_at": now, "last_event": "upgrade"}},
        upsert=True
    )
    print(f"Updated {employer_id} to enterprise tier")

print("Done")

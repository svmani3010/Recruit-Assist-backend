"""Dev helper: clear stuck OTP / check-mobile rate-limit events."""
from __future__ import annotations

import os
import sys

ROOT = r"C:\Users\DELL\OneDrive\Desktop\Fanisko\Recruit_assist_website\Github\RecruitAssist-backend"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from app.database import get_db  # noqa: E402


def main() -> None:
    db = get_db()
    col = db["otp_send_events"]
    query = {
        "$or": [
            {"account_id": "6ab245ef9cc8c99ed0ef08e9"},
            {"account_id": "6ab23bacf75dfebcee462baf"},
            {"account_id": "6aacd5f6a00aa95896dbf197"},
            {"purpose": "candidate_mobile_dup_check"},
        ]
    }
    before = col.count_documents({})
    deleted = col.delete_many(query)
    print(f"deleted={deleted.deleted_count} remaining={col.count_documents({})} before={before}")


if __name__ == "__main__":
    main()

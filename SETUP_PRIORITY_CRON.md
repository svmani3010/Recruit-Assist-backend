# Priority Listing System - SLA Cron Job Setup

## Overview
The priority listing system includes a daily SLA (Service Level Agreement) report that monitors employer performance and flags any that fall below the 90% guaranteed hit rate.

## Running the SLA Report

### Manual Execution
```bash
python run_daily_sla_report.py
```

### Automated Scheduling

#### Windows Task Scheduler
1. Open Task Scheduler (taskschd.msc)
2. Create a new task:
   - Name: "Priority Listing SLA Report"
   - Trigger: Daily at 2:00 AM
   - Action: Start a program
     - Program: `python`
     - Arguments: `run_daily_sla_report.py`
     - Start in: `C:\Users\DELL\OneDrive\Desktop\Fanisko\Recruit_assist_website\Github\RecruitAssist-backend`

#### Linux/Mac Cron Job
```bash
# Edit crontab
crontab -e

# Add this line to run daily at 2:00 AM
0 2 * * * cd /path/to/RecruitAssist-backend && python run_daily_sla_report.py >> /var/log/priority_sla.log 2>&1
```

#### CI/CD Pipeline
Add to your existing CI/CD pipeline:
```yaml
# Example for GitHub Actions
- name: Run Priority SLA Report
  run: |
    cd RecruitAssist-backend
    python run_daily_sla_report.py
```

## What the SLA Report Does

1. **Analyzes Impressions**: Examines the last 24 hours of job impressions for priority jobs
2. **Calculates Hit Rates**: For each employer, calculates what percentage of their priority jobs appeared in guaranteed positions
3. **Flags Violations**: Any employer with <90% guaranteed hit rate gets flagged
4. **Stores Results**: Results are stored in the `admin_sla_flags` collection for review

## Admin Monitoring

### View SLA Flags via API
```bash
curl -X GET "http://localhost:8000/api/admin/reports/priority-sla" \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN"
```

### View SLA Flags in Database
```python
from app.database import get_db

db = get_db()
flags = list(db.admin_sla_flags.find().sort("date", -1))
for flag in flags:
    print(f"Employer: {flag['employer_id']}, Hit Rate: {flag['guaranteed_hit_rate']:.2%}")
```

## Troubleshooting

### No Results Expected Initially
When first setting up the system, you may see "No SLA violations detected" because:
- The system needs at least 24 hours of impression data
- Employers need to have priority impressions recorded
- The hit rate calculation requires sufficient data points

### Verify Collections Exist
```python
from app.database import get_db
db = get_db()
print("Collections:", db.list_collection_names())
```

### Check Recent Impressions
```python
from app.database import get_db
from datetime import datetime, timedelta, timezone

db = get_db()
since = datetime.now(timezone.utc) - timedelta(days=1)
recent = list(db.impressions.find({"ts": {"$gte": since}}).limit(10))
print(f"Recent impressions in last 24h: {len(recent)}")
```

## Maintenance

### Clean Old SLA Flags
```python
from app.database import get_db
from datetime import datetime, timedelta, timezone

db = get_db()
# Remove flags older than 90 days
cutoff = datetime.now(timezone.utc) - timedelta(days=90)
result = db.admin_sla_flags.delete_many({"date": {"$lt": cutoff.date().isoformat()}})
print(f"Deleted {result.deleted_count} old SLA flags")
```

### Clean Old Impressions
```python
from app.database import get_db
from datetime import datetime, timedelta, timezone

db = get_db()
# Remove impressions older than 180 days
cutoff = datetime.now(timezone.utc) - timedelta(days=180)
result = db.impressions.delete_many({"ts": {"$lt": cutoff}})
print(f"Deleted {result.deleted_count} old impressions")
```

"""
Daily SLA Report Script
-----------------------
This script runs the daily SLA report for the priority listing system.
It can be scheduled using external tools like:
- Windows Task Scheduler
- Cron jobs (Linux/Mac)
- External CI/CD pipelines

Usage:
    python run_daily_sla_report.py

The script will:
1. Run the daily SLA analysis
2. Flag employers with <90% guaranteed hit rate
3. Store results in the admin_sla_flags collection
4. Print a summary of the results
"""

import sys
from datetime import datetime

# Add the app directory to the path
sys.path.insert(0, '.')

from app.routers.routes_priority import daily_sla_report


def main():
    print(f"Running daily SLA report - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("-" * 50)
    
    try:
        flags = daily_sla_report()
        
        print(f"SLA analysis complete")
        print(f"Total employers analyzed: {len(flags) if flags else 0}")
        
        if flags:
            print(f"Employers flagged for SLA violations: {len(flags)}")
            print("\nFlagged employers:")
            for flag in flags:
                print(f"  - Employer ID: {flag['employer_id']}")
                print(f"    Reason: {flag['reason']}")
                print(f"    Hit Rate: {flag['guaranteed_hit_rate']:.2%}")
                print(f"    Date: {flag['date']}")
                print()
        else:
            print("No SLA violations detected - all employers meeting 90%+ hit rate")
        
        print("-" * 50)
        print("Daily SLA report completed successfully")
        
    except Exception as e:
        print(f"Error running SLA report: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

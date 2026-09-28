"""
Migration script to add employer_id field to existing job documents.

This script ensures all jobs in the jobview collection have an employer_id field.
For jobs that don't have employer_id, it will use hr_user_id as a fallback.
"""

from app.database import get_db


def migrate_employer_id():
    """
    Add employer_id field to job documents that are missing it.
    Uses hr_user_id as a fallback for existing jobs.
    """
    db = get_db()
    
    # Find all jobs without employer_id field
    query = {"employer_id": {"$exists": False}}
    jobs_without_employer_id = list(db["jobview"].find(query))
    
    print(f"Found {len(jobs_without_employer_id)} jobs without employer_id")
    
    if not jobs_without_employer_id:
        print("All jobs already have employer_id field")
        return
    
    # Update each job to add employer_id
    updated_count = 0
    for job in jobs_without_employer_id:
        job_id = job.get("jobid")
        hr_user_id = job.get("hr_user_id")
        
        if not job_id:
            print(f"Skipping job without jobid: {job.get('_id')}")
            continue
        
        # Use hr_user_id as employer_id if available, otherwise use "unknown"
        employer_id = hr_user_id if hr_user_id else "unknown"
        
        result = db["jobview"].update_one(
            {"jobid": job_id, "employer_id": {"$exists": False}},
            {"$set": {"employer_id": employer_id}}
        )
        
        if result.modified_count > 0:
            updated_count += 1
            print(f"Updated job {job_id} with employer_id: {employer_id}")
    
    print(f"Migration complete. Updated {updated_count} jobs")


if __name__ == "__main__":
    migrate_employer_id()

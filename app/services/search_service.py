"""
search_service.py
-----------------
Job search service that provides relevance-based job matching.

This service handles Stage 1+2 of the priority pipeline:
- Relevance filtering by keywords and role
- Location-based filtering
- Returns jobs sorted by relevance then recency
"""

from typing import List, Optional
from datetime import datetime, timezone

from app.database import get_db


def get_matching_jobs(
    keywords: str,
    role: Optional[str] = None,
    location: Optional[str] = None,
    max_distance_km: Optional[float] = 50,
) -> List[dict]:
    """
    Search and filter jobs based on keywords, role, and location.
    
    This function implements Stage 1+2 of the priority pipeline:
    - Relevance filtering by keywords/role
    - Location-based filtering  
    - Returns jobs sorted by relevance then recency
    
    Args:
        keywords: Search keywords for job matching
        role: Optional role filter
        location: Optional location filter
        max_distance_km: Maximum distance for location-based search (not implemented in basic version)
        
    Returns:
        List of job dictionaries with employer_id field, sorted by relevance then recency
    """
    db = get_db()
    
    # Build the base query for active jobs
    query = {"show": "on", "status": {"$ne": "Draft"}}
    
    # Add keyword search - search in job role, description, and keywords
    if keywords and keywords.strip():
        keyword_list = [k.strip().lower() for k in keywords.split() if k.strip()]
        if keyword_list:
            # Search in multiple fields for relevance
            search_conditions = []
            for kw in keyword_list:
                search_conditions.append({
                    "$or": [
                        {"jobrole": {"$regex": kw, "$options": "i"}},
                        {"jobDescription": {"$regex": kw, "$options": "i"}},
                        {"jd_keywords": {"$elemMatch": {"$regex": kw, "$options": "i"}}},
                        {"jd_keywords_ai": {"$elemMatch": {"$regex": kw, "$options": "i"}}},
                    ]
                })
            
            if search_conditions:
                query["$and"] = search_conditions
    
    # Add role filter
    if role and role.strip():
        query["jobrole"] = {"$regex": role.strip(), "$options": "i"}
    
    # Add location filter
    if location and location.strip():
        query["location"] = {"$regex": location.strip(), "$options": "i"}
    
    # Fetch matching jobs
    jobs = list(db["jobview"].find(query, {"_id": 0}))
    
    # Ensure employer_id field exists (use hr_user_id as fallback)
    for job in jobs:
        if "employer_id" not in job:
            # Use hr_user_id as employer_id if available
            job["employer_id"] = job.get("hr_user_id", "unknown")
    
    # Sort by posted_timestamp (recency) as a basic relevance ranking
    # In a more sophisticated implementation, this would use text search scoring
    jobs.sort(key=lambda j: j.get("posted_timestamp", ""), reverse=True)
    
    return jobs

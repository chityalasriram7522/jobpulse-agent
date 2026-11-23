"""
JobPulse Tools Module
---------------------
Contains helper functions for:
1. Fetching job listings from the Adzuna API (India endpoint).
2. Sending digest messages via Telegram Bot API (for Stage 4).
"""

import os
import sys
import logging
from typing import Any, Dict, List, Optional
import requests
from dotenv import load_dotenv

# Load local environment variables from .env if present
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("jobpulse.tools")

# Adzuna API constants for India
ADZUNA_BASE_URL = "https://api.adzuna.com/v1/api/jobs/in/search"


def fetch_jobs(
    query: str,
    location: str = "Hyderabad",
    results_per_page: int = 20,
    page: int = 1
) -> List[Dict[str, Any]]:
    """
    Fetch job listings from the Adzuna Jobs API for India.

    Parameters:
        query: Keywords or job title to search for (e.g. 'Python Developer', 'Java Developer').
        location: City or region in India (default: 'Hyderabad').
        results_per_page: Number of listings to retrieve per request (default: 20).
        page: Page number for pagination (default: 1).

    Returns:
        List of standardized job dictionaries containing:
        - id: Unique Adzuna job ID
        - title: Job title
        - company: Employer / company name
        - location: Formatted location string
        - description: Snippet or full job description
        - salary_min: Minimum salary if provided (or None)
        - salary_max: Maximum salary if provided (or None)
        - redirect_url: Canonical link to apply / view posting
        - created: Posting timestamp
        - contract_type: e.g. 'permanent', 'contract'
        - contract_time: e.g. 'full_time', 'part_time'
    """
    app_id = os.getenv("ADZUNA_APP_ID")
    app_key = os.getenv("ADZUNA_APP_KEY")

    if not app_id or not app_key:
        error_msg = (
            "Missing Adzuna API credentials. "
            "Please set ADZUNA_APP_ID and ADZUNA_APP_KEY in your .env file or environment."
        )
        logger.error(error_msg)
        raise ValueError(error_msg)

    endpoint = f"{ADZUNA_BASE_URL}/{page}"
    params = {
        "app_id": app_id,
        "app_key": app_key,
        "what": query,
        "where": location,
        "results_per_page": results_per_page,
        "content-type": "application/json",
    }

    logger.info("Fetching jobs from Adzuna: query='%s', location='%s', page=%d", query, location, page)

    try:
        response = requests.get(endpoint, params=params, timeout=15)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.error("Adzuna API request failed: %s", exc)
        raise RuntimeError(f"Adzuna API request failed: {exc}") from exc

    data = response.json()
    raw_results = data.get("results", [])
    logger.info("Retrieved %d listings from Adzuna", len(raw_results))

    jobs: List[Dict[str, Any]] = []
    for item in raw_results:
        company_info = item.get("company", {})
        location_info = item.get("location", {})
        
        # Standardize job structure
        job_record = {
            "id": str(item.get("id", "")),
            "title": (item.get("title") or "Untitled Position").strip(),
            "company": (company_info.get("display_name") or "Unknown Company").strip(),
            "location": (location_info.get("display_name") or location).strip(),
            "description": (item.get("description") or "").strip(),
            "salary_min": item.get("salary_min"),
            "salary_max": item.get("salary_max"),
            "contract_type": item.get("contract_type"),
            "contract_time": item.get("contract_time"),
            "redirect_url": item.get("redirect_url", ""),
            "created": item.get("created", ""),
        }
        jobs.append(job_record)

    return jobs


def send_telegram(message: str) -> bool:
    """
    Placeholder for Telegram delivery (fully implemented in Stage 4).
    """
    logger.info("Telegram placeholder called: %d characters", len(message))
    return True


if __name__ == "__main__":
    print("=" * 60)
    print("JobPulse - Stage 1: Testing fetch_jobs()")
    print("=" * 60)

    # Check for credentials
    test_app_id = os.getenv("ADZUNA_APP_ID")
    test_app_key = os.getenv("ADZUNA_APP_KEY")

    if not test_app_id or not test_app_key:
        print("\n[!] Notice: ADZUNA_APP_ID or ADZUNA_APP_KEY is not set.")
        print("    To fetch live jobs from Adzuna:")
        print("    1. Sign up at https://developer.adzuna.com/")
        print("    2. Copy .env.example to .env and fill in your keys.")
        print("\nDisplaying sample raw listings format for demonstration:\n")

        # Provide sample listings showcasing the exact structure
        sample_jobs = [
            {
                "id": f"sample-{i}",
                "title": f"{title} #{i}",
                "company": comp,
                "location": "Hyderabad, Telangana",
                "description": desc,
                "salary_min": salary,
                "salary_max": salary + 100000 if salary else None,
                "contract_type": "permanent",
                "contract_time": "full_time",
                "redirect_url": f"https://www.adzuna.in/details/sample-{i}",
                "created": "2026-11-23T10:00:00Z",
            }
            for i, (title, comp, desc, salary) in enumerate([
                ("Junior Python Developer", "TechPulse Labs", "Seeking Python developer with FastAPI and SQLite knowledge.", 500000),
                ("Java Software Engineer", "Infosys Solutions", "Core Java, Spring Boot, RESTful APIs, and database experience.", 650000),
                ("Full Stack Developer (React / Python)", "Global Dynamics", "Full-stack engineer building modern responsive apps.", 800000),
                ("AI / LLM Intern", "Cognitive Ventures", "Internship with 25,000 INR/month stipend. Working with Gemini & LangChain.", 300000),
            ] * 5, start=1)  # 20 listings
        ]

        for idx, job in enumerate(sample_jobs, start=1):
            salary_str = f"₹{job['salary_min']:,}" if job['salary_min'] else "Not disclosed"
            print(f"[{idx:02d}] {job['title']}")
            print(f"     Company : {job['company']}")
            print(f"     Location: {job['location']}")
            print(f"     Salary  : {salary_str}")
            print(f"     URL     : {job['redirect_url']}")
            print("-" * 60)
        print(f"\nSuccessfully demonstrated {len(sample_jobs)} raw listings structure.")
        sys.exit(0)

    try:
        query_role = "Python Developer"
        query_loc = "Hyderabad"
        print(f"Fetching 20 listings for '{query_role}' in '{query_loc}'...")
        listings = fetch_jobs(query=query_role, location=query_loc, results_per_page=20)
        print(f"\nSuccessfully fetched {len(listings)} listings from Adzuna:\n")
        for idx, job in enumerate(listings, start=1):
            sal_min = job.get("salary_min")
            sal_max = job.get("salary_max")
            sal_text = f"₹{sal_min:,} - ₹{sal_max:,}" if sal_min and sal_max else (f"₹{sal_min:,}" if sal_min else "Not specified")
            print(f"[{idx:02d}] {job['title']}")
            print(f"     Company : {job['company']}")
            print(f"     Location: {job['location']}")
            print(f"     Salary  : {sal_text}")
            print(f"     URL     : {job['redirect_url']}")
            print("-" * 60)
    except Exception as e:
        print(f"[!] Error fetching jobs: {e}")
        sys.exit(1)

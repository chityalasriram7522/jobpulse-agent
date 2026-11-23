"""
JobPulse Main Agent
-------------------
Autonomous daily agent that:
1. Loads candidate resume and preferences.
2. Fetches fresh listings via tools.fetch_jobs().
3. Uses Google Gemini with structured JSON output to match and rank the best 5 jobs.
4. Includes retry logic for JSON validation.
5. Supports --dry-run for local inspection without side effects.
"""

import os
import sys
import json
import re
import argparse
import logging
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Import local tools
from tools import fetch_jobs, send_telegram

# Load local environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("jobpulse.agent")

# System prompt specified for JobPulse matching
SYSTEM_PROMPT = (
    "You are a job-matching agent. Compare each job to the resume. "
    "Return exactly 5 best matches as a JSON array. "
    "Each item has: title, company, location, stipend_or_salary, match_reason (one sentence), link. "
    "Internships MUST list a stipend; skip internships with no stipend shown. "
    "Skip anything unrelated to the resume. Return only JSON."
)


class JobMatch(BaseModel):
    """Schema for each matched job returned by the Gemini AI brain."""
    title: str = Field(description="Exact job title from the posting")
    company: str = Field(description="Company or employer name")
    location: str = Field(description="Job location or Remote")
    stipend_or_salary: str = Field(description="Salary or stipend information from the posting")
    match_reason: str = Field(description="One sentence explaining why this matches the resume and preferences")
    link: str = Field(description="Direct URL to apply or view the job posting")


def load_preferences(filepath: str = "preferences.json") -> Dict[str, Any]:
    """Load search preferences from JSON file."""
    if not os.path.exists(filepath):
        logger.warning("Preferences file '%s' not found. Using defaults.", filepath)
        return {
            "locations": ["Hyderabad", "remote"],
            "roles": ["Python Developer", "Java Developer", "Full Stack Developer", "AI LLM Engineer"],
            "job_types": ["internship", "full-time"],
            "stipend_required_for_internship": True,
            "max_listings_per_run": 30
        }
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def load_resume(filepath: str = "resume.txt") -> str:
    """Load resume text from file."""
    if not os.path.exists(filepath):
        logger.warning("Resume file '%s' not found. Using fallback text.", filepath)
        return "Experienced Python and Java developer with interest in AI/LLMs and full-stack development."
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read().strip()


def validate_job_matches(
    raw_data: Any,
    allowed_links: Optional[Set[str]] = None,
    max_matches: int = 5
) -> List[Dict[str, str]]:
    """
    Validate that raw_data is a valid list of JobMatch dictionaries.
    Ensures required keys are present and non-empty, and verifies links exist in source jobs.
    """
    if isinstance(raw_data, str):
        # Strip potential markdown code fences ```json ... ```
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw_data.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned.strip())
        data = json.loads(cleaned)
    elif isinstance(raw_data, list):
        data = raw_data
    else:
        raise ValueError(f"Expected JSON array or string, got {type(raw_data).__name__}")

    if not isinstance(data, list):
        raise ValueError(f"Expected a list of job matches, got {type(data).__name__}")

    validated: List[Dict[str, str]] = []
    required_fields = ["title", "company", "location", "stipend_or_salary", "match_reason", "link"]

    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Item at index {idx} is not a dictionary: {item}")

        # Check required fields
        for field in required_fields:
            if field not in item or not str(item[field]).strip():
                raise ValueError(f"Item at index {idx} is missing required field '{field}'")

        link = str(item["link"]).strip()
        # Verify link wasn't fabricated if allowed_links is provided
        if allowed_links and link not in allowed_links:
            # Check for close match or substring
            matched_link = next((al for al in allowed_links if link in al or al in link), None)
            if matched_link:
                item["link"] = matched_link
            else:
                logger.warning("Link '%s' was not in original job listings; keeping for review.", link)

        validated.append({
            "title": str(item["title"]).strip(),
            "company": str(item["company"]).strip(),
            "location": str(item["location"]).strip(),
            "stipend_or_salary": str(item["stipend_or_salary"]).strip(),
            "match_reason": str(item["match_reason"]).strip(),
            "link": link
        })

    # Enforce maximum of 5 matches
    return validated[:max_matches]


def match_and_rank_jobs(
    jobs: List[Dict[str, Any]],
    resume_text: str,
    preferences: Dict[str, Any],
    max_matches: int = 5
) -> List[Dict[str, str]]:
    """
    Send resume, preferences, and fetched jobs to Gemini API.
    Enforces structured JSON output and retries once if output is invalid.
    """
    if not jobs:
        logger.info("No jobs to match.")
        return []

    # Import google-genai
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        logger.error("google-genai package not found. Run: pip install google-genai")
        raise e

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY. Please set it in your .env file or environment.")

    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    client = genai.Client(api_key=api_key)

    # Prepare available links for validation
    allowed_links = {j.get("redirect_url") for j in jobs if j.get("redirect_url")}

    # Minimize prompt tokens by extracting relevant fields
    condensed_jobs = [
        {
            "id": j.get("id"),
            "title": j.get("title"),
            "company": j.get("company"),
            "location": j.get("location"),
            "salary_min": j.get("salary_min"),
            "salary_max": j.get("salary_max"),
            "contract_type": j.get("contract_type"),
            "contract_time": j.get("contract_time"),
            "description": (j.get("description") or "")[:400],  # first 400 chars
            "link": j.get("redirect_url")
        }
        for j in jobs
    ]

    user_prompt = (
        f"--- CANDIDATE RESUME ---\n{resume_text}\n\n"
        f"--- CANDIDATE PREFERENCES ---\n{json.dumps(preferences, indent=2)}\n\n"
        f"--- AVAILABLE JOB LISTINGS ({len(condensed_jobs)} total) ---\n"
        f"{json.dumps(condensed_jobs, indent=2)}\n\n"
        "Please select the best 5 matching jobs according to the system instructions."
    )

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=list[JobMatch],
        temperature=0.2,
    )

    # Attempt 1: Call Gemini
    last_error: Optional[Exception] = None
    for attempt in (1, 2):
        logger.info("Sending %d jobs to Gemini (%s) for ranking [Attempt %d/2]...", len(jobs), model_name, attempt)
        try:
            current_prompt = user_prompt
            if attempt == 2 and last_error:
                current_prompt += f"\n\nNOTE: Your previous response was invalid with error: {last_error}. Please correct it and return only a valid JSON array matching the schema."

            response = client.models.generate_content(
                model=model_name,
                contents=current_prompt,
                config=config
            )

            raw_text = response.text or ""
            logger.debug("Raw Gemini response: %s", raw_text)

            matches = validate_job_matches(raw_text, allowed_links=allowed_links, max_matches=max_matches)
            logger.info("Gemini successfully matched and ranked %d jobs", len(matches))
            return matches

        except Exception as exc:
            logger.warning("Attempt %d failed with error: %s", attempt, exc)
            last_error = exc
            if attempt == 2:
                logger.error("Failed to obtain valid JSON from Gemini after 2 attempts.")
                raise RuntimeError(f"Gemini job matching failed: {last_error}") from last_error

    return []


def format_digest(matches: List[Dict[str, str]]) -> str:
    """Format matched jobs into a clean, readable text digest."""
    if not matches:
        return "JobPulse Daily Digest: No matching openings found today."

    lines = [
        "✨ *JobPulse Daily Job & Internship Digest*",
        f"Found {len(matches)} matching opening{'s' if len(matches) != 1 else ''} for you today:\n"
    ]

    for idx, job in enumerate(matches, start=1):
        lines.append(f"{idx}. *{job['title']}* @ *{job['company']}*")
        lines.append(f"   📍 Location: {job['location']}")
        lines.append(f"   💰 Compensation: {job['stipend_or_salary']}")
        lines.append(f"   💡 Match Reason: {job['match_reason']}")
        lines.append(f"   🔗 [View & Apply]({job['link']})\n")

    return "\n".join(lines).strip()


def run_agent(dry_run: bool = False) -> int:
    """
    Main agent execution flow for JobPulse.
    Returns 0 on success, non-zero on failure.
    """
    logger.info("Starting JobPulse Agent (dry_run=%s)", dry_run)

    # Step 1: Load inputs
    try:
        preferences = load_preferences()
        resume_text = load_resume()
    except Exception as e:
        logger.error("Failed to load preferences or resume: %s", e)
        return 1

    # Step 2: Fetch jobs from Adzuna
    roles = preferences.get("roles", ["Python Developer"])
    locations = preferences.get("locations", ["Hyderabad"])
    primary_location = locations[0] if locations else "Hyderabad"

    all_jobs: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()

    adzuna_id = os.getenv("ADZUNA_APP_ID")
    adzuna_key = os.getenv("ADZUNA_APP_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")

    # If keys are missing and running in dry-run, provide fallback demonstration
    if dry_run and (not adzuna_id or not adzuna_key or not gemini_key):
        print("\n" + "=" * 65)
        print("JobPulse --dry-run (Demonstration Mode - API Keys not yet configured)")
        print("=" * 65)
        print("Tip: Add GEMINI_API_KEY, ADZUNA_APP_ID, and ADZUNA_APP_KEY to .env for live API queries.")
        print("\nSimulating Gemini ranking for 5 matching jobs:\n")

        mock_matches = [
            {
                "title": "Junior Python / Backend Developer",
                "company": "TechPulse Labs",
                "location": "Hyderabad, Telangana",
                "stipend_or_salary": "₹5,50,000 / year",
                "match_reason": "Matches your strong Python background, RESTful API development, and SQLite database skills.",
                "link": "https://www.adzuna.in/details/sample-101"
            },
            {
                "title": "AI / GenAI Engineering Intern",
                "company": "Cognitive AI Systems",
                "location": "Hyderabad (Hybrid)",
                "stipend_or_salary": "₹25,000 / month",
                "match_reason": "Direct match for your experience with Gemini API, prompt engineering, and agentic workflows.",
                "link": "https://www.adzuna.in/details/sample-102"
            },
            {
                "title": "Full-Stack Developer (React & Python)",
                "company": "Nexus Innovations",
                "location": "Remote, India",
                "stipend_or_salary": "₹7,000,000 / year",
                "match_reason": "Aligns with your demonstrated ability building React frontends and Python/Java backends.",
                "link": "https://www.adzuna.in/details/sample-103"
            },
            {
                "title": "Java Software Engineer",
                "company": "InnoTech Solutions",
                "location": "Hyderabad, Telangana",
                "stipend_or_salary": "₹6,00,000 / year",
                "match_reason": "Directly matches your Core Java, Spring Boot, and relational database project background.",
                "link": "https://www.adzuna.in/details/sample-104"
            },
            {
                "title": "Python & LLM Application Intern",
                "company": "HyperScale Labs",
                "location": "Hyderabad, Telangana",
                "stipend_or_salary": "₹30,000 / month",
                "match_reason": "Fulfills your preference for an AI internship offering a confirmed monthly stipend.",
                "link": "https://www.adzuna.in/details/sample-105"
            }
        ]

        digest_text = format_digest(mock_matches)
        print(digest_text)
        print("\n" + "=" * 65)
        print("Dry run completed successfully with 5 ranked jobs.")
        return 0

    # Live execution: Fetch ~30 listings across preferred roles
    listings_per_role = max(5, 30 // len(roles))
    for role in roles:
        try:
            listings = fetch_jobs(query=role, location=primary_location, results_per_page=listings_per_role)
            for j in listings:
                if j["id"] not in seen_ids:
                    seen_ids.add(j["id"])
                    all_jobs.append(j)
        except Exception as err:
            logger.error("Error fetching jobs for '%s': %s", role, err)

    if not all_jobs:
        logger.error("No jobs could be retrieved from Adzuna.")
        return 1

    logger.info("Total fresh listings retrieved: %d", len(all_jobs))

    # Step 3: Rank with Gemini
    try:
        matches = match_and_rank_jobs(
            jobs=all_jobs,
            resume_text=resume_text,
            preferences=preferences,
            max_matches=5
        )
    except Exception as exc:
        error_msg = f"JobPulse failed: Gemini matching error - {exc}"
        logger.error(error_msg)
        return 1

    # Step 4: Display or Deliver
    digest = format_digest(matches)

    if dry_run:
        print("\n" + "=" * 65)
        print("JobPulse --dry-run Output:")
        print("=" * 65)
        print(digest)
        print("=" * 65)
        return 0

    # Telegram send will be hooked up in Stage 4
    logger.info("Digest prepared with %d matches.", len(matches))
    return 0


def main():
    parser = argparse.ArgumentParser(description="JobPulse - AI Job & Internship Digest Agent")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the job digest to the terminal without saving to DB or sending to Telegram"
    )
    args = parser.parse_args()
    exit_code = run_agent(dry_run=args.dry_run)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

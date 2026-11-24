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

# Import local tools and database memory
from tools import fetch_jobs, send_telegram, send_notification, fetch_ai_news
import db

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


def sanitize_resume_text(text: str) -> str:
    """
    Sanitize resume text to protect user privacy (PII Redaction):
    Removes phone numbers, email addresses, and personal contact details.
    Ensures that only professional skills, education, and projects are evaluated.
    """
    # Scrub email addresses
    sanitized = re.sub(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", "[EMAIL REDACTED]", text)
    # Scrub phone numbers (international or standard 10-digit formats)
    sanitized = re.sub(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3,5}\)?[-.\s]?\d{3,5}[-.\s]?\d{3,5}", "[PHONE REDACTED]", sanitized)
    return sanitized


def load_resume(filepath: str = "resume.txt") -> str:
    """Load resume text from file and sanitize all personal contact PI."""
    if not os.path.exists(filepath):
        logger.warning("Resume file '%s' not found. Using fallback text.", filepath)
        return "Experienced Python and Java developer with interest in AI/LLMs and full-stack development."
    with open(filepath, "r", encoding="utf-8") as f:
        raw_text = f.read().strip()
        return sanitize_resume_text(raw_text)


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

    primary_model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
    # Model fallback list to ensure zero-downtime if a model experiences temporary high demand
    models_to_try = [primary_model]
    for fallback in ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.7-flash", "gemini-3.8-flash"]:
        if fallback not in models_to_try:
            models_to_try.append(fallback)

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

    last_error: Optional[Exception] = None
    for model_name in models_to_try:
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
                logger.info("Gemini (%s) successfully matched and ranked %d jobs", model_name, len(matches))
                return matches

            except Exception as exc:
                logger.warning("Model %s Attempt %d failed: %s", model_name, attempt, exc)
                last_error = exc
                err_str = str(exc)
                # If model is unavailable (503) or not found (404), break immediately to try next model
                if "503" in err_str or "UNAVAILABLE" in err_str or "404" in err_str:
                    logger.info("Model %s unavailable, switching to next model...", model_name)
                    break

    logger.error("Failed to obtain valid JSON from Gemini across models: %s", models_to_try)
    raise RuntimeError(f"Gemini job matching failed: {last_error}") from last_error

    return []


def format_digest(matches: List[Dict[str, str]], news: Optional[List[Dict[str, str]]] = None) -> str:
    """Format matched jobs and daily AI/tech news into a clean morning digest."""
    lines = [
        "✨ *JobPulse Daily Morning Briefing*",
        "===================================="
    ]

    # Section 1: Top Job Matches
    if matches:
        lines.append(f"\n💼 *Top {len(matches)} Matching Openings for Sri Ram:*\n")
        for idx, job in enumerate(matches, start=1):
            lines.append(f"{idx}. *{job['title']}* @ *{job['company']}*")
            lines.append(f"   📍 Location: {job['location']}")
            lines.append(f"   💰 Compensation: {job['stipend_or_salary']}")
            lines.append(f"   💡 Match Reason: {job['match_reason']}")
            lines.append(f"   🔗 [View & Apply]({job['link']})\n")
    else:
        lines.append("\n💼 *Job Openings:* No new unread openings in this run.\n")

    # Section 2: AI News & Current Affairs
    if news:
        lines.append("🌐 *Daily AI & Tech Current Affairs:*")
        for item in news[:3]:
            title = item.get("title", "").strip()
            link = item.get("link", "").strip()
            if link:
                lines.append(f"• [{title}]({link})")
            else:
                lines.append(f"• {title}")
        lines.append("")

    return "\n".join(lines).strip()


def run_agent(dry_run: bool = False, db_path: str = "sent_jobs.db") -> int:
    """
    Main agent execution flow for JobPulse.
    Returns 0 on success, non-zero on failure.
    """
    logger.info("Starting JobPulse Agent (dry_run=%s, db_path=%s)", dry_run, db_path)

    # Initialize SQLite memory
    db.init_db(db_path)

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

    # If Gemini API key is missing and running in dry-run, provide fallback demonstration
    if dry_run and not gemini_key:
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
        error_msg = "No fresh jobs could be retrieved from job sources."
        logger.error(error_msg)
        if not dry_run:
            send_notification(f"⚠️ JobPulse failed: {error_msg}", title="JobPulse Failed")
        return 1

    logger.info("Total fresh listings retrieved: %d", len(all_jobs))

    # Step 3: SQLite Deduplication Memory (Drop jobs already sent)
    unsent_jobs = db.filter_new_jobs(all_jobs, db_path=db_path)
    if not unsent_jobs:
        logger.info("All retrieved jobs have already been sent in previous runs. No new jobs today.")
        if dry_run:
            print("\n[JobPulse] All retrieved jobs have already been sent in previous runs. Zero duplicates found.")
        return 0

    logger.info("Unsent listings to rank with Gemini: %d", len(unsent_jobs))

    # Step 4: Rank with Gemini
    try:
        matches = match_and_rank_jobs(
            jobs=unsent_jobs,
            resume_text=resume_text,
            preferences=preferences,
            max_matches=5
        )
    except Exception as exc:
        error_msg = f"Gemini matching error: {exc}"
        logger.error("JobPulse failed: %s", error_msg)
        if not dry_run:
            send_notification(f"⚠️ JobPulse failed: {error_msg}", title="JobPulse Failed")
        return 1

    # Step 5: Fetch AI News & Format Morning Digest
    ai_news = fetch_ai_news(limit=3)
    digest = format_digest(matches, news=ai_news)

    if dry_run:
        print("\n" + "=" * 65)
        print("JobPulse --dry-run Output:")
        print("=" * 65)
        print(digest)
        print("=" * 65)
        print("\n[Notice] --dry-run active: Database and Notifications were NOT modified.")
        return 0

    # Step 6: Deliver Notification
    logger.info("Delivering digest to configured channels...")
    try:
        send_notification(digest, title="✨ JobPulse Daily Job Digest")
    except Exception as deliv_err:
        logger.error("Notification delivery failed: %s", deliv_err)
        return 1

    # Step 7: Save newly sent jobs to SQLite memory
    saved_count = db.save_sent_jobs(matches, db_path=db_path)
    logger.info("Digest completed with %d matches (saved %d to sent_jobs).", len(matches), saved_count)
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

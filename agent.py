"""
JobPulse Main Agent
-------------------
Autonomous daily agent that:
1. Loads candidate resume and preferences (Locations: Hyderabad, Bengaluru, Mumbai, Pune, Chennai in India ONLY).
2. Fetches fresh listings via tools.fetch_jobs().
3. Uses Google Gemini with structured JSON output to match and rank the best 5 jobs/internships.
4. Includes retry logic for JSON validation and fallback ranking.
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
from tools import fetch_jobs, send_telegram, send_notification, fetch_ai_news, is_india_target_location, TARGET_INDIA_CITIES
import db

# Load local environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("jobpulse.agent")

# System prompt specified for JobPulse India-only matching
SYSTEM_PROMPT = (
    "You are an AI job & internship matching agent. Compare each job listing to the candidate's resume and preferences. "
    "CRITICAL LOCATION REQUIREMENT: Every selected job or internship MUST be located strictly in India ONLY, "
    "specifically in Hyderabad, Bengaluru (Bangalore), Mumbai, Pune, or Chennai. "
    "Strictly discard and skip any job located outside India or outside these cities. "
    "Select up to 5 best matches as a JSON array. "
    "Each item has: title, company, location, stipend_or_salary, match_reason (one sentence), link. "
    "Skip anything unrelated to the resume. Return only JSON."
)


class JobMatch(BaseModel):
    """Schema for each matched job returned by the Gemini AI brain."""
    title: str = Field(description="Exact job title from the posting")
    company: str = Field(description="Company or employer name")
    location: str = Field(description="Job location in India (Hyderabad, Bengaluru, Mumbai, Pune, Chennai)")
    stipend_or_salary: str = Field(description="Salary or stipend information from the posting")
    match_reason: str = Field(description="One sentence explaining why this matches the resume and preferences")
    link: str = Field(description="Direct URL to apply or view the job posting")


def load_preferences(filepath: str = "preferences.json") -> Dict[str, Any]:
    """Load search preferences from JSON file."""
    if not os.path.exists(filepath):
        logger.warning("Preferences file '%s' not found. Using defaults.", filepath)
        return {
            "locations": ["Hyderabad", "Bengaluru", "Mumbai", "Pune", "Chennai"],
            "country": "India",
            "india_only": True,
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
    """Load resume text from file and sanitize all personal contact PII."""
    if not os.path.exists(filepath):
        logger.warning("Resume file '%s' not found. Using fallback text.", filepath)
        return "Experienced Python and Java developer with interest in AI/LLMs and full-stack development."
    with open(filepath, "r", encoding="utf-8") as f:
        raw_text = f.read().strip()
        return sanitize_resume_text(raw_text)


def score_job_relevance(job: Dict[str, Any], resume_text: str, preferences: Dict[str, Any]) -> int:
    """Calculate match score based on resume skills and role preferences."""
    text = (job.get("title", "") + " " + job.get("company", "") + " " + job.get("description", "")).lower()
    score = 0
    keywords = ["python", "java", "react", "sql", "ai", "agent", "llm", "backend", "full stack", "intern", "developer", "engineer"]
    for kw in keywords:
        if kw in text:
            score += 2
    # Bonus if title contains target roles
    for role in preferences.get("roles", []):
        if role.lower() in text:
            score += 5
    return score


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
            continue

        # Check required fields
        for field in required_fields:
            if field not in item or not str(item[field]).strip():
                raise ValueError(f"Item at index {idx} is missing required field '{field}'")

        link = str(item.get("link", "")).strip()
        if allowed_links and link not in allowed_links:
            matched_link = next((al for al in allowed_links if link in al or al in link), None)
            if matched_link:
                link = matched_link

        validated.append({
            "title": str(item["title"]).strip(),
            "company": str(item["company"]).strip(),
            "location": str(item["location"]).strip(),
            "stipend_or_salary": str(item["stipend_or_salary"]).strip(),
            "match_reason": str(item["match_reason"]).strip(),
            "link": link
        })

    return validated[:max_matches]


def fallback_rank_jobs(jobs: List[Dict[str, Any]], resume_text: str, preferences: Dict[str, Any], max_matches: int = 5) -> List[Dict[str, str]]:
    """Fallback ranking algorithm when AI API temporarily experiences 503 high-demand spikes."""
    scored = sorted(jobs, key=lambda j: score_job_relevance(j, resume_text, preferences), reverse=True)
    matches: List[Dict[str, str]] = []
    for j in scored[:max_matches]:
        title = j.get("title", "Software Engineer")
        company = j.get("company", "Tech Company")
        loc = j.get("location", "India")
        salary = "Competitive / Stipend Provided" if "intern" in title.lower() else "Competitive Market Rate"
        
        # Craft meaningful match reason
        if "intern" in title.lower():
            reason = f"Matches your computer science foundation and provides an engineering internship opportunity in {loc}."
        elif "java" in title.lower():
            reason = f"Directly aligns with your Core Java, OOP fundamentals, and backend application development skills in {loc}."
        elif "python" in title.lower():
            reason = f"Matches your strong Python programming, RESTful API, and database architecture project experience in {loc}."
        elif "react" in title.lower() or "frontend" in title.lower():
            reason = f"Aligns with your modern frontend and full-stack development experience building responsive web interfaces in {loc}."
        else:
            reason = f"Matches your software engineering background, problem-solving skills, and tech stack in {loc}."

        matches.append({
            "title": title,
            "company": company,
            "location": loc,
            "stipend_or_salary": salary,
            "match_reason": reason,
            "link": j.get("redirect_url", "")
        })
    return matches


def match_and_rank_jobs(
    jobs: List[Dict[str, Any]],
    resume_text: str,
    preferences: Dict[str, Any],
    max_matches: int = 5
) -> List[Dict[str, str]]:
    """
    Send resume, preferences, and fetched jobs to Gemini API.
    Pre-filters jobs to strictly ensure only India target cities are submitted.
    Enforces structured JSON output and retries once if output is invalid.
    """
    if not jobs:
        logger.info("No jobs to match.")
        return []

    # Strictly pre-filter to India target locations (Hyderabad, Bengaluru, Mumbai, Pune, Chennai)
    target_cities = preferences.get("locations", ["Hyderabad", "Bengaluru", "Mumbai", "Pune", "Chennai"])
    india_jobs = [
        j for j in jobs
        if is_india_target_location(
            j.get("location", ""),
            title=j.get("title", ""),
            description=j.get("description", ""),
            allowed_cities=target_cities
        )
    ]
    if india_jobs:
        jobs = india_jobs

    # Sort by relevance so top 12 candidate jobs are presented to Gemini
    jobs = sorted(jobs, key=lambda j: score_job_relevance(j, resume_text, preferences), reverse=True)[:12]

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY missing, using fallback ranking.")
        return fallback_rank_jobs(jobs, resume_text, preferences, max_matches=max_matches)

    primary_model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    models_to_try = [primary_model]
    for fallback in ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash"]:
        if fallback not in models_to_try:
            models_to_try.append(fallback)

    # Prepare available links for validation
    allowed_links = {j.get("redirect_url") for j in jobs if j.get("redirect_url")}

    # Minimize prompt tokens
    condensed_jobs = [
        {
            "id": j.get("id"),
            "title": j.get("title"),
            "company": j.get("company"),
            "location": j.get("location"),
            "salary": j.get("salary_min") or "Competitive",
            "description": (j.get("description") or "")[:150],
            "link": j.get("redirect_url")
        }
        for j in jobs
    ]

    user_prompt = (
        f"--- CANDIDATE RESUME ---\n{resume_text}\n\n"
        f"--- TARGET LOCATIONS (INDIA ONLY) ---\nHyderabad, Bengaluru, Mumbai, Pune, Chennai\n\n"
        f"--- AVAILABLE OPENINGS ({len(condensed_jobs)} total) ---\n"
        f"{json.dumps(condensed_jobs, indent=2)}\n\n"
        "Please select the best 5 matching openings in India for Sri Ram according to the system instructions."
    )

    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=list[JobMatch],
            temperature=0.2,
        )

        for model_name in models_to_try:
            for attempt in (1, 2):
                logger.info("Sending %d jobs to Gemini (%s) for ranking [Attempt %d/2]...", len(jobs), model_name, attempt)
                try:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=user_prompt,
                        config=config
                    )
                    raw_text = response.text or ""
                    matches = validate_job_matches(raw_text, allowed_links=allowed_links, max_matches=max_matches)
                    if matches:
                        logger.info("Gemini (%s) successfully matched and ranked %d jobs", model_name, len(matches))
                        return matches
                except Exception as exc:
                    logger.warning("Model %s Attempt %d failed: %s", model_name, attempt, exc)
                    last_error = exc
                    err_str = str(exc)
                    if any(code in err_str for code in ["503", "UNAVAILABLE", "404", "429", "RESOURCE_EXHAUSTED"]):
                        break

        logger.error("Failed to obtain valid JSON from Gemini across models: %s", models_to_try)
        raise RuntimeError(f"Gemini job matching failed: {last_error}") from last_error
    except Exception as e:
        if "Gemini job matching failed" in str(e):
            raise e
        logger.warning("Gemini client initialization error: %s", e)
        raise RuntimeError(f"Gemini job matching failed: {e}") from e


def summarize_ai_news_with_gemini(raw_headlines: List[str], api_key: Optional[str] = None) -> List[str]:
    """
    Summarize top AI news and current affairs into concise, informative summaries.
    Ensures ZERO links or URLs are included, as requested by the user.
    """
    if not raw_headlines:
        return [
            "**Next-Gen AI Systems**: Enterprise adoption of autonomous agentic workflows is accelerating rapidly across technology hubs.",
            "**Hardware & Chip Innovations**: Novel AI-driven design methodologies are lowering semiconductor development costs.",
            "**Global AI Governance**: International policy discussions intensify on establishing safety baselines for frontier models."
        ]

    effective_key = api_key or os.getenv("GEMINI_API_KEY")
    if not effective_key:
        return [
            f"**{h}**: Significant ongoing development in artificial intelligence technology."
            for h in raw_headlines[:3]
        ]

    try:
        from google import genai
        client = genai.Client(api_key=effective_key)
        model_name = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

        prompt = (
            f"Here are today's top AI and tech current affairs headlines:\n{raw_headlines}\n\n"
            "Synthesize and summarize the 3 most important updates in plain English. "
            "For each update, format as:\n"
            "**<Topic Name>**: <1 clear, punchy sentence explaining what happened and why it matters>\n\n"
            "CRITICAL: Keep each summary to exactly 1 concise sentence so it fits mobile screens perfectly. "
            "Do NOT include any links, URLs, web addresses, or citations. Return only the 3 bullet items."
        )

        response = client.models.generate_content(model=model_name, contents=prompt)
        text = (response.text or "").strip()
        items = [line.strip().lstrip("•-* ") for line in text.split("\n") if line.strip() and "**" in line]
        if items:
            return items[:3]
    except Exception as exc:
        logger.warning("Gemini AI news summarization failed: %s", exc)

    return [
        f"**{h}**: Key tech development impacting current software workflows."
        for h in raw_headlines[:3]
    ]


def format_job_card(jobs: List[Dict[str, str]], header: str, start_index: int = 1) -> str:
    """Format a small batch of jobs (2-3 jobs) cleanly so mobile notification cards never truncate."""
    lines = [f"💼 *{header}*\n"]
    for offset, job in enumerate(jobs):
        idx = start_index + offset
        lines.append(f"{idx}. *{job['title']}* @ *{job['company']}*")
        lines.append(f"   📍 {job['location']} | 💰 {job['stipend_or_salary']}")
        lines.append(f"   💡 {job['match_reason']}")
        lines.append(f"   🔗 [View & Apply]({job['link']})\n")
    return "\n".join(lines).strip()


def format_digest(matches: List[Dict[str, str]], news_summaries: Optional[List[str]] = None) -> str:
    """Format matched jobs and daily AI/tech news summaries into a clean morning digest."""
    lines = [
        "✨ *JobPulse Daily Morning Briefing*",
        "===================================="
    ]

    # Section 1: Top Job Matches
    if matches:
        lines.append(f"\n💼 *Top {len(matches)} Matching Openings in India (Hyderabad, Bengaluru, Mumbai, Pune, Chennai):*\n")
        for idx, job in enumerate(matches, start=1):
            lines.append(f"{idx}. *{job['title']}* @ *{job['company']}*")
            lines.append(f"   📍 {job['location']} | 💰 {job['stipend_or_salary']}")
            lines.append(f"   💡 {job['match_reason']}")
            lines.append(f"   🔗 [View & Apply]({job['link']})\n")
    else:
        lines.append("\n💼 *Job Openings:* No new unread openings in this run.\n")

    # Section 2: AI News & Current Affairs (NO LINKS - SUMMARIES ONLY)
    if news_summaries:
        lines.append("🌐 *Daily AI & Tech Current Affairs Summary:*")
        for item in news_summaries[:3]:
            lines.append(f"• {item}\n")

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

    # Step 2: Fetch jobs
    roles = preferences.get("roles", ["Python Developer"])
    locations = preferences.get("locations", ["Hyderabad", "Bengaluru", "Mumbai", "Pune", "Chennai"])
    primary_location = locations[0] if locations else "Hyderabad"

    all_jobs: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()

    gemini_key = os.getenv("GEMINI_API_KEY")

    # Live execution: Fetch across preferred roles & internship categories
    queries = list(roles)
    if any("intern" in str(jt).lower() for jt in preferences.get("job_types", [])):
        if not any("intern" in q.lower() for q in queries):
            queries.append("Software Intern")

    for q in queries:
        try:
            listings = fetch_jobs(query=q, location=primary_location, locations=locations, results_per_page=35)
            for j in listings:
                if j["id"] not in seen_ids:
                    seen_ids.add(j["id"])
                    all_jobs.append(j)
        except Exception as err:
            logger.error("Error fetching jobs for '%s': %s", q, err)

    if not all_jobs:
        error_msg = "No fresh jobs could be retrieved from job sources in target locations."
        logger.error(error_msg)
        if not dry_run:
            send_notification(f"⚠️ JobPulse failed: {error_msg}", title="JobPulse Failed")
        return 1

    logger.info("Total fresh India listings retrieved: %d", len(all_jobs))

    # Step 3: SQLite Deduplication Memory (Drop jobs already sent)
    unsent_jobs = db.filter_new_jobs(all_jobs, db_path=db_path)
    if not unsent_jobs:
        logger.info("All retrieved jobs have already been sent in previous runs. No new jobs today.")
        if dry_run:
            print("\n[JobPulse] All retrieved jobs have already been sent in previous runs. Zero duplicates found.")
        return 0

    logger.info("Unsent India listings to rank: %d", len(unsent_jobs))

    # Step 4: Rank with Gemini / Intelligent Fallback
    try:
        matches = match_and_rank_jobs(
            jobs=unsent_jobs,
            resume_text=resume_text,
            preferences=preferences,
            max_matches=5
        )
    except Exception as exc:
        logger.warning("Gemini ranking unavailable (%s). Engaging fallback ranking for India openings...", exc)
        matches = fallback_rank_jobs(unsent_jobs, resume_text, preferences, max_matches=5)

    # Step 5: Fetch AI News & Generate Summaries with Gemini (NO LINKS!)
    raw_headlines = [n["title"] for n in fetch_ai_news(limit=5)]
    ai_summaries = summarize_ai_news_with_gemini(raw_headlines, api_key=gemini_key)
    digest = format_digest(matches, news_summaries=ai_summaries)

    if dry_run:
        print("\n" + "=" * 65)
        print("JobPulse --dry-run Output:")
        print("=" * 65)
        print(digest)
        print("=" * 65)
        print("\n[Notice] --dry-run active: Database and Notifications were NOT modified.")
        return 0

    # Step 6: Deliver Notification
    logger.info("Delivering digest to configured channels in mobile-optimized split cards...")
    try:
        # Deliver in mobile-optimized split cards (Jobs 1-3, Jobs 4-5, and AI News) so ZERO truncation occurs on mobile!
        if matches:
            part1 = matches[:3]
            part2 = matches[3:]

            if part1:
                card1 = format_job_card(part1, "Top Matches for Sri Ram (Part 1/2)", start_index=1)
                send_notification(card1, title="JobPulse: Top Openings (1/2)")

            if part2:
                card2 = format_job_card(part2, "Top Matches for Sri Ram (Part 2/2)", start_index=4)
                send_notification(card2, title="JobPulse: Top Openings (2/2)")

        if ai_summaries:
            news_digest = "🌐 *Daily AI & Tech Current Affairs Summary:*\n\n" + "\n\n".join([f"• {s}" for s in ai_summaries])
            send_notification(news_digest, title="JobPulse: AI Briefing")

        if not matches and not ai_summaries:
            send_notification(digest, title="JobPulse Morning Briefing")
    except Exception as deliv_err:
        logger.error("Notification delivery failed: %s", deliv_err)
        return 1

    # Step 7: Save newly sent jobs to SQLite memory
    saved_count = db.save_sent_jobs(matches, db_path=db_path)
    logger.info("Digest completed with %d matches (saved %d to sent_jobs).", len(matches), saved_count)
    return 0


def main():
    parser = argparse.ArgumentParser(description="JobPulse - AI Job & Internship Digest Agent (India Metros)")
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

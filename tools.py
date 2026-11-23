"""
JobPulse Tools Module
---------------------
Helper functions for:
1. Fetching job listings:
   - Uses Adzuna Jobs API if ADZUNA_APP_ID & ADZUNA_APP_KEY are provided.
   - Automatically falls back to 100% Free Public Job APIs (Remotive / Arbeitnow)
     requiring ZERO API keys or registration when Adzuna credentials are not set!
2. Delivering Notifications:
   - send_telegram(): Delivers clean Markdown messages via Telegram Bot API.
   - send_ntfy(): Delivers push notifications directly to user's phone via ntfy.sh (No account required).
   - send_notification(): Unified dispatcher that delivers to all configured channels and handles error alerts.
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
# Free Public Job APIs (No API key needed)
REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
ARBEITNOW_URL = "https://www.arbeitnow.com/api/job-board-api"
# Default ntfy topic for phone push notifications
DEFAULT_NTFY_TOPIC = "jobpulse-sriram-chityala"


def _fetch_from_adzuna(
    query: str,
    location: str,
    results_per_page: int,
    page: int,
    app_id: str,
    app_key: str
) -> List[Dict[str, Any]]:
    """Fetch jobs from Adzuna Jobs API (India)."""
    endpoint = f"{ADZUNA_BASE_URL}/{page}"
    params = {
        "app_id": app_id,
        "app_key": app_key,
        "what": query,
        "where": location,
        "results_per_page": results_per_page,
        "content-type": "application/json",
    }
    logger.info("Calling Adzuna API (India): query='%s', where='%s'", query, location)
    response = requests.get(endpoint, params=params, timeout=15)
    response.raise_for_status()
    raw_results = response.json().get("results", [])

    jobs: List[Dict[str, Any]] = []
    for item in raw_results:
        company_info = item.get("company", {})
        location_info = item.get("location", {})
        jobs.append({
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
        })
    return jobs


def _fetch_from_free_apis(query: str, limit: int = 20) -> List[Dict[str, Any]]:
    """
    Fetch tech and developer jobs from 100% Free Public APIs (Remotive / Arbeitnow).
    Requires ZERO registration and ZERO API keys.
    """
    logger.info("Using Free Public Job API (Zero API key required) for query: '%s'", query)
    jobs: List[Dict[str, Any]] = []

    # 1. Try Remotive API
    try:
        params = {"category": "software-dev", "limit": limit}
        if query:
            params["search"] = query
        resp = requests.get(REMOTIVE_URL, params=params, timeout=15)
        if resp.status_code == 200:
            remotive_jobs = resp.json().get("jobs", [])
            for item in remotive_jobs:
                jobs.append({
                    "id": f"remotive-{item.get('id')}",
                    "title": item.get("title", "Software Developer"),
                    "company": item.get("company_name", "Tech Company"),
                    "location": item.get("candidate_required_location") or "Remote",
                    "description": item.get("description", "")[:600],
                    "salary_min": None,
                    "salary_max": None,
                    "contract_type": item.get("job_type", "full_time"),
                    "contract_time": "full_time",
                    "redirect_url": item.get("url", ""),
                    "created": item.get("publication_date", ""),
                })
    except Exception as exc:
        logger.warning("Remotive API request failed: %s", exc)

    # 2. If we need more jobs, try Arbeitnow API
    if len(jobs) < limit:
        try:
            resp = requests.get(ARBEITNOW_URL, timeout=15)
            if resp.status_code == 200:
                arbeit_jobs = resp.json().get("data", [])
                query_lower = query.lower() if query else ""
                for item in arbeit_jobs:
                    title = item.get("title", "")
                    if query_lower and (query_lower not in title.lower() and query_lower not in " ".join(item.get("tags", [])).lower()):
                        continue
                    jobs.append({
                        "id": f"arbeit-{item.get('slug')}",
                        "title": title,
                        "company": item.get("company_name", "Innovative Co"),
                        "location": item.get("location") or ("Remote" if item.get("remote") else "Hybrid"),
                        "description": item.get("description", "")[:600],
                        "salary_min": None,
                        "salary_max": None,
                        "contract_type": "full-time",
                        "contract_time": "full_time",
                        "redirect_url": item.get("url", ""),
                        "created": str(item.get("created_at", "")),
                    })
                    if len(jobs) >= limit:
                        break
        except Exception as exc:
            logger.warning("Arbeitnow API request failed: %s", exc)

    return jobs[:limit]


def fetch_jobs(
    query: str,
    location: str = "Hyderabad",
    results_per_page: int = 20,
    page: int = 1
) -> List[Dict[str, Any]]:
    """
    Fetch job listings:
    - If ADZUNA_APP_ID and ADZUNA_APP_KEY are set in .env, uses the Adzuna API for India.
    - Otherwise, automatically falls back to Free Public Job APIs requiring NO key!
    """
    app_id = os.getenv("ADZUNA_APP_ID")
    app_key = os.getenv("ADZUNA_APP_KEY")

    if app_id and app_key:
        try:
            return _fetch_from_adzuna(
                query=query,
                location=location,
                results_per_page=results_per_page,
                page=page,
                app_id=app_id,
                app_key=app_key
            )
        except Exception as e:
            logger.warning("Adzuna API call failed (%s). Falling back to Free Public Job API...", e)

    # Fallback to free public job API
    return _fetch_from_free_apis(query=query, limit=results_per_page)


def send_telegram(message: str) -> bool:
    """
    Send formatted digest or alert message via the Telegram Bot API.
    Enforces Telegram's 4096 character limit per message.
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        logger.warning("Telegram credentials not configured (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing).")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message[:4090],  # Keep safely under Telegram 4096 char limit
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }

    try:
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
        logger.info("Successfully sent message to Telegram (chat_id: %s)", chat_id)
        return True
    except Exception as exc:
        logger.error("Telegram API request failed: %s", exc)
        raise RuntimeError(f"Telegram API request failed: {exc}") from exc


def send_ntfy(message: str, title: str = "JobPulse Daily Digest", topic: Optional[str] = None) -> bool:
    """
    Send push notification directly to user's phone via ntfy.sh (No account or keys required).
    """
    resolved_topic = topic or os.getenv("NTFY_TOPIC", DEFAULT_NTFY_TOPIC)
    url = f"https://ntfy.sh/{resolved_topic}"

    # Clean title for HTTP headers (avoid latin-1 encoding errors with emojis in header fields)
    safe_title = title.encode("ascii", "ignore").decode("ascii").strip() or "JobPulse Digest"

    headers = {
        "Title": safe_title,
        "Priority": "high" if "failed" in safe_title.lower() or "error" in safe_title.lower() else "default",
        "Tags": "warning" if "failed" in safe_title.lower() else "briefcase,sparkles",
        "Markdown": "yes"
    }

    try:
        response = requests.post(
            url,
            data=message.encode("utf-8"),
            headers=headers,
            timeout=15
        )
        response.raise_for_status()
        logger.info("Successfully pushed notification to phone via ntfy (topic: %s)", resolved_topic)
        return True
    except Exception as exc:
        logger.error("ntfy push request failed: %s", exc)
        return False


def send_notification(message: str, title: str = "JobPulse Daily Digest") -> bool:
    """
    Unified notification dispatcher:
    1. Sends mobile push notification via ntfy.sh (Option A - Zero keys needed).
    2. Also sends via Telegram if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are configured.
    Returns True if at least one delivery succeeded.
    """
    sent_any = False

    # 1. Deliver to phone via ntfy
    if send_ntfy(message=message, title=title):
        sent_any = True

    # 2. Deliver via Telegram if configured
    try:
        if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
            if send_telegram(message=message):
                sent_any = True
    except Exception as e:
        logger.warning("Telegram delivery skipped or failed: %s", e)

    return sent_any


if __name__ == "__main__":
    print("=" * 60)
    print("JobPulse - Testing Delivery Tools")
    print("=" * 60)

    test_msg = (
        "✨ *JobPulse Test Alert*\n\n"
        "1. *Python / AI Engineer* @ *TechCorp*\n"
        "   📍 Location: Hyderabad, India\n"
        "   💰 Compensation: ₹6,00,000 / year\n"
        "   💡 Match Reason: Perfect match for your Python and Gemini experience.\n"
        "   🔗 [Apply](https://example.com)\n\n"
        "Ready to deliver alerts directly to your phone!"
    )

    print("Sending test push notification via ntfy...")
    success = send_ntfy(test_msg, title="JobPulse Test Notification")
    print("Push notification result:", "SUCCESS" if success else "FAILED")
    print(f"Check your phone app or visit: https://ntfy.sh/{os.getenv('NTFY_TOPIC', DEFAULT_NTFY_TOPIC)}")

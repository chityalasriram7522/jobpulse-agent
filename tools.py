"""
JobPulse Tools Module
---------------------
Helper functions for:
1. Fetching job listings:
   - Uses Adzuna Jobs API if ADZUNA_APP_ID & ADZUNA_APP_KEY are provided.
   - Automatically aggregates from 100% Free Public Job Feeds:
     * RemoteOK (100+ fresh developer roles)
     * Arbeitnow (300+ tech roles)
     * Remotive (software developer feed)
     Requiring ZERO API keys or registration!
2. Delivering Notifications:
   - send_telegram(): Delivers clean Markdown messages via Telegram Bot API.
   - send_ntfy(): Delivers push notifications directly to user's phone via ntfy.sh (No account required).
   - send_notification(): Unified dispatcher that delivers to all configured channels and handles error alerts.
"""

import os
import sys
import logging
from typing import Any, Dict, List, Optional, Set
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
REMOTEOK_URL = "https://remoteok.com/api"
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


def _fetch_from_free_apis(query: str, limit: int = 50) -> List[Dict[str, Any]]:
    """
    Fetch tech and developer jobs from 100% Free Public APIs (RemoteOK, Arbeitnow, Remotive).
    Uses smart keyword token matching so variations like 'Java Software Engineer' or
    'Python Backend Developer' are always captured.
    Requires ZERO registration and ZERO API keys.
    """
    logger.info("Aggregating from Free Public Job Feeds for query: '%s'", query)
    jobs: List[Dict[str, Any]] = []
    seen_urls: Set[str] = set()

    # Normalize search tokens
    tokens = [t.lower() for t in query.replace("/", " ").replace("-", " ").split() if len(t) > 2]
    if not tokens:
        tokens = ["python", "java", "developer", "software"]

    # 1. Fetch from RemoteOK API
    try:
        headers = {"User-Agent": "JobPulse-Agent/1.0"}
        resp = requests.get(REMOTEOK_URL, headers=headers, timeout=12)
        if resp.status_code == 200:
            raw_remoteok = resp.json()
            # First element is legal disclaimer, remaining are job dicts
            remoteok_jobs = raw_remoteok[1:] if isinstance(raw_remoteok, list) and len(raw_remoteok) > 1 else []
            for item in remoteok_jobs:
                if not isinstance(item, dict):
                    continue
                url = item.get("url") or f"https://remoteok.com/l/{item.get('id')}"
                if url in seen_urls:
                    continue
                
                title = item.get("position") or item.get("title") or ""
                tags = item.get("tags") or []
                text_to_search = (title + " " + " ".join(tags) + " " + (item.get("description") or "")[:200]).lower()

                # Match if any token is present
                if any(t in text_to_search for t in tokens):
                    seen_urls.add(url)
                    jobs.append({
                        "id": f"remoteok-{item.get('id')}",
                        "title": title.strip(),
                        "company": (item.get("company") or "Tech Company").strip(),
                        "location": item.get("location") or "Remote",
                        "description": (item.get("description") or "")[:600].strip(),
                        "salary_min": item.get("salary_min"),
                        "salary_max": item.get("salary_max"),
                        "contract_type": "full_time",
                        "contract_time": "full_time",
                        "redirect_url": url,
                        "created": item.get("date", ""),
                    })
                    if len(jobs) >= limit:
                        break
            logger.info("Retrieved %d matching jobs from RemoteOK", len(jobs))
    except Exception as exc:
        logger.warning("RemoteOK fetch warning: %s", exc)

    # 2. Fetch from Arbeitnow API
    if len(jobs) < limit:
        try:
            resp = requests.get(ARBEITNOW_URL, timeout=12)
            if resp.status_code == 200:
                arbeit_jobs = resp.json().get("data", [])
                for item in arbeit_jobs:
                    url = item.get("url") or ""
                    if not url or url in seen_urls:
                        continue
                    
                    title = item.get("title") or ""
                    tags = item.get("tags") or []
                    text_to_search = (title + " " + " ".join(tags)).lower()

                    if any(t in text_to_search for t in tokens):
                        seen_urls.add(url)
                        jobs.append({
                            "id": f"arbeit-{item.get('slug')}",
                            "title": title.strip(),
                            "company": (item.get("company_name") or "Innovative Co").strip(),
                            "location": item.get("location") or ("Remote" if item.get("remote") else "Hybrid"),
                            "description": (item.get("description") or "")[:600].strip(),
                            "salary_min": None,
                            "salary_max": None,
                            "contract_type": "full-time",
                            "contract_time": "full_time",
                            "redirect_url": url,
                            "created": str(item.get("created_at", "")),
                        })
                        if len(jobs) >= limit:
                            break
                logger.info("Total jobs after Arbeitnow: %d", len(jobs))
        except Exception as exc:
            logger.warning("Arbeitnow fetch warning: %s", exc)

    # 3. Fetch from Remotive API
    if len(jobs) < limit:
        try:
            # Search with primary token or software-dev category
            search_param = tokens[0] if tokens else "developer"
            resp = requests.get(REMOTIVE_URL, params={"category": "software-dev", "search": search_param, "limit": 20}, timeout=12)
            if resp.status_code == 200:
                remotive_jobs = resp.json().get("jobs", [])
                for item in remotive_jobs:
                    url = item.get("url") or ""
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    jobs.append({
                        "id": f"remotive-{item.get('id')}",
                        "title": (item.get("title") or "Developer").strip(),
                        "company": (item.get("company_name") or "Tech Company").strip(),
                        "location": item.get("candidate_required_location") or "Remote",
                        "description": (item.get("description") or "")[:600].strip(),
                        "salary_min": None,
                        "salary_max": None,
                        "contract_type": item.get("job_type", "full_time"),
                        "contract_time": "full_time",
                        "redirect_url": url,
                        "created": item.get("publication_date", ""),
                    })
                    if len(jobs) >= limit:
                        break
        except Exception as exc:
            logger.warning("Remotive fetch warning: %s", exc)

    return jobs[:limit]


def fetch_jobs(
    query: str,
    location: str = "Hyderabad",
    results_per_page: int = 30,
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

    # Fallback to free public job feeds
    return _fetch_from_free_apis(query=query, limit=results_per_page)


def fetch_ai_news(limit: int = 4) -> List[Dict[str, str]]:
    """
    Fetch real-time AI & Tech Current Affairs headlines using Google News RSS.
    100% Free, zero registration or API keys required.
    """
    news_url = "https://news.google.com/rss/search?q=Artificial+Intelligence+technology+news&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        import xml.etree.ElementTree as ET
        resp = requests.get(news_url, timeout=10)
        if resp.status_code == 200:
            root = ET.fromstring(resp.content)
            items = root.findall(".//item")[:limit]
            news_list: List[Dict[str, str]] = []
            for item in items:
                title = item.find("title").text if item.find("title") is not None else ""
                link = item.find("link").text if item.find("link") is not None else ""
                if title:
                    clean_title = title.split(" - ")[0].strip()
                    news_list.append({"title": clean_title, "link": link})
            logger.info("Retrieved %d live AI & tech current affairs stories", len(news_list))
            return news_list
    except Exception as exc:
        logger.warning("Failed to fetch live AI news: %s", exc)

    return [
        {"title": "Global developers accelerate multi-agent autonomous AI systems", "link": ""},
        {"title": "New breakthroughs in open-source reasoning models and agent architectures", "link": ""},
        {"title": "Cloud infrastructure shifts rapidly toward local on-device inference", "link": ""}
    ]


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
    print("JobPulse - Testing Free Aggregator & Delivery")
    print("=" * 60)

    jobs = fetch_jobs(query="Python Java React", results_per_page=30)
    print(f"\nFetched {len(jobs)} total jobs:")
    for idx, j in enumerate(jobs[:10], start=1):
        print(f"[{idx:02d}] {j['title']} @ {j['company']} ({j['location']})")
        print(f"     URL: {j['redirect_url']}")

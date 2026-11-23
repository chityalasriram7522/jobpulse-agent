"""
JobPulse Database Module (SQLite Memory)
----------------------------------------
Manages persistent memory so JobPulse never sends duplicate job alerts.

Table: sent_jobs
  - url (TEXT, PRIMARY KEY): Canonical job URL
  - title (TEXT): Job title
  - company (TEXT): Company name
  - sent_date (TIMESTAMP): Timestamp when sent (defaults to CURRENT_TIMESTAMP)
"""

import sqlite3
import logging
from typing import Any, Dict, List, Set

logger = logging.getLogger("jobpulse.db")
DEFAULT_DB_PATH = "sent_jobs.db"


def get_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Create and return a database connection with row factory enabled."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """
    Initialize the SQLite database schema if it doesn't already exist.
    Creates the 'sent_jobs' table.
    """
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_jobs (
                url TEXT PRIMARY KEY,
                title TEXT,
                company TEXT,
                sent_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()
    logger.debug("Database initialized at '%s'", db_path)


def get_sent_urls(db_path: str = DEFAULT_DB_PATH) -> Set[str]:
    """Retrieve all previously sent job URLs as a set for O(1) deduplication lookup."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT url FROM sent_jobs")
        rows = cursor.fetchall()
        return {row["url"] for row in rows if row["url"]}


def filter_new_jobs(jobs: List[Dict[str, Any]], db_path: str = DEFAULT_DB_PATH) -> List[Dict[str, Any]]:
    """
    Filter out any listings whose canonical URL already exists in sent_jobs table.
    Ensures the user only receives fresh, unread postings.
    """
    sent_urls = get_sent_urls(db_path)
    fresh_jobs: List[Dict[str, Any]] = []

    for job in jobs:
        # Check both redirect_url (from API) and link (from match schema)
        job_url = job.get("redirect_url") or job.get("link") or ""
        if job_url and job_url in sent_urls:
            logger.debug("Filtering out duplicate job: %s (%s)", job.get("title"), job_url)
            continue
        fresh_jobs.append(job)

    logger.info("Filtered %d raw jobs down to %d fresh (unsent) jobs", len(jobs), len(fresh_jobs))
    return fresh_jobs


def save_sent_jobs(jobs: List[Dict[str, Any]], db_path: str = DEFAULT_DB_PATH) -> int:
    """
    Save newly sent job postings into the database to prevent future repeats.
    Returns the number of new records inserted.
    """
    if not jobs:
        return 0

    init_db(db_path)
    saved_count = 0

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        for job in jobs:
            job_url = job.get("link") or job.get("redirect_url") or ""
            if not job_url:
                continue
            title = job.get("title", "")
            company = job.get("company", "")
            cursor.execute(
                """
                INSERT OR IGNORE INTO sent_jobs (url, title, company)
                VALUES (?, ?, ?)
                """,
                (job_url, title, company)
            )
            if cursor.rowcount > 0:
                saved_count += 1
        conn.commit()

    logger.info("Saved %d new jobs to database '%s'", saved_count, db_path)
    return saved_count


def get_sent_count(db_path: str = DEFAULT_DB_PATH) -> int:
    """Return total number of jobs recorded in sent_jobs."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS total FROM sent_jobs")
        row = cursor.fetchone()
        return row["total"] if row else 0

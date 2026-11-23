"""
Unit tests for db.py (SQLite Memory & Deduplication)
"""

import os
import pytest
import sqlite3
from db import init_db, get_sent_urls, filter_new_jobs, save_sent_jobs, get_sent_count


@pytest.fixture
def temp_db(tmp_path):
    """Provides a fresh temporary SQLite database for each test."""
    db_file = tmp_path / "test_sent_jobs.db"
    db_path = str(db_file)
    init_db(db_path)
    return db_path


def test_init_db_creates_table(temp_db):
    """Verify that sent_jobs table is created with correct columns."""
    conn = sqlite3.connect(temp_db)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(sent_jobs)")
    columns = {row[1] for row in cursor.fetchall()}
    conn.close()

    assert "url" in columns
    assert "title" in columns
    assert "company" in columns
    assert "sent_date" in columns


def test_save_and_get_sent_urls(temp_db):
    """Test saving jobs and retrieving their URLs."""
    sample_jobs = [
        {"link": "https://example.com/job1", "title": "Python Dev", "company": "Acme Inc"},
        {"link": "https://example.com/job2", "title": "Java Dev", "company": "Beta Corp"},
    ]

    saved = save_sent_jobs(sample_jobs, db_path=temp_db)
    assert saved == 2
    assert get_sent_count(temp_db) == 2

    sent_urls = get_sent_urls(temp_db)
    assert "https://example.com/job1" in sent_urls
    assert "https://example.com/job2" in sent_urls


def test_filter_new_jobs_drops_existing(temp_db):
    """Test that filter_new_jobs removes previously saved jobs."""
    # Seed DB with job1
    save_sent_jobs([{"link": "https://example.com/job1", "title": "Python Dev", "company": "Acme"}], db_path=temp_db)

    # Incoming batch has job1 (duplicate) and job2 (new)
    incoming = [
        {"redirect_url": "https://example.com/job1", "title": "Python Dev", "company": "Acme"},
        {"redirect_url": "https://example.com/job2", "title": "Java Dev", "company": "Beta"},
    ]

    fresh = filter_new_jobs(incoming, db_path=temp_db)
    assert len(fresh) == 1
    assert fresh[0]["redirect_url"] == "https://example.com/job2"


def test_save_is_idempotent(temp_db):
    """Test that saving the same job twice does not create duplicate rows or crash."""
    job = [{"link": "https://example.com/job1", "title": "Python Dev", "company": "Acme"}]

    first_save = save_sent_jobs(job, db_path=temp_db)
    assert first_save == 1

    second_save = save_sent_jobs(job, db_path=temp_db)
    assert second_save == 0
    assert get_sent_count(temp_db) == 1

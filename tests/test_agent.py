"""
Unit tests for agent.py (Gemini matching, JSON validation, retry logic, formatting)
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from agent import (
    validate_job_matches,
    match_and_rank_jobs,
    format_digest,
    run_agent,
    sanitize_resume_text,
    JobMatch
)


@pytest.fixture
def sample_jobs():
    return [
        {
            "id": "1",
            "title": "Junior Python Developer",
            "company": "Tech Corp",
            "location": "Hyderabad",
            "salary_min": 500000,
            "salary_max": 700000,
            "contract_type": "permanent",
            "contract_time": "full_time",
            "description": "Python, FastAPI and SQLite required.",
            "redirect_url": "https://www.adzuna.in/details/1"
        },
        {
            "id": "2",
            "title": "AI Intern",
            "company": "Cognitive AI",
            "location": "Hyderabad",
            "salary_min": 250000,
            "salary_max": 250000,
            "contract_type": "contract",
            "contract_time": "full_time",
            "description": "Internship with 25,000 monthly stipend. Work with Gemini.",
            "redirect_url": "https://www.adzuna.in/details/2"
        }
    ]


@pytest.fixture
def valid_matches_json():
    return [
        {
            "title": "Junior Python Developer",
            "company": "Tech Corp",
            "location": "Hyderabad",
            "stipend_or_salary": "₹5,00,000 - ₹7,00,000",
            "match_reason": "Matches your Python and database skills.",
            "link": "https://www.adzuna.in/details/1"
        },
        {
            "title": "AI Intern",
            "company": "Cognitive AI",
            "location": "Hyderabad",
            "stipend_or_salary": "₹25,000 / month",
            "match_reason": "Matches your interest in Gemini API and LLM agents.",
            "link": "https://www.adzuna.in/details/2"
        }
    ]


def test_sanitize_resume_text():
    """Verify that personal contact PII (emails and phone numbers) are stripped."""
    raw_resume = (
        "Name: John Doe\n"
        "Phone: +91-7416507522\n"
        "Email: myemail@gmail.com\n"
        "Skills: Python, Java, SQL"
    )
    sanitized = sanitize_resume_text(raw_resume)
    assert "+91-7416507522" not in sanitized
    assert "myemail@gmail.com" not in sanitized
    assert "[EMAIL REDACTED]" in sanitized
    assert "[PHONE REDACTED]" in sanitized
    assert "Python, Java, SQL" in sanitized

def test_validate_job_matches_valid(valid_matches_json):
    """Test that a valid JSON structure parses correctly."""
    result = validate_job_matches(valid_matches_json)
    assert len(result) == 2
    assert result[0]["title"] == "Junior Python Developer"
    assert result[0]["company"] == "Tech Corp"


def test_validate_job_matches_with_markdown_fences(valid_matches_json):
    """Test that markdown code fences are stripped before parsing."""
    raw_str = f"```json\n{json.dumps(valid_matches_json)}\n```"
    result = validate_job_matches(raw_str)
    assert len(result) == 2
    assert result[1]["title"] == "AI Intern"


def test_validate_job_matches_missing_field(valid_matches_json):
    """Test that missing required fields raises ValueError."""
    bad_data = [dict(valid_matches_json[0])]
    del bad_data[0]["match_reason"]
    with pytest.raises(ValueError, match="missing required field 'match_reason'"):
        validate_job_matches(bad_data)


def test_validate_job_matches_enforces_cap(valid_matches_json):
    """Test that output is capped at max_matches (e.g. 5)."""
    expanded = valid_matches_json * 5  # 10 items
    result = validate_job_matches(expanded, max_matches=5)
    assert len(result) == 5


# -------------------------------------------------------------
# Test Gemini Matching & Retry Logic
# -------------------------------------------------------------

def test_match_and_rank_jobs_success(sample_jobs, valid_matches_json, monkeypatch):
    """Test successful Gemini ranking on first attempt."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = json.dumps(valid_matches_json)
    mock_client.models.generate_content.return_value = mock_resp

    with patch("google.genai.Client", return_value=mock_client):
        result = match_and_rank_jobs(
            jobs=sample_jobs,
            resume_text="Experienced in Python and AI.",
            preferences={"roles": ["Python Developer"]}
        )

        assert len(result) == 2
        assert result[0]["title"] == "Junior Python Developer"
        mock_client.models.generate_content.assert_called_once()


def test_match_and_rank_jobs_retry_on_invalid_json(sample_jobs, valid_matches_json, monkeypatch):
    """Test that Gemini retries once when attempt 1 returns invalid JSON."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    mock_client = MagicMock()
    bad_resp = MagicMock()
    bad_resp.text = "This is not valid JSON"

    good_resp = MagicMock()
    good_resp.text = json.dumps(valid_matches_json)

    # First call fails validation, second call succeeds
    mock_client.models.generate_content.side_effect = [bad_resp, good_resp]

    with patch("google.genai.Client", return_value=mock_client):
        result = match_and_rank_jobs(
            jobs=sample_jobs,
            resume_text="Experienced in Python and AI.",
            preferences={"roles": ["Python Developer"]}
        )

        assert len(result) == 2
        assert mock_client.models.generate_content.call_count == 2


def test_match_and_rank_jobs_fails_after_retries(sample_jobs, monkeypatch):
    """Test that RuntimeError is raised when all retries fail."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    mock_client = MagicMock()
    bad_resp = MagicMock()
    bad_resp.text = "Not JSON"
    mock_client.models.generate_content.return_value = bad_resp

    with patch("google.genai.Client", return_value=mock_client):
        with pytest.raises(RuntimeError, match="Gemini job matching failed"):
            match_and_rank_jobs(
                jobs=sample_jobs,
                resume_text="Python dev",
                preferences={}
            )


# -------------------------------------------------------------
# Test Message Formatting & Dry Run
# -------------------------------------------------------------

def test_format_digest(valid_matches_json):
    """Test formatting output for digest including AI news summary section without links."""
    summaries = ["**Autonomous AI Agents**: Breakthroughs in multi-agent workflows show major enterprise efficiency gains."]
    digest = format_digest(valid_matches_json, news_summaries=summaries)
    assert "JobPulse Daily Morning Briefing" in digest
    assert "Junior Python Developer" in digest
    assert "Tech Corp" in digest
    assert "Daily AI & Tech Current Affairs Summary" in digest
    assert "Autonomous AI Agents" in digest


def test_run_agent_dry_run(sample_jobs, valid_matches_json):
    """Test running agent in dry-run mode succeeds with exit code 0."""
    with patch("agent.fetch_jobs", return_value=sample_jobs), \
         patch("agent.match_and_rank_jobs", return_value=valid_matches_json):
        exit_code = run_agent(dry_run=True)
        assert exit_code == 0


def test_dedup_across_runs(tmp_path, sample_jobs, valid_matches_json):
    """Test that running the agent saves jobs and second run drops them."""
    test_db = str(tmp_path / "sent_jobs.db")

    with patch("agent.fetch_jobs", return_value=sample_jobs), \
         patch("agent.match_and_rank_jobs", return_value=valid_matches_json):
        # Run 1: Saves matches
        code1 = run_agent(dry_run=False, db_path=test_db)
        assert code1 == 0

    # Run 2: Incoming jobs have same URLs, so filter_new_jobs drops them
    with patch("agent.fetch_jobs", return_value=sample_jobs), \
         patch("agent.match_and_rank_jobs") as mock_rank:
        code2 = run_agent(dry_run=False, db_path=test_db)
        assert code2 == 0
        # Gemini should NOT be called on Run 2 because all jobs were already sent!
        mock_rank.assert_not_called()

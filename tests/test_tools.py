"""
Unit tests for tools.py (fetch_jobs)
"""

import pytest
from unittest.mock import patch, MagicMock
from tools import fetch_jobs, _fetch_from_adzuna, _fetch_from_free_apis


@pytest.fixture
def mock_adzuna_response():
    """Sample raw payload returned by Adzuna India API"""
    return {
        "results": [
            {
                "id": "123456",
                "title": "Junior Python Developer",
                "company": {"display_name": "Tech Corp"},
                "location": {"display_name": "Hyderabad, Telangana"},
                "description": "Python, Django, FastAPI developer needed.",
                "salary_min": 400000.0,
                "salary_max": 600000.0,
                "contract_type": "permanent",
                "contract_time": "full_time",
                "redirect_url": "https://www.adzuna.in/details/123456",
                "created": "2026-11-23T08:00:00Z"
            },
            {
                "id": "789012",
                "title": "AI Intern",
                "company": {"display_name": "AI Innovations"},
                "location": {"display_name": "Hyderabad, Telangana"},
                "description": "Internship with 20000/month stipend.",
                "salary_min": 240000.0,
                "salary_max": 240000.0,
                "contract_type": "contract",
                "contract_time": "full_time",
                "redirect_url": "https://www.adzuna.in/details/789012",
                "created": "2026-11-23T09:00:00Z"
            }
        ]
    }


def test_fetch_jobs_adzuna_success(mock_adzuna_response, monkeypatch):
    """Test successful fetching and parsing of jobs from Adzuna API when keys are provided"""
    monkeypatch.setenv("ADZUNA_APP_ID", "dummy_id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "dummy_key")

    mock_resp = MagicMock()
    mock_resp.json.return_value = mock_adzuna_response
    mock_resp.raise_for_status.return_value = None

    with patch("requests.get", return_value=mock_resp) as mock_get:
        jobs = fetch_jobs(query="Python Developer", location="Hyderabad", results_per_page=2)

        assert len(jobs) == 2
        assert jobs[0]["title"] == "Junior Python Developer"
        assert jobs[0]["company"] == "Tech Corp"
        assert jobs[0]["location"] == "Hyderabad, Telangana"
        assert jobs[0]["redirect_url"] == "https://www.adzuna.in/details/123456"

        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        assert "https://api.adzuna.com/v1/api/jobs/in/search/1" in args[0]
        assert kwargs["params"]["app_id"] == "dummy_id"
        assert kwargs["params"]["app_key"] == "dummy_key"


def test_fetch_jobs_free_api_fallback(monkeypatch):
    """Test automatic fallback to Free Public Job APIs when no Adzuna keys are set"""
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)

    mock_remotive_resp = MagicMock()
    mock_remotive_resp.status_code = 200
    mock_remotive_resp.json.return_value = {
        "jobs": [
            {
                "id": 999,
                "title": "Backend Python Engineer",
                "company_name": "Open Source Tech",
                "candidate_required_location": "Remote",
                "description": "Python, SQLite, REST.",
                "job_type": "full_time",
                "url": "https://remotive.com/job/999",
                "publication_date": "2026-11-23"
            }
        ]
    }

    with patch("requests.get", return_value=mock_remotive_resp):
        jobs = fetch_jobs(query="Python", results_per_page=1)
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Backend Python Engineer"
        assert jobs[0]["company"] == "Open Source Tech"
        assert jobs[0]["redirect_url"] == "https://remotive.com/job/999"

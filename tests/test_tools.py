"""
Unit tests for tools.py (fetch_jobs)
"""

import pytest
from unittest.mock import patch, MagicMock
from tools import fetch_jobs


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


def test_fetch_jobs_success(mock_adzuna_response, monkeypatch):
    """Test successful fetching and parsing of jobs from Adzuna API"""
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
        assert jobs[0]["salary_min"] == 400000.0
        assert jobs[0]["redirect_url"] == "https://www.adzuna.in/details/123456"

        assert jobs[1]["title"] == "AI Intern"
        assert jobs[1]["company"] == "AI Innovations"

        # Verify correct URL and parameters were called
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        assert "https://api.adzuna.com/v1/api/jobs/in/search/1" in args[0]
        assert kwargs["params"]["app_id"] == "dummy_id"
        assert kwargs["params"]["app_key"] == "dummy_key"
        assert kwargs["params"]["what"] == "Python Developer"
        assert kwargs["params"]["where"] == "Hyderabad"


def test_fetch_jobs_missing_credentials(monkeypatch):
    """Test that missing credentials raises ValueError"""
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)

    with pytest.raises(ValueError, match="Missing Adzuna API credentials"):
        fetch_jobs(query="Python")

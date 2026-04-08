import pytest
from unittest.mock import MagicMock


@pytest.fixture
def mock_supabase():
    """Mock Supabase client for testing."""
    client = MagicMock()
    # Default: empty results
    client.table.return_value.select.return_value.eq.return_value.execute.return_value.data = []
    client.table.return_value.select.return_value.in_.return_value.execute.return_value.data = []
    client.table.return_value.upsert.return_value.execute.return_value.data = []
    client.table.return_value.insert.return_value.execute.return_value.data = []
    return client

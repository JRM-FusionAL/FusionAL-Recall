import pytest
from recall.db import RecallDB


@pytest.fixture
def db():
    """In-memory RecallDB instance, closed after each test."""
    instance = RecallDB(":memory:")
    yield instance
    instance.close()

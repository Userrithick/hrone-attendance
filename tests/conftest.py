"""Tests use a fresh, uniquely named database, never the configured app database."""
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app import main


@pytest.fixture(scope="session")
def mongo():
    uri = os.getenv("TEST_MONGO_URI", "mongodb://127.0.0.1:27017")
    connection = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=3000)
    connection.admin.command("ping")  # Fail clearly: these tests must not silently skip MongoDB.
    yield connection
    connection.close()


@pytest.fixture
def database(mongo, monkeypatch):
    name = "attendance_test_" + uuid.uuid4().hex
    database = mongo[name]
    monkeypatch.setattr(main, "db", database)
    main.create_indexes()
    yield database
    mongo.drop_database(name)


@pytest.fixture
def api(database):
    # Indexes are created above. Do not close the application global client per test.
    with TestClient(main.app) as client:
        yield client


def employee_doc(code="EMP0001", department="Engineering", joined="2026-01-01", **extra):
    return dict(emp_code=code, name=code, email=f"{code}@example.com", department=department,
                shift_start="09:30", shift_end="18:30", joined_on=joined) | extra


def ms(value):
    from datetime import datetime
    return main.epoch_ms(datetime.fromisoformat(value))

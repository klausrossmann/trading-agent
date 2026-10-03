import os

import pytest

from trading_agent.settings import Settings

# Tests never read a developer's .env; they set environment variables explicitly.
Settings.model_config["env_file"] = None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # Guard: db tests drop tables, so they only run against a database named *_test.
    if os.environ.get("DB_NAME", "").endswith("_test"):
        return
    skip = pytest.mark.skip(reason="needs DB_NAME=*_test (see `make test-db`)")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)

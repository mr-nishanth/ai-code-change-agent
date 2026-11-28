"""The studio's Floci-backed tests use the shared per-test table fixtures."""

from tests.support.dynamo import app_table, floci  # noqa: F401 - fixtures

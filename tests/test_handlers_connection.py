"""
Tests for Connection Handler
"""

import pytest

from handlers.connection import ConnectionHandler


class TestConnectionHandler:
    """Test Connection Handler functionality"""

    @pytest.fixture
    def connection_handler(self, mock_checkmk_client):
        """Create connection handler with mocked client"""
        return ConnectionHandler(mock_checkmk_client)

    @pytest.mark.asyncio
    async def test_get_version_reads_nested_checkmk_version(self, connection_handler, mock_checkmk_responses):
        """Version is taken from versions.checkmk, as returned by the REST API"""
        connection_handler.client.get.return_value = mock_checkmk_responses["version"]

        result = await connection_handler.handle("vibemk_get_checkmk_version", {})

        assert "Version: 2.3.0p1" in result[0]["text"]
        assert "Edition: cre" in result[0]["text"]

    @pytest.mark.asyncio
    async def test_debug_connection_reads_nested_checkmk_version(self, connection_handler, mock_checkmk_responses):
        """Connection debug output shows the version from versions.checkmk"""
        connection_handler.client.get.return_value = mock_checkmk_responses["version"]

        result = await connection_handler.handle("vibemk_debug_checkmk_connection", {})

        assert "Version: 2.3.0p1" in result[0]["text"]

    @pytest.mark.asyncio
    async def test_get_version_unknown_when_missing(self, connection_handler):
        """Falls back to 'Unknown' when the API returns no version"""
        connection_handler.client.get.return_value = {"success": True, "data": {"edition": "cre"}}

        result = await connection_handler.handle("vibemk_get_checkmk_version", {})

        assert "Version: Unknown" in result[0]["text"]

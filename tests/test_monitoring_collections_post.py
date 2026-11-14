"""
The problem overview must never turn "no state" into "no problem".

A service row without a state used to be read as OK, so an answer that could
not see the states reported a clean site. The overview now asks for the state
column explicitly, through the POST form of the monitoring collections (the
GET form is deprecated and goes in CheckMK 3.0), and answers with an error
instead of counts whenever any row comes back without a real state.
"""

import json
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from vibemk.api import CheckMKClient
from vibemk.api.exceptions import CheckMKAPIError, CheckMKNotFoundError, CheckMKPermissionError
from vibemk.handlers.monitoring import MAX_LISTED_PROBLEMS, MonitoringHandler

PROBLEMS = "vibemk_get_current_problems"


def collection(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"success": True, "status": 200, "headers": {}, "data": {"value": rows}}


def host(name: str, state: Any, **extra: Any) -> Dict[str, Any]:
    return {"extensions": {"name": name, "state": state, **extra}}


def service(host_name: str, description: str, state: Any, output: str = "", **extra: Any) -> Dict[str, Any]:
    return {
        "extensions": {
            "host_name": host_name,
            "description": description,
            "state": state,
            "plugin_output": output,
            "last_state_change": 0,
            **extra,
        }
    }


def answering(client: Any, hosts: List[Dict[str, Any]], services: List[Dict[str, Any]]) -> Any:
    def post(endpoint: str, data: Any = None, **_: Any) -> Dict[str, Any]:
        return collection(hosts if "host/collections" in endpoint else services)

    client.post.side_effect = post
    return client


def text_of(content: List[Dict[str, Any]]) -> str:
    return content[0]["text"]


def data_of(content: List[Dict[str, Any]]) -> Dict[str, Any]:
    return next(block["data"] for block in content if "data" in block)


class TestListMonitoring:
    def test_posts_the_columns_as_json(self, mock_config):
        client = CheckMKClient(mock_config, skip_url_detection=True)
        response = MagicMock(status=200, headers={})
        response.read.return_value = b'{"value": []}'
        response.__enter__.return_value = response

        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            client.list_monitoring(
                "service", ["host_name", "state"], query={"op": "=", "left": "state", "right": "2"}, host_name="web01"
            )

        request = urlopen.call_args.args[0]
        assert request.get_method() == "POST"
        assert request.full_url.endswith("/domain-types/service/collections/all")
        assert json.loads(request.data) == {
            "columns": ["host_name", "state"],
            "query": {"op": "=", "left": "state", "right": "2"},
            "host_name": "web01",
        }

    @pytest.mark.parametrize("status", [404, 405])
    def test_a_checkmk_without_the_post_form_gets_the_get_form(self, mock_checkmk_client, status):
        mock_checkmk_client.post.side_effect = CheckMKNotFoundError("no such route", status)
        mock_checkmk_client.get.return_value = collection([])

        mock_checkmk_client.list_monitoring("host", ["name", "state"])

        assert mock_checkmk_client.get.call_args.kwargs["params"] == {"columns": ["name", "state"]}

    @pytest.mark.parametrize(
        "error", [CheckMKPermissionError("denied", 403), CheckMKAPIError("bad request", 400)], ids=["403", "400"]
    )
    def test_other_failures_are_not_retried_as_get(self, mock_checkmk_client, error):
        mock_checkmk_client.post.side_effect = error

        with pytest.raises(type(error)):
            mock_checkmk_client.list_monitoring("service", ["state"])
        mock_checkmk_client.get.assert_not_called()

    def test_only_live_collections_are_listed(self, mock_checkmk_client):
        with pytest.raises(ValueError, match="'host' or 'service'"):
            mock_checkmk_client.list_monitoring("host_config", ["name"])
        with pytest.raises(ValueError, match="filters services only"):
            mock_checkmk_client.list_monitoring("host", ["name"], host_name="web01")


class TestAMissingStateIsNeverOk:
    @pytest.mark.asyncio
    async def test_a_service_without_a_state_gives_no_counts(self, mock_checkmk_client):
        answering(
            mock_checkmk_client,
            [host("web01", 0)],
            [{"extensions": {"host_name": "web01", "description": "CPU"}}, service("web01", "Memory", 0)],
        )

        content = await MonitoringHandler(mock_checkmk_client).handle(PROBLEMS, {})

        text = text_of(content)
        assert "No current problems" not in text
        assert "1 of 2 services" in text
        assert "No counts are reported" in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [None, False, True, 7, -1, "2"], ids=repr)
    async def test_anything_but_an_int_from_0_to_3_is_not_a_state(self, mock_checkmk_client, bad):
        answering(mock_checkmk_client, [host("web01", 0)], [service("web01", "CPU", bad)])

        text = text_of(await MonitoringHandler(mock_checkmk_client).handle(PROBLEMS, {}))

        assert "0 of 1 hosts and 1 of 1 services" in text

    @pytest.mark.asyncio
    async def test_no_hosts_at_all_is_not_an_all_clear(self, mock_checkmk_client):
        # What an account that may see nothing gets back.
        answering(mock_checkmk_client, [], [])

        text = text_of(await MonitoringHandler(mock_checkmk_client).handle(PROBLEMS, {}))

        assert "No current problems" not in text
        assert "no hosts" in text

    @pytest.mark.asyncio
    async def test_a_failed_request_gives_no_counts(self, mock_checkmk_client):
        mock_checkmk_client.post.side_effect = CheckMKPermissionError("Permission denied", 403)

        text = text_of(await MonitoringHandler(mock_checkmk_client).handle(PROBLEMS, {}))

        assert "No counts are reported" in text
        assert "No current problems" not in text


class TestTheOverviewCoversTheWholeSite:
    @pytest.mark.asyncio
    async def test_counts_cover_every_service(self, mock_checkmk_client):
        hosts = [host(f"h{i}", 0) for i in range(30)] + [host("down1", 1)]
        services = [service(f"h{i % 30}", f"ok{i}", 0) for i in range(200)]
        services += [service("h29", f"crit{i}", 2) for i in range(7)]
        services += [service("h29", "warn", 1), service("h29", "unk", 3, acknowledged=1)]
        answering(mock_checkmk_client, hosts, services)

        content = await MonitoringHandler(mock_checkmk_client).handle(PROBLEMS, {})

        assert data_of(content)["summary"] == {
            "hosts_checked": 31,
            "services_checked": 209,
            "hosts_down": 1,
            "hosts_unreachable": 0,
            "services_critical": 7,
            "services_warning": 1,
            "services_unknown": 1,
            "unhandled": 9,
        }
        text = text_of(content)
        assert "Checked 31 hosts and 209 services" in text
        assert "Services: 7 CRITICAL, 1 WARNING, 1 UNKNOWN" in text

    @pytest.mark.asyncio
    async def test_the_list_is_capped_but_the_counts_and_data_are_not(self, mock_checkmk_client):
        services = [service("h1", f"c{i:03}", 2, "CRIT") for i in range(MAX_LISTED_PROBLEMS + 50)]
        answering(mock_checkmk_client, [host("h1", 0)], services)

        content = await MonitoringHandler(mock_checkmk_client).handle(PROBLEMS, {})

        text = text_of(content)
        assert f"{MAX_LISTED_PROBLEMS + 50} CRITICAL" in text
        assert "... and 50 more" in text
        assert len(data_of(content)["service_problems"]) == MAX_LISTED_PROBLEMS + 50

    @pytest.mark.asyncio
    async def test_the_worst_come_first(self, mock_checkmk_client):
        answering(
            mock_checkmk_client,
            [host("h1", 0)],
            [service("h1", "a-warn", 1), service("h1", "b-crit-acked", 2, acknowledged=1), service("h1", "c-crit", 2)],
        )

        content = await MonitoringHandler(mock_checkmk_client).handle(PROBLEMS, {})

        order = [p["service_description"] for p in data_of(content)["service_problems"]]
        assert order == ["c-crit", "b-crit-acked", "a-warn"]

    @pytest.mark.asyncio
    async def test_long_output_is_trimmed_in_the_text_only(self, mock_checkmk_client):
        output = "CRIT - " + "x" * 400 + "\nsecond line of detail"
        answering(mock_checkmk_client, [host("h1", 0)], [service("h1", "Log", 2, output)])

        content = await MonitoringHandler(mock_checkmk_client).handle(PROBLEMS, {})

        assert "second line of detail" not in text_of(content)
        assert "x" * 400 not in text_of(content)
        assert data_of(content)["service_problems"][0]["plugin_output"] == output

    @pytest.mark.asyncio
    async def test_a_host_filter_is_asked_of_checkmk(self, mock_checkmk_client):
        answering(mock_checkmk_client, [host("web01", 0)], [service("web01", "CPU", 2)])

        await MonitoringHandler(mock_checkmk_client).handle(PROBLEMS, {"host_name": "web01"})

        host_call, service_call = mock_checkmk_client.post.call_args_list
        assert host_call.kwargs["data"]["query"] == {"op": "=", "left": "name", "right": "web01"}
        assert service_call.kwargs["data"]["host_name"] == "web01"
        assert "state" in service_call.kwargs["data"]["columns"]

    @pytest.mark.asyncio
    async def test_an_unknown_host_is_said_so(self, mock_checkmk_client):
        answering(mock_checkmk_client, [], [])

        text = text_of(await MonitoringHandler(mock_checkmk_client).handle(PROBLEMS, {"host_name": "nope"}))

        assert "Host not found" in text

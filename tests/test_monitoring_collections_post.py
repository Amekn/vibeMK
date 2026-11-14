"""
Monitoring collections must be POSTed with explicit columns, and a missing
state must never be reported as OK (seen on Checkmk 2.5).
"""

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from api import CheckMKClient
from api.exceptions import CheckMKAPIError, CheckMKPermissionError
from handlers.monitoring import MonitoringHandler
from handlers.services import ServiceHandler


def _ok_response(payload):
    response = MagicMock()
    response.status = 200
    response.read.return_value = json.dumps(payload).encode()
    response.headers = {}
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    return response


def _http_error(code):
    error = urllib.error.HTTPError(url="test", code=code, msg="err", hdrs={}, fp=None)
    error.read = MagicMock(return_value=b"{}")
    return error


def _host(name, state, **extra):
    return {"extensions": {"name": name, "state": state, "state_type": 1, "plugin_output": "out", **extra}}


def _svc(host, desc, state, **extra):
    return {
        "extensions": {
            "host_name": host,
            "description": desc,
            "state": state,
            "state_type": 1,
            "plugin_output": f"{desc} output",
            **extra,
        }
    }


def _rows(rows):
    return {"success": True, "data": {"value": rows}}


def _text(result):
    return result[0]["text"]


class TestListMonitoring:
    def test_posts_json_body_with_columns(self, mock_config):
        client = CheckMKClient(mock_config, skip_url_detection=True)
        with patch("urllib.request.urlopen", return_value=_ok_response({"value": []})) as urlopen:
            client.list_monitoring(
                "service",
                ["host_name", "state"],
                query={"op": "=", "left": "state", "right": "2"},
                host_name="web01",
            )
        req = urlopen.call_args[0][0]
        assert req.get_method() == "POST"
        assert req.full_url.endswith("/domain-types/service/collections/all")
        assert "?" not in req.full_url
        assert json.loads(req.data) == {
            "columns": ["host_name", "state"],
            "query": {"op": "=", "left": "state", "right": "2"},
            "host_name": "web01",
        }

    def test_string_query_is_sent_as_json_object(self, mock_config):
        client = CheckMKClient(mock_config, skip_url_detection=True)
        with patch("urllib.request.urlopen", return_value=_ok_response({"value": []})) as urlopen:
            client.list_monitoring("host", ["name"], query='{"op": "=", "left": "name", "right": "a"}')
        assert json.loads(urlopen.call_args[0][0].data)["query"] == {"op": "=", "left": "name", "right": "a"}

    @pytest.mark.parametrize("code", [404, 405])
    def test_falls_back_to_get_when_post_unsupported(self, mock_config, code):
        client = CheckMKClient(mock_config, skip_url_detection=True)
        with patch("urllib.request.urlopen", side_effect=[_http_error(code), _ok_response({"value": []})]) as urlopen:
            result = client.list_monitoring("host", ["name", "state"])
        assert result["success"] is True
        get_req = urlopen.call_args_list[1][0][0]
        assert get_req.get_method() == "GET"
        assert "columns=name&columns=state" in get_req.full_url

    @pytest.mark.parametrize("code,exc", [(403, CheckMKPermissionError), (400, CheckMKAPIError)])
    def test_other_errors_are_not_retried_as_get(self, mock_config, code, exc):
        client = CheckMKClient(mock_config, skip_url_detection=True)
        with patch("urllib.request.urlopen", side_effect=[_http_error(code)]) as urlopen:
            with pytest.raises(exc):
                client.list_monitoring("service", ["state"])
        assert urlopen.call_count == 1

    def test_rejects_bad_arguments(self, mock_config):
        client = CheckMKClient(mock_config, skip_url_detection=True)
        with pytest.raises(ValueError):
            client.list_monitoring("host_config", ["name"])
        with pytest.raises(ValueError):
            client.list_monitoring("host", ["name"], host_name="web01")


class TestCurrentProblems:
    def _handler(self, hosts, services):
        client = MagicMock()
        client.list_monitoring.side_effect = [_rows(hosts), _rows(services)]
        return MonitoringHandler(client), client

    @pytest.mark.asyncio
    async def test_counts_every_service_not_just_the_first_50(self):
        hosts = [_host(f"h{i}", 0) for i in range(30)] + [_host("down1", 1)]
        services = [_svc(f"h{i % 30}", f"ok{i}", 0) for i in range(200)]
        services += [_svc("h29", f"crit{i}", 2) for i in range(7)]
        services += [_svc("h29", "warn", 1), _svc("h29", "unk", 3, acknowledged=1)]
        handler, client = self._handler(hosts, services)

        text = _text(await handler.handle("vibemk_get_current_problems", {}))

        assert "Checked 31 hosts and 209 services" in text
        assert "Hosts: 1 DOWN, 0 UNREACHABLE (1 unhandled)" in text
        assert "Services: 7 CRITICAL, 1 WARNING, 1 UNKNOWN (8 unhandled)" in text
        assert "HOST: down1 - DOWN" in text
        assert "SERVICE: h29/unk - UNKNOWN (acknowledged)" in text
        assert client.list_monitoring.call_count == 2
        assert "state" in client.list_monitoring.call_args_list[0][0][1]
        assert "state" in client.list_monitoring.call_args_list[1][0][1]

    @pytest.mark.asyncio
    async def test_missing_state_is_an_error_not_all_clear(self):
        hosts = [_host("h1", 0)]
        services = [{"extensions": {"host_name": "h1", "description": "CPU"}}, _svc("h1", "Mem", 0)]
        handler, _ = self._handler(hosts, services)

        text = _text(await handler.handle("vibemk_get_current_problems", {}))

        assert "No current problems" not in text
        assert "1 of 2 services" in text
        assert "No counts are reported" in text

    @pytest.mark.asyncio
    async def test_boolean_and_out_of_range_states_are_rejected(self):
        handler, _ = self._handler([_host("h1", False)], [_svc("h1", "CPU", 7)])
        text = _text(await handler.handle("vibemk_get_current_problems", {}))
        assert "1 of 1 hosts and 1 of 1 services" in text

    @pytest.mark.asyncio
    async def test_all_ok_reports_what_was_checked(self):
        handler, _ = self._handler([_host("h1", 0)], [_svc("h1", "CPU", 0), _svc("h1", "Mem", 0)])
        text = _text(await handler.handle("vibemk_get_current_problems", {}))
        assert "No current problems" in text
        assert "Checked 1 hosts and 2 services" in text

    @pytest.mark.asyncio
    async def test_no_hosts_is_not_all_clear(self):
        handler, _ = self._handler([], [])
        text = _text(await handler.handle("vibemk_get_current_problems", {}))
        assert "No current problems" not in text
        assert "no hosts" in text

    @pytest.mark.asyncio
    async def test_api_error_gives_no_counts(self):
        client = MagicMock()
        client.list_monitoring.side_effect = CheckMKPermissionError("Permission denied", 403)
        text = _text(await MonitoringHandler(client).handle("vibemk_get_current_problems", {}))
        assert "No counts are reported" in text
        assert "No current problems" not in text

    @pytest.mark.asyncio
    async def test_host_filter_is_sent_to_checkmk(self):
        handler, client = self._handler([_host("web01", 0)], [_svc("web01", "CPU", 2)])
        text = _text(await handler.handle("vibemk_get_current_problems", {"host_name": "web01"}))
        host_call, service_call = client.list_monitoring.call_args_list
        assert host_call.kwargs["query"] == {"op": "=", "left": "name", "right": "web01"}
        assert service_call.kwargs["host_name"] == "web01"
        assert "SERVICE: web01/CPU - CRITICAL" in text

    @pytest.mark.asyncio
    async def test_long_problem_lists_are_truncated_but_counts_complete(self):
        services = [_svc("h1", f"c{i}", 2) for i in range(400)]
        handler, _ = self._handler([_host("h1", 0)], services)
        text = _text(await handler.handle("vibemk_get_current_problems", {}))
        assert "Services: 400 CRITICAL" in text
        assert "... and 250 more (the counts above are complete)" in text


class TestServiceList:
    @pytest.mark.asyncio
    async def test_all_services_list_requests_state_column(self):
        client = MagicMock()
        client.list_monitoring.return_value = _rows([_svc("h1", "CPU", 2)])
        text = _text(await ServiceHandler(client).handle("vibemk_get_checkmk_services", {}))
        assert "state" in client.list_monitoring.call_args[0][1]
        assert "h1/CPU (Status: CRITICAL)" in text
        assert "returned no state" not in text

    @pytest.mark.asyncio
    async def test_missing_state_is_flagged_never_unknown_none(self):
        client = MagicMock()
        client.list_monitoring.return_value = _rows(
            [{"extensions": {"host_name": "h1", "description": "CPU"}}, _svc("h1", "Mem", 0)]
        )
        text = _text(await ServiceHandler(client).handle("vibemk_get_checkmk_services", {}))
        assert "UNKNOWN(None)" not in text
        assert "STATE NOT RETURNED" in text
        assert "CheckMK returned no state for 1 of 2 services" in text

    @pytest.mark.asyncio
    async def test_host_action_path_flags_missing_state(self):
        client = MagicMock()
        client.post.return_value = _rows([{"extensions": {"description": "CPU"}}])
        text = _text(await ServiceHandler(client).handle("vibemk_get_checkmk_services", {"host_name": "h1"}))
        assert "UNKNOWN(None)" not in text
        assert "STATE NOT RETURNED" in text

    @pytest.mark.asyncio
    async def test_service_status_query_uses_post_collection(self):
        client = MagicMock()
        client.get.side_effect = Exception("show_service unavailable")
        client.list_monitoring.return_value = _rows([["h1", "CPU", 2, "CPU high", 0, 0]])
        text = _text(
            await ServiceHandler(client).handle("vibemk_get_service_status", {"host_name": "h1", "service_description": "CPU"})
        )
        assert "Status: CRITICAL" in text
        assert client.list_monitoring.call_args.kwargs["query"]["expr"][1]["right"] == "CPU"

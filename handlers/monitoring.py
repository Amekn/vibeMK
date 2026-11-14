"""
Monitoring and problem management handlers

Copyright (C) 2024 Andre <andre@example.com>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

import urllib.parse
from typing import Any, Dict, List

from api.exceptions import CheckMKError
from handlers.base import BaseHandler


HOST_PROBLEM_COLUMNS = ["name", "state", "state_type", "plugin_output", "acknowledged", "scheduled_downtime_depth"]
SERVICE_PROBLEM_COLUMNS = [
    "host_name",
    "description",
    "state",
    "state_type",
    "plugin_output",
    "acknowledged",
    "scheduled_downtime_depth",
]
HOST_STATE_NAMES = {0: "UP", 1: "DOWN", 2: "UNREACHABLE"}
SERVICE_STATE_NAMES = {0: "OK", 1: "WARNING", 2: "CRITICAL", 3: "UNKNOWN"}
SERVICE_SEVERITY = {2: 0, 3: 1, 1: 2, 0: 3}  # CRIT, UNKNOWN, WARN, OK
MAX_LISTED_PROBLEMS = 150
NO_COUNTS_NOTE = "No counts are reported; check the CheckMK web UI."


def _is_state(value: Any) -> bool:
    """A real monitoring state: int 0-3 (bool is not accepted)."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 3


def _is_handled(row: Dict[str, Any]) -> bool:
    return bool(row.get("acknowledged")) or bool(row.get("scheduled_downtime_depth"))


def _flags(row: Dict[str, Any]) -> str:
    flags = []
    if row.get("state_type") == 0:
        flags.append("soft")
    if row.get("acknowledged"):
        flags.append("acknowledged")
    if row.get("scheduled_downtime_depth"):
        flags.append("in downtime")
    return f" ({', '.join(flags)})" if flags else ""


def _output(row: Dict[str, Any]) -> str:
    text = str(row.get("plugin_output") or "").strip().splitlines()
    if not text:
        return ""
    first = text[0]
    return f" - {first[:120]}{'...' if len(first) > 120 else ''}"


class MonitoringHandler(BaseHandler):
    """Handle monitoring and problem management operations"""

    async def handle(self, tool_name: str, arguments: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Handle monitoring-related tool calls"""

        try:
            if tool_name == "vibemk_get_current_problems":
                return await self._get_current_problems(arguments)
            elif tool_name == "vibemk_acknowledge_problem":
                return await self._acknowledge_problem(arguments)
            elif tool_name == "vibemk_schedule_downtime":
                return await self._schedule_downtime(arguments)
            elif tool_name == "vibemk_get_downtimes":
                return await self._get_downtimes(arguments)
            elif tool_name == "vibemk_delete_downtime":
                return await self._delete_downtime(arguments)
            elif tool_name == "vibemk_reschedule_check":
                return await self._reschedule_check(arguments)
            elif tool_name == "vibemk_get_comments":
                return await self._get_comments(arguments)
            elif tool_name == "vibemk_add_comment":
                return await self._add_comment(arguments)
            else:
                return self.error_response("Unknown tool", f"Tool '{tool_name}' is not supported")

        except CheckMKError as e:
            return self.error_response("CheckMK API Error", str(e))
        except Exception as e:
            self.logger.exception(f"Error in {tool_name}")
            return self.error_response("Unexpected Error", str(e))

    async def _get_current_problems(self, arguments: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Get current problems (hosts not UP, services not OK) across the whole site.

        One POST per object type with explicit columns. Every row must carry a
        valid state; if any is missing we report an error rather than counts, so
        a missing state can never be read as OK.
        """
        target_host = arguments.get("host_name")
        host_query = {"op": "=", "left": "name", "right": target_host} if target_host else None

        try:
            host_result = self.client.list_monitoring("host", HOST_PROBLEM_COLUMNS, query=host_query)
            service_result = self.client.list_monitoring("service", SERVICE_PROBLEM_COLUMNS, host_name=target_host)
        except CheckMKError as e:
            return self.error_response("Could not read monitoring states", f"{e}\n\n{NO_COUNTS_NOTE}")

        if not host_result.get("success") or not service_result.get("success"):
            return self.error_response("Could not read monitoring states", NO_COUNTS_NOTE)

        hosts = [row.get("extensions", {}) for row in host_result.get("data", {}).get("value", [])]
        services = [row.get("extensions", {}) for row in service_result.get("data", {}).get("value", [])]

        if not hosts:
            if target_host:
                return self.error_response("Host not found", f"Host '{target_host}' is not in monitoring")
            return self.error_response("CheckMK returned no hosts", NO_COUNTS_NOTE)

        hosts_missing = sum(1 for h in hosts if not _is_state(h.get("state")))
        services_missing = sum(1 for s in services if not _is_state(s.get("state")))
        if hosts_missing or services_missing:
            return self.error_response(
                "CheckMK returned no state",
                f"State missing for {hosts_missing} of {len(hosts)} hosts and "
                f"{services_missing} of {len(services)} services.\n\n{NO_COUNTS_NOTE}",
            )

        host_problems = [h for h in hosts if h["state"] != 0]
        service_problems = [s for s in services if s["state"] != 0]
        host_problems.sort(key=lambda h: (-h["state"], str(h.get("name", ""))))
        service_problems.sort(
            key=lambda s: (SERVICE_SEVERITY[s["state"]], str(s.get("host_name", "")), str(s.get("description", "")))
        )

        scope = f"host '{target_host}'" if target_host else "all hosts"
        checked = f"Checked {len(hosts)} hosts and {len(services)} services ({scope}); every object returned a state."

        if not host_problems and not service_problems:
            return [{"type": "text", "text": f"✅ **No current problems**\n\n{checked}"}]

        def count(rows: List[Dict[str, Any]], state: int) -> int:
            return sum(1 for r in rows if r["state"] == state)

        def unhandled(rows: List[Dict[str, Any]]) -> int:
            return sum(1 for r in rows if not _is_handled(r))

        summary = [
            checked,
            "",
            f"Hosts: {count(host_problems, 1)} DOWN, {count(host_problems, 2)} UNREACHABLE "
            f"({unhandled(host_problems)} unhandled)",
            f"Services: {count(service_problems, 2)} CRITICAL, {count(service_problems, 1)} WARNING, "
            f"{count(service_problems, 3)} UNKNOWN ({unhandled(service_problems)} unhandled)",
            "",
            "Counts include acknowledged problems and problems in downtime; 'unhandled' excludes them.",
        ]

        lines = []
        for h in host_problems:
            name = HOST_STATE_NAMES.get(h["state"], f"STATE({h['state']})")
            lines.append(f"🖥️ HOST: {h.get('name', 'Unknown')} - {name}{_flags(h)}{_output(h)}")
        for s in service_problems:
            name = SERVICE_STATE_NAMES.get(s["state"], f"STATE({s['state']})")
            lines.append(
                f"🔧 SERVICE: {s.get('host_name', 'Unknown')}/{s.get('description', 'Unknown')} - "
                f"{name}{_flags(s)}{_output(s)}"
            )

        shown = lines[:MAX_LISTED_PROBLEMS]
        if len(lines) > len(shown):
            shown.append(f"... and {len(lines) - len(shown)} more (the counts above are complete)")

        total = len(host_problems) + len(service_problems)
        return [
            {
                "type": "text",
                "text": f"🚨 **Current Problems** ({total} total)\n\n" + "\n".join(summary) + "\n\n" + "\n".join(shown),
            }
        ]

    async def _acknowledge_problem(self, arguments: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Acknowledge a host or service problem"""
        ack_type = arguments.get("acknowledge_type")
        host_name = arguments.get("host_name")
        service_description = arguments.get("service_description")
        comment = arguments.get("comment")

        if not ack_type or not host_name or not comment:
            return self.error_response("Missing parameters", "acknowledge_type, host_name, and comment are required")

        if ack_type == "host":
            data = {
                "acknowledge_type": "host",
                "host_name": host_name,
                "comment": comment,
                "sticky": True,
                "notify": True,
            }
            result = self.client.post("domain-types/acknowledge/collections/host", data=data)
            target = f"host '{host_name}'"

        elif ack_type == "service":
            if not service_description:
                return self.error_response(
                    "Missing parameter", "service_description is required for service acknowledgment"
                )

            data = {
                "acknowledge_type": "service",
                "host_name": host_name,
                "service_description": service_description,
                "comment": comment,
                "sticky": True,
                "notify": True,
            }
            result = self.client.post("domain-types/acknowledge/collections/service", data=data)
            target = f"service '{host_name}/{service_description}'"
        else:
            return self.error_response("Invalid acknowledge_type", "acknowledge_type must be 'host' or 'service'")

        if result.get("success"):
            return [
                {
                    "type": "text",
                    "text": (f"✅ **Problem Acknowledged**\n\n" f"Target: {target}\n" f"Comment: {comment}"),
                }
            ]
        else:
            return self.error_response("Acknowledgment failed", f"Could not acknowledge {target}")

    async def _schedule_downtime(self, arguments: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Schedule maintenance downtime"""
        downtime_type = arguments.get("downtime_type")
        host_name = arguments.get("host_name")
        service_description = arguments.get("service_description")
        start_time = arguments.get("start_time")
        end_time = arguments.get("end_time")
        comment = arguments.get("comment")

        if not downtime_type or not start_time or not end_time or not comment:
            return self.error_response(
                "Missing parameters", "downtime_type, start_time, end_time, and comment are required"
            )

        data = {"downtime_type": downtime_type, "start_time": start_time, "end_time": end_time, "comment": comment}

        if downtime_type == "host" and host_name:
            data["host_name"] = host_name
            target = f"host '{host_name}'"
        elif downtime_type == "service" and host_name and service_description:
            data["host_name"] = host_name
            data["service_description"] = service_description
            target = f"service '{host_name}/{service_description}'"
        else:
            return self.error_response(
                "Invalid parameters", "Invalid downtime_type or missing host/service information"
            )

        result = self.client.post("domain-types/downtime/collections/all", data=data)

        if result.get("success"):
            return [
                {
                    "type": "text",
                    "text": (
                        f"⏰ **Downtime Scheduled**\n\n"
                        f"Target: {target}\n"
                        f"Start: {start_time}\n"
                        f"End: {end_time}\n"
                        f"Comment: {comment}"
                    ),
                }
            ]
        else:
            return self.error_response("Downtime scheduling failed", f"Could not schedule downtime for {target}")

    async def _get_downtimes(self, arguments: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Get list of scheduled downtimes"""
        params = {}
        if host_name := arguments.get("host_name"):
            params["host_name"] = host_name

        result = self.client.get("domain-types/downtime/collections/all", params=params)

        if not result.get("success"):
            return self.error_response("Failed to retrieve downtimes")

        downtimes = result["data"].get("value", [])
        if not downtimes:
            return [{"type": "text", "text": "ℹ️ No scheduled downtimes"}]

        downtime_list = []
        for downtime in downtimes[:20]:
            extensions = downtime.get("extensions", {})
            downtime_id = downtime.get("id", "Unknown")
            host = extensions.get("host_name", "Unknown")
            service = extensions.get("service_description", "")
            comment = extensions.get("comment", "No comment")
            start_time = extensions.get("start_time", "Unknown")
            end_time = extensions.get("end_time", "Unknown")

            target = f"{host}/{service}" if service else host
            downtime_list.append(f"⏰ ID:{downtime_id} - {target} ({start_time} - {end_time}) - {comment}")

        return [
            {
                "type": "text",
                "text": f"⏰ **Scheduled Downtimes** ({len(downtimes)} total):\n\n" + "\n".join(downtime_list),
            }
        ]

    async def _delete_downtime(self, arguments: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Delete a scheduled downtime"""
        downtime_id = arguments.get("downtime_id")

        if not downtime_id:
            return self.error_response("Missing parameter", "downtime_id is required")

        result = self.client.delete(f"objects/downtime/{downtime_id}")

        if result.get("success"):
            return [
                {
                    "type": "text",
                    "text": f"✅ **Downtime Deleted**\n\nDowntime ID: {downtime_id}\nThe downtime has been removed.",
                }
            ]
        else:
            return self.error_response("Downtime deletion failed", f"Could not delete downtime '{downtime_id}'")

    async def _reschedule_check(self, arguments: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Force immediate check execution"""
        check_type = arguments.get("check_type")
        host_name = arguments.get("host_name")
        service_description = arguments.get("service_description")

        if not check_type or not host_name:
            return self.error_response("Missing parameters", "check_type and host_name are required")

        if check_type == "host":
            # Host check reschedule
            data = {"host_name": host_name}
            result = self.client.post(f"objects/host/{host_name}/actions/reschedule_check/invoke", data=data)
            target = f"host '{host_name}'"
        elif check_type == "service":
            if not service_description:
                return self.error_response("Missing parameter", "service_description is required for service checks")
            data = {"host_name": host_name, "service_description": service_description}
            # URL-encode the service description to handle spaces and special characters
            encoded_service = urllib.parse.quote(service_description, safe="")
            result = self.client.post(
                f"objects/service/{host_name}/{encoded_service}/actions/reschedule_check/invoke", data=data
            )
            target = f"service '{host_name}/{service_description}'"
        else:
            return self.error_response("Invalid check_type", "check_type must be 'host' or 'service'")

        if result.get("success"):
            return [
                {
                    "type": "text",
                    "text": f"🔄 **Check Rescheduled**\n\nTarget: {target}\nImmediate check has been scheduled.",
                }
            ]
        else:
            return self.error_response("Check reschedule failed", f"Could not reschedule check for {target}")

    async def _get_comments(self, arguments: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Get list of comments"""
        params = {}
        if host_name := arguments.get("host_name"):
            params["host_name"] = host_name
        if service_description := arguments.get("service_description"):
            params["service_description"] = service_description

        # Try host comments
        host_result = self.client.get("domain-types/comment/collections/host", params=params)
        service_result = self.client.get("domain-types/comment/collections/service", params=params)

        comments = []

        if host_result.get("success"):
            host_comments = host_result["data"].get("value", [])
            for comment in host_comments:
                extensions = comment.get("extensions", {})
                comment_id = comment.get("id", "Unknown")
                host = extensions.get("host_name", "Unknown")
                comment_text = extensions.get("comment", "No comment")
                author = extensions.get("author", "Unknown")
                comments.append(f"💬 HOST:{comment_id} - {host} - {comment_text} (by {author})")

        if service_result.get("success"):
            service_comments = service_result["data"].get("value", [])
            for comment in service_comments:
                extensions = comment.get("extensions", {})
                comment_id = comment.get("id", "Unknown")
                host = extensions.get("host_name", "Unknown")
                service = extensions.get("service_description", "Unknown")
                comment_text = extensions.get("comment", "No comment")
                author = extensions.get("author", "Unknown")
                comments.append(f"💬 SERVICE:{comment_id} - {host}/{service} - {comment_text} (by {author})")

        if not comments:
            return [{"type": "text", "text": "💬 No comments found"}]

        return [{"type": "text", "text": f"💬 **Comments** ({len(comments)} total):\n\n" + "\n".join(comments[:20])}]

    async def _add_comment(self, arguments: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Add comment to host or service"""
        comment_type = arguments.get("comment_type")
        host_name = arguments.get("host_name")
        service_description = arguments.get("service_description")
        comment = arguments.get("comment")
        persistent = arguments.get("persistent", True)

        if not comment_type or not host_name or not comment:
            return self.error_response("Missing parameters", "comment_type, host_name, and comment are required")

        data = {"host_name": host_name, "comment": comment, "persistent": persistent}

        if comment_type == "host":
            result = self.client.post("domain-types/comment/collections/host", data=data)
            target = f"host '{host_name}'"
        elif comment_type == "service":
            if not service_description:
                return self.error_response("Missing parameter", "service_description is required for service comments")
            data["service_description"] = service_description
            result = self.client.post("domain-types/comment/collections/service", data=data)
            target = f"service '{host_name}/{service_description}'"
        else:
            return self.error_response("Invalid comment_type", "comment_type must be 'host' or 'service'")

        if result.get("success"):
            return [
                {
                    "type": "text",
                    "text": (
                        f"💬 **Comment Added**\n\n"
                        f"Target: {target}\n"
                        f"Comment: {comment}\n"
                        f"Persistent: {persistent}"
                    ),
                }
            ]
        else:
            return self.error_response("Comment creation failed", f"Could not add comment to {target}")

"""Every array in a tool's inputSchema declares its items.

Strict MCP clients (VS Code among them) reject a tool whose array parameter
has no "items" schema, and the model is left guessing the element shape.
"""

from typing import Any, Iterator

import pytest

from vibemk.server.tools import get_all_tools


def _arrays_without_items(schema: Any, path: str = "") -> Iterator[str]:
    if isinstance(schema, dict):
        if schema.get("type") == "array" and "items" not in schema:
            yield path or "<root>"
        for key, value in schema.items():
            yield from _arrays_without_items(value, f"{path}.{key}" if path else key)
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            yield from _arrays_without_items(value, f"{path}[{index}]")


@pytest.mark.parametrize("tool", get_all_tools(), ids=lambda t: t["name"])
def test_every_array_parameter_declares_items(tool):
    assert list(_arrays_without_items(tool["inputSchema"])) == []

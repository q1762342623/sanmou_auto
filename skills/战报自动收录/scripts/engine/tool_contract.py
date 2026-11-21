"""Load the cross-language version contract for this tool."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


DEFAULT_TOOL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT_PATH = DEFAULT_TOOL_ROOT / "reference" / "tool_contract.json"
REQUIRED_POSITIVE_INTEGERS = (
    "schemaVersion",
    "reportSchemaVersion",
    "reportCardKeyVersion",
    "liveIndexSchemaVersion",
    "scanAnchorSchemaVersion",
    "captureMetadataSchemaVersion",
    "roundHistorySchemaVersion",
    "minimumScanAnchorGroups",
)


@lru_cache(maxsize=4)
def load_tool_contract(path: Path = DEFAULT_CONTRACT_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("tool contract must be a JSON object")
    for key in REQUIRED_POSITIVE_INTEGERS:
        value = payload.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"tool contract {key} must be a positive integer")
    if int(payload["minimumScanAnchorGroups"]) < 3:
        raise ValueError("tool contract minimumScanAnchorGroups must be at least 3")
    return payload


TOOL_CONTRACT = load_tool_contract()
REPORT_SCHEMA_VERSION = int(TOOL_CONTRACT["reportSchemaVersion"])
REPORT_CARD_KEY_VERSION = int(TOOL_CONTRACT["reportCardKeyVersion"])
LIVE_INDEX_SCHEMA_VERSION = int(TOOL_CONTRACT["liveIndexSchemaVersion"])
SCAN_ANCHOR_SCHEMA_VERSION = int(TOOL_CONTRACT["scanAnchorSchemaVersion"])
CAPTURE_METADATA_SCHEMA_VERSION = int(TOOL_CONTRACT["captureMetadataSchemaVersion"])
ROUND_HISTORY_SCHEMA_VERSION = int(TOOL_CONTRACT["roundHistorySchemaVersion"])
MINIMUM_SCAN_ANCHOR_GROUPS = int(TOOL_CONTRACT["minimumScanAnchorGroups"])

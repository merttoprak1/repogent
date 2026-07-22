"""Regression coverage for the repository-local Codex plugin package."""

import json
from pathlib import Path


def test_plugin_manifest_and_mcp_command() -> None:
    root = Path("plugins/repogent")
    manifest = json.loads((root / ".codex-plugin/plugin.json").read_text())
    mcp = json.loads((root / ".mcp.json").read_text())

    assert manifest["name"] == "repogent"
    assert manifest["version"] == "0.1.0"
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert manifest["interface"]["category"] == "Developer Tools"
    assert mcp["mcpServers"]["repogent"] == {
        "command": "repogent",
        "args": ["mcp", "--stdio"],
    }


def test_repository_marketplace_uses_local_install_policy() -> None:
    marketplace_path = Path(".agents/plugins/marketplace.json")
    marketplace = json.loads(marketplace_path.read_text())
    plugin = marketplace["plugins"][0]

    assert marketplace["name"] == "repogent-local"
    assert marketplace["interface"]["displayName"] == "Repogent"
    assert plugin["name"] == "repogent"
    assert plugin["source"] == {
        "source": "local",
        "path": "./plugins/repogent",
    }
    assert plugin["source"]["path"].startswith("./")
    assert (marketplace_path.parent.parent / plugin["source"]["path"]).resolve().is_relative_to(
        Path.cwd().resolve()
    )
    assert plugin["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }
    assert plugin["category"] == "Developer Tools"

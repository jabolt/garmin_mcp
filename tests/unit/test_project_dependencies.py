"""Project metadata regression tests."""

from pathlib import Path


def test_project_pins_mcp_to_v2_series() -> None:
    """The server uses the mcp 2.x MCPServer API; a future major version may break it."""
    repo_root = Path(__file__).resolve().parents[2]
    pyproject_text = (repo_root / "pyproject.toml").read_text()

    assert '"mcp>=2.2.0,<3"' in pyproject_text

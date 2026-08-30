"""Native least-privilege policy for proposal-only Codex execution."""

from __future__ import annotations

import os

CODEX_PERMISSION_PROFILE_NAME = "agentgraph_provider"


def restricted_permission_config_overrides(*, windows: bool | None = None) -> tuple[str, ...]:
    """Return strict runtime-only Codex permission-profile configuration.

    The root deny is deliberately explicit. The only narrower grants are Codex's
    minimal runtime/helper paths and read access to each effective workspace root.
    """

    overrides = [
        f'default_permissions="{CODEX_PERMISSION_PROFILE_NAME}"',
        (
            f"permissions.{CODEX_PERMISSION_PROFILE_NAME}={{ "
            'filesystem = { ":root" = "deny", ":minimal" = "read", '
            '":workspace_roots" = { "." = "read" } }, '
            "network = { enabled = false } }"
        ),
    ]

    if windows is None:
        windows = os.name == "nt"
    if windows:
        overrides.append('windows.sandbox="elevated"')

    return tuple(overrides)

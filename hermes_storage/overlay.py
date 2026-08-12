"""Deep-merge helpers for shared config ⊕ machine overlay."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


# Keys that belong on a per-machine overlay (not shared profile config).
MACHINE_LOCAL_TOP_KEYS = frozenset({
    "terminal",
    "browser",
    "mcp_servers",
    "environment_hint",
    "agent.environment_hint",
    "proxy",  # iron-proxy / sandbox egress — per PC
})

MACHINE_LOCAL_TERMINAL_KEYS = frozenset({
    "cwd", "backend", "sandbox_dir", "shell_init_files", "home_mode",
    "env_passthrough", "docker_image", "docker_volumes", "docker_network",
    "docker_env", "docker_forward_env", "docker_extra_args", "docker_run_as_host_user",
    "ssh_host", "ssh_user", "ssh_port", "ssh_key", "singularity_image",
    "container_cpu", "container_memory", "container_disk",
})

# Platform config fields that describe *this machine's* network egress,
# not fleet-shared bot identity.
MACHINE_LOCAL_PLATFORM_PROXY_KEYS = frozenset({
    "proxy",
    "proxy_url",
    "http_proxy",
    "https_proxy",
    "socks_proxy",
})

# Env/secret names that must stay per-PC (network path differs by machine).
MACHINE_LOCAL_SECRET_KEYS = frozenset({
    "TELEGRAM_PROXY",
    "DISCORD_PROXY",
    "SLACK_PROXY",
    "MATTERMOST_PROXY",
    "WHATSAPP_PROXY",
    "SIGNAL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
    "no_proxy",
})


def is_machine_local_secret(key: str) -> bool:
    return str(key or "") in MACHINE_LOCAL_SECRET_KEYS


def split_machine_local_secrets(
    values: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str]]:
    """Split a secrets map into (profile_shared, machine_local)."""
    shared: dict[str, str] = {}
    local: dict[str, str] = {}
    for key, value in (values or {}).items():
        if value is None:
            continue
        name = str(key)
        text = str(value)
        if is_machine_local_secret(name):
            local[name] = text
        else:
            shared[name] = text
    return shared, local


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge overlay onto a deepcopy of base (overlay wins)."""
    result = deepcopy(base) if base else {}
    if not overlay:
        return result
    for key, value in overlay.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _extract_platform_proxy_fields(plat: dict[str, Any]) -> dict[str, Any]:
    local: dict[str, Any] = {}
    for key in MACHINE_LOCAL_PLATFORM_PROXY_KEYS:
        if key in plat:
            local[key] = deepcopy(plat[key])
    extra = plat.get("extra")
    if isinstance(extra, dict):
        extra_local = {
            k: deepcopy(v)
            for k, v in extra.items()
            if k in MACHINE_LOCAL_PLATFORM_PROXY_KEYS
        }
        if extra_local:
            local["extra"] = extra_local
    return local


def extract_machine_overlay(config: dict[str, Any]) -> dict[str, Any]:
    """Pull machine-local keys out of a full config for storage in machine_*."""
    overlay: dict[str, Any] = {}
    terminal = config.get("terminal")
    if isinstance(terminal, dict):
        local_term = {
            k: deepcopy(v) for k, v in terminal.items()
            if k in MACHINE_LOCAL_TERMINAL_KEYS
        }
        if local_term:
            overlay["terminal"] = local_term

    browser = config.get("browser")
    if isinstance(browser, dict) and browser:
        overlay["browser"] = deepcopy(browser)

    mcp = config.get("mcp_servers")
    if isinstance(mcp, dict) and mcp:
        overlay["mcp_servers"] = deepcopy(mcp)

    proxy = config.get("proxy")
    if isinstance(proxy, dict) and proxy:
        overlay["proxy"] = deepcopy(proxy)

    platforms = config.get("platforms")
    if isinstance(platforms, dict):
        for name, plat in platforms.items():
            if not isinstance(plat, dict):
                continue
            local_plat: dict[str, Any] = {}
            if name == "api_server":
                local_plat.update({
                    k: deepcopy(v) for k, v in plat.items()
                    if k in ("host", "port", "cors_origins")
                })
            local_plat.update(_extract_platform_proxy_fields(plat))
            if local_plat:
                overlay.setdefault("platforms", {})[name] = local_plat

    # Legacy top-level telegram:/discord: blocks may carry proxy_url.
    for legacy in ("telegram", "discord", "slack", "mattermost", "whatsapp", "signal"):
        block = config.get(legacy)
        if isinstance(block, dict):
            local = _extract_platform_proxy_fields(block)
            if local:
                overlay[legacy] = local

    agent = config.get("agent")
    if isinstance(agent, dict):
        hint = agent.get("environment_hint")
        if hint:
            overlay.setdefault("agent", {})["environment_hint"] = deepcopy(hint)

    return overlay


def strip_machine_local(config: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of config without machine-local keys (for shared profile)."""
    result = deepcopy(config)
    if "terminal" in result and isinstance(result["terminal"], dict):
        for key in list(result["terminal"].keys()):
            if key in MACHINE_LOCAL_TERMINAL_KEYS:
                result["terminal"].pop(key, None)
    result.pop("browser", None)
    result.pop("mcp_servers", None)
    result.pop("proxy", None)
    platforms = result.get("platforms")
    if isinstance(platforms, dict):
        for name, plat in list(platforms.items()):
            if not isinstance(plat, dict):
                continue
            if name == "api_server":
                for key in ("host", "port", "cors_origins"):
                    plat.pop(key, None)
            for key in MACHINE_LOCAL_PLATFORM_PROXY_KEYS:
                plat.pop(key, None)
            extra = plat.get("extra")
            if isinstance(extra, dict):
                for key in MACHINE_LOCAL_PLATFORM_PROXY_KEYS:
                    extra.pop(key, None)
    for legacy in ("telegram", "discord", "slack", "mattermost", "whatsapp", "signal"):
        block = result.get(legacy)
        if isinstance(block, dict):
            for key in MACHINE_LOCAL_PLATFORM_PROXY_KEYS:
                block.pop(key, None)
            extra = block.get("extra")
            if isinstance(extra, dict):
                for key in MACHINE_LOCAL_PLATFORM_PROXY_KEYS:
                    extra.pop(key, None)
    agent = result.get("agent")
    if isinstance(agent, dict):
        agent.pop("environment_hint", None)
    # Overlay-only bag for machine secrets — never share.
    result.pop("secrets", None)
    return result

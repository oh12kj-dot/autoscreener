"""Defect 2 (2026-09-05 audit, docs/audit_followup_2026-09-05.md): the
frontend dev server had no pinned port, so Vite's default "5173, else
silently fall back to 5174" behavior could land the dev server on a port
the API's CORS allowlist does not accept -- observed live on this machine
on 2026-09-05 (both 5173 *and* 5174 simultaneously listening).

`frontend/vite.config.ts` now pins `server.port` and sets
`server.strictPort: true` so a taken port fails the dev server loudly
instead of silently drifting. `src/autoscreener/api/main.py`'s CORS
`allow_origins` and that pinned port are two independent hardcoded
literals with no shared source of truth across the Python/TypeScript
boundary -- this test is the mechanical tie between them: it fails if
either file is ever edited to disagree with the other, rather than relying
on the comments in each file (which a future edit could miss).
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MAIN_PY = _REPO_ROOT / "src" / "autoscreener" / "api" / "main.py"
_VITE_CONFIG = _REPO_ROOT / "frontend" / "vite.config.ts"


def _cors_allow_origins() -> list[str]:
    text = _MAIN_PY.read_text(encoding="utf-8")
    match = re.search(r"allow_origins\s*=\s*\[([^\]]*)\]", text)
    assert match is not None, "could not find allow_origins=[...] in api/main.py"
    return re.findall(r'"([^"]+)"', match.group(1))


def _vite_server_block() -> str:
    text = _VITE_CONFIG.read_text(encoding="utf-8")
    match = re.search(r"server:\s*\{([^}]*)\}", text)
    assert match is not None, "could not find a server: { ... } block in vite.config.ts"
    return match.group(1)


def test_vite_dev_server_port_is_pinned_and_strict():
    """Vite must not be left to pick a default port that could silently
    drift on a collision -- both `port` and `strictPort: true` must be
    set."""
    server_block = _vite_server_block()
    port_match = re.search(r"port\s*:\s*(\d+)", server_block)
    assert port_match is not None, "vite.config.ts server block has no pinned port"
    assert re.search(r"strictPort\s*:\s*true", server_block), (
        "vite.config.ts must set strictPort: true, or a taken port silently "
        "falls back instead of failing the dev server loudly"
    )


def test_cors_allowlist_matches_the_pinned_vite_port():
    """The exact tie: every CORS-allowed localhost origin must carry the
    same port Vite is pinned to. If someone changes one without the other,
    this fails instead of the drift surfacing later as a live CORS outage."""
    server_block = _vite_server_block()
    port_match = re.search(r"port\s*:\s*(\d+)", server_block)
    assert port_match is not None
    vite_port = port_match.group(1)

    origins = _cors_allow_origins()
    localhost_origins = [o for o in origins if "localhost" in o or "127.0.0.1" in o]
    assert localhost_origins, "expected at least one localhost/127.0.0.1 CORS origin"
    for origin in localhost_origins:
        assert origin.endswith(f":{vite_port}"), (
            f"CORS origin {origin!r} does not match the pinned Vite dev "
            f"server port {vite_port!r} -- these two hardcoded literals "
            "have drifted apart (see api/main.py and vite.config.ts "
            "comments for why they must move together)"
        )

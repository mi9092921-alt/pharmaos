"""RC-A permission audit: every /api/v1 route must be permission-guarded or
explicitly allowlisted here — with a justification, so an unguarded endpoint
can never slip in silently again.

Walks the DEPENDENCY TREE (route.dependant), not the decorator text: a guard
applied via Depends() anywhere in the chain counts, while a permission check
written inline inside the handler body does NOT — such a route fails this
audit until it gets a real guard dependency or a justified allowlist entry.

FastAPI 0.139 note: include_router wraps each router in _IncludedRouter, so
the walk recurses through .original_router.routes instead of app.routes.
"""

from fastapi.routing import APIRoute

from pharmaos_api.main import create_app

# Pre-session endpoints — reachable before any login BY DESIGN.
PUBLIC_ALLOWLIST = {
    ("GET", "/api/v1/health"),  # device liveness probe (installer / watchdog)
    ("POST", "/api/v1/auth/login"),  # session bootstrap
    ("POST", "/api/v1/auth/refresh"),  # silent session renewal
    ("GET", "/api/v1/license/status"),  # activation screen HWID, pre-session
    ("POST", "/api/v1/license/activate"),  # fresh device activates before login
    ("GET", "/api/v1/system/setup-status"),  # first-run bootstrap (owner creation)
    ("POST", "/api/v1/system/setup-complete"),  # first-run bootstrap
}

# Authenticated operations on the CALLER'S OWN session — no object permission.
AUTH_ONLY_ALLOWLIST = {
    ("GET", "/api/v1/auth/me"),
    ("POST", "/api/v1/auth/logout"),
}


def _api_routes(routes):  # type: ignore[no-untyped-def]
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif hasattr(r, "original_router"):  # FastAPI 0.139 _IncludedRouter
            yield from _api_routes(r.original_router.routes)


def _dep_qualnames(route: APIRoute) -> set[str]:
    names: set[str] = set()

    def walk(dependant):  # type: ignore[no-untyped-def]
        names.add(dependant.call.__qualname__)
        for sub in dependant.dependencies:
            walk(sub)

    walk(route.dependant)
    return names


def _classified() -> list[tuple[str, str, bool, bool]]:
    app = create_app(license_scheduler=False)
    rows: list[tuple[str, str, bool, bool]] = []
    for r in _api_routes(app.routes):
        if not r.path.startswith("/api/v1"):
            continue
        q = _dep_qualnames(r)
        has_perm = any(n.startswith(("require_permission", "require_any_permission")) for n in q)
        has_auth = any(n.endswith("get_current_user") for n in q)
        for method in sorted(m for m in r.methods if m != "HEAD"):
            rows.append((method, r.path, has_perm, has_auth))
    return rows


def test_every_route_is_guarded_or_allowlisted() -> None:
    unguarded: list[str] = []
    for method, path, has_perm, has_auth in _classified():
        if has_perm:
            continue
        key = (method, path)
        if key in PUBLIC_ALLOWLIST:
            assert not has_auth, f"{method} {path}: allowlisted public but requires auth"
            continue
        if key in AUTH_ONLY_ALLOWLIST:
            assert has_auth, f"{method} {path}: allowlisted auth-only but has no auth dep"
            continue
        unguarded.append(f"{method} {path}")
    assert not unguarded, (
        "routes without require_permission and not allowlisted "
        "(add a real guard dependency, or justify an allowlist entry here):\n"
        + "\n".join(unguarded)
    )


def test_allowlists_stay_exact() -> None:
    """A stale allowlist entry (route renamed/removed) must be pruned —
    orphaned exceptions are how guards quietly rot."""
    seen = {(m, p) for m, p, _, _ in _classified()}
    stale = [k for k in PUBLIC_ALLOWLIST | AUTH_ONLY_ALLOWLIST if k not in seen]
    assert not stale, f"allowlist entries no longer matching any route: {stale}"

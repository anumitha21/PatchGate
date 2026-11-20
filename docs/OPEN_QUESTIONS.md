# OPEN QUESTIONS
1. Does a bare `"Edit"` entry in `permissions.deny` block all native edit tools, and is `MultiEdit` still a real tool name? (UNVERIFIED; hook covers both.)
2. Should the settings template set `"onFailure": "block"` on the hook so a hook crash/timeout fails closed? Documented to exist; not added because it is outside the spec and untested against a live Claude Code.
3. Pyright via PyPI wrapper may download Node on first run on other machines; here the dist was already bundled, so network behaviour is unobserved.
4. Dependents are direct importers only (depth 1); `__init__` re-exports and dynamic imports are not tracked.

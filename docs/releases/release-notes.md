# Release Notes

### 2026-08-28

- Improved proxy import compatibility for common `host:port:user:pass` entries so proxy checks and browser tasks can use the same stored pool consistently.
- Proxy health accounting now updates the original stored proxy entry even when the runtime uses a normalized URL form.
- Proxies that pass a neutral health check are re-enabled automatically, while never-successful proxies are disabled after repeated failures.

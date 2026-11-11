# MiddleAuthClient

Currrently a set of decorators for helping flask apps interact with the middle-auth authentication service

## Missing table mappings

Every table a service protects (a datastack, aligned volume, PCG table, ...) must have a service
table mapping to a dataset in middle_auth. When one is missing, or the auth server can't be reached,
the client handles it predictably:

- `dataset_from_table_id` raises a typed error. Each one subclasses `RuntimeError`, so existing
  handlers still catch it:
  - `TableMappingNotFound` (`kind="unmapped"`) when there is no mapping.
  - `AuthServiceUnavailable` (`kind="auth_unavailable"`) for network errors or a 5xx.
  - `TableMappingError` for anything else.

  Each error carries a `remediation` string that says how to fix it.
- `user_has_permission` and `filter_permitted(items, permission, namespace, name=...)` never raise.
  A failed lookup counts as no permission, so one unmapped row can't break a list view.
- Each failure is recorded for the current request. `auth_problems()` returns the recorded failures.
  By convention, services show them to global admins only (`is_global_admin()`).
- `auth_requires_permission` responds:
  - `400 invalid_table_id` for an unmapped table, explaining the problem and the fix.
  - `503 auth_service_unavailable` when the auth server is down.
- `auth_requires_permission(..., on_unmapped="admin_passthrough")` lets global admins reach the
  view for an unmapped table, so the page can explain the problem using `auth_problems()`. Everyone
  else gets a 403.

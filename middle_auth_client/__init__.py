__version__ = '3.19.2'
from middle_auth_client.decorators import (
    AuthServiceUnavailable,
    TableMappingError,
    TableMappingNotFound,
    auth_problems,
    auth_required,
    auth_requires_admin,
    auth_requires_dataset_admin,
    auth_requires_permission,
    filter_permitted,
    get_usernames,
    is_global_admin,
    record_auth_problem,
    setPermissionLookupOverride,
    user_has_permission,
    users_share_common_group,
)
